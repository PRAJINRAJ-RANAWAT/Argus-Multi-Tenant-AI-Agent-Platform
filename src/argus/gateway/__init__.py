"""LLM gateway: the one code path every model call goes through.

Routing (tier → model chain), per-tenant token budgets, token-bucket rate limits per
tenant and per provider, retries with backoff that honour ``Retry-After``, a circuit
breaker per model with fallback, timeouts, and per-call accounting in ``llm_calls``.
"""

import uuid
from dataclasses import dataclass


class GatewayError(Exception):
    pass


class RateLimitedError(GatewayError):
    """The tenant (or every provider) is over its rate. Retry after ``retry_after_s``."""

    def __init__(self, scope: str, retry_after_s: float) -> None:
        super().__init__(f"rate limited ({scope}); retry after {retry_after_s:.1f}s")
        self.scope = scope
        self.retry_after_s = retry_after_s
        self.is_wait = True  # a wait, not a failure: does not count toward the DLQ


class TenantBudgetExceededError(GatewayError):
    """The tenant's daily token budget is spent. Not retryable today."""


class AllModelsUnavailableError(GatewayError):
    """Every model in the route failed or has an open breaker. Retryable later."""


@dataclass(frozen=True)
class ModelRef:
    provider: str
    name: str

    @classmethod
    def parse(cls, spec: str) -> "ModelRef":
        provider, sep, name = spec.partition(":")
        if not sep or not provider or not name:
            raise ValueError(f"model spec must be provider:model, got {spec!r}")
        return cls(provider, name)

    def __str__(self) -> str:
        return f"{self.provider}:{self.name}"


@dataclass(frozen=True)
class CallContext:
    tenant_id: uuid.UUID
    run_id: uuid.UUID | None
    agent: str
