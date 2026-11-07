"""Persist an accepted webhook: event row + outbox row in one transaction."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass(frozen=True)
class PersistResult:
    event_id: uuid.UUID | None  # None when the delivery was already stored
    enqueued: bool

    @property
    def duplicate(self) -> bool:
        return self.event_id is None


async def persist_event(
    pool: asyncpg.Pool,
    *,
    source: str,
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
    topic: str | None,
    trace: dict[str, str] | None = None,
) -> PersistResult:
    """Insert the event and (if routed) its outbox message atomically.

    The ``(source, delivery_id)`` unique constraint is the durable dedupe: a
    redelivery that slips past the Redis fast path inserts nothing.
    """
    action = payload.get("action")
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    status = "received" if topic else "ignored"
    async with pool.acquire() as conn, conn.transaction():
        event_id: uuid.UUID | None = await conn.fetchval(
            """
            INSERT INTO events (source, delivery_id, event_type, action, installation_id,
                                payload, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (source, delivery_id) DO NOTHING
            RETURNING id
            """,
            source,
            delivery_id,
            event_type,
            action,
            installation_id,
            json.dumps(payload),
            status,
        )
        if event_id is None or topic is None:
            return PersistResult(event_id=event_id, enqueued=False)
        message = {
            "event_id": str(event_id),
            "source": source,
            "delivery_id": delivery_id,
            "event_type": event_type,
            "action": action,
            "installation_id": installation_id,
            "trace": trace or {},  # W3C trace context, continued by the relay and worker
        }
        await conn.execute(
            "INSERT INTO outbox (topic, payload) VALUES ($1, $2::jsonb)",
            topic,
            json.dumps(message),
        )
        return PersistResult(event_id=event_id, enqueued=True)
