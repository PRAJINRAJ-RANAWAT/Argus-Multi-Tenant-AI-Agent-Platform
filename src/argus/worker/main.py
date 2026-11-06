"""Worker process: consumes ``jobs:review`` and runs the PR review agent."""

import asyncio
import contextlib
import functools
import logging
import os
import signal
import socket

import asyncpg
import httpx
from redis.asyncio import Redis

from argus.agents.pr_review import AGENT_TYPE as PR_REVIEW
from argus.agents.pr_review import ReviewDeps, handle_review_message
from argus.config import Settings, get_settings
from argus.db.pool import create_pool
from argus.github.auth import InstallationTokenProvider
from argus.github.client import GitHubClient
from argus.llm.client import LLMClient
from argus.logging import configure_logging
from argus.queue.consumer import run_consumer
from argus.queue.streams import StreamQueue
from argus.queue.topics import JOBS_REVIEW
from argus.runs import reap_runs

log = logging.getLogger("argus.worker")

REVIEW_GROUP = "review-workers"
REAP_INTERVAL_S = 10.0
TOPIC_FOR_AGENT = {PR_REVIEW: JOBS_REVIEW}


async def reaper_loop(pool: asyncpg.Pool, stop: asyncio.Event, interval_s: float) -> None:
    """Re-enqueue runs whose worker died (expired lease) or whose message was lost."""
    while not stop.is_set():
        try:
            n = await reap_runs(pool, TOPIC_FOR_AGENT)
            if n:
                log.warning("reaped runs", extra={"count": n})
        except Exception:
            log.exception("run reaper failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    worker_id = settings.worker_id or default_worker_id()
    pool = await create_pool(settings.database_url, max_size=max(5, settings.worker_concurrency))
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        tokens = InstallationTokenProvider(
            http,
            redis,
            app_id=settings.github_app_id,
            private_key_pem=settings.github_private_key_pem(),
            api_url=settings.github_api_url,
        )
        deps = ReviewDeps(
            pool=pool,
            github=GitHubClient(http, tokens.get, settings.github_api_url),
            llm=LLMClient(
                http,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                model=settings.llm_model,
            ),
            worker_id=worker_id,
        )
        queue = StreamQueue(redis, JOBS_REVIEW, REVIEW_GROUP, worker_id)
        log.info("worker started", extra={"worker_id": worker_id, "stream": JOBS_REVIEW})
        reaper = asyncio.create_task(reaper_loop(pool, stop, REAP_INTERVAL_S))
        try:
            await run_consumer(
                queue,
                functools.partial(handle_review_message, deps),
                stop,
                concurrency=settings.worker_concurrency,
            )
        finally:
            log.info("worker stopping")
            stop.set()
            await reaper
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
