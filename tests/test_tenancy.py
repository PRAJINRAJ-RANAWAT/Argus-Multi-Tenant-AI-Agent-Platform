"""M4: tenant isolation (RLS), GitHub login, roles, repo permissions, API keys, audit.

Done when: these tests prove tenant A cannot read tenant B's runs, code or answers.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from argus.api.app import create_app as create_api
from argus.api.events import RunEvents
from argus.auth import tenant_conn
from argus.config import Settings
from argus.db.pool import APP_ROLE, create_pool
from argus.runs import upsert_repo, upsert_tenant
from tests.conftest import app_client
from tests.factories import make_api_key
from tests.fakes import FakeGitHub

pytestmark = pytest.mark.integration

# Tables the API role cannot read at all (no grant); RLS still applies underneath.
NO_APP_GRANT = ["side_effects", "embeddings"]
RLS_TABLES = [
    "repos",
    "runs",
    "run_steps",
    "side_effects",
    "code_files",
    "code_chunks",
    "code_refs",
    "embeddings",
    "repo_index_state",
    "memberships",
    "api_keys",
    "audit_log",
]


@dataclass
class Tenant:
    id: uuid.UUID
    login: str
    installation_id: int
    repo_id: uuid.UUID
    repo: str
    private_repo_id: uuid.UUID
    private_repo: str
    run_id: uuid.UUID


async def _seed_tenant(pool: asyncpg.Pool, n: int, login: str) -> Tenant:
    tid = await upsert_tenant(pool, n, login)
    repo = await upsert_repo(pool, tid, n * 10, f"{login}/public", False)
    private = await upsert_repo(pool, tid, n * 10 + 1, f"{login}/secret", True)
    run_ids = []
    for r, answer in ((repo, f"{login} public answer"), (private, f"{login} secret answer")):
        rid = await pool.fetchval(
            """
            INSERT INTO runs (tenant_id, repo_id, agent_type, idempotency_key, status, result)
            VALUES ($1, $2, 'qa', $3, 'succeeded', $4::jsonb) RETURNING id
            """,
            tid,
            r,
            f"qa:{uuid.uuid4()}",
            json.dumps({"answer": answer}),
        )
        run_ids.append(rid)
        await pool.execute(
            "INSERT INTO run_steps (run_id, seq, kind, name, input_hash, lease_version,"
            " tenant_id, status, output) VALUES ($1, 0, 'llm_call', 'm', 'h', 1, $2, 'done',"
            " '\"x\"')",
            rid,
            tid,
        )
        await pool.execute(
            "INSERT INTO side_effects (tenant_id, run_id, idempotency_key, kind)"
            " VALUES ($1, $2, $3, 'k')",
            tid,
            rid,
            f"se:{rid}",
        )
    for r in (repo, private):
        await pool.execute(
            "INSERT INTO code_files (tenant_id, repo_id, path, commit_sha, source_received_at)"
            " VALUES ($1, $2, 'app.py', 'c', now())",
            tid,
            r,
        )
        await pool.execute(
            "INSERT INTO code_chunks (tenant_id, repo_id, path, ordinal, kind, start_line,"
            " end_line, commit_sha, content, content_hash, search_text, embedding)"
            " VALUES ($1, $2, 'app.py', 0, 'function', 1, 2, 'c', $3, 'h', $3,"
            " array_fill(0.1, ARRAY[256])::vector)",
            tid,
            r,
            f"secret code of {login}",
        )
        await pool.execute(
            "INSERT INTO code_refs (tenant_id, repo_id, path, callee, line)"
            " VALUES ($1, $2, 'app.py', 'f', 1)",
            tid,
            r,
        )
        await pool.execute(
            "INSERT INTO repo_index_state (repo_id, tenant_id, last_commit) VALUES ($1, $2, 'c')",
            r,
            tid,
        )
    await pool.execute(
        "INSERT INTO embeddings (tenant_id, model, content_hash, embedding)"
        " VALUES ($1, 'm', 'h', array_fill(0.1, ARRAY[256])::vector)",
        tid,
    )
    await pool.execute("SELECT argus_audit($1, 'system', NULL, 'seed', NULL, 'info', NULL)", tid)
    await make_api_key(pool, tid)
    uid = await pool.fetchval(
        "INSERT INTO users (github_id, github_login) VALUES ($1, $2) RETURNING id",
        n * 1000,
        f"{login}-owner",
    )
    await pool.execute(
        "INSERT INTO memberships (tenant_id, user_id, role) VALUES ($1, $2, 'owner')", tid, uid
    )
    return Tenant(tid, login, n, repo, f"{login}/public", private, f"{login}/secret", run_ids[0])


@dataclass
class Env:
    pool: asyncpg.Pool
    app_pool: asyncpg.Pool
    redis: Redis
    gh: FakeGitHub
    settings: Settings
    a: Tenant
    b: Tenant
    user_ids: dict[str, uuid.UUID]

    def api(self) -> Any:
        async def tokens(_: int) -> str:
            return "ghs_fake_1_1"

        return create_api(self.settings, http_transport=self.gh.transport(), github_tokens=tokens)

    async def login(
        self,
        c: httpx.AsyncClient,
        login: str,
        installations: list[tuple[int, str, str]],
        org_roles: dict[str, str] | None = None,
    ) -> None:
        code = f"code-{uuid.uuid4()}"
        self.gh.oauth_codes[code] = {
            "id": abs(hash(login)) % 10**8,
            "login": login,
            "name": login.title(),
            "installations": installations,
            "org_roles": org_roles or {},
        }
        r = await c.get("/auth/github/login", params={"next": "/dashboard"})
        assert r.status_code == 302
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = await c.get("/auth/github/callback", params={"code": code, "state": state})
        assert r.status_code == 302 and r.headers["location"] == "/dashboard"
        assert "argus_session" in r.cookies
        c.cookies.set("argus_session", r.cookies["argus_session"])


@pytest.fixture
async def env(
    db: asyncpg.Connection, redis: Redis, settings: Settings, migrated_dsn: str
) -> AsyncIterator[Env]:
    pool = await create_pool(migrated_dsn, max_size=10)
    app_pool = await create_pool(migrated_dsn, max_size=5, role=APP_ROLE)
    a = await _seed_tenant(pool, 1, "acme")
    b = await _seed_tenant(pool, 2, "globex")
    s = settings.model_copy(
        update={
            "github_oauth_client_id": "cid",
            "github_oauth_client_secret": SecretStr("csecret"),
            "github_web_url": "https://github.test",
            "github_api_url": "https://api.github.test",
            "public_url": "http://test",
            "cookie_secure": False,
        }
    )
    yield Env(pool, app_pool, redis, FakeGitHub(), s, a, b, {})
    await app_pool.close()
    await pool.close()


# --- row-level security --------------------------------------------------------------


async def test_rls_shows_each_tenant_only_its_rows(env: Env) -> None:
    for me, other in ((env.a, env.b), (env.b, env.a)):
        async with tenant_conn(env.app_pool, me.id) as conn:
            for table in [t for t in RLS_TABLES if t not in NO_APP_GRANT]:
                # No WHERE clause at all: the policy does the filtering.
                tenants = await conn.fetch(f"SELECT DISTINCT tenant_id FROM {table}")  # noqa: S608
                assert [r["tenant_id"] for r in tenants] == [me.id], table
            assert await conn.fetchval("SELECT count(*) FROM tenants") == 1
            assert await conn.fetchrow("SELECT * FROM runs WHERE id = $1", other.run_id) is None
            leaked = await conn.fetchval(
                "SELECT count(*) FROM code_chunks WHERE content LIKE $1", f"%{other.login}%"
            )
            assert leaked == 0


async def test_rls_without_tenant_sees_nothing_and_cannot_write_across(env: Env) -> None:
    async with env.app_pool.acquire() as conn:
        for table in [*RLS_TABLES, "tenants"]:
            if table in NO_APP_GRANT:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
                continue
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM events")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM runs")
    async with tenant_conn(env.app_pool, env.a.id) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # WITH CHECK
            await conn.execute(
                "INSERT INTO runs (tenant_id, agent_type, idempotency_key) VALUES ($1, 'qa', 'x')",
                env.b.id,
            )
    # The tenant setting is transaction-local: it does not leak to the next user.
    async with env.app_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM runs") == 0


async def test_api_key_cannot_reach_other_tenant_runs_code_or_answers(env: Env) -> None:
    key = await make_api_key(env.pool, env.a.id, role="admin")
    h = {"authorization": f"Bearer {key}"}
    async with app_client(env.api()) as c:
        runs = (await c.get("/api/runs", headers=h)).json()["runs"]
        assert {r["repo"] for r in runs} == {env.a.repo, env.a.private_repo}
        assert all(env.b.login not in json.dumps(r) for r in runs)
        assert (await c.get(f"/api/runs/{env.b.run_id}", headers=h)).status_code == 404
        mine = await c.get(f"/api/runs/{env.a.run_id}", headers=h)
        assert mine.json()["result"]["answer"] == "acme public answer"
        q = await c.post("/api/qa", json={"repo": env.b.repo, "question": "?"}, headers=h)
        assert q.status_code == 404
        audit = (await c.get("/api/audit", headers=h)).json()
        assert audit["chain_intact"]
        assert "seed" in {e["action"] for e in audit["entries"]}
        assert all(json.dumps(e).find(env.b.login) == -1 for e in audit["entries"])
        assert (
            await c.get("/api/runs", headers={"authorization": "Bearer nope"})
        ).status_code == 401
        assert (await c.get("/api/runs")).status_code == 401


# --- login, roles and GitHub permissions --------------------------------------------


async def test_github_login_creates_memberships_from_github(env: Env) -> None:
    async with app_client(env.api()) as c:
        await env.login(c, "alice", [(1, "acme", "Organization")], {"acme": "admin"})
        me = (await c.get("/api/me")).json()
        assert me["tenant"] == {"login": "acme", "role": "admin"}
        assert me["actor"]["github_login"] == "alice"

        # A second installation: the first tenant by default, switchable by header/cookie.
        await env.login(c, "alice", [(1, "acme", "Organization"), (2, "globex", "Organization")])
        assert (await c.get("/api/me")).json()["tenant"]["login"] == "acme"
        me = (await c.get("/api/me", headers={"x-argus-tenant": "globex"})).json()
        assert me["tenant"] == {"login": "globex", "role": "member"}
        c.cookies.set("argus_tenant", "globex")
        assert (await c.get("/api/me")).json()["tenant"]["login"] == "globex"
        c.cookies.set("argus_tenant", "not-mine")  # a stale cookie never grants access
        assert (await c.get("/api/me")).json()["tenant"]["login"] == "acme"
        c.cookies.delete("argus_tenant")

        # Losing GitHub access removes Argus access at the next login.
        await env.login(c, "alice", [(2, "globex", "Organization")])
        assert (await c.get("/api/me", headers={"x-argus-tenant": "acme"})).status_code == 403

        await c.post("/auth/logout")
        c.cookies.clear()
        assert (await c.get("/api/me")).status_code == 401


async def test_oauth_rejects_bad_state_and_open_redirects(env: Env) -> None:
    async with app_client(env.api()) as c:
        r = await c.get("/auth/github/callback", params={"code": "x", "state": "forged"})
        assert r.status_code == 400
        r = await c.get("/auth/github/login", params={"next": "//evil.example/x"})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert await env.redis.get(f"oauth_state:{state}") == "/"
        r = await c.get("/auth/github/callback", params={"code": "unknown", "state": state})
        assert r.status_code == 400


async def test_private_repo_needs_the_users_own_github_permission(env: Env) -> None:
    async with app_client(env.api()) as c:
        await env.login(c, "bob", [(1, "acme", "Organization")], {"acme": "member"})
        runs = (await c.get("/api/runs")).json()["runs"]
        assert {r["repo"] for r in runs} == {env.a.repo}  # public only
        r = await c.post("/api/qa", json={"repo": env.a.private_repo, "question": "how?"})
        assert r.status_code == 404
        assert (
            await c.post("/api/qa", json={"repo": env.a.repo, "question": "how?"})
        ).status_code == 202

        env.gh.repo_permissions[(env.a.private_repo, "bob")] = "read"
        await env.redis.delete(f"repoperm:1:{env.a.private_repo}:bob")
        runs = (await c.get("/api/runs")).json()["runs"]
        assert env.a.private_repo in {r["repo"] for r in runs}

    denied = await env.pool.fetch(
        "SELECT action, target FROM audit_log WHERE tenant_id = $1 AND decision = 'deny'",
        env.a.id,
    )
    assert [(d["action"], d["target"]) for d in denied] == [("qa.create", env.a.private_repo)]


async def test_roles_gate_actions(env: Env) -> None:
    viewer = await make_api_key(env.pool, env.a.id, role="viewer")
    member = await make_api_key(env.pool, env.a.id, role="member")
    async with app_client(env.api()) as c:
        hv = {"authorization": f"Bearer {viewer}"}
        hm = {"authorization": f"Bearer {member}"}
        assert (await c.get("/api/runs", headers=hv)).status_code == 200
        assert (
            await c.post("/api/qa", json={"repo": env.a.repo, "question": "?"}, headers=hv)
        ).status_code == 403
        assert (await c.get("/api/audit", headers=hm)).status_code == 403
        assert (await c.post("/api/keys", json={"name": "k"}, headers=hm)).status_code == 403

        await env.login(c, "carol", [(1, "acme", "Organization")], {"acme": "member"})
        assert (await c.post("/api/keys", json={"name": "k"})).status_code == 403


async def test_api_key_lifecycle_and_repo_limits(env: Env) -> None:
    async with app_client(env.api()) as c:
        await env.login(c, "dave", [(3, "dave", "User")])  # personal account → owner
        assert (await c.get("/api/me")).json()["tenant"]["role"] == "owner"
        r = await c.post("/api/keys", json={"name": "ci", "role": "member", "repos": [env.a.repo]})
        assert r.status_code == 404  # acme/public belongs to another tenant
        tid = await env.pool.fetchval("SELECT id FROM tenants WHERE github_account_login='dave'")
        rid = await upsert_repo(env.pool, tid, 99, "dave/tool", False)
        await upsert_repo(env.pool, tid, 98, "dave/other", False)
        r = await c.post("/api/keys", json={"name": "ci", "role": "member", "repos": ["dave/tool"]})
        assert r.status_code == 201
        token, key_id = r.json()["token"], r.json()["id"]
        stored = await env.pool.fetchval(
            "SELECT key_hash FROM api_keys WHERE id = $1", uuid.UUID(key_id)
        )
        assert token not in stored  # only the hash is stored
        listed = (await c.get("/api/keys")).json()["keys"]
        assert [k["name"] for k in listed] == ["ci"] and "token" not in listed[0]

        c.cookies.clear()
        hk = {"authorization": f"Bearer {token}"}
        assert (
            await c.post("/api/qa", json={"repo": "dave/tool", "question": "?"}, headers=hk)
        ).status_code == 202
        assert (
            await c.post("/api/qa", json={"repo": "dave/other", "question": "?"}, headers=hk)
        ).status_code == 404
        runs = (await c.get("/api/runs", headers=hk)).json()["runs"]
        assert {r["repo"] for r in runs} == {"dave/tool"}
        assert rid

        await env.login(c, "dave", [(3, "dave", "User")])
        assert (await c.delete(f"/api/keys/{key_id}")).status_code == 204
        c.cookies.clear()
        assert (await c.get("/api/runs", headers=hk)).status_code == 401


async def test_members_roles_and_last_owner(env: Env) -> None:
    async with app_client(env.api()) as c:
        await env.login(c, "erin", [(4, "erin", "User")])
        me = await env.pool.fetchval("SELECT id FROM users WHERE github_login = 'erin'")
        r = await c.put(f"/api/members/{me}", json={"role": "admin"})
        assert r.status_code == 409  # cannot remove the last owner
        r = await c.put(f"/api/members/{uuid.uuid4()}", json={"role": "admin"})
        assert r.status_code == 404


# --- audit log -----------------------------------------------------------------------


async def test_audit_log_is_append_only_and_tamper_evident(env: Env) -> None:
    for i in range(5):
        await env.pool.execute(
            "SELECT argus_audit($1, 'user', 'u', 'x', $2, 'allow', $3::jsonb)",
            env.a.id,
            f"t{i}",
            json.dumps({"token": "ghp_" + "a" * 36, "i": i}),
        )
    assert await env.pool.fetchval("SELECT argus_audit_verify($1)", env.a.id) is None
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await env.pool.execute("UPDATE audit_log SET action = 'y'")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await env.pool.execute("DELETE FROM audit_log")
    async with tenant_conn(env.app_pool, env.a.id) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO audit_log (tenant_id, at, actor_type, action, decision,"
                " prev_hash, hash) VALUES ($1, now(), 'x', 'x', 'info', '', '')",
                env.a.id,
            )

    # A superuser who disables the trigger can edit a row, but the chain shows it.
    victim = await env.pool.fetchval(
        "SELECT id FROM audit_log WHERE tenant_id = $1 AND target = 't2'", env.a.id
    )
    await env.pool.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update")
    try:
        await env.pool.execute("UPDATE audit_log SET decision = 'deny' WHERE id = $1", victim)
    finally:
        await env.pool.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update")
    assert await env.pool.fetchval("SELECT argus_audit_verify($1)", env.a.id) == victim
    assert await env.pool.fetchval("SELECT argus_audit_verify($1)", env.b.id) is None


async def test_audit_details_are_redacted(env: Env) -> None:
    from argus.auth import audit

    await audit(
        env.pool,
        env.a.id,
        actor_type="agent",
        actor_id="r",
        action="tool.call",
        target="read_file",
        decision="allow",
        details={"args": {"path": "x", "api_key": "abc"}, "note": "token ghp_" + "b" * 36},
    )
    row = await env.pool.fetchval("SELECT details::text FROM audit_log WHERE action = 'tool.call'")
    assert "ghp_" not in row and "abc" not in row and "[REDACTED]" in row


# --- live events ---------------------------------------------------------------------


async def test_run_events_are_delivered_only_to_their_tenant(env: Env, migrated_dsn: str) -> None:
    events = RunEvents(migrated_dsn)
    await events.start()
    try:
        async with events.subscribe(env.a.id) as qa, events.subscribe(env.b.id) as qb:
            await env.pool.execute("UPDATE runs SET status = 'failed' WHERE id = $1", env.a.run_id)
            await env.pool.execute(
                "INSERT INTO runs (tenant_id, agent_type, idempotency_key) VALUES ($1, 'qa', 'n')",
                env.b.id,
            )
            ea = await asyncio.wait_for(qa.get(), 2)
            eb = await asyncio.wait_for(qb.get(), 2)
            assert ea["run_id"] == str(env.a.run_id) and ea["status"] == "failed"
            assert eb["tenant_id"] == str(env.b.id) and eb["status"] == "queued"
            await asyncio.sleep(0.1)
            assert qa.empty() and qb.empty()
        assert events.subscribers == 0
    finally:
        await events.stop()


async def test_budget_and_usage_endpoints(env: Env) -> None:
    admin = await make_api_key(env.pool, env.a.id, role="admin")
    member = await make_api_key(env.pool, env.a.id, role="member")
    await env.pool.execute(
        "INSERT INTO llm_calls (tenant_id, tier, provider, model, route_reason, status,"
        " input_tokens, output_tokens, cost_usd) VALUES ($1, 'large', 'openai', 'mock-large',"
        " 'primary', 'ok', 100, 50, 0.001), ($2, 'large', 'openai', 'mock-large', 'primary',"
        " 'ok', 999, 999, 9)",
        env.a.id,
        env.b.id,
    )
    async with app_client(env.api()) as c:
        ha, hm = {"authorization": f"Bearer {admin}"}, {"authorization": f"Bearer {member}"}
        assert (
            await c.put("/api/budget", json={"daily_tokens": 5000}, headers=hm)
        ).status_code == 403
        r = await c.put("/api/budget", json={"daily_tokens": 5000, "alert_pct": 90}, headers=ha)
        assert r.json() == {"daily_tokens": 5000, "alert_pct": 90}
        usage = (await c.get("/api/usage", headers=hm)).json()
    assert usage["budget"] == {"daily_tokens": 5000, "alert_pct": 90}
    assert usage["models"] == [
        {
            "model": "mock-large",
            "calls": 1,
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "cost_usd": 0.001,
            "errors": 0,
        }
    ]  # tenant B's calls are invisible
