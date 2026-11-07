# 0012. Observability: OpenTelemetry traces across queue hops, Prometheus metrics, SLO burn-rate alerts

- Status: accepted
- Date: 2026-11-07

## Context

A review crosses four processes and two queues (ingest → Postgres outbox → relay → Redis Stream → worker → LLM gateway → MCP tools → GitHub). Auto-instrumentation stops at each async hop, so by default every hop starts a new trace and nobody can answer "why did this PR's review take 4 minutes?". M7's bar: one trace from webhook to GitHub comment.

## Decision

- **Traces (OpenTelemetry, OTLP/HTTP).** Manual spans at each hop with W3C context carried in the data itself:
  - ingest `webhook.receive` writes `traceparent` into the outbox payload, in the same transaction as the event;
  - relay `outbox.relay` (PRODUCER) continues it and publishes its own `traceparent` as a stream field;
  - worker `queue.process <stream>` (CONSUMER) continues from the field, or from the payload copy after a retry re-publish, so retries stay in the same trace;
  - then `agent.run`, `step.<kind> <name>`, `llm.chat <tier>` (model, route, tokens, cached tokens, cost), `tool <name>` (MCP), and one CLIENT span per outgoing HTTP request (GitHub, providers) from an httpx transport. Trace headers are not sent to third parties.
  - Log lines carry the current `trace_id`/`span_id`, so logs and traces join.
- **Export** to any OTLP endpoint: Jaeger locally, Tempo or a collector in production, or Langfuse's OTLP endpoint for LLM-focused views (headers configurable). Without an endpoint, spans are non-recording and nearly free.
- **Metrics (Prometheus client).** HTTP rate/latency by route template, webhooks by outcome, outbox publishes, queue depth by state and oldest unacked age, messages by outcome, runs by outcome and duration, steps, LLM calls/tokens/cost/latency by model, breaker state. Labels stay low-cardinality (no tenant ids; per-tenant cost is in Postgres).
- **SLO alerts** as multi-window, multi-burn-rate rules for webhook latency (99% < 300 ms), availability (99.5%) and review latency (95% < 2 min), plus symptom alerts (old backlog, DLQ growth, open breaker, LLM error rate). Rules are checked with `promtool`.
- `docker-compose.observability.yml` adds Prometheus, Grafana (provisioned "Argus overview" dashboard) and Jaeger.

## Consequences

- One trace per webhook end to end, asserted by a test that walks the parent chain from the GitHub comment POST back to `webhook.receive`.
- The trace context lives in the outbox payload and stream fields; it adds ~100 bytes per message.
- Manual spans mean new hops must be instrumented on purpose; the test catches a broken chain.

## When we would revisit

- More services or languages (e.g. a Go gateway) → add an OTel collector with tail sampling, and sample traces instead of exporting all.
- High-cardinality questions (per tenant) → exemplars or logs-based metrics rather than tenant labels.
