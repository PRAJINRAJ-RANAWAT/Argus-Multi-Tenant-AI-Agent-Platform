"""Common FastAPI app factory: lifespan-managed Postgres pool and Redis client."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

import argus
from argus.config import Settings
from argus.db.pool import create_pool
from argus.health import Resources, health_router
from argus.logging import configure_logging


def build_app(
    settings: Settings,
    *,
    title: str,
    configure: Callable[[FastAPI], None] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.service_name, settings.log_level)
        pool = await create_pool(settings.database_url)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.resources = Resources(settings=settings, db=pool, redis=redis)
        try:
            yield
        finally:
            await redis.aclose()
            await pool.close()

    app = FastAPI(title=title, version=argus.__version__, lifespan=lifespan)
    app.include_router(health_router())
    if configure is not None:
        configure(app)
    return app
