"""Common FastAPI app factory: lifespan-managed Postgres pool and Redis client."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

import argus
from argus.config import Settings
from argus.db.pool import create_pool
from argus.health import Resources, health_router
from argus.logging import configure_logging
from argus.metrics import HTTP_LATENCY, HTTP_REQUESTS
from argus.telemetry import configure_tracing


def build_app(
    settings: Settings,
    *,
    title: str,
    configure: Callable[[FastAPI], None] | None = None,
    extra_lifespan: Callable[[Resources], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.service_name, settings.log_level)
        if settings.otel_endpoint:
            configure_tracing(
                settings.service_name,
                endpoint=settings.otel_endpoint,
                headers=settings.otel_header_map(),
            )
        pool = await create_pool(settings.database_url)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        resources = Resources(settings=settings, db=pool, redis=redis)
        app.state.resources = resources
        try:
            if extra_lifespan is None:
                yield
            else:
                async with extra_lifespan(resources):
                    yield
        finally:
            await redis.aclose()
            await pool.close()

    app = FastAPI(title=title, version=argus.__version__, lifespan=lifespan)
    app.include_router(health_router())

    @app.middleware("http")
    async def http_metrics(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        response: Response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        HTTP_REQUESTS.labels(
            settings.service_name, route, request.method, str(response.status_code)
        ).inc()
        HTTP_LATENCY.labels(settings.service_name, route).observe(time.perf_counter() - started)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if configure is not None:
        configure(app)
    return app
