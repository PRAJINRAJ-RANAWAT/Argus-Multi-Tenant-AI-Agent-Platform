"""Live run events for the dashboard.

A trigger on ``runs`` sends ``pg_notify('argus_runs', …)`` on every insert and status
change. Each API process holds one LISTEN connection and fans events out to its SSE
subscribers, filtered by tenant (and by repo access, in the endpoint). Slow
subscribers drop events instead of blocking the others; the dashboard reloads the list
on reconnect, so a dropped event costs freshness, not correctness.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

log = logging.getLogger("argus.api.events")

CHANNEL = "argus_runs"
QUEUE_SIZE = 200


class RunEvents:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: asyncpg.Connection | None = None
        self._subs: dict[int, tuple[uuid.UUID, asyncio.Queue[dict[str, Any]]]] = {}
        self._next = 0
        self.dropped = 0

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: object) -> None:
        try:
            event = json.loads(str(payload))
            tenant = uuid.UUID(event["tenant_id"])
        except (ValueError, KeyError, TypeError):
            return
        for sub_tenant, queue in self._subs.values():
            if sub_tenant == tenant:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.dropped += 1

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self.dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)

    async def stop(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

    @contextlib.asynccontextmanager
    async def subscribe(self, tenant_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        sub_id = self._next
        self._next += 1
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs[sub_id] = (tenant_id, queue)
        try:
            yield queue
        finally:
            self._subs.pop(sub_id, None)

    @property
    def subscribers(self) -> int:
        return len(self._subs)
