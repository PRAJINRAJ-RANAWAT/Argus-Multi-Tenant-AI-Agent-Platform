"""Worker process entry point. M0: connects to dependencies and idles until signalled."""

import asyncio
import contextlib
import logging
import signal

from redis.asyncio import Redis

from argus.config import Settings, get_settings
from argus.db.pool import create_pool
from argus.health import check_postgres, check_redis
from argus.logging import configure_logging

log = logging.getLogger("argus.worker")


async def run_worker(settings: Settings, stop: asyncio.Event, heartbeat_s: float = 30.0) -> None:
    pool = await create_pool(settings.database_url, max_size=5)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        log.info("worker started")
        while not stop.is_set():
            pg_ok, redis_ok = await asyncio.gather(check_postgres(pool), check_redis(redis))
            log.info("worker heartbeat", extra={"postgres": pg_ok, "redis": redis_ok})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_s)
        log.info("worker stopping")
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    settings = get_settings().model_copy(update={"service_name": "argus-worker"})
    configure_logging(settings.service_name, settings.log_level)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
