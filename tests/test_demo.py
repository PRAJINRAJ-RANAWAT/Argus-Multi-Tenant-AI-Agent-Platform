"""Public demo: realistic idempotent seed, server-side read-only mode, isolation, limits."""

import statistics
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from argus.api.app import create_app as create_api
from argus.config import Settings
from argus.db.pool import create_pool
from argus.demo_seed import DEMO_LOGIN, seed_demo
from argus.rag.embed import HashingEmbedder
from argus.runs import upsert_repo, upsert_tenant
from tests.conftest import app_client
from tests.fakes import FakeGitHub

pytestmark = pytest.mark.integration


@dataclass
class Env:
    pool: asyncpg.Pool
    redis: Redis
    settings: Settings
    summary: dict[str, Any]
    other_tenant: uuid.UUID
    other_run: uuid.UUID

    def api(self, **overrides: Any) -> Any:
        async def tokens(_: int) -> str:
            return "ghs_fake_1_1"

        s = self.settings.model_copy(update=overrides) if overrides else self.settings
        return create_api(s, http_transport=FakeGitHub().transport(), github_tokens=tokens)


@pytest.fixture
async def env(
    db: asyncpg.Connection, redis: Redis, settings: Settings, migrated_dsn: str
) -> AsyncIterator[Env]:
    pool = await create_pool(migrated_dsn, max_size=10)
    other = await upsert_tenant(pool, 77, "globex")
    repo = await upsert_repo(pool, other, 7701, "globex/secret-sauce", False)
    other_run = await pool.fetchval(
        "INSERT INTO runs (tenant_id, repo_id, agent_type, idempotency_key, status, result)"
        " VALUES ($1, $2, 'qa', 'globex-q', 'succeeded', '{\"answer\": \"globex only\"}')"
        " RETURNING id",
        other,
        repo,
    )
    summary = await seed_demo(pool, HashingEmbedder("mock-embed"))
    s = settings.model_copy(
        update={
            "demo_tenant": DEMO_LOGIN,
            "cookie_secure": False,
            "admin_token": SecretStr("x"),
            "embed_model": "mock-embed",
        }
    )
    yield Env(pool, redis, s, summary, other, other_run)
    await pool.close()


async def _visit(c: httpx.AsyncClient) -> None:
    r = await c.get("/demo")
    assert r.status_code == 302 and r.headers["location"] == "/app/"
    assert "argus_session" in r.cookies


# --- seed ----------------------------------------------------------------------------


async def test_seed_is_realistic(env: Env) -> None:
    s = env.summary
    assert s["repos"] == 4
    assert {"succeeded", "failed", "superseded", "waiting_approval"} <= set(s["statuses"])
    assert s["pending_approvals"] == 3 and s["flagged"] == 2
    assert 150 <= s["reviews"] <= 300 and s["questions"] >= 40
    reviews = await env.pool.fetch(
        """
        SELECT extract(epoch FROM finished_at - started_at) AS secs, input_tokens,
               output_tokens, cost_usd
        FROM runs r JOIN tenants t ON t.id = r.tenant_id
        WHERE t.github_account_login = $1 AND agent_type = 'pr_review' AND status = 'succeeded'
        """,
        DEMO_LOGIN,
    )
    secs = statistics.median(float(r["secs"]) for r in reviews)
    tokens = statistics.median(r["input_tokens"] for r in reviews)
    cost = statistics.median(float(r["cost_usd"]) for r in reviews)
    assert 45 <= secs <= 120  # about a minute, like a real multi-turn review
    assert 20_000 <= tokens <= 90_000  # several turns over a growing context
    assert 0.03 <= cost <= 0.60  # cents per review at Sonnet-class prices
    days = await env.pool.fetchval(
        "SELECT count(*) FROM tenant_usage_daily u JOIN tenants t ON t.id = u.tenant_id"
        " WHERE t.github_account_login = $1",
        DEMO_LOGIN,
    )
    assert 20 <= days <= 28
    future = await env.pool.fetchval(
        "SELECT count(*) FROM runs r JOIN tenants t ON t.id = r.tenant_id"
        " WHERE t.github_account_login = $1"
        " AND greatest(r.created_at, coalesce(r.finished_at, r.created_at)) > now()",
        DEMO_LOGIN,
    )
    assert future == 0, "nothing in the demo happens in the future"


async def test_seed_qa_citations_point_at_indexed_code(env: Env) -> None:
    rows = await env.pool.fetch(
        """
        SELECT r.result -> 'citations' -> 0 ->> 'path' AS path, r.repo_id
        FROM runs r JOIN tenants t ON t.id = r.tenant_id
        WHERE t.github_account_login = $1 AND r.agent_type = 'qa'
        """,
        DEMO_LOGIN,
    )
    assert rows
    for row in rows:
        assert row["path"], "every seeded answer cites a file"
        indexed = await env.pool.fetchval(
            "SELECT count(*) FROM code_chunks WHERE repo_id = $1 AND path = $2",
            row["repo_id"],
            row["path"],
        )
        assert indexed > 0


async def test_seed_is_idempotent_and_leaves_other_tenants_alone(env: Env) -> None:
    async def counts() -> dict[str, int]:
        out = {}
        for table in ("runs", "run_steps", "llm_calls", "approvals", "code_chunks"):
            out[table] = await env.pool.fetchval(
                f"SELECT count(*) FROM {table} x JOIN tenants t ON t.id = x.tenant_id"  # noqa: S608
                " WHERE t.github_account_login = $1",
                DEMO_LOGIN,
            )
        return out

    before = await counts()
    again = await seed_demo(env.pool, HashingEmbedder("mock-embed"))
    assert again == env.summary
    assert await counts() == before
    assert await env.pool.fetchval("SELECT count(*) FROM runs WHERE id = $1", env.other_run) == 1


# --- read-only demo mode -----------------------------------------------------------------


async def test_demo_visitor_is_a_viewer_of_the_demo_tenant_only(env: Env) -> None:
    async with app_client(env.api()) as c:
        await _visit(c)
        me = (await c.get("/api/me")).json()
        assert me["demo"] is True
        assert me["tenant"] == {"login": DEMO_LOGIN, "role": "viewer"}
        assert me["tenants"] == []
        runs = (await c.get("/api/runs", params={"limit": 200})).json()
        assert runs["total"] == env.summary["runs"]
        assert {r["repo"].split("/")[0] for r in runs["runs"]} == {DEMO_LOGIN}
        # Nothing from another tenant, however it is asked for.
        assert (await c.get(f"/api/runs/{env.other_run}")).status_code == 404
        hdr = await c.get("/api/me", headers={"x-argus-tenant": "globex"})
        assert hdr.json()["tenant"]["login"] == DEMO_LOGIN
        c.cookies.set("argus_tenant", "globex")
        assert (await c.get("/api/me")).json()["tenant"]["login"] == DEMO_LOGIN
        search = (await c.get("/api/runs", params={"q": "secret-sauce"})).json()
        assert search["total"] == 0
        repos = [r["full_name"] for r in (await c.get("/api/repos")).json()["repos"]]
        assert repos and all(r.startswith(f"{DEMO_LOGIN}/") for r in repos)


async def test_every_write_is_refused_on_the_server(env: Env) -> None:
    approval = await env.pool.fetchval("SELECT id FROM approvals WHERE status = 'pending' LIMIT 1")
    run = await env.pool.fetchval(
        "SELECT r.id FROM runs r JOIN tenants t ON t.id = r.tenant_id"
        " WHERE t.github_account_login = $1 AND r.status = 'succeeded' LIMIT 1",
        DEMO_LOGIN,
    )
    member = await env.pool.fetchval("SELECT user_id FROM memberships LIMIT 1")
    writes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("PUT", "/api/settings", {"require_approval_for": []}),
        ("PUT", "/api/budget", {"daily_tokens": 10**9}),
        ("POST", f"/api/approvals/{approval}", {"decision": "approve"}),
        ("PUT", f"/api/members/{member}", {"role": "owner"}),
        ("POST", "/api/keys", {"name": "x", "role": "admin"}),
        ("DELETE", f"/api/keys/{uuid.uuid4()}", None),
        ("POST", f"/api/runs/{run}/feedback", {"rating": "down"}),
    ]
    async with app_client(env.api()) as c:
        await _visit(c)
        for method, path, body in writes:
            r = await c.request(method, path, json=body)
            assert r.status_code == 403, (method, path, r.status_code)
            assert "read-only" in r.json()["detail"]
    # Nothing changed, and every attempt was audited as a denial.
    assert await env.pool.fetchval("SELECT status FROM approvals WHERE id = $1", approval) == (
        "pending"
    )
    assert await env.pool.fetchval("SELECT count(*) FROM api_keys") == 0
    denied = await env.pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'demo.write_blocked' AND decision = 'deny'"
    )
    assert denied == len(writes)


async def test_demo_questions_are_rate_limited_per_ip(env: Env) -> None:
    async with app_client(env.api(demo_ip_questions_per_10min=2)) as c:
        await _visit(c)
        body = {"repo": f"{DEMO_LOGIN}/checkout-service", "question": "How do retries work?"}
        assert (await c.post("/api/qa", json=body)).status_code == 202
        assert (await c.post("/api/qa", json=body)).status_code == 202
        r = await c.post("/api/qa", json=body)
        assert r.status_code == 429
        assert "questions every 10 minutes" in r.json()["detail"]
        assert int(r.headers["retry-after"]) > 0


async def test_demo_questions_stop_when_the_daily_budget_is_spent(env: Env) -> None:
    await env.pool.execute(
        """
        INSERT INTO tenant_usage_daily (tenant_id, day, used)
        SELECT id, (now() AT TIME ZONE 'UTC')::date, 10000000 FROM tenants
        WHERE github_account_login = $1
        ON CONFLICT (tenant_id, day) DO UPDATE SET used = 10000000
        """,
        DEMO_LOGIN,
    )
    async with app_client(env.api()) as c:
        await _visit(c)
        r = await c.post(
            "/api/qa", json={"repo": f"{DEMO_LOGIN}/checkout-service", "question": "Why?"}
        )
        assert r.status_code == 429 and "today's AI budget" in r.json()["detail"]


async def test_demo_route_needs_a_configured_seeded_tenant(env: Env) -> None:
    async with app_client(env.api(demo_tenant="")) as c:
        assert (await c.get("/demo")).status_code == 404
    async with app_client(env.api(demo_tenant="no-such-org")) as c:
        assert (await c.get("/demo")).status_code == 503
    async with app_client(env.api(demo_sessions_per_ip_per_hour=1)) as c:
        await _visit(c)
        assert (await c.get("/demo")).status_code == 429


async def test_a_demo_session_cannot_survive_the_demo_being_switched_off(env: Env) -> None:
    async with app_client(env.api()) as c:
        await _visit(c)
        cookie = c.cookies["argus_session"]
    async with app_client(env.api(demo_tenant="")) as c:
        c.cookies.set("argus_session", cookie)
        assert (await c.get("/api/me")).status_code == 401
