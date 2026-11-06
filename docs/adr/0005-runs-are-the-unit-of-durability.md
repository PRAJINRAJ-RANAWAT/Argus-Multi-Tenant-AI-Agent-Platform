# 0005. Runs, not queue messages, are the unit of durability

- Status: accepted
- Date: 2026-11-07

## Context

An agent run is long-lived (30 s to minutes) and has external side effects. The queue delivers at least once and can redeliver after a crash, but a message alone cannot tell "the worker that has this run died" from "the worker that has this run is still working".

We found the failure mode while writing the M1 tests: a duplicate message arrives while run R is leased, so it is acked and dropped (correctly, since someone else has it). Then the lease holder dies. Its own message is eventually reclaimed, but the lease has not expired yet, so that delivery is *also* acked as "held by another worker". The run then stays in `running` forever: a lost event.

## Decision

- The `runs` row is the durable unit; a queue message is only a **wake-up** ("look at event E" or "look at run R").
- A run is created idempotently: `UNIQUE (tenant_id, idempotency_key)`, with the key `review:<repo>:<pr>:<head_sha>`.
- Workers take a **lease** (`lease_owner`, `lease_expires_at`) with a monotonically increasing **fencing token** (`lease_version`). Every subsequent write on the run is `WHERE lease_version = $mine`, and a mismatch raises `LeaseLostError`.
- An in-process failure **releases** the lease (back to `queued`, fenced) before the queue retries, so the retry can claim immediately.
- A **run reaper** (in every worker, `FOR UPDATE SKIP LOCKED`) puts runs with an expired lease back to `queued`, bumps `lease_version` (fencing off the presumed-dead worker), and re-enqueues them **through the outbox**. It also re-enqueues `queued` runs that nobody touched for 10 minutes (wake-up lost).
- So it is always safe to ack a message whose run is leased by someone else: liveness comes from the reaper, not from the message.
- External side effects go through a `side_effects` ledger (`perform_once`). The claim records the claimer's lease version; a newer lease of the same run treats an older pending claim as dead and asks GitHub (hidden `<!-- argus:review:… -->` marker in the comment) whether the call happened before retrying it.

## Consequences

- Recovery time after a hard crash ≈ lease length + reaper interval. M1 uses a fixed 180 s lease. M2 adds heartbeats so the lease can be about 30 s.
- A retried run re-executes its LLM call (tokens spent twice). M2's `run_steps` checkpointing replays completed steps from storage instead.
- Tests: concurrent claims (one winner), zombie writes fenced, release and immediate reclaim, reaper requeue + fencing, `kill -9` mid-run recovered with one comment, lost response after the comment was created (no duplicate).
