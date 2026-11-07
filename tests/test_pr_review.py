"""End-to-end review flow: webhook → ingest → outbox → relay → stream → worker → GitHub."""

import asyncio
import contextlib
import functools
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from argus.agents.pr_review import (
    PrBusyError,
    ReviewDeps,
    comment_marker,
    handle_review_message,
)
from argus.config import Settings
from argus.db.pool import create_pool
from argus.github.client import GitHubClient
from argus.ingest.app import create_app as create_ingest
from argus.llm.client import LLMClient
from argus.mock_llm.app import Faults
from argus.mock_llm.app import create_app as create_mock_llm
from argus.queue.consumer import handle_one, run_consumer
from argus.queue.relay import relay_once
from argus.queue.streams import Message, StreamQueue
from argus.queue.topics import JOBS_REVIEW
from argus.runs import reap_runs
from tests.conftest import WEBHOOK_SECRET, app_client
from tests.factories import pr_payload, signed_request
from tests.fakes import GITHUB_API, FakeGitHub

pytestmark = pytest.mark.integration

REPO = "acme/widgets"


@dataclass
class Env:
    pool: asyncpg.Pool
    redis: Redis
    gh: FakeGitHub
    llm_app_faults: object
    deps: ReviewDeps
    settings: Settings

    def queue(self, consumer: str = "w1") -> StreamQueue:
        return StreamQueue(
            self.redis,
            JOBS_REVIEW,
            "review-workers",
            consumer,
            backoff_base_s=0.01,
            backoff_cap_s=0.02,
        )

    def handler(self, worker_id: str = "w1") -> functools.partial[object]:
        deps = ReviewDeps(**{**self.deps.__dict__, "worker_id": worker_id})
        return functools.partial(handle_review_message, deps)

    async def deliver(self, payload: dict[str, object], delivery_id: str | None = None) -> int:
        body, headers = signed_request(payload, WEBHOOK_SECRET, delivery_id=delivery_id)
        async with app_client(create_ingest(self.settings)) as c:
            r = await c.post("/webhooks/github", content=body, headers=headers)
        while await relay_once(self.pool, self.redis):
            pass
        return r.status_code

    async def drain(self, consumer: str = "w1", rounds: int = 50) -> list[str]:
        q = self.queue(consumer)
        await q.ensure_group()
        outcomes: list[str] = []
        for _ in range(rounds):
            await q.promote_due(now_ms=int(time.time() * 1000) + 3_600_000)
            msgs = await q.read(count=10, block_ms=10)
            if not msgs:
                if not await self.redis.zcard(f"{JOBS_REVIEW}:delayed"):
                    break
                continue
            for m in msgs:
                outcomes.append(await handle_one(q, self.handler(consumer), m))  # type: ignore[arg-type]
        return outcomes

    def comments(self, number: int = 7) -> list[dict[str, object]]:
        return self.gh.comments_for(REPO, number)


@pytest.fixture
async def env(
    db: asyncpg.Connection, redis: Redis, settings: Settings, migrated_dsn: str
) -> AsyncIterator[Env]:
    pool = await create_pool(migrated_dsn, max_size=20)
    gh = FakeGitHub()
    mock_llm = create_mock_llm(Faults(latency_ms=0, seed=1))

    async def token(_: int) -> str:
        return "ghs_fake_1_1"

    async with (
        httpx.AsyncClient(transport=gh.transport()) as gh_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_llm), base_url="http://llm"
        ) as llm_http,
    ):
        deps = ReviewDeps(
            pool=pool,
            github=GitHubClient(gh_http, token, GITHUB_API),
            llm=LLMClient(llm_http, base_url="http://llm/v1", api_key="k", model="mock-large"),
            redis=redis,
            worker_id="w1",
        )
        yield Env(pool, redis, gh, mock_llm.state.faults, deps, settings)
    await pool.close()


async def test_pr_opened_produces_one_review_comment(env: Env) -> None:
    assert await env.deliver(pr_payload()) == 202
    assert await env.drain() == ["acked"]
    [comment] = env.comments()
    key = f"review:5001:7:{'a' * 40}"
    assert comment_marker(key) in comment["body"]
    assert "Argus review" in comment["body"]
    run = await env.pool.fetchrow("SELECT * FROM runs")
    assert run["status"] == "succeeded"
    assert run["input_tokens"] > 0 and run["output_tokens"] > 0
    assert json.loads(run["result"])["comment_id"] == str(comment["id"])
    assert await env.pool.fetchval("SELECT status FROM events") == "processed"
    assert await env.pool.fetchval("SELECT count(*) FROM tenants") == 1
    assert await env.pool.fetchval("SELECT full_name FROM repos") == REPO


async def test_replayed_and_duplicate_deliveries_produce_one_comment(env: Env) -> None:
    payload = pr_payload()
    fixed = str(uuid.uuid4())
    codes = [await env.deliver(payload, delivery_id=fixed) for _ in range(3)]  # replays
    codes += [await env.deliver(payload) for _ in range(2)]  # new deliveries, same SHA
    codes += [await env.deliver(pr_payload(action="reopened"))]
    assert codes == [202, 200, 200, 202, 202, 202]
    await env.drain()
    assert len(env.comments()) == 1
    assert await env.pool.fetchval("SELECT count(*) FROM runs") == 1


async def test_relay_republish_is_harmless(env: Env) -> None:
    await env.deliver(pr_payload())
    # Simulate the relay crashing after XADD but before marking the row published.
    [(_, fields)] = await env.redis.xrange(JOBS_REVIEW)
    await env.redis.xadd(JOBS_REVIEW, fields)
    await env.drain()
    assert len(env.comments()) == 1


async def test_new_head_sha_gets_its_own_review(env: Env) -> None:
    await env.deliver(pr_payload(head_sha="a" * 40))
    await env.deliver(pr_payload(action="synchronize", head_sha="c" * 40))
    await env.drain()
    assert len(env.comments()) == 2
    assert await env.pool.fetchval("SELECT count(*) FROM runs") == 2


async def test_concurrent_workers_post_once(env: Env) -> None:
    for _ in range(6):
        await env.deliver(pr_payload())  # distinct deliveries, same (repo, pr, sha)
    stop = asyncio.Event()
    workers = [
        asyncio.create_task(
            run_consumer(env.queue(f"w{i}"), env.handler(f"w{i}"), stop, block_ms=20)  # type: ignore[arg-type]
        )
        for i in range(3)
    ]
    for _ in range(100):
        if await env.pool.fetchval("SELECT status FROM runs") == "succeeded":
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.gather(*workers)
    assert len(env.comments()) == 1


async def test_llm_outage_is_retried_without_duplicates(env: Env) -> None:
    env.llm_app_faults.update(Faults(outage=True))  # type: ignore[attr-defined]
    await env.deliver(pr_payload())
    q = env.queue()
    await q.ensure_group()
    [m] = await q.read(block_ms=10)
    assert await handle_one(q, env.handler(), m) == "retry"  # type: ignore[arg-type]
    run = await env.pool.fetchrow("SELECT status, error FROM runs")
    assert run["status"] == "queued"  # lease released for the retry
    assert "LLM error 500" in run["error"]
    env.llm_app_faults.update(Faults())  # type: ignore[attr-defined]
    assert await env.drain() == ["acked"]
    assert len(env.comments()) == 1
    assert await env.pool.fetchval("SELECT attempts FROM runs") == 2


async def test_lost_response_after_comment_created_does_not_duplicate(env: Env) -> None:
    """GitHub created the comment but the worker never saw the response."""
    env.gh.fail_next_comment_after_create = True
    await env.deliver(pr_payload())
    outcomes = await env.drain()
    assert outcomes == ["retry", "acked"]
    assert len(env.comments()) == 1
    run = await env.pool.fetchrow("SELECT status, result FROM runs")
    assert run["status"] == "succeeded"
    assert json.loads(run["result"])["comment_reused"] is True


class HangingGitHub(GitHubClient):
    """Hangs on the first GitHub call so the worker can be 'killed' mid-run."""

    async def get_pr_diff(self, installation_id: int, repo: str, number: int) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_worker_killed_mid_run_is_recovered_by_reaper(env: Env) -> None:
    await env.deliver(pr_payload())
    q = env.queue("doomed")
    await q.ensure_group()
    [m] = await q.read(block_ms=10)
    doomed = ReviewDeps(
        **{
            **env.deps.__dict__,
            "worker_id": "doomed",
            "lease_s": 0.2,
            "github": HangingGitHub(env.deps.github.http, env.deps.github.tokens, GITHUB_API),
        }
    )
    task = asyncio.create_task(handle_review_message(doomed, m))
    for _ in range(50):
        if await env.pool.fetchval("SELECT status FROM runs") == "running":
            break
        await asyncio.sleep(0.02)
    # kill -9: cancellation is not an Exception, so no lease release and no ack happen.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert await env.pool.fetchval("SELECT lease_owner FROM runs") == "doomed"

    await asyncio.sleep(0.3)  # lease expires
    assert await reap_runs(env.pool, {"pr_review": JOBS_REVIEW}) == 1
    while await relay_once(env.pool, env.redis):
        pass
    outcomes = await env.drain("live")
    assert outcomes == ["acked"]
    assert len(env.comments()) == 1
    run = await env.pool.fetchrow("SELECT status, lease_version, attempts FROM runs")
    assert run["status"] == "succeeded"
    assert run["attempts"] == 2
    # The doomed worker's original message is still pending; once reclaimed it is a no-op.
    reclaimed = await env.queue("live").reclaim(min_idle_ms=0)
    assert [r.id for r in reclaimed] == [m.id]
    assert await handle_one(env.queue("live"), env.handler("live"), reclaimed[0]) == "acked"  # type: ignore[arg-type]
    assert len(env.comments()) == 1


async def test_malformed_event_is_dead_lettered(env: Env) -> None:
    bad = pr_payload()
    del bad["pull_request"]["head"]  # type: ignore[attr-defined]
    await env.deliver(bad)
    assert await env.drain() == ["dead"]
    assert await env.redis.xlen(f"{JOBS_REVIEW}:dlq") == 1
    assert env.comments() == []


class GatedGitHub(GitHubClient):
    """Blocks a chosen call until the test opens the gate."""

    def __init__(self, base: GitHubClient, *, hang_on: str) -> None:
        super().__init__(base.http, base.tokens, GITHUB_API)
        self.hang_on = hang_on
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def _maybe_hang(self, name: str) -> None:
        if name == self.hang_on:
            self.entered.set()
            await self.gate.wait()

    async def get_pr_diff(self, installation_id: int, repo: str, number: int) -> str:
        await self._maybe_hang("get_pr_diff")
        return await super().get_pr_diff(installation_id, repo, number)

    async def create_issue_comment(
        self, installation_id: int, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        await self._maybe_hang("create_issue_comment")
        return await super().create_issue_comment(installation_id, repo, number, body)


SHA_A, SHA_B = "a" * 40, "b" * 40


async def _read_one(env: Env, consumer: str) -> Message:
    q = env.queue(consumer)
    await q.ensure_group()
    [m] = await q.read(count=1, block_ms=10)
    return m


async def test_kill_after_llm_step_resumes_without_recalling_llm_or_duplicating(
    env: Env,
) -> None:
    await env.deliver(pr_payload())
    m = await _read_one(env, "doomed")
    gh = GatedGitHub(env.deps.github, hang_on="create_issue_comment")
    doomed = ReviewDeps(**{**env.deps.__dict__, "worker_id": "doomed", "lease_s": 0.3})
    doomed.github = gh
    task = asyncio.create_task(handle_review_message(doomed, m))
    await asyncio.wait_for(gh.entered.wait(), timeout=5)
    task.cancel()  # kill -9 while the comment call is in flight
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.4)  # heartbeat died with the task; the short lease lapses
    assert await reap_runs(env.pool, {"pr_review": JOBS_REVIEW}) == 1
    while await relay_once(env.pool, env.redis):
        pass
    assert await env.drain("live") == ["acked"]

    assert len(env.comments()) == 1
    steps = await env.pool.fetch("SELECT kind, name, status, attempts FROM run_steps ORDER BY seq")
    assert [(s["kind"], s["name"], s["status"], s["attempts"]) for s in steps] == [
        ("llm_call", "mock-large", "done", 1),  # LLM asks for the diff: replayed
        ("tool_call", "get_pr_diff", "done", 1),  # diff: replayed, not refetched
        ("llm_call", "mock-large", "done", 1),  # final review: replayed, not re-billed
        ("tool_call", "post_review_comment", "done", 2),  # re-run, guarded by the ledger
    ]
    run = await env.pool.fetchrow("SELECT status, result FROM runs")
    assert run["status"] == "succeeded"
    assert json.loads(run["result"])["steps_replayed"] == 3


async def test_new_push_supersedes_running_review(env: Env) -> None:
    await env.deliver(pr_payload(head_sha=SHA_A))
    m_a = await _read_one(env, "wa")
    gh = GatedGitHub(env.deps.github, hang_on="get_pr_diff")
    deps_a = ReviewDeps(**{**env.deps.__dict__, "worker_id": "wa"})
    deps_a.github = gh
    task_a = asyncio.create_task(handle_review_message(deps_a, m_a))
    await asyncio.wait_for(gh.entered.wait(), timeout=5)

    # A new push arrives while run A is mid-review. Run B finds the PR locked.
    await env.deliver(pr_payload(action="synchronize", head_sha=SHA_B))
    m_b = await _read_one(env, "wb")
    with pytest.raises(PrBusyError):
        await handle_review_message(ReviewDeps(**{**env.deps.__dict__, "worker_id": "wb"}), m_b)

    gh.gate.set()  # run A wakes, sees run B at its next step boundary, and stops
    await task_a
    await handle_review_message(ReviewDeps(**{**env.deps.__dict__, "worker_id": "wb"}), m_b)

    rows = await env.pool.fetch("SELECT head_sha, status FROM runs ORDER BY created_at")
    assert [(r["head_sha"], r["status"]) for r in rows] == [
        (SHA_A, "superseded"),
        (SHA_B, "succeeded"),
    ]
    [comment] = env.comments()
    assert SHA_B[:12] in comment["body"]
    assert await env.redis.keys("lock:pr:*") == []


async def test_stale_run_is_superseded_before_it_starts(env: Env) -> None:
    await env.deliver(pr_payload(head_sha=SHA_A))
    m_a = await _read_one(env, "w1")
    await env.deliver(pr_payload(action="synchronize", head_sha=SHA_B))
    m_b = await _read_one(env, "w1")
    # The newer push is processed first; the older message arrives late.
    await handle_review_message(env.deps, m_b)
    await handle_review_message(env.deps, m_a)
    rows = await env.pool.fetch("SELECT head_sha, status FROM runs ORDER BY head_sha")
    assert [(r["head_sha"], r["status"]) for r in rows] == [
        (SHA_A, "superseded"),
        (SHA_B, "succeeded"),
    ]
    assert len(env.comments()) == 1
    stale_steps = await env.pool.fetchval(
        "SELECT count(*) FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE r.head_sha = $1",
        SHA_A,
    )
    assert stale_steps == 0


async def test_step_budget_fails_run_without_retry_or_comment(env: Env) -> None:
    await env.pool.execute("ALTER TABLE runs ALTER COLUMN max_steps SET DEFAULT 2")
    try:
        await env.deliver(pr_payload())
        assert await env.drain() == ["acked"]
    finally:
        await env.pool.execute("ALTER TABLE runs ALTER COLUMN max_steps SET DEFAULT 25")
    run = await env.pool.fetchrow("SELECT status, error FROM runs")
    assert run["status"] == "failed"
    assert "max_steps" in run["error"]
    assert env.comments() == []


# --- M2b: tool-calling loop over MCP ---------------------------------------------------


def _script(env: Env, *calls: tuple[str, dict[str, object]]) -> None:
    env.llm_app_faults.faults.tool_script = [  # type: ignore[attr-defined]
        {"name": n, "arguments": a} for n, a in calls
    ]


async def _tool_steps(env: Env) -> list[tuple[str, dict[str, Any]]]:
    rows = await env.pool.fetch(
        "SELECT name, output FROM run_steps WHERE kind = 'tool_call' ORDER BY seq"
    )
    return [(r["name"], json.loads(r["output"])) for r in rows]


async def test_model_drives_multiple_read_tools(env: Env) -> None:
    _script(
        env,
        ("list_changed_files", {}),
        ("read_file", {"path": "app.py"}),
        ("get_ci_status", {}),
        ("get_pr_diff", {}),
    )
    await env.deliver(pr_payload())
    assert await env.drain() == ["acked"]
    steps = await _tool_steps(env)
    assert [n for n, _ in steps] == [
        "list_changed_files",
        "read_file",
        "get_ci_status",
        "get_pr_diff",
        "post_review_comment",
    ]
    assert "def retry" in steps[1][1]["text"]
    assert '"success"' in steps[2][1]["text"]
    assert len(env.comments()) == 1


async def test_tools_outside_the_allowlist_are_denied_and_audited(env: Env) -> None:
    _script(
        env,
        ("open_issue", {"title": "pwned", "body": "x", "idempotency_key": "k"}),
        ("post_review_comment", {"body": "spam", "idempotency_key": "k2"}),
        ("delete_repo", {}),
    )
    await env.deliver(pr_payload())
    assert await env.drain() == ["acked"]
    steps = await _tool_steps(env)
    assert [(n, o.get("denied")) for n, o in steps[:3]] == [
        ("open_issue", True),
        ("post_review_comment", True),
        ("delete_repo", True),
    ]
    assert env.gh.issues == []
    [comment] = env.comments()  # only the runtime's own review comment
    assert "spam" not in comment["body"]
    audited = await env.pool.fetch(
        "SELECT target, decision, details::text AS d FROM audit_log WHERE action = 'tool.call'"
        " ORDER BY id"
    )
    assert [(a["target"], a["decision"]) for a in audited][:3] == [
        ("open_issue", "deny"),
        ("post_review_comment", "deny"),
        ("delete_repo", "deny"),
    ]


async def test_path_traversal_and_missing_files_are_tool_errors(env: Env) -> None:
    _script(
        env,
        ("read_file", {"path": "../../etc/passwd"}),
        ("read_file", {"path": "nope.py"}),
    )
    await env.deliver(pr_payload())
    assert await env.drain() == ["acked"]
    steps = await _tool_steps(env)
    assert steps[0][1]["is_error"] and "inside the repository" in steps[0][1]["text"]
    assert steps[1][1]["is_error"] and "file not found" in steps[1][1]["text"]
    assert not any("/etc/passwd" in c for c in env.gh.calls)


async def test_tool_crash_is_retried_not_shown_to_model(env: Env) -> None:
    env.gh.fail_diff_with = 502
    await env.deliver(pr_payload())
    assert await env.drain() == ["retry", "acked"]
    row = await env.pool.fetchrow(
        "SELECT status, attempts FROM run_steps WHERE name = 'get_pr_diff'"
    )
    assert row is not None and (row["status"], row["attempts"]) == ("done", 2)
    [(_, diff_out), _] = await _tool_steps(env)
    assert "def retry" in diff_out["text"]
    assert len(env.comments()) == 1


async def test_model_never_sees_write_tools_or_runtime_args(env: Env) -> None:
    from argus.mcp_servers import RunContext
    from argus.mcp_servers.code import build_code_server
    from argus.mcp_servers.github import build_github_server
    from argus.tools.hub import POLICIES, AgentToolPolicy, ToolDeniedError, ToolHub

    ctx = RunContext(
        pool=env.pool,
        github=env.deps.github,
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        lease_version=1,
        installation_id=1001,
        repo=REPO,
        pr_number=7,
        head_sha=SHA_A,
    )
    servers = [build_code_server(ctx), build_github_server(ctx)]
    async with ToolHub(servers, POLICIES["pr_review"]) as hub:
        names = {s["function"]["name"] for s in hub.model_specs()}
        assert names == {
            "get_pr_diff",
            "list_changed_files",
            "read_file",
            "get_ci_status",
            "search_code",
            "find_callers",
        }
        with pytest.raises(ToolDeniedError):
            await hub.call("post_review_comment", {"body": "x"}, by_model=True)
        with pytest.raises(ToolDeniedError):
            await hub.call("open_issue", {"title": "t", "body": "b"}, by_model=False)

    writer = AgentToolPolicy(model_tools=frozenset({"post_review_comment"}))
    async with ToolHub([build_github_server(ctx)], writer) as hub:
        [spec] = hub.model_specs()
        params = spec["function"]["parameters"]
        assert "idempotency_key" not in params["properties"]
        assert "idempotency_key" not in params.get("required", [])
        # A model-supplied key is stripped, so the call fails validation server-side.
        out = await hub.call(
            "post_review_comment", {"body": "x", "idempotency_key": "evil"}, by_model=True
        )
        assert out.is_error
    assert env.comments() == []
