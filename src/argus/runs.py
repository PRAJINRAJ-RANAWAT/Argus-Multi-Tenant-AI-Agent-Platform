"""Run persistence: idempotent creation, lease claim with fencing, fenced completion."""

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

log = logging.getLogger("argus.runs")

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "superseded"})


@dataclass(frozen=True)
class Run:
    id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    idempotency_key: str


@dataclass(frozen=True)
class Lease:
    run_id: uuid.UUID
    owner: str
    version: int  # fencing token


class LeaseLostError(Exception):
    """A fenced write found a newer lease version: this worker no longer owns the run."""


async def upsert_tenant(pool: asyncpg.Pool, installation_id: int, account_login: str) -> uuid.UUID:
    tenant_id: uuid.UUID = await pool.fetchval(
        """
        INSERT INTO tenants (github_installation_id, github_account_login)
        VALUES ($1, $2)
        ON CONFLICT (github_installation_id)
        DO UPDATE SET github_account_login = EXCLUDED.github_account_login, updated_at = now()
        RETURNING id
        """,
        installation_id,
        account_login,
    )
    return tenant_id


async def upsert_repo(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, github_repo_id: int, full_name: str, private: bool
) -> uuid.UUID:
    repo_id: uuid.UUID = await pool.fetchval(
        """
        INSERT INTO repos (tenant_id, github_repo_id, full_name, private)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id, github_repo_id)
        DO UPDATE SET full_name = EXCLUDED.full_name, private = EXCLUDED.private,
                      updated_at = now()
        RETURNING id
        """,
        tenant_id,
        github_repo_id,
        full_name,
        private,
    )
    return repo_id


async def get_or_create_run(
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    repo_id: uuid.UUID | None,
    agent_type: str,
    idempotency_key: str,
    trigger_event_id: uuid.UUID | None,
    pr_number: int | None = None,
    head_sha: str | None = None,
) -> Run:
    """Insert the run unless one with this idempotency key exists; return it either way."""
    row = await pool.fetchrow(
        """
        WITH ins AS (
            INSERT INTO runs (tenant_id, repo_id, agent_type, idempotency_key, trigger_event_id,
                              pr_number, head_sha)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING id, tenant_id, status, idempotency_key
        )
        SELECT * FROM ins
        UNION ALL
        SELECT id, tenant_id, status, idempotency_key FROM runs
        WHERE tenant_id = $1 AND idempotency_key = $4
        LIMIT 1
        """,
        tenant_id,
        repo_id,
        agent_type,
        idempotency_key,
        trigger_event_id,
        pr_number,
        head_sha,
    )
    assert row is not None
    return Run(row["id"], row["tenant_id"], row["status"], row["idempotency_key"])


async def claim_run(
    pool: asyncpg.Pool, run_id: uuid.UUID, owner: str, lease_s: float
) -> Lease | None:
    """Take the lease if the run is queued or its previous lease expired.

    Bumps ``lease_version``, which every later write must present (fencing).
    """
    version = await pool.fetchval(
        """
        UPDATE runs
        SET status = 'running', lease_owner = $2,
            lease_expires_at = now() + make_interval(secs => $3),
            lease_version = lease_version + 1, attempts = attempts + 1,
            started_at = coalesce(started_at, now()), updated_at = now()
        WHERE id = $1
          AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
        RETURNING lease_version
        """,
        run_id,
        owner,
        lease_s,
    )
    return None if version is None else Lease(run_id, owner, int(version))


async def release_run(pool: asyncpg.Pool, lease: Lease, error: str) -> bool:
    """Give the run back (status ``queued``) after an in-process failure so a retry can
    claim it immediately instead of waiting for the lease to expire. Fenced."""
    tag = await pool.execute(
        """
        UPDATE runs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                        error = $3, updated_at = now()
        WHERE id = $1 AND lease_version = $2 AND status = 'running'
        """,
        lease.run_id,
        lease.version,
        error[:2000],
    )
    return bool(tag == "UPDATE 1")


async def reap_runs(
    pool: asyncpg.Pool,
    topic_for_agent: dict[str, str],
    *,
    stuck_queued_s: float = 600.0,
    limit: int = 100,
) -> int:
    """Self-healing for runs whose worker died.

    * ``running`` with an expired lease → back to ``queued``, ``lease_version`` bumped (so
      the presumed-dead worker's later writes are fenced off), and re-enqueued.
    * ``queued`` but untouched for ``stuck_queued_s`` (its message was lost or acked
      by a worker that lost the race) → re-enqueued.

    Re-enqueueing goes through the outbox in the same transaction.
    """
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """
            WITH picked AS (
                SELECT id FROM runs
                WHERE (status = 'running' AND lease_expires_at < now())
                   OR (status = 'queued'
                       AND updated_at < now() - make_interval(secs => $1))
                ORDER BY updated_at
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE runs r
            SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                lease_version = CASE WHEN r.status = 'running'
                                     THEN r.lease_version + 1 ELSE r.lease_version END,
                updated_at = now()
            FROM picked WHERE r.id = picked.id
            RETURNING r.id, r.tenant_id, r.agent_type
            """,
            stuck_queued_s,
            limit,
        )
        for r in rows:
            topic = topic_for_agent.get(r["agent_type"])
            if topic is None:
                continue
            await conn.execute(
                "INSERT INTO outbox (topic, payload) VALUES ($1, $2::jsonb)",
                topic,
                json.dumps({"run_id": str(r["id"]), "reason": "reaped"}),
            )
    return len(rows)


async def add_usage(
    pool: asyncpg.Pool, lease: Lease, input_tokens: int, output_tokens: int, cost_usd: float = 0
) -> None:
    status = await pool.execute(
        """
        UPDATE runs SET input_tokens = input_tokens + $3, output_tokens = output_tokens + $4,
                        cost_usd = cost_usd + $5, updated_at = now()
        WHERE id = $1 AND lease_version = $2
        """,
        lease.run_id,
        lease.version,
        input_tokens,
        output_tokens,
        cost_usd,
    )
    if status != "UPDATE 1":
        raise LeaseLostError(str(lease.run_id))


async def finish_run(
    pool: asyncpg.Pool,
    lease: Lease,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    assert status in TERMINAL
    tag = await pool.execute(
        """
        UPDATE runs SET status = $3, result = $4::jsonb, error = $5, finished_at = now(),
                        lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE id = $1 AND lease_version = $2 AND status = 'running'
        """,
        lease.run_id,
        lease.version,
        status,
        json.dumps(result) if result is not None else None,
        error,
    )
    if tag != "UPDATE 1":
        raise LeaseLostError(str(lease.run_id))


async def renew_lease(pool: asyncpg.Pool, lease: Lease, lease_s: float) -> bool:
    """Extend the lease if this worker still holds it (fenced). False = lease lost."""
    tag = await pool.execute(
        """
        UPDATE runs SET lease_expires_at = now() + make_interval(secs => $3), updated_at = now()
        WHERE id = $1 AND lease_version = $2 AND status = 'running'
        """,
        lease.run_id,
        lease.version,
        lease_s,
    )
    return bool(tag == "UPDATE 1")


class LeaseKeeper:
    """Background heartbeat: renews a short lease every ``lease_s / 3`` seconds.

    Use as ``async with LeaseKeeper(pool, lease, lease_s) as keeper:``. If a renewal finds
    the lease gone (reaped, superseded), ``keeper.lost`` is set and renewals stop; every
    fenced write fails with ``LeaseLostError`` from then on. Transient DB errors are
    retried on the next tick; if they last past the lease, the reaper takes over.
    """

    def __init__(self, pool: asyncpg.Pool, lease: Lease, lease_s: float) -> None:
        self.pool = pool
        self.lease = lease
        self.lease_s = lease_s
        self.interval_s = lease_s / 3
        self.lost = asyncio.Event()
        self.renewals = 0
        self._task: asyncio.Task[None] | None = None

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                ok = await renew_lease(self.pool, self.lease, self.lease_s)
            except Exception:
                log.warning(
                    "lease renewal failed", exc_info=True, extra={"run_id": str(self.lease.run_id)}
                )
                continue
            if not ok:
                log.warning("lease lost", extra={"run_id": str(self.lease.run_id)})
                self.lost.set()
                return
            self.renewals += 1

    async def __aenter__(self) -> "LeaseKeeper":
        self._task = asyncio.create_task(self._beat())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
