"""Consumer loop: bounded concurrency, ack on success, retry/DLQ on failure, reclaim and
promote housekeeping."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from opentelemetry.trace import SpanKind

from argus.metrics import MESSAGES
from argus.queue.streams import Message
from argus.telemetry import extract, span

log = logging.getLogger(__name__)

Handler = Callable[[Message], Awaitable[None]]


class WorkQueue(Protocol):
    """What the consumer needs: ``StreamQueue`` (one stream) or ``FairQueue`` (per-tenant
    lanes) both provide it."""

    stream: str

    async def ensure_group(self) -> None: ...
    async def read(self, count: int = 10, block_ms: int = 1000) -> list[Message]: ...
    async def ack(self, msg: Message) -> bool: ...
    async def retry(
        self,
        msg: Message,
        error: str,
        *,
        min_delay_s: float | None = None,
        count_attempt: bool = True,
    ) -> str: ...
    async def dead_letter(self, msg: Message, error: str) -> str: ...
    async def promote_due(self, limit: int = 100, now_ms: int | None = None) -> int: ...
    async def reclaim(self, count: int = 50, min_idle_ms: int | None = None) -> list[Message]: ...


class PermanentError(Exception):
    """Handler error that retrying cannot fix; the message goes straight to the DLQ."""


def _parent(msg: Message) -> Any:
    """Trace context: the relay's ``traceparent`` field, or (after a retry re-publish)
    the original one carried in the payload."""
    if "traceparent" in msg.fields:
        return extract(msg.fields)
    try:
        data = msg.json()
    except ValueError:
        return None
    return extract(data.get("trace") if isinstance(data, dict) else None)


async def handle_one(queue: WorkQueue, handler: Handler, msg: Message) -> str:
    with span(
        f"queue.process {queue.stream}",
        kind=SpanKind.CONSUMER,
        parent=_parent(msg),
        attributes={"messaging.message.id": msg.id, "messaging.attempt": msg.attempt},
    ) as s:
        outcome = await _handle_one(queue, handler, msg)
        s.set_attribute("argus.outcome", outcome)
        MESSAGES.labels(queue.stream, outcome).inc()
        return outcome


async def _handle_one(queue: WorkQueue, handler: Handler, msg: Message) -> str:
    try:
        return await _settle(queue, handler, msg)
    except Exception:
        # The queue itself failed (e.g. Redis restarting) while acking or retrying. The
        # message stays pending and is reclaimed after the visibility timeout; the work
        # is idempotent, so redoing it is safe.
        log.warning(
            "queue operation failed; message left for reclaim",
            extra={"msg_id": msg.id},
            exc_info=True,
        )
        return "unsettled"


async def _settle(queue: WorkQueue, handler: Handler, msg: Message) -> str:
    try:
        await handler(msg)
    except PermanentError as exc:
        log.warning("permanent failure", extra={"msg_id": msg.id, "error": str(exc)})
        return await queue.dead_letter(msg, f"permanent: {exc}")
    except Exception as exc:
        log.warning(
            "handler failed", extra={"msg_id": msg.id, "attempt": msg.attempt}, exc_info=True
        )
        retry_after = getattr(exc, "retry_after_s", None)
        return await queue.retry(
            msg,
            f"{type(exc).__name__}: {exc}",
            min_delay_s=retry_after,
            count_attempt=not getattr(exc, "is_wait", False),
        )
    return "acked" if await queue.ack(msg) else "lost_ownership"


async def run_consumer(
    queue: WorkQueue,
    handler: Handler,
    stop: asyncio.Event,
    *,
    concurrency: int = 10,
    block_ms: int = 500,
    housekeeping_s: float = 1.0,
    pause_if: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Single loop: never more than ``concurrency`` handlers in flight.

    Every ``housekeeping_s`` it promotes due retries and reclaims messages from dead
    consumers (XAUTOCLAIM) before reading new ones.
    """
    await queue.ensure_group()
    tasks: set[asyncio.Task[str]] = set()
    loop = asyncio.get_running_loop()
    next_housekeeping = 0.0

    def spawn(msg: Message) -> None:
        t = asyncio.create_task(handle_one(queue, handler, msg))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    try:
        while not stop.is_set():
            if len(tasks) >= concurrency:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            if pause_if is not None and await pause_if():
                # Load shedding: low-priority work waits while interactive queues lag.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                continue
            try:
                if loop.time() >= next_housekeeping:
                    next_housekeeping = loop.time() + housekeeping_s
                    await queue.promote_due()
                    for msg in await queue.reclaim(count=concurrency - len(tasks)):
                        spawn(msg)
                    if len(tasks) >= concurrency:
                        continue
                msgs = await queue.read(count=concurrency - len(tasks), block_ms=block_ms)
            except Exception:
                log.exception("queue read failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                continue
            for msg in msgs:
                spawn(msg)
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
