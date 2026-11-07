# 0010. LLM gateway as an in-process library with shared state in Redis and Postgres

- Status: accepted
- Date: 2026-11-07

## Context

M5 puts cost, limits and resilience in one place: routing, budgets, rate limits, retries, circuit breaking with fallback, timeouts and accounting. The spec allows a separate gateway service (possibly in Go). Every worker already holds Redis and Postgres connections.

## Decision

- The gateway is a library (`argus.gateway`) that every agent uses through `ModelSource.for_run(tenant, run, agent)`. No agent code calls a provider directly. All state that must be shared across workers lives outside the process:
  - **Token buckets** (tenant tokens/min, provider requests/min) and **circuit breakers** (per `provider:model`) are Redis hashes updated by single Lua scripts.
  - **Budgets** are per-tenant daily rows in Postgres: a conditional `UPDATE … WHERE used + reserved + estimate <= limit` reserves before the call; settlement replaces the estimate with actual usage. Concurrent calls cannot overspend together.
  - **Accounting** writes one `llm_calls` row per call: tier, provider, model, route reason, status, attempts, tokens (incl. cached), cost, latency.
- **Routing**: a tier (`large`, `small`) maps to an ordered model chain from config. The step identity of an LLM call is the tier (`gateway:large`), not the model, so a fallback on resume does not break replay.
- **Retries** happen per model: 429/5xx/timeouts with full-jitter backoff, honouring `Retry-After` (capped), within a total deadline. A model that still fails counts against its breaker, and the next model in the chain is tried. 4xx errors are not retried or failed over.
- **Breaker**: opens when ≥ `min_calls` in the window failed at ≥ `failure_ratio`; after the cooldown it lets one probe through (half-open); success closes it.
- Rate-limit waits re-queue the message with the wait as delay and do **not** count toward dead-lettering. A spent tenant budget fails the run (not retried).
- Providers: OpenAI-compatible and Anthropic Messages adapters behind one request shape; the Anthropic adapter marks the system prompt and tools with `cache_control`, and both report cached tokens. Streaming passthrough has a time-to-first-token deadline.
- Q&A semantic cache scoped by tenant, repo, indexed commit and requester role.

## Consequences

- No extra hop, deployment or failure point; the gateway scales with the workers.
- One language and codebase for now; a second service would add operational cost before there is a need.
- Every process loads the routing config; changing routes needs a restart (fine at this size).

## When we would revisit

- Non-Python services need to call models → extract the gateway into a service (the stretch Go gateway) with the same Redis/Postgres state, so behaviour does not change.
- Streaming to end users at high fan-out, or connection pooling to providers across many workers, becomes a bottleneck.
