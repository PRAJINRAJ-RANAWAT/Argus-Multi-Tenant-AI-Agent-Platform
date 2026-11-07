"""Human approval of write actions.

A write the tenant marked as needing approval runs as an ``approval`` step before the
tool call. The step creates (idempotently) an ``approvals`` row and, while it is
pending, raises ``WaitingForApprovalError``: the run is parked in ``waiting_approval``,
its lease and worker are released, and nothing is held while a human decides (hours
are fine). A decision re-queues the run through the outbox; on resume, earlier steps
replay from storage and the approval step now returns the decision.
"""

import json
import uuid
from typing import Any

import asyncpg

from argus.queue.topics import JOBS_QA, JOBS_REVIEW
from argus.runs import Lease

TOPIC_FOR_AGENT = {"pr_review": JOBS_REVIEW, "qa": JOBS_QA}


class WaitingForApprovalError(Exception):
    def __init__(self, approval_id: uuid.UUID) -> None:
        super().__init__(f"waiting for approval {approval_id}")
        self.approval_id = approval_id


async def required_tools(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> frozenset[str]:
    tools = await pool.fetchval(
        "SELECT require_approval_for FROM tenant_settings WHERE tenant_id = $1", tenant_id
    )
    return frozenset(tools or [])


async def approval_gate(
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_key: str,
    tool: str,
    preview: dict[str, Any],
) -> dict[str, Any]:
    """Return the decision, or raise ``WaitingForApprovalError`` while pending."""
    await pool.execute(
        """
        INSERT INTO approvals (tenant_id, run_id, step_key, tool, preview)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (run_id, step_key) DO NOTHING
        """,
        tenant_id,
        run_id,
        step_key,
        tool,
        json.dumps(preview),
    )
    row = await pool.fetchrow(
        "SELECT id, status, decided_by, reason FROM approvals WHERE run_id = $1 AND step_key = $2",
        run_id,
        step_key,
    )
    assert row is not None
    if row["status"] == "pending":
        raise WaitingForApprovalError(row["id"])
    return {
        "approved": row["status"] == "approved",
        "by": row["decided_by"],
        "reason": row["reason"],
        "approval_id": str(row["id"]),
    }


async def park_run(pool: asyncpg.Pool, lease: Lease) -> bool:
    tag = await pool.execute(
        """
        UPDATE runs SET status = 'waiting_approval', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = now()
        WHERE id = $1 AND lease_version = $2 AND status = 'running'
        """,
        lease.run_id,
        lease.version,
    )
    return bool(tag == "UPDATE 1")


async def decide(
    conn: asyncpg.Connection,
    approval_id: uuid.UUID,
    *,
    approve: bool,
    actor: str,
    reason: str | None,
) -> uuid.UUID | None:
    """Record a decision and re-queue the parked run. Returns the run id, or None if the
    approval does not exist or was already decided (decisions are final)."""
    run_id: uuid.UUID | None = await conn.fetchval(
        """
        UPDATE approvals SET status = $2, decided_at = now(), decided_by = $3, reason = $4
        WHERE id = $1 AND status = 'pending'
        RETURNING run_id
        """,
        approval_id,
        "approved" if approve else "rejected",
        actor,
        reason,
    )
    if run_id is None:
        return None
    agent = await conn.fetchval(
        """
        UPDATE runs SET status = 'queued', updated_at = now()
        WHERE id = $1 AND status = 'waiting_approval'
        RETURNING agent_type
        """,
        run_id,
    )
    if agent in TOPIC_FOR_AGENT:
        await conn.execute(
            "INSERT INTO outbox (topic, payload) VALUES ($1, $2::jsonb)",
            TOPIC_FOR_AGENT[agent],
            json.dumps({"run_id": str(run_id), "reason": "approval_decided"}),
        )
    return run_id
