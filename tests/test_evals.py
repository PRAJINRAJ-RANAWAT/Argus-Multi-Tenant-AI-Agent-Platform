"""M8 'done when': a deliberately worse prompt fails the eval gate (and so CI)."""

import json
from collections.abc import AsyncIterator

import asyncpg
import pytest
from redis.asyncio import Redis

from argus.config import Settings
from argus.db.pool import create_pool
from argus.evals.suite import ROOT, gate, render_markdown, run_all, run_review_suite

pytestmark = pytest.mark.integration

WORSE_PROMPT = """You are Argus, a friendly code reviewer. Keep the review short and
encouraging. Comment on naming, formatting and style so the code looks consistent."""


@pytest.fixture
async def pool(db: asyncpg.Connection, migrated_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_dsn, max_size=10)
    yield p
    await p.close()


def _baseline() -> dict[str, float]:
    data: dict[str, float] = json.loads((ROOT / "baseline.json").read_text())
    return data


async def test_current_prompt_passes_the_gate(pool: asyncpg.Pool, redis: Redis) -> None:
    report = await run_all(pool, redis)
    failures = gate(report.metrics, _baseline())
    assert failures == [], render_markdown(report, _baseline(), failures)
    assert report.metrics["review.recall"] == 1.0
    assert report.metrics["judge.agreement"] >= 0.8


async def test_deliberately_worse_prompt_fails_the_gate(pool: asyncpg.Pool, redis: Redis) -> None:
    metrics, _, _ = await run_review_suite(pool, redis, system_prompt=WORSE_PROMPT)
    failures = gate(metrics, _baseline())
    names = {f.split(":")[0] for f in failures}
    assert {"review.recall", "review.judge_mean"} <= names, failures
    assert metrics["review.recall"] < 0.2


async def test_dropping_security_from_the_prompt_is_caught(
    pool: asyncpg.Pool, redis: Redis
) -> None:
    from argus.agents.pr_review import SYSTEM_PROMPT

    subtle = SYSTEM_PROMPT.replace("bugs, security problems, missing tests", "bugs, missing tests")
    assert subtle != SYSTEM_PROMPT
    metrics, _, _ = await run_review_suite(pool, redis, system_prompt=subtle)
    assert any(f.startswith("review.recall") for f in gate(metrics, _baseline()))


def test_gate_rules() -> None:
    base = {"review.recall": 0.9, "safety.attack_success_rate": 0.03}
    assert gate({"review.recall": 0.86, "safety.attack_success_rate": 0.05}, base) == []
    assert gate({"review.recall": 0.8}, base) == ["review.recall: 0.800 < baseline 0.900 - 0.05"]
    assert gate({"safety.attack_success_rate": 0.2}, base)
    assert gate({"judge.agreement": 0.5}, {})  # an untrustworthy judge fails the gate


async def test_online_sampling_judge_and_feedback(
    pool: asyncpg.Pool, redis: Redis, settings: Settings
) -> None:
    import httpx
    from pydantic import SecretStr

    from argus.api.app import create_app as create_api
    from argus.evals.online import handle_eval_message, maybe_sample
    from argus.llm.client import LLMClient
    from argus.mock_llm.app import Faults
    from argus.mock_llm.app import create_app as create_mock_llm
    from argus.queue.relay import relay_once
    from argus.queue.streams import StreamQueue
    from argus.queue.topics import JOBS_EVAL
    from argus.runs import upsert_repo, upsert_tenant
    from tests.conftest import app_client
    from tests.factories import make_api_key
    from tests.fakes import FakeGitHub

    tenant = await upsert_tenant(pool, 1001, "acme")
    repo = await upsert_repo(pool, tenant, 1, "acme/app", False)
    run_id = await pool.fetchval(
        "INSERT INTO runs (tenant_id, repo_id, agent_type, idempotency_key, status, result)"
        " VALUES ($1, $2, 'pr_review', 'k', 'succeeded', $3::jsonb) RETURNING id",
        tenant,
        repo,
        '{"review": "### Findings\\n- **SQL injection** (`a.py:1`): x"}',
    )
    import random

    assert not await maybe_sample(
        pool, run_id=run_id, tenant_id=tenant, agent="pr_review", prompt_version="v1", rate=0.0
    )
    assert await maybe_sample(
        pool,
        run_id=run_id,
        tenant_id=tenant,
        agent="pr_review",
        prompt_version="v1",
        rate=1.0,
        rng=random.Random(1),
    )
    while await relay_once(pool, redis):
        pass
    q = StreamQueue(redis, JOBS_EVAL, "eval-workers", "w1")
    await q.ensure_group()
    [msg] = await q.read(block_ms=10)
    mock = create_mock_llm(Faults(latency_ms=0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock)) as http:
        llm = LLMClient(http, base_url="http://llm/v1", api_key="", model="mock-small")
        await handle_eval_message(pool, llm, msg)
    sample = await pool.fetchrow("SELECT status, judge_score FROM eval_samples")
    assert sample is not None and sample["status"] == "done" and 1 <= sample["judge_score"] <= 5

    key = await make_api_key(pool, tenant, role="viewer")

    async def tokens(_: int) -> str:
        return "ghs_fake_1_1"

    api_settings = settings.model_copy(update={"admin_token": SecretStr("x")})
    api = create_api(api_settings, http_transport=FakeGitHub().transport(), github_tokens=tokens)
    async with app_client(api) as c:
        h = {"authorization": f"Bearer {key}"}
        r = await c.post(f"/api/runs/{run_id}/feedback", json={"rating": "up"}, headers=h)
        assert r.status_code == 200
        r = await c.post(f"/api/runs/{run_id}/feedback", json={"rating": "down"}, headers=h)
        assert r.status_code == 200  # changing your mind replaces the vote
        summary = (await c.get("/api/evals/online", headers=h)).json()
    assert summary["feedback"] == {"up": 0, "down": 1}
    assert summary["judge_by_prompt"][0]["prompt_version"] == "v1"
