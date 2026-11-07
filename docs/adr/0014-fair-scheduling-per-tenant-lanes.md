# 0014. Fair scheduling with per-tenant lanes on Redis Streams

- Status: accepted
- Date: 2026-11-07

## Context

One stream per job type is FIFO: a tenant that opens 500 PRs puts every other tenant's reviews behind them (SPEC: noisy neighbour). We need per-tenant fairness and a per-tenant concurrency limit across all workers, without a new broker (ADR 0004).

## Decision

- **Lanes.** Fair topics (`jobs:review`, `jobs:qa`) get one sub-stream per tenant (`<topic>:t:inst:<installation>`), all with the same consumer group, plus a set of active tenants. The relay routes outbox rows to the lane from `fair_key`/`installation_id`; reaper and approval re-enqueues carry the same key. Indexing and evals stay on single streams.
- **Round-robin.** Each read rotates the tenant list and takes at most one message per tenant in one `XREADGROUP` call.
- **Concurrency cap with leases.** A tenant's in-flight work is a sorted set of slot leases with expiry, taken and checked in one Lua script, so the cap holds across workers and a dead worker cannot leak capacity.
- **Work-conserving.** In a round where no other tenant has work, a capped tenant may borrow idle slots (up to 4× its cap); when every tenant is at capacity the read waits for a slot instead of spinning.
- **Pruning.** An atomic Lua check removes a tenant from the active set when its lane has no backlog, nothing in flight and nothing delayed; the relay re-adds it on the next publish, so it cannot be lost.
- **Load shedding.** Indexing and online evals pause while reviews or Q&A wait longer than 60 s.

## Consequences

- Live (`docs/load-and-chaos-report.md`): quiet tenants' p95 1.9 s vs 5.9 s with FIFO while a 100-PR flood finished in p95 5.7 s vs 5.0 s.
- More Redis keys (one stream, slot set and delayed set per active tenant) and more round trips per read than a single stream; pruning keeps this proportional to tenants with work, not all tenants.
- Getting it right took four live-only fixes (late consumer groups, busy loop, idle capacity, unpruned lanes); the in-process tests now cover each.

## When we would revisit

- Thousands of concurrently active tenants → read cost per round grows with them; move to a scheduler service that hands out work (or Kafka with partitioning per tenant plus a fair consumer), or weight tenants by plan.
- Need for strict priorities between plans → weighted round-robin (take N messages per tenant per round by weight).
