"""Worker process: ``jobs:review`` (PR review), ``jobs:index`` (code indexing) and
``jobs:qa`` (codebase Q&A)."""

import asyncio
import contextlib
import functools
import json
import logging
import os
import signal
import socket

import asyncpg
import httpx
from prometheus_client import start_http_server
from redis.asyncio import Redis

from argus.agents.pr_review import AGENT_TYPE as PR_REVIEW
from argus.agents.pr_review import ReviewDeps, handle_review_message
from argus.agents.qa import AGENT_TYPE as QA_AGENT
from argus.agents.qa import QADeps, handle_qa_message
from argus.config import Settings, get_settings
from argus.db.pool import create_pool
from argus.gateway.core import Gateway, GatewayConfig
from argus.gateway.providers import AnthropicProvider, OpenAIProvider, Provider
from argus.gateway.semcache import QACache
from argus.github.auth import InstallationTokenProvider
from argus.github.client import GitHubClient
from argus.guardrails.egress import EgressGuard
from argus.http_tracing import TracingTransport
from argus.logging import configure_logging
from argus.metrics import QUEUE_DEPTH, QUEUE_OLDEST_AGE
from argus.queue.consumer import run_consumer
from argus.queue.streams import StreamQueue
from argus.queue.topics import JOBS_INDEX, JOBS_QA, JOBS_REVIEW
from argus.rag.embed import HttpEmbedder
from argus.rag.jobs import IndexDeps, handle_index_message
from argus.runs import reap_runs
from argus.telemetry import configure_tracing

log = logging.getLogger("argus.worker")

REVIEW_GROUP = "review-workers"
INDEX_GROUP = "index-workers"
QA_GROUP = "qa-workers"
REAP_INTERVAL_S = 10.0
TOPIC_FOR_AGENT = {PR_REVIEW: JOBS_REVIEW, QA_AGENT: JOBS_QA}


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


def build_gateway(
    settings: Settings, http: httpx.AsyncClient, redis: Redis, pool: asyncpg.Pool
) -> Gateway:
    providers: dict[str, Provider] = {
        "openai": OpenAIProvider(
            http, base_url=settings.llm_base_url, api_key=settings.llm_api_key.get_secret_value()
        )
    }
    if settings.anthropic_base_url:
        providers["anthropic"] = AnthropicProvider(
            http,
            base_url=settings.anthropic_base_url,
            api_key=settings.anthropic_api_key.get_secret_value(),
        )
    routes = json.loads(settings.llm_routes)
    missing = {m.split(":")[0] for chain in routes.values() for m in chain} - providers.keys()
    if missing:
        raise RuntimeError(f"routes use unconfigured providers: {sorted(missing)}")
    config = GatewayConfig(
        routes=routes,
        tenant_tokens_per_min=settings.tenant_tokens_per_min,
        provider_requests_per_min=settings.provider_requests_per_min,
        total_timeout_s=settings.llm_timeout_s,
    )
    return Gateway(providers, redis, pool, config)


async def queue_gauges(queues: list[StreamQueue], stop: asyncio.Event, every_s: float = 15) -> None:
    """Publish queue depth and oldest-message age for Prometheus."""
    while not stop.is_set():
        for q in queues:
            try:
                for state, n in (await q.depth()).items():
                    QUEUE_DEPTH.labels(q.stream, state).set(n)
                QUEUE_OLDEST_AGE.labels(q.stream).set(await q.oldest_age_s())
            except Exception:
                log.debug("queue gauge update failed", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    worker_id = settings.worker_id or default_worker_id()
    pool = await create_pool(settings.database_url, max_size=max(5, settings.worker_concurrency))
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=5.0)
    egress = EgressGuard(settings.egress_hosts())
    log.info("egress allowlist", extra={"hosts": sorted(egress.allowed)})
    transport = TracingTransport(egress)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
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
            llm=build_gateway(settings, http, redis, pool),
            redis=redis,
            worker_id=worker_id,
        )
        embedder = HttpEmbedder(
            http,
            base_url=settings.embed_base_url or settings.llm_base_url,
            api_key=(
                settings.embed_api_key.get_secret_value() or settings.llm_api_key.get_secret_value()
            ),
            model=settings.embed_model,
        )
        deps.embedder = embedder
        index_deps = IndexDeps(pool=pool, github=deps.github, embedder=embedder)
        qa_deps = QADeps(
            pool=pool,
            github=deps.github,
            llm=deps.llm,
            embedder=embedder,
            worker_id=worker_id,
            cache=QACache(pool, embedder, settings.qa_cache_threshold),
        )

        review_q = StreamQueue(redis, JOBS_REVIEW, REVIEW_GROUP, worker_id)
        index_q = StreamQueue(redis, JOBS_INDEX, INDEX_GROUP, worker_id)
        qa_q = StreamQueue(redis, JOBS_QA, QA_GROUP, worker_id)
        log.info(
            "worker started",
            extra={"worker_id": worker_id, "streams": [JOBS_REVIEW, JOBS_INDEX, JOBS_QA]},
        )
        reaper = asyncio.create_task(reaper_loop(pool, stop, REAP_INTERVAL_S))
        gauges = asyncio.create_task(queue_gauges([review_q, index_q, qa_q], stop))
        try:
            await asyncio.gather(
                run_consumer(
                    review_q,
                    functools.partial(handle_review_message, deps),
                    stop,
                    concurrency=settings.worker_concurrency,
                ),
                run_consumer(
                    index_q,
                    functools.partial(handle_index_message, index_deps),
                    stop,
                    concurrency=settings.index_concurrency,
                ),
                run_consumer(
                    qa_q,
                    functools.partial(handle_qa_message, qa_deps),
                    stop,
                    concurrency=settings.worker_concurrency,
                ),
            )
        finally:
            log.info("worker stopping")
            stop.set()
            await reaper
            await gauges
            await redis.aclose()
            await pool.close()


def main() -> None:
    settings = get_settings().model_copy(update={"service_name": "argus-worker"})
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(
        settings.service_name,
        endpoint=settings.otel_endpoint,
        headers=settings.otel_header_map(),
    )
    start_http_server(settings.metrics_port)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
