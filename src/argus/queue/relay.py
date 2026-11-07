"""Transactional outbox relay: Postgres outbox → Redis Streams.

Rows are locked with ``FOR UPDATE SKIP LOCKED`` so any number of relays can run
without publishing a row twice *concurrently*. A crash after XADD but before the
COMMIT republishes those rows on the next pass, so delivery is at-least-once and
consumers must be idempotent (they are: runs are keyed by idempotency key).
"""

import asyncio
import contextlib
import json
import logging
import signal

import asyncpg
from opentelemetry.trace import SpanKind
from prometheus_client import start_http_server
from redis.asyncio import Redis

from argus.config import Settings, get_settings
from argus.db.pool import create_pool
from argus.logging import configure_logging
from argus.metrics import OUTBOX_PUBLISHED
from argus.queue.streams import STREAM_MAXLEN
from argus.telemetry import configure_tracing, extract, inject, span

log = logging.getLogger("argus.relay")

BATCH = 200


async def relay_once(pool: asyncpg.Pool, redis: Redis, batch: int = BATCH) -> int:
    """Publish one batch of unpublished outbox rows. Returns rows published."""
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """
            SELECT id, topic, payload::text AS payload FROM outbox
            WHERE published_at IS NULL
            ORDER BY id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            batch,
        )
        if not rows:
            return 0
        pipe = redis.pipeline(transaction=False)
        for r in rows:
            fields = {"data": r["payload"], "attempt": "0", "outbox_id": str(r["id"])}
            parent = extract((json.loads(r["payload"]) or {}).get("trace"))
            with span(
                "outbox.relay",
                kind=SpanKind.PRODUCER,
                parent=parent,
                attributes={"messaging.destination": r["topic"], "outbox.id": r["id"]},
            ):
                fields.update(inject())  # traceparent of this hop, for the consumer
            pipe.xadd(r["topic"], fields, maxlen=STREAM_MAXLEN, approximate=True)  # type: ignore[arg-type]
            OUTBOX_PUBLISHED.labels(r["topic"]).inc()
        await pipe.execute()
        await conn.execute(
            "UPDATE outbox SET published_at = now(), attempts = attempts + 1 WHERE id = ANY($1)",
            [r["id"] for r in rows],
        )
    return len(rows)


async def record_failure(pool: asyncpg.Pool, error: str) -> None:
    with contextlib.suppress(Exception):
        await pool.execute(
            """
            UPDATE outbox SET attempts = attempts + 1, last_error = $1
            WHERE id = (SELECT id FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 1)
            """,
            error[:1000],
        )


async def run_relay(
    pool: asyncpg.Pool,
    redis: Redis,
    dsn: str,
    stop: asyncio.Event,
    *,
    poll_s: float = 1.0,
) -> None:
    """Drain the outbox; wake on NOTIFY, poll as a fallback."""
    wake = asyncio.Event()
    listener = await asyncpg.connect(dsn)

    def on_notify(*_: object) -> None:
        wake.set()

    await listener.add_listener("outbox", on_notify)
    backoff = 0.5
    try:
        while not stop.is_set():
            wake.clear()
            try:
                while await relay_once(pool, redis) == BATCH:
                    pass
                backoff = 0.5
            except Exception as exc:
                log.warning("relay pass failed", exc_info=True)
                await record_failure(pool, f"{type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())]
            await asyncio.wait(waiters, timeout=poll_s, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
    finally:
        with contextlib.suppress(Exception):
            await listener.remove_listener("outbox", on_notify)
        await listener.close()


def main() -> None:
    settings: Settings = get_settings().model_copy(update={"service_name": "argus-relay"})
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(
        settings.service_name, endpoint=settings.otel_endpoint, headers=settings.otel_header_map()
    )
    start_http_server(settings.metrics_port + 1)  # 9101: next to the worker's 9100

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        pool = await create_pool(settings.database_url, max_size=4)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            log.info("relay started")
            await run_relay(pool, redis, settings.database_url, stop)
        finally:
            await redis.aclose()
            await pool.close()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
