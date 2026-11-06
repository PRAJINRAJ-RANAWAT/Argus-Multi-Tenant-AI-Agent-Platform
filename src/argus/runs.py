"""Run persistence: idempotent creation, lease claim with fencing, fenced completion."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

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
