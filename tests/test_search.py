"""Hybrid search, find-callers and codebase Q&A against real Postgres and Redis."""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from argus.agents.qa import QADeps, handle_qa_message, verify_citations
from argus.api.app import create_app as create_api
from argus.config import Settings
from argus.db.pool import create_pool
from argus.github.client import GitHubClient
from argus.llm.client import LLMClient
from argus.mock_llm.app import Faults
from argus.mock_llm.app import create_app as create_mock_llm
from argus.queue.relay import relay_once
from argus.queue.streams import StreamQueue
from argus.queue.topics import JOBS_QA
from argus.rag.embed import HashingEmbedder
from argus.rag.indexer import Indexer, RepoRef
from argus.rag.search import LexicalReranker, find_callers, hybrid_search, query_terms
from argus.runs import upsert_repo, upsert_tenant
from tests.conftest import app_client
from tests.factories import make_api_key
from tests.fakes import GITHUB_API, FakeGitHub

pytestmark = pytest.mark.integration

ADMIN = "admin-token"
FILES = {
    "app/retry.py": (
        "import time\n\n\ndef retry_with_backoff(fn, attempts=5):\n"
        "    for i in range(attempts):\n        try:\n            return fn()\n"
        "        except Exception:\n            time.sleep(2 ** i)\n"
    ),
    "app/billing.py": (
        "def charge_customer(customer, amount):\n"
        "    return retry_with_backoff(lambda: gateway.charge(customer, amount))\n\n\n"
        "def refund(customer, amount):\n    return gateway.refund(customer, amount)\n"
    ),
    "app/render.py": "def render_template(name, ctx):\n    return Template(name).render(ctx)\n",
    "docs/ops.md": "# Operations\n\nPage the on-call engineer when the queue lag grows.\n",
}


@dataclass
class Env:
    pool: asyncpg.Pool
    redis: Redis
    embedder: HashingEmbedder
    repo: RepoRef
    other: RepoRef
    gh: FakeGitHub
    github: GitHubClient
    llm: LLMClient
    settings: Settings


@pytest.fixture
async def env(
    db: asyncpg.Connection, redis: Redis, settings: Settings, migrated_dsn: str
) -> AsyncIterator[Env]:
    pool = await create_pool(migrated_dsn, max_size=20)
    gh = FakeGitHub(files=dict(FILES))
    mock = create_mock_llm(Faults(latency_ms=0))

    async def token(_: int) -> str:
        return "ghs_fake_1_1"

    async with (
        httpx.AsyncClient(transport=gh.transport()) as gh_http,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=mock)) as llm_http,
    ):
        github = GitHubClient(gh_http, token, GITHUB_API)
        embedder = HashingEmbedder()
        t1 = await upsert_tenant(pool, 1001, "acme")
        r1 = await upsert_repo(pool, t1, 5001, "acme/shop", True)
        t2 = await upsert_tenant(pool, 2002, "globex")
        r2 = await upsert_repo(pool, t2, 6001, "globex/shop", True)
        repo = RepoRef(t1, r1, 1001, "acme/shop")
        other = RepoRef(t2, r2, 2002, "globex/shop")
        ix = Indexer(pool, github, embedder)
        now = datetime.now(UTC)
        await ix.full_index(repo, "a" * 40, now)
        await ix.full_index(other, "b" * 40, now)  # same code, another tenant
        llm = LLMClient(llm_http, base_url="http://llm/v1", api_key="k", model="mock-large")
        api_settings = settings.model_copy(update={"admin_token": SecretStr(ADMIN)})
        yield Env(pool, redis, embedder, repo, other, gh, github, llm, api_settings)
    await pool.close()


async def _search(env: Env, query: str, repo: RepoRef | None = None) -> list[str]:
    r = repo or env.repo
    hits = await hybrid_search(
        env.pool,
        env.embedder,
        tenant_id=r.tenant_id,
        repo_ids=[r.repo_id],
        query=query,
        reranker=LexicalReranker(),
    )
    return [h.symbol or h.path for h in hits]


def test_query_terms() -> None:
    assert query_terms("How do we retryWithBackoff?") == ["backoff", "retry", "retrywithbackoff"]


async def test_hybrid_search_finds_by_meaning_and_identifier(env: Env) -> None:
    assert (await _search(env, "how do we retry with exponential backoff"))[0] == (
        "retry_with_backoff"
    )
    assert (await _search(env, "charge_customer"))[0] == "charge_customer"
    assert (await _search(env, "who gets paged on call"))[0] == "docs/ops.md"
    hits = await hybrid_search(
        env.pool,
        env.embedder,
        tenant_id=env.repo.tenant_id,
        repo_ids=[env.repo.repo_id],
        query="refund customer",
    )
    top = hits[0]
    assert top.symbol == "refund" and top.sources == {"fts", "vector"}
    assert top.citation == f"app/billing.py:5-6@{'a' * 12}"
    assert top.url.endswith(f"/acme/shop/blob/{'a' * 40}/app/billing.py#L5-L6")


async def test_search_never_crosses_tenants_or_repos(env: Env) -> None:
    for query in ("retry backoff", "charge_customer", "render template"):
        for repo in (env.repo, env.other):
            hits = await hybrid_search(
                env.pool,
                env.embedder,
                tenant_id=repo.tenant_id,
                repo_ids=[repo.repo_id],
                query=query,
                k=50,
            )
            assert hits and {h.repo for h in hits} == {repo.full_name}
    # A repo id from another tenant returns nothing, even if passed explicitly.
    leaked = await hybrid_search(
        env.pool,
        env.embedder,
        tenant_id=env.repo.tenant_id,
        repo_ids=[env.other.repo_id],
        query="retry",
    )
    assert leaked == []


async def test_find_callers(env: Env) -> None:
    sites = await find_callers(
        env.pool,
        tenant_id=env.repo.tenant_id,
        repo_id=env.repo.repo_id,
        symbol="retry_with_backoff",
    )
    assert [(s.path, s.caller_symbol, s.line) for s in sites] == [
        ("app/billing.py", "charge_customer", 2)
    ]
    assert (
        await find_callers(
            env.pool,
            tenant_id=env.other.tenant_id,
            repo_id=env.repo.repo_id,
            symbol="retry_with_backoff",
        )
        == []
    )
    assert (
        await find_callers(
            env.pool, tenant_id=env.repo.tenant_id, repo_id=env.repo.repo_id, symbol="x; drop"
        )
        == []
    )


async def test_qa_end_to_end_with_verified_citations(env: Env) -> None:
    key = await make_api_key(env.pool, env.repo.tenant_id)
    headers = {"authorization": f"Bearer {key}", "idempotency-key": "q1"}
    body = {"repo": "acme/shop", "question": "Where do we retry with backoff?"}

    async def no_tokens(_: int) -> str:
        return "ghs_fake_1_1"

    api = create_api(env.settings, http_transport=env.gh.transport(), github_tokens=no_tokens)
    async with app_client(api) as c:
        r = await c.post("/api/qa", json=body, headers=headers)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        again = await c.post("/api/qa", json=body, headers=headers)
        assert again.json() == {**r.json(), "created": False}
        assert (
            await c.post("/api/qa", json={**body, "repo": "x/y"}, headers=headers)
        ).status_code == 404
        assert (await c.post("/api/qa", json=body)).status_code == 401

        while await relay_once(env.pool, env.redis):
            pass
        q = StreamQueue(env.redis, JOBS_QA, "qa-workers", "w1")
        await q.ensure_group()
        [msg] = await q.read(block_ms=10)
        deps = QADeps(env.pool, env.github, env.llm, env.embedder, "w1")
        await handle_qa_message(deps, msg)

        run = (await c.get(f"/api/runs/{run_id}", headers=headers)).json()
        assert (await c.get(f"/api/runs/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert run["status"] == "succeeded" and run["agent_type"] == "qa"
    result = run["result"]
    assert "app/retry.py:4-9" in result["answer"]
    assert [c["citation"] for c in result["citations"]] == [f"app/retry.py:4-9@{'a' * 12}"]
    assert result["unverified_citations"] == []
    assert result["index_commit"] is None or isinstance(result["index_commit"], str)
    assert run["steps"] == 3  # LLM → search_code → LLM


async def test_unverifiable_citations_are_flagged(env: Env) -> None:
    verified, unverified = await verify_citations(
        env.pool,
        env.repo.repo_id,
        "acme/shop",
        "See app/billing.py:1-2 and also secrets/prod.env:1 and notes.txt:3-4.",
    )
    assert [v["path"] for v in verified] == ["app/billing.py"]
    assert unverified == ["notes.txt:3-4", "secrets/prod.env:1-1"]


async def test_repeat_question_is_answered_from_the_semantic_cache(env: Env) -> None:
    from argus.agents.qa import create_qa_run
    from argus.gateway.semcache import QACache

    deps = QADeps(
        env.pool,
        env.github,
        env.llm,
        env.embedder,
        "w1",
        cache=QACache(env.pool, env.embedder, threshold=0.95),
    )
    q = StreamQueue(env.redis, JOBS_QA, "qa-workers", "w1")
    await q.ensure_group()
    results = []
    for role in ("member", "member", "viewer"):
        async with env.pool.acquire() as conn:
            run_id, _ = await create_qa_run(
                conn,
                tenant_id=env.repo.tenant_id,
                repo_id=env.repo.repo_id,
                question="Where do we retry with backoff?",
                requested_by="t",
                role=role,
            )
        while await relay_once(env.pool, env.redis):
            pass
        [msg] = await q.read(block_ms=10)
        await handle_qa_message(deps, msg)
        await q.ack(msg)
        row = await env.pool.fetchrow(
            "SELECT result::text AS r, (SELECT count(*) FROM run_steps WHERE run_id = $1) AS n"
            " FROM runs WHERE id = $1",
            run_id,
        )
        assert row is not None
        results.append((json.loads(row["r"]), row["n"]))
    (first, n1), (second, n2), (third, n3) = results
    assert not first.get("cache_hit") and n1 == 3
    assert second["cache_hit"] and n2 == 0 and second["answer"] == first["answer"]
    assert not third.get("cache_hit") and n3 == 3  # another role never reuses the answer
