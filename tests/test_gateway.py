"""LLM gateway: budgets, failover, breaker, retries, rate limits, adapters, caching.

Done when: a per-tenant budget stops runs, and a provider outage fails over cleanly.
"""

import asyncio
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

from argus.agents.pr_review import ReviewDeps
from argus.db.pool import create_pool
from argus.gateway import (
    AllModelsUnavailableError,
    CallContext,
    RateLimitedError,
    TenantBudgetExceededError,
)
from argus.gateway.core import Gateway, GatewayConfig
from argus.gateway.providers import AnthropicProvider, OpenAIProvider, to_anthropic
from argus.gateway.semcache import QACache
from argus.llm.client import LLMError
from argus.mock_llm.app import Faults
from argus.mock_llm.app import create_app as create_mock_llm
from argus.queue.streams import StreamQueue, publish
from argus.rag.embed import HashingEmbedder
from argus.runs import upsert_repo, upsert_tenant
from tests.factories import pr_payload
from tests.test_pr_review import Env, env  # noqa: F401 - fixture reuse

pytestmark = pytest.mark.integration

MSGS = [
    {"role": "system", "content": "You review code."},
    {"role": "user", "content": "Summarise the change."},
]


@dataclass
class GW:
    pool: asyncpg.Pool
    redis: Redis
    mock: Any
    http: httpx.AsyncClient
    tenant: uuid.UUID
    sleeps: list[float]

    def gateway(self, **overrides: Any) -> Gateway:
        cfg = GatewayConfig(
            backoff_base_s=0.001, backoff_cap_s=0.002, breaker_cooldown_s=0.3, **overrides
        )

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        providers = {
            "openai": OpenAIProvider(self.http, base_url="http://llm/v1", api_key=""),
            "anthropic": AnthropicProvider(self.http, base_url="http://llm/v1", api_key="k"),
        }
        return Gateway(providers, self.redis, self.pool, cfg, sleep=fake_sleep)

    @property
    def faults(self) -> Faults:
        f: Faults = self.mock.faults
        return f

    def ctx(self) -> CallContext:
        return CallContext(self.tenant, None, "test")

    async def budget(self, tokens: int, pct: int = 80) -> None:
        await self.pool.execute(
            "INSERT INTO tenant_budgets (tenant_id, daily_tokens, alert_pct) VALUES ($1, $2, $3)"
            " ON CONFLICT (tenant_id) DO UPDATE SET daily_tokens = $2, alert_pct = $3",
            self.tenant,
            tokens,
            pct,
        )


@pytest.fixture
async def gw(db: asyncpg.Connection, redis: Redis, migrated_dsn: str) -> AsyncIterator[GW]:
    pool = await create_pool(migrated_dsn, max_size=30)
    mock = create_mock_llm(Faults(latency_ms=0, seed=7))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock)) as http:
        tenant = await upsert_tenant(pool, 1001, "acme")
        yield GW(pool, redis, mock.state.faults, http, tenant, [])
    await pool.close()


# --- done when -----------------------------------------------------------------------


async def test_tenant_budget_stops_runs(env: Env, redis: Redis) -> None:  # noqa: F811
    providers = {"openai": OpenAIProvider(env.deps.llm.http, base_url="http://llm/v1", api_key="")}
    gateway = Gateway(providers, redis, env.pool, GatewayConfig())
    env.deps = ReviewDeps(**{**env.deps.__dict__, "llm": gateway})

    await env.deliver(pr_payload(number=1, head_sha="1" * 40))
    assert await env.drain() == ["acked"]
    first = await env.pool.fetchrow("SELECT tenant_id, input_tokens + output_tokens AS t FROM runs")
    assert first is not None and len(env.comments(1)) == 1

    # Budget = what was already used: the next run is stopped, not retried.
    await env.pool.execute(
        "INSERT INTO tenant_budgets (tenant_id, daily_tokens) VALUES ($1, $2)",
        first["tenant_id"],
        first["t"] + 10,
    )
    await env.deliver(pr_payload(number=2, head_sha="2" * 40))
    assert await env.drain() == ["acked"]
    run = await env.pool.fetchrow("SELECT status, error FROM runs WHERE pr_number = 2")
    assert run is not None and run["status"] == "failed"
    assert "budget" in run["error"]
    assert env.comments(2) == []
    statuses = await env.pool.fetch("SELECT status FROM llm_calls ORDER BY id")
    assert [s["status"] for s in statuses][-1] == "budget"
    usage = await env.pool.fetchrow("SELECT used, reserved FROM tenant_usage_daily")
    assert usage is not None and usage["reserved"] == 0 and usage["used"] == first["t"]


async def test_provider_outage_fails_over_then_recovers(gw: GW) -> None:
    g = gw.gateway(breaker_min_calls=3)
    gw.faults.fail_models = ["mock-large"]
    for _ in range(6):
        res = await g.chat(gw.ctx(), "large", MSGS)
        assert res.model == "mock-small" and res.content
    calls_to_large = gw.mock.by_model["mock-large"]
    # 3 failures (each retried 3 times) opened the breaker; later calls skip mock-large.
    assert calls_to_large == 3 * 3
    assert await g.breaker.state("openai:mock-large") == "open"
    reasons = [
        r["route_reason"]
        for r in await gw.pool.fetch(
            "SELECT route_reason FROM llm_calls WHERE status = 'ok' ORDER BY id"
        )
    ]
    assert reasons[:3] == ["fallback:mock-large_error_500"] * 3
    assert reasons[3:] == ["fallback:mock-large_breaker_open"] * 3

    # Provider recovers: after the cooldown one probe goes through and closes the breaker.
    gw.faults.fail_models = []
    await asyncio.sleep(0.35)
    res = await g.chat(gw.ctx(), "large", MSGS)
    assert res.model == "mock-large"
    assert await g.breaker.state("openai:mock-large") == "closed"
    last = await gw.pool.fetchval("SELECT route_reason FROM llm_calls ORDER BY id DESC LIMIT 1")
    assert last == "primary+probe"


async def test_total_outage_is_retryable_and_releases_the_budget(gw: GW) -> None:
    await gw.budget(1_000_000)
    gw.faults.outage = True
    g = gw.gateway()
    with pytest.raises(AllModelsUnavailableError):
        await g.chat(gw.ctx(), "large", MSGS)
    usage = await gw.pool.fetchrow("SELECT used, reserved FROM tenant_usage_daily")
    assert usage is not None and (usage["used"], usage["reserved"]) == (0, 0)


# --- retries, limits, budgets -------------------------------------------------------


async def test_retry_after_is_honoured(gw: GW) -> None:
    gw.faults.error_rate_429 = 1.0
    gw.faults.retry_after_s = 3
    g = gw.gateway(routes={"large": ["openai:mock-large"]})
    with pytest.raises(AllModelsUnavailableError):
        await g.chat(gw.ctx(), "large", MSGS)
    assert len(gw.sleeps) == 2 and all(s >= 3 for s in gw.sleeps)
    assert gw.mock.by_model["mock-large"] == 3  # bounded: no retry stampede


async def test_non_retryable_errors_are_not_retried_or_failed_over(gw: GW) -> None:
    g = gw.gateway()

    class Bad:
        name = "openai"

        async def chat(self, *a: Any, **k: Any) -> Any:
            raise LLMError(400, "bad request")

    g.providers["openai"] = Bad()  # type: ignore[assignment]
    with pytest.raises(LLMError):
        await g.chat(gw.ctx(), "large", MSGS)
    assert await gw.pool.fetchval("SELECT count(*) FROM llm_calls") == 1


async def test_tenant_token_bucket_rate_limits(gw: GW) -> None:
    gw.faults.latency_ms = 100  # calls overlap, so estimates are held at the same time
    g = gw.gateway(tenant_tokens_per_min=2500)
    results = await asyncio.gather(
        *(g.chat(gw.ctx(), "large", MSGS) for _ in range(4)), return_exceptions=True
    )
    limited = [r for r in results if isinstance(r, RateLimitedError)]
    assert len(limited) == 2 and all(r.retry_after_s > 0 for r in limited)
    assert {r.scope for r in limited} == {"tenant"}
    other = await upsert_tenant(gw.pool, 2002, "globex")
    await g.chat(CallContext(other, None, "t"), "large", MSGS)  # other tenants unaffected


async def test_rate_limited_messages_retry_without_using_attempts(gw: GW) -> None:
    q = StreamQueue(gw.redis, "jobs:review", "g", "c", max_attempts=2)
    await q.ensure_group()
    await publish(gw.redis, "jobs:review", {"x": 1})
    [m] = await q.read(block_ms=10)
    from argus.queue.consumer import handle_one

    async def limited(_: Any) -> None:
        raise RateLimitedError("tenant", 5.0)

    assert await handle_one(q, limited, m) == "retry"
    [(member, due)] = await gw.redis.zrange("jobs:review:delayed", 0, -1, withscores=True)
    assert json.loads(member)["attempt"] == 0
    assert due >= (time.time() + 4.5) * 1000


async def test_concurrent_calls_never_overspend_the_budget(gw: GW) -> None:
    await gw.budget(2_500)
    gw.faults.latency_ms = 200  # every call reserves before any settles
    g = gw.gateway()
    results = await asyncio.gather(
        *(g.chat(gw.ctx(), "large", MSGS) for _ in range(20)), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    stopped = [r for r in results if isinstance(r, TenantBudgetExceededError)]
    assert len(ok) == 2 and len(stopped) == 18  # 2 x ~1.05k estimate fit in 2.5k
    usage = await gw.pool.fetchrow("SELECT used, reserved FROM tenant_usage_daily")
    assert usage is not None and usage["reserved"] == 0 and usage["used"] <= 2_500


async def test_budget_alert_fires_once_at_threshold(gw: GW) -> None:
    await gw.budget(400, pct=10)
    g = gw.gateway()
    with pytest.raises(TenantBudgetExceededError):
        for _ in range(20):
            await g.chat(gw.ctx(), "large", MSGS, max_tokens=50)
    alerts = await gw.pool.fetch("SELECT details FROM audit_log WHERE action = 'budget.alert'")
    assert len(alerts) == 1


# --- adapters, streaming, accounting ------------------------------------------------


def test_to_anthropic_conversion() -> None:
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "Q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "b", "arguments": '{"x":1}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]
    tools = [{"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}}]
    body = to_anthropic(msgs, tools)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in body["messages"][2]["content"]] == ["c1", "c2"]
    assert body["messages"][1]["content"][1]["input"] == {"x": 1}


async def test_anthropic_adapter_tool_round_trip_and_cache_tokens(gw: GW) -> None:
    g = gw.gateway(routes={"large": ["anthropic:mock-claude"]})
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_pr_diff",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]
    first = await g.chat(gw.ctx(), "large", MSGS, tools=tools)
    assert [tc.name for tc in first.tool_calls] == ["get_pr_diff"]
    assert first.cached_tokens > 0
    follow = [
        *MSGS,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {"name": "get_pr_diff", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "diff"},
    ]
    second = await g.chat(gw.ctx(), "large", follow, tools=tools)
    assert not second.tool_calls and "review" in second.content.lower()


async def test_streaming_passthrough_and_ttft_timeout(gw: GW) -> None:
    p = OpenAIProvider(gw.http, base_url="http://llm/v1", api_key="")
    chunks = [c async for c in p.stream("mock-large", MSGS, max_tokens=50, ttft_timeout_s=2)]
    assert len(chunks) > 1 and "".join(chunks).strip()
    gw.faults.latency_ms = 400
    with pytest.raises(LLMError, match="first token"):
        async for _ in p.stream("mock-large", MSGS, max_tokens=50, ttft_timeout_s=0.05):
            pass


async def test_every_call_is_accounted(gw: GW) -> None:
    g = gw.gateway()
    await g.chat(gw.ctx(), "small", MSGS)
    await g.chat(gw.ctx(), "large", MSGS)
    rows = await gw.pool.fetch(
        "SELECT tier, model, status, input_tokens, output_tokens, cost_usd, latency_ms"
        " FROM llm_calls ORDER BY id"
    )
    assert [(r["tier"], r["model"], r["status"]) for r in rows] == [
        ("small", "mock-small", "ok"),
        ("large", "mock-large", "ok"),
    ]
    assert all(r["input_tokens"] > 0 and r["cost_usd"] > 0 for r in rows)
    assert rows[1]["cost_usd"] > rows[0]["cost_usd"]  # large costs more
    usage = await gw.pool.fetchval("SELECT cost_usd FROM tenant_usage_daily")
    assert usage == rows[0]["cost_usd"] + rows[1]["cost_usd"]


async def test_semantic_cache_is_scoped(gw: GW) -> None:
    repo = await upsert_repo(gw.pool, gw.tenant, 1, "acme/x", True)
    run = await gw.pool.fetchval(
        "INSERT INTO runs (tenant_id, agent_type, idempotency_key) VALUES ($1, 'qa', 'k')"
        " RETURNING id",
        gw.tenant,
    )
    cache = QACache(gw.pool, HashingEmbedder(), threshold=0.9)
    scope = {"tenant_id": gw.tenant, "repo_id": repo, "commit": "c1", "role": "member"}
    await cache.store(
        **scope,
        question="Where do we retry with backoff?",
        result={"answer": "app/retry.py:1-5"},
        run_id=run,
    )
    hit = await cache.lookup(**scope, question="where do we retry with backoff")
    assert hit is not None and hit["cache_hit"] and hit["answer"] == "app/retry.py:1-5"
    assert (
        await cache.lookup(
            **{**scope, "role": "viewer"}, question="Where do we retry with backoff?"
        )
        is None
    )
    assert (
        await cache.lookup(**{**scope, "commit": "c2"}, question="Where do we retry with backoff?")
        is None
    )
    assert await cache.lookup(**scope, question="How is billing computed?") is None
    other = await upsert_tenant(gw.pool, 2002, "globex")
    assert (
        await cache.lookup(
            **{**scope, "tenant_id": other}, question="Where do we retry with backoff?"
        )
        is None
    )
