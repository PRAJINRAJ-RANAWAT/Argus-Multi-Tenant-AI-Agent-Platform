# 0003. Webhook dedupe: Postgres is the truth, Redis is the fast path; transactional outbox

- Status: accepted
- Date: 2026-11-07

## Context

GitHub delivers webhooks at least once and redelivers on request. The SLO is zero lost events and zero duplicate reviews. The spec sketches: verify HMAC → `SET delivery:<id> 1 NX EX 86400` → insert event + outbox in one transaction.

Taken literally, claiming the Redis key *before* the database commit has a hole: if the process dies (or Postgres errors) after `SET NX` but before `COMMIT`, the key exists but the event does not. Every redelivery within 24 h is then answered "duplicate", and the event is lost.

## Decision

1. Verify `X-Hub-Signature-256` with `hmac.compare_digest`; an empty configured secret never verifies.
2. **Fast path:** if `delivery:github:<id>` exists in Redis, answer 200 `duplicate` without touching Postgres.
3. **Durable dedupe:** `INSERT INTO events ... ON CONFLICT (source, delivery_id) DO NOTHING RETURNING id`. If a row comes back, insert the outbox row in the **same transaction**. Concurrent duplicates serialise on the unique index: exactly one inserts.
4. Only **after commit** set the Redis key (`SET NX EX 86400`). Redis is a cache of committed deliveries, never a claim.
5. Redis failures degrade to the database path (the webhook still succeeds); a Postgres failure returns 500 and leaves the delivery retryable.
6. Events Argus does not handle are still stored (`status = 'ignored'`) for audit and replay, but get no outbox row.

The outbox row is published by a separate relay (M1b), which closes the dual-write gap between "DB committed" and "queue published".

## Consequences

- Every non-duplicate webhook costs one Postgres transaction; duplicates that hit the fast path cost one Redis `EXISTS`.
- A concurrent duplicate arriving while the first request is still in flight passes the fast path but is stopped by the unique index. Covered by a 20-way concurrency test.
- Tests cover: flushed Redis (DB still dedupes), Redis down (still accepts and dedupes), persist failure (the redelivery is accepted, not dropped).

## When we would revisit

- If Postgres write latency threatens the 300 ms p99 under a 300/s burst, batch event inserts or put a durable log (Kafka) in front of the database. The load test in M9 decides.
