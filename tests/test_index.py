"""Incremental code indexing against real Postgres (pgvector) and Redis."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from argus.config import Settings
from argus.db.pool import create_pool
from argus.github.client import GitHubClient
from argus.ingest.app import create_app as create_ingest
from argus.mock_llm.app import Faults
from argus.mock_llm.app import create_app as create_mock_llm
from argus.queue.relay import relay_once
from argus.queue.streams import StreamQueue
from argus.queue.topics import JOBS_INDEX
from argus.rag.embed import EMBED_DIM, HashingEmbedder, HttpEmbedder
from argus.rag.indexer import Indexer, RepoRef
from argus.rag.jobs import IndexDeps, handle_index_message
from argus.runs import upsert_repo, upsert_tenant
from tests.conftest import WEBHOOK_SECRET, app_client
from tests.factories import installation_payload, push_payload, signed_request
from tests.fakes import GITHUB_API, FakeGitHub

pytestmark = pytest.mark.integration

REPO = "acme/widgets"

STORE = """import os


class Store:
    def get(self, key):
        return fetch(key)

    def put(self, key, value):
        save(key, value)


def retry_with_backoff(fn):
    return fn()
"""
UTIL = "def fetch(key):\n    return key\n\n\ndef save(key, value):\n    return None\n"
README = "# Widgets\n\nHow to run the widgets service.\n"


@dataclass
class Env:
    pool: asyncpg.Pool
    redis: Redis
    gh: FakeGitHub
    embedder: HashingEmbedder
    deps: IndexDeps
    settings: Settings
    repo: RepoRef

    async def deliver(self, payload: dict[str, object], event: str) -> int:
        body, headers = signed_request(payload, WEBHOOK_SECRET, event=event)
        async with app_client(create_ingest(self.settings)) as c:
            r = await c.post("/webhooks/github", content=body, headers=headers)
        while await relay_once(self.pool, self.redis):
            pass
        return r.status_code

    async def drain(self) -> int:
        q = StreamQueue(self.redis, JOBS_INDEX, "index-workers", "w1")
        await q.ensure_group()
        n = 0
        while msgs := await q.read(count=10, block_ms=10):
            for m in msgs:
                await handle_index_message(self.deps, m)
                await q.ack(m)
                n += 1
        return n

    async def chunk(self, symbol: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM code_chunks WHERE repo_id = $1 AND symbol = $2",
            self.repo.repo_id,
            symbol,
        )


@pytest.fixture
async def env(
    db: asyncpg.Connection, redis: Redis, settings: Settings, migrated_dsn: str
) -> AsyncIterator[Env]:
    pool = await create_pool(migrated_dsn, max_size=20)
    gh = FakeGitHub(files={"app/store.py": STORE, "app/util.py": UTIL, "README.md": README})

    async def token(_: int) -> str:
        return "ghs_fake_1_1"

    async with httpx.AsyncClient(transport=gh.transport()) as http:
        github = GitHubClient(http, token, GITHUB_API)
        embedder = HashingEmbedder()
        tenant = await upsert_tenant(pool, 1001, "acme")
        repo_id = await upsert_repo(pool, tenant, 5001, REPO, True)
        repo = RepoRef(tenant, repo_id, 1001, REPO)
        yield Env(pool, redis, gh, embedder, IndexDeps(pool, github, embedder), settings, repo)
    await pool.close()


def _t(seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)


async def test_one_line_push_reembeds_only_the_changed_chunk(env: Env) -> None:
    """M3 'done when'."""
    indexer = env.deps.indexer()
    full = await indexer.full_index(env.repo, "c" * 40, _t(0))
    total = await env.pool.fetchval("SELECT count(*) FROM code_chunks")
    assert full.files_indexed == 3 and full.chunks == total
    assert full.embedded == total and full.reused == 0

    env.gh.files["app/store.py"] = STORE.replace("save(key, value)", "save(key, value, 60)")
    inc = await indexer.index_paths(env.repo, "d" * 40, _t(1), ["app/store.py"])
    assert inc.embedded == 1  # Store.put only
    assert inc.reused == inc.chunks - 1
    put = await env.chunk("Store.put")
    assert put is not None and "save(key, value, 60)" in put["content"]
    assert put["commit_sha"] == "d" * 40
    assert await env.pool.fetchval("SELECT count(*) FROM code_chunks") == total


async def test_push_webhook_indexes_changed_paths_end_to_end(env: Env) -> None:
    await env.deps.indexer().full_index(env.repo, "c" * 40, _t(0))
    reads_before = len(env.gh.file_reads)
    env.gh.files["app/util.py"] = UTIL + "\n\ndef purge():\n    return save(None, None)\n"
    assert await env.deliver(push_payload(modified=["app/util.py"]), "push") == 202
    assert await env.drain() == 1
    assert env.gh.file_reads[reads_before:] == ["app/util.py"]
    assert await env.chunk("purge") is not None
    assert await env.pool.fetchval("SELECT status FROM events") == "processed"

    # Pushes to other branches and branch deletions are ignored at ingest.
    assert await env.deliver(push_payload(ref="refs/heads/feature"), "push") == 202
    assert await env.drain() == 0


async def test_installation_triggers_full_index(env: Env) -> None:
    await env.deliver(installation_payload(repos=[(5001, REPO)]), "installation")
    assert await env.drain() == 1
    state = await env.pool.fetchrow("SELECT * FROM repo_index_state")
    assert state is not None and state["last_commit"] == env.gh.head_sha
    assert state["default_branch"] == "main" and state["last_full_index"] is not None
    assert await env.pool.fetchval("SELECT count(DISTINCT path) FROM code_chunks") == 3


async def test_late_older_push_never_overwrites_newer_content(env: Env) -> None:
    indexer = env.deps.indexer()
    env.gh.files["app/util.py"] = "def fetch(key):\n    return 'new'\n"
    await indexer.index_paths(env.repo, "e" * 40, _t(10), ["app/util.py"])
    env.gh.files["app/util.py"] = "def fetch(key):\n    return 'old'\n"
    stale = await indexer.index_paths(env.repo, "d" * 40, _t(5), ["app/util.py"])
    assert stale.files_stale == 1 and stale.files_indexed == 0
    fetch = await env.chunk("fetch")
    assert fetch is not None and "'new'" in fetch["content"]


async def test_deleted_files_leave_tombstones(env: Env) -> None:
    indexer = env.deps.indexer()
    await indexer.full_index(env.repo, "c" * 40, _t(0))
    del env.gh.files["app/util.py"]
    out = await indexer.index_paths(env.repo, "d" * 40, _t(2), ["app/util.py"])
    assert out.files_deleted == 1
    assert await env.chunk("fetch") is None
    assert await env.pool.fetchval("SELECT count(*) FROM code_refs WHERE path = 'app/util.py'") == 0
    # The deletion is newer than a late event that still had the file.
    env.gh.files["app/util.py"] = UTIL
    late = await indexer.index_paths(env.repo, "c" * 40, _t(1), ["app/util.py"])
    assert late.files_stale == 1 and await env.chunk("fetch") is None

    # Full index tombstones files missing from the tree.
    del env.gh.files["README.md"]
    await indexer.full_index(env.repo, "f" * 40, _t(3))
    deleted = await env.pool.fetchval("SELECT deleted FROM code_files WHERE path = 'README.md'")
    assert deleted is True


async def test_concurrent_indexing_of_one_file_stays_consistent(env: Env) -> None:
    indexers = [Indexer(env.pool, env.deps.github, env.embedder) for _ in range(6)]
    await asyncio.gather(
        *(
            ix.index_paths(env.repo, f"{i:040d}", _t(i), ["app/store.py"])
            for i, ix in enumerate(indexers)
        )
    )
    rows = await env.pool.fetch("SELECT ordinal, commit_sha FROM code_chunks ORDER BY ordinal")
    assert [r["ordinal"] for r in rows] == list(range(len(rows)))
    assert {r["commit_sha"] for r in rows} == {f"{5:040d}"}  # the newest event wins
    assert await env.pool.fetchval("SELECT count(*) FROM embeddings") == len(rows)


async def test_call_graph_and_tenant_scoped_embedding_cache(env: Env) -> None:
    indexer = env.deps.indexer()
    await indexer.full_index(env.repo, "c" * 40, _t(0))
    callers = await env.pool.fetch(
        "SELECT caller_symbol FROM code_refs WHERE repo_id = $1 AND callee = 'save'",
        env.repo.repo_id,
    )
    assert [r["caller_symbol"] for r in callers] == ["Store.put"]

    # Same code in another tenant is embedded again: caches never cross tenants.
    other = await upsert_tenant(env.pool, 2002, "globex")
    other_repo = await upsert_repo(env.pool, other, 6001, "globex/widgets", True)
    out = await indexer.index_paths(
        RepoRef(other, other_repo, 2002, "globex/widgets"), "c" * 40, _t(0), ["app/store.py"]
    )
    assert out.reused == 0 and out.embedded == out.chunks


async def test_http_embedder_against_mock_server() -> None:
    app = create_mock_llm(Faults(latency_ms=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        emb = HttpEmbedder(http, base_url="http://llm/v1", api_key="", model="mock-embed")
        vecs = await emb.embed(["def retry():", "hello world"])
    assert len(vecs) == 2 and all(len(v) == EMBED_DIM for v in vecs)
    assert vecs[0] != vecs[1]
