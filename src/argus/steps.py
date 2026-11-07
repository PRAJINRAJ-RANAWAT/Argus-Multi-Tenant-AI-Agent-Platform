"""Checkpointed run steps: record before acting, replay on resume.

``StepRunner.step`` is the only way agent code performs an LLM call, tool call or
retrieval. For step ``seq`` of a run it:

1. Looks up ``run_steps(run_id, seq)``. If it is ``done`` and its ``input_hash`` matches,
   it returns the stored output without calling anything (replay after a crash).
   A different hash means the agent code is not deterministic for this run →
   ``NonDeterminismError`` (permanent; replay would be wrong).
2. Checks the run's budgets (step count, tokens, deadline) → ``BudgetExceededError``.
3. Writes the step as ``started``, fenced on the run's ``lease_version``.
4. Calls the function, then marks the step ``done`` with its output and adds token usage
   to the run, in one fenced transaction.

A step left ``started`` by a dead worker is executed again. That is safe for reads and
LLM calls; write tools are additionally guarded by the side-effect ledger
(``argus.tools.idempotency.perform_once``), keyed on the step.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from argus.metrics import STEPS
from argus.runs import Lease, LeaseLostError
from argus.telemetry import span

STEP_KINDS = frozenset({"llm_call", "tool_call", "retrieval", "approval"})


class NonDeterminismError(Exception):
    """A replayed step's input differs from the recorded one."""


class BudgetExceededError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"run budget exceeded: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class StepOutcome:
    output: Any
    input_tokens: int = 0
    output_tokens: int = 0


def input_hash(kind: str, name: str, payload: Any) -> str:
    canonical = json.dumps(
        {"kind": kind, "name": name, "input": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class StepRunner:
    """Hands out sequence numbers for one leased run and checkpoints each step."""

    def __init__(self, pool: asyncpg.Pool, lease: Lease) -> None:
        self.pool = pool
        self.lease = lease
        self.seq = 0
        self.replayed = 0
        self.executed = 0

    async def step(
        self,
        kind: str,
        name: str,
        payload: Any,
        fn: Callable[[], Awaitable[StepOutcome]],
        *,
        store_input: bool = True,
    ) -> Any:
        if kind not in STEP_KINDS:
            raise ValueError(f"unknown step kind {kind!r}")
        seq = self.seq
        self.seq += 1
        digest = input_hash(kind, name, payload)

        row = await self.pool.fetchrow(
            "SELECT status, input_hash, output FROM run_steps WHERE run_id = $1 AND seq = $2",
            self.lease.run_id,
            seq,
        )
        if row is not None and row["input_hash"] != digest:
            raise NonDeterminismError(
                f"run {self.lease.run_id} step {seq} ({kind}:{name}) input changed on replay"
            )
        if row is not None and row["status"] == "done":
            self.replayed += 1
            STEPS.labels(kind, "replayed").inc()
            return json.loads(row["output"]) if row["output"] is not None else None

        await self._check_budget(seq)
        await self._start(seq, kind, name, digest, payload if store_input else None)
        with span(
            f"step.{kind} {name}",
            attributes={"argus.run_id": str(self.lease.run_id), "argus.step": seq},
        ) as s:
            try:
                outcome = await fn()
            except Exception as exc:
                await self._fail(seq, f"{type(exc).__name__}: {exc}")
                STEPS.labels(kind, "failed").inc()
                raise
            s.set_attribute("llm.input_tokens", outcome.input_tokens)
            s.set_attribute("llm.output_tokens", outcome.output_tokens)
        await self._finish(seq, outcome)
        self.executed += 1
        STEPS.labels(kind, "executed").inc()
        return outcome.output

    async def _check_budget(self, seq: int) -> None:
        row = await self.pool.fetchrow(
            """
            SELECT max_steps, max_tokens, input_tokens + output_tokens AS used_tokens,
                   deadline_at IS NOT NULL AND deadline_at < now() AS past_deadline
            FROM runs WHERE id = $1
            """,
            self.lease.run_id,
        )
        if row is None:
            raise LeaseLostError(str(self.lease.run_id))
        if seq >= row["max_steps"]:
            raise BudgetExceededError(f"max_steps={row['max_steps']}")
        if row["used_tokens"] >= row["max_tokens"]:
            raise BudgetExceededError(f"max_tokens={row['max_tokens']}")
        if row["past_deadline"]:
            raise BudgetExceededError("deadline")

    async def _start(self, seq: int, kind: str, name: str, digest: str, payload: Any) -> None:
        # Fenced insert: only succeeds while this worker's lease_version is current.
        # A 'started'/'failed' row from an earlier attempt is taken over; a 'done' row is
        # never overwritten (WHERE on the DO UPDATE).
        tag = await self.pool.execute(
            """
            INSERT INTO run_steps (run_id, seq, kind, name, input_hash, input, lease_version,
                                   tenant_id)
            SELECT r.id, $3, $4, $5, $6, $7::jsonb, r.lease_version, r.tenant_id
            FROM runs r WHERE r.id = $1 AND r.lease_version = $2 AND r.status = 'running'
            ON CONFLICT (run_id, seq) DO UPDATE
            SET status = 'started', lease_version = EXCLUDED.lease_version, error = NULL,
                attempts = run_steps.attempts + 1, started_at = now(), finished_at = NULL
            WHERE run_steps.status <> 'done'
            """,
            self.lease.run_id,
            self.lease.version,
            seq,
            kind,
            name,
            digest,
            json.dumps(payload),
        )
        if tag != "INSERT 0 1":
            raise LeaseLostError(str(self.lease.run_id))

    async def _finish(self, seq: int, outcome: StepOutcome) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            tag = await conn.execute(
                """
                UPDATE runs SET input_tokens = input_tokens + $3,
                                output_tokens = output_tokens + $4, updated_at = now()
                WHERE id = $1 AND lease_version = $2 AND status = 'running'
                """,
                self.lease.run_id,
                self.lease.version,
                outcome.input_tokens,
                outcome.output_tokens,
            )
            if tag != "UPDATE 1":
                raise LeaseLostError(str(self.lease.run_id))
            await conn.execute(
                """
                UPDATE run_steps SET status = 'done', output = $3::jsonb, input_tokens = $4,
                                     output_tokens = $5, finished_at = now()
                WHERE run_id = $1 AND seq = $2
                """,
                self.lease.run_id,
                seq,
                json.dumps(outcome.output),
                outcome.input_tokens,
                outcome.output_tokens,
            )

    async def _fail(self, seq: int, error: str) -> None:
        await self.pool.execute(
            """
            UPDATE run_steps SET status = 'failed', error = $4, finished_at = now()
            WHERE run_id = $1 AND seq = $2 AND lease_version = $3 AND status = 'started'
            """,
            self.lease.run_id,
            seq,
            self.lease.version,
            error[:2000],
        )
