"""Prometheus metrics. Names follow ``argus_<thing>_<unit>``; labels stay low-cardinality
(no tenant ids on high-volume series; per-tenant cost lives in Postgres)."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "argus_http_requests_total", "HTTP requests", ["service", "route", "method", "status"]
)
HTTP_LATENCY = Histogram(
    "argus_http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5),
)
WEBHOOKS = Counter("argus_webhooks_total", "Webhooks by outcome", ["event", "outcome"])
OUTBOX_PUBLISHED = Counter("argus_outbox_published_total", "Outbox rows published", ["topic"])

QUEUE_DEPTH = Gauge("argus_queue_depth", "Messages by state", ["stream", "state"])
QUEUE_OLDEST_AGE = Gauge(
    "argus_queue_oldest_pending_age_seconds", "Age of the oldest unacked message", ["stream"]
)
MESSAGES = Counter("argus_queue_messages_total", "Handled messages", ["stream", "outcome"])

RUNS = Counter("argus_runs_total", "Finished runs", ["agent", "outcome"])
RUN_DURATION = Histogram(
    "argus_run_duration_seconds",
    "Run duration from first claim to finish",
    ["agent"],
    buckets=(1, 5, 10, 30, 60, 90, 120, 180, 300, 600),
)
STEPS = Counter("argus_run_steps_total", "Run steps", ["kind", "result"])

LLM_CALLS = Counter("argus_llm_calls_total", "LLM calls", ["model", "status", "route"])
LLM_TOKENS = Counter("argus_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_COST = Counter("argus_llm_cost_usd_total", "LLM cost in USD", ["model"])
LLM_LATENCY = Histogram(
    "argus_llm_latency_seconds",
    "LLM call latency",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
)
BREAKER_STATE = Gauge(
    "argus_breaker_state", "Circuit breaker state (0 closed, 1 half-open, 2 open)", ["model"]
)
GUARDRAILS = Counter("argus_guardrail_events_total", "Guardrail events", ["kind"])
