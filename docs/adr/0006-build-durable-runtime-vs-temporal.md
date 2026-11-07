# 0006. Build the durable agent runtime on Postgres instead of adopting Temporal

- Status: accepted
- Date: 2026-11-07

## Context

M2 needs agent runs that survive worker crashes: resume at the step after the last one that finished, never repeat a side effect, stop stale work when a newer commit arrives, and enforce budgets. Durable-execution engines (Temporal, Restate, DBOS, Inngest) solve exactly this: workflow code is replayed from an event history, and activities are retried with recorded results.

What we have already: Postgres as the source of truth, a transactional outbox, Redis Streams for delivery (ADRs 0003–0004), and runs as the unit of durability with leases and fencing tokens (ADR 0005). The deployment target is one VPS (ADR 0002).

## Decision

Build it on Postgres:

- `run_steps(run_id, seq)` is the history. `StepRunner.step()` writes a step as `started` (fenced on the run's `lease_version`), performs it, then marks it `done` with its output and token usage in one fenced transaction.
- On resume a `done` step with the same input hash replays from storage; a different hash raises `NonDeterminismError` and fails the run. This is Temporal's determinism rule, enforced by hashing the step input.
- A short lease (30 s) is renewed by a heartbeat every 10 s. The reaper reclaims expired leases and bumps the fencing token, so a paused "zombie" worker cannot write.
- Write tools also pass through the side-effect ledger (`perform_once`), because a step left `started` by a dead worker is executed again: the ledger and the hidden marker on GitHub decide whether the call already happened.
- Supersede: between steps the run checks for a newer run of the same PR (ordered by the trigger's first receipt) and stops; an owner-checked Redis lock per (repo, PR) keeps one live review per PR.
- Budgets (max steps, max tokens, deadline) are checked before every step.

## Consequences

- No new infrastructure: about 400 lines of code, tested with real Postgres and Redis (crash between steps, crash mid-step, zombie writes, concurrent supersede, budgets).
- Every step's input and output is in SQL, so the dashboard, evals and debugging can query run history directly.
- We own the hard parts Temporal would give us: timers (we use queue delays and `deadline_at`), signals (approvals in M6 re-enqueue the run), versioning of workflow code (we only detect changes through input hashes, and have no patching API), and visibility tooling.
- The step model is coarse: one row per LLM or tool call. Parallel steps inside a run would need a `parent_seq` and are out of scope.

## When we would revisit

Switch to Temporal (or a similar engine) when one of these becomes true:

- Runs need long timers, child workflows, or fan-out and fan-in across many steps, which we would otherwise build ourselves.
- Agent code changes often while runs are in flight and we need real workflow versioning instead of failing mismatched runs.
- Several teams write workflows and need a shared programming model and UI.
- Scale moves past one Postgres primary for run state (thousands of step writes per second).

To keep the switch cheap, agent code only talks to `StepRunner.step()` and the tool layer, which map one-to-one to Temporal activities.
