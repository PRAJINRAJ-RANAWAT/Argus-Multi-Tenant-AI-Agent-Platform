"""Exactly-once-ish external side effects on top of at-least-once delivery.

``perform_once`` uses the ``side_effects`` row as a claim:

1. ``INSERT ... ON CONFLICT DO NOTHING`` a ``pending`` row. Whoever inserts it owns
   the side effect and performs it, then marks it ``done``.
2. If a row exists and is ``done``: return the recorded result, do nothing.
3. If it is ``pending`` and fresh: another worker is mid-call → ``SideEffectInProgressError``
   (retryable; the queue retries later).
4. If it is ``pending`` and stale (the owner probably crashed mid-call, so we do not
   know whether the call happened): ask the external system (``find_existing``,
   e.g. search PR comments for a hidden marker). Found → record ``done``. Not found →
   take over the claim with a compare-and-set on ``claimed_at`` and perform it.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import asyncpg

STALE_AFTER_S = 120


class SideEffectInProgressError(Exception):
    """Another worker holds a fresh claim on this side effect."""


@dataclass(frozen=True)
class SideEffectResult:
    external_id: str
    performed: bool  # False when it had already happened


async def perform_once(
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None,
    key: str,
    kind: str,
    perform: Callable[[], Awaitable[str]],
    find_existing: Callable[[], Awaitable[str | None]],
    lease_version: int | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> SideEffectResult:
    claimed_at: datetime | None = await pool.fetchval(
        """
        INSERT INTO side_effects (tenant_id, run_id, idempotency_key, kind, lease_version)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        RETURNING claimed_at
        """,
        tenant_id,
        run_id,
        key,
        kind,
        lease_version,
    )
    if claimed_at is None:
        row = await pool.fetchrow(
            """
            SELECT status, external_id, claimed_at,
                   now() - claimed_at > make_interval(secs => $3)
                   OR (run_id = $4 AND lease_version < $5) AS stale
            FROM side_effects WHERE tenant_id = $1 AND idempotency_key = $2
            """,
            tenant_id,
            key,
            stale_after_s,
            run_id,
            lease_version,
        )
        if row is None:  # pragma: no cover - deleted between statements
            raise SideEffectInProgressError(key)
        if row["status"] == "done":
            return SideEffectResult(external_id=row["external_id"] or "", performed=False)
        if not row["stale"]:
            raise SideEffectInProgressError(key)
        existing = await find_existing()
        if existing is not None:
            await _complete(pool, tenant_id, key, existing)
            return SideEffectResult(external_id=existing, performed=False)
        claimed_at = await pool.fetchval(
            """
            UPDATE side_effects
            SET claimed_at = now(), run_id = coalesce($4, run_id), lease_version = $5
            WHERE tenant_id = $1 AND idempotency_key = $2 AND status = 'pending'
              AND claimed_at = $3
            RETURNING claimed_at
            """,
            tenant_id,
            key,
            row["claimed_at"],
            run_id,
            lease_version,
        )
        if claimed_at is None:
            raise SideEffectInProgressError(key)  # someone else took it over first
    external_id = await perform()
    await _complete(pool, tenant_id, key, external_id)
    return SideEffectResult(external_id=external_id, performed=True)


async def _complete(pool: asyncpg.Pool, tenant_id: uuid.UUID, key: str, external_id: str) -> None:
    await pool.execute(
        """
        UPDATE side_effects SET status = 'done', external_id = $3, completed_at = now()
        WHERE tenant_id = $1 AND idempotency_key = $2
        """,
        tenant_id,
        key,
        external_id,
    )
