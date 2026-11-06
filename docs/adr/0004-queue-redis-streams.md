# 0004. Queue: Redis Streams (not Kafka/Redpanda or NATS JetStream), for now

- Status: accepted
- Date: 2026-11-07

## Context

Jobs (`review`, `qa`, `index`, `eval`) need at-least-once delivery, horizontal consumers, redelivery after crashes, retries with backoff and a dead-letter queue. Targets: 100k events/day, 300/s replay bursts, about 450 concurrent runs at peak. Redis is already required for dedupe, locks, rate limits, caching and pub/sub.

| | Redis Streams | Kafka / Redpanda | NATS JetStream |
|---|---|---|---|
| Ops cost | Already deployed | New cluster (Redpanda: single binary) | New server |
| Consumer groups, ack, redelivery | `XREADGROUP`, `XACK`, `XAUTOCLAIM` | Offsets per partition; no per-message ack | Per-message ack, `AckWait`, `MaxDeliver` |
| Per-key ordering | None across consumers | Per partition (key = repo) | Per subject with a single consumer |
| Retention / replay | Memory-bound; trimmed with `MAXLEN` | Disk, days to forever | Disk, configurable |
| Delayed retry | Build it (ZSET) | Build it (retry topics) | `NakWithDelay` |
| Atomic multi-key ops | Lua scripts | Transactions (heavyweight) | No |

## Decision

Redis Streams with one stream per job type and one consumer group per worker pool:

- Workers ack only after the work is durably recorded. `XAUTOCLAIM` reclaims messages idle past the visibility timeout.
- Retries go to a `<stream>:delayed` sorted set (score = due time; exponential backoff with full jitter) and are promoted back by the consumers' housekeeping loop. "Schedule retry + XACK" and "XADD DLQ + XACK" are single Lua scripts, so a message is never both acked and lost.
- Every ack, retry or dead-letter first checks that this consumer still **owns** the pending entry, so a zombie whose message was reclaimed cannot ack it from under the new owner.
- Two poison bounds: `attempt` (handler-reported failures) and the delivery count (crash redeliveries, checked on reclaim). Exceeding either sends the message to `<stream>:dlq` with the error.
- Streams are capped with `MAXLEN ~ 100000`. Postgres (`events`, `outbox`, `runs`) is the system of record, so the stream is a transport, not history.
- Producers never write to streams directly: they write a Postgres outbox row in the business transaction, and the relay publishes it (ADR 0003).

## Consequences

- No ordering per repo or PR across consumers. Correctness comes from idempotency keys and supersede checks (newer head SHA wins), not from ordering.
- Redis durability is AOF `everysec`: a Redis crash can lose up to about 1 s of stream writes. The outbox holds the truth, so lost entries can be republished. A "republish outbox rows whose run never started" sweeper is planned with the M9 Redis-restart chaos test.
- Memory bounds retention: about 1 KB per message × 100k/day ≈ 100 MB/day before trimming. Fine.

## When we would move

- Need for **replayable history** beyond the DB (event sourcing, re-running a day of traffic through a new agent version) → Kafka/Redpanda.
- **Strict per-repo ordering** with many consumers → Kafka partitions keyed by repo.
- Consumer count in the thousands, or stream memory pressure on a shared Redis → split out, or move to JetStream/Kafka.
- Throughput well beyond about 10k msg/s sustained on one Redis primary (far beyond the 1.2/s average and 300/s burst targets).
