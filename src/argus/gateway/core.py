"""The gateway: route → budget → rate limit → breaker → retries → fallback → accounting.

For one call at tier T (e.g. ``large``):

1. Estimate tokens and reserve them against the tenant's daily budget
   (``TenantBudgetExceededError`` if spent; the run fails, not retried).
2. Take the estimate from the tenant's token bucket (``RateLimitedError`` with the wait).
3. Walk the tier's model chain. Skip a model whose breaker is open or whose provider
   bucket is empty; otherwise call it, retrying 429/5xx with full-jitter backoff that
   honours ``Retry-After``, within the total deadline. A model that still fails counts
   against its breaker and the next model is tried (the route reason is recorded).
4. Settle the budget and bucket with actual usage, record the call in ``llm_calls``.

LLM calls have no side effects, so retrying and failing over are always safe.
"""

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from redis.asyncio import Redis

from argus.gateway import (
    AllModelsUnavailableError,
    CallContext,
    ModelRef,
    RateLimitedError,
)
from argus.gateway.budget import Budgets
from argus.gateway.limits import CircuitBreaker, TokenBucket
from argus.gateway.providers import Provider
from argus.llm.client import ChatResult, LLMError
from argus.metrics import BREAKER_STATE, LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS
from argus.telemetry import span

log = logging.getLogger("argus.gateway")

DEFAULT_ROUTES = {
    "large": ["openai:mock-large", "openai:mock-small"],
    "small": ["openai:mock-small", "openai:mock-large"],
}
# USD per 1M tokens (input, output). Unknown models cost 0 (logged as such).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "mock-large": (3.0, 15.0),
    "mock-small": (0.25, 1.25),
}


@dataclass
class GatewayConfig:
    routes: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_ROUTES))
    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    tenant_tokens_per_min: int = 400_000
    provider_requests_per_min: int = 600
    max_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    max_retry_after_s: float = 20.0
    total_timeout_s: float = 120.0
    breaker_window_s: float = 30.0
    breaker_min_calls: int = 5
    breaker_failure_ratio: float = 0.5
    breaker_cooldown_s: float = 30.0


def estimate_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_tokens: int
) -> int:
    chars = len(json.dumps(messages)) + (len(json.dumps(tools)) if tools else 0)
    return chars // 4 + max_tokens


class Gateway:
    def __init__(
        self,
        providers: dict[str, Provider],
        redis: Redis,
        pool: asyncpg.Pool,
        config: GatewayConfig | None = None,
        *,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.providers = providers
        self.pool = pool
        self.config = config or GatewayConfig()
        c = self.config
        self.routes = {t: [ModelRef.parse(m) for m in chain] for t, chain in c.routes.items()}
        self.budgets = Budgets(pool)
        self.tenant_bucket = TokenBucket(
            redis, capacity=c.tenant_tokens_per_min, per_second=c.tenant_tokens_per_min / 60
        )
        self.provider_bucket = TokenBucket(
            redis,
            capacity=c.provider_requests_per_min,
            per_second=c.provider_requests_per_min / 60,
        )
        self.breaker = CircuitBreaker(
            redis,
            window_s=c.breaker_window_s,
            min_calls=c.breaker_min_calls,
            failure_ratio=c.breaker_failure_ratio,
            cooldown_s=c.breaker_cooldown_s,
        )
        self._sleep = sleep

    def for_run(
        self, tenant_id: uuid.UUID, run_id: uuid.UUID, agent: str, tier: str = "large"
    ) -> "GatewayModel":
        return GatewayModel(self, CallContext(tenant_id, run_id, agent), tier)

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        p_in, p_out = self.config.prices.get(model, (0.0, 0.0))
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000

    async def _record(
        self,
        ctx: CallContext,
        tier: str,
        ref: ModelRef,
        reason: str,
        status: str,
        attempts: int,
        res: ChatResult | None,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        LLM_CALLS.labels(ref.name, status, reason.split(":")[0].split("+")[0]).inc()
        if res is not None:
            LLM_TOKENS.labels(ref.name, "input").inc(res.input_tokens)
            LLM_TOKENS.labels(ref.name, "output").inc(res.output_tokens)
            LLM_TOKENS.labels(ref.name, "cached").inc(res.cached_tokens)
            LLM_COST.labels(ref.name).inc(self.cost(ref.name, res.input_tokens, res.output_tokens))
            LLM_LATENCY.labels(ref.name).observe(latency_ms / 1000)
            s = trace.get_current_span()
            s.set_attribute("llm.model", ref.name)
            s.set_attribute("llm.provider", ref.provider)
            s.set_attribute("llm.route", reason)
            s.set_attribute("llm.input_tokens", res.input_tokens)
            s.set_attribute("llm.output_tokens", res.output_tokens)
            s.set_attribute("llm.cached_tokens", res.cached_tokens)
            s.set_attribute(
                "llm.cost_usd", self.cost(ref.name, res.input_tokens, res.output_tokens)
            )
        try:
            await self.pool.execute(
                """
                INSERT INTO llm_calls (tenant_id, run_id, agent, tier, provider, model,
                    route_reason, status, attempts, input_tokens, output_tokens,
                    cached_tokens, cost_usd, latency_ms, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """,
                ctx.tenant_id,
                ctx.run_id,
                ctx.agent,
                tier,
                ref.provider,
                ref.name,
                reason,
                status,
                attempts,
                res.input_tokens if res else 0,
                res.output_tokens if res else 0,
                res.cached_tokens if res else 0,
                self.cost(ref.name, res.input_tokens, res.output_tokens) if res else 0,
                int(latency_ms),
                error[:500] if error else None,
            )
        except Exception:  # accounting must never fail the call
            log.exception("failed to record llm call")

    async def _call_with_retries(
        self,
        ref: ModelRef,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        deadline: float,
    ) -> tuple[ChatResult, int]:
        provider = self.providers[ref.provider]
        c = self.config
        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(504, "gateway deadline exceeded")
            try:
                async with asyncio.timeout(remaining):
                    res = await provider.chat(
                        ref.name,
                        messages,
                        tools=tools,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                return res, attempt
            except TimeoutError as exc:
                raise LLMError(504, "gateway deadline exceeded") from exc
            except LLMError as exc:
                if not exc.retryable or attempt >= c.max_attempts:
                    raise
                delay = random.uniform(0, min(c.backoff_cap_s, c.backoff_base_s * 2**attempt))
                if exc.retry_after_s is not None:
                    delay = max(delay, min(exc.retry_after_s, c.max_retry_after_s))
                if time.monotonic() + delay >= deadline:
                    raise
                await self._sleep(delay)

    async def chat(
        self,
        ctx: CallContext,
        tier: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> ChatResult:
        with span(
            f"llm.chat {tier}",
            kind=SpanKind.CLIENT,
            attributes={
                "llm.tier": tier,
                "argus.agent": ctx.agent,
                "argus.run_id": str(ctx.run_id) if ctx.run_id else None,
                "llm.tools": len(tools or []),
            },
        ):
            return await self._chat(
                ctx, tier, messages, tools=tools, max_tokens=max_tokens, temperature=temperature
            )

    async def _chat(
        self,
        ctx: CallContext,
        tier: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> ChatResult:
        chain = self.routes.get(tier)
        if not chain:
            raise ValueError(f"no route for tier {tier!r}")
        estimate = estimate_tokens(messages, tools, max_tokens)
        try:
            reservation = await self.budgets.reserve(ctx.tenant_id, estimate)
        except Exception as exc:
            await self._record(ctx, tier, chain[0], "budget", "budget", 0, None, 0, str(exc))
            raise
        tenant_key = f"tenant:{ctx.tenant_id}"
        allowed, wait = await self.tenant_bucket.take(tenant_key, estimate)
        if not allowed:
            await self.budgets.settle(reservation, 0, 0)
            await self._record(ctx, tier, chain[0], "tenant_rate", "rate_limited", 0, None, 0)
            raise RateLimitedError("tenant", wait)

        deadline = time.monotonic() + self.config.total_timeout_s
        reason = "primary"
        provider_wait = 0.0
        settled = False
        try:
            for ref in chain:
                gate = await self.breaker.allow(str(ref))
                if gate != 1:
                    BREAKER_STATE.labels(str(ref)).set(2 if gate == 0 else 1)
                if gate == 0:
                    reason = f"fallback:{ref.name}_breaker_open"
                    continue
                ok, pw = await self.provider_bucket.take(f"provider:{ref.provider}", 1)
                if not ok:
                    provider_wait = max(provider_wait, pw)
                    reason = f"fallback:{ref.provider}_rate_limited"
                    continue
                started = time.perf_counter()
                route = reason if gate == 1 else f"{reason}+probe"
                try:
                    res, attempts = await self._call_with_retries(
                        ref, messages, tools, max_tokens, temperature, deadline
                    )
                except LLMError as exc:
                    latency = (time.perf_counter() - started) * 1000
                    if exc.retryable:
                        state = await self.breaker.record(str(ref), ok=False)
                        BREAKER_STATE.labels(str(ref)).set(2 if state == "open" else 0)
                        await self._record(
                            ctx,
                            tier,
                            ref,
                            route,
                            "error",
                            self.config.max_attempts,
                            None,
                            latency,
                            str(exc),
                        )
                        log.warning(
                            "model failed, trying next",
                            extra={"model": str(ref), "error": str(exc), "breaker": state},
                        )
                        reason = f"fallback:{ref.name}_error_{exc.status}"
                        continue
                    await self._record(ctx, tier, ref, route, "error", 1, None, latency, str(exc))
                    raise
                await self.breaker.record(str(ref), ok=True)
                BREAKER_STATE.labels(str(ref)).set(0)
                latency = (time.perf_counter() - started) * 1000
                actual = res.input_tokens + res.output_tokens
                await self.budgets.settle(
                    reservation, actual, self.cost(ref.name, res.input_tokens, res.output_tokens)
                )
                settled = True
                await self.tenant_bucket.adjust(tenant_key, actual - estimate)
                await self._record(ctx, tier, ref, route, "ok", attempts, res, latency)
                return res
        finally:
            if not settled:
                await self.budgets.settle(reservation, 0, 0)
        if provider_wait:
            raise RateLimitedError("providers", provider_wait)
        raise AllModelsUnavailableError(f"no model available for tier {tier!r} ({reason})")


class GatewayModel:
    """A ``ChatModel`` bound to one run. Its ``model`` name is the tier, which is stable
    across fallbacks, so replayed steps keep the same identity."""

    def __init__(self, gateway: Gateway, ctx: CallContext, tier: str) -> None:
        self.gateway = gateway
        self.ctx = ctx
        self.tier = tier
        self.model = f"gateway:{tier}"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        return await self.gateway.chat(
            self.ctx,
            self.tier,
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
