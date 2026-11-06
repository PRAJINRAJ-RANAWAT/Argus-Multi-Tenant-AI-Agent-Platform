"""Shared liveness/readiness endpoints for HTTP services."""

import asyncio
from dataclasses import dataclass, field

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

import argus
from argus.config import Settings

CHECK_TIMEOUT_S = 2.0


@dataclass
class Resources:
    """Long-lived clients shared by a service, attached to ``app.state.resources``."""

    settings: Settings
    db: asyncpg.Pool
    redis: Redis
    extras: dict[str, object] = field(default_factory=dict)


async def check_postgres(pool: asyncpg.Pool) -> bool:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            return bool(await pool.fetchval("SELECT 1") == 1)
    except Exception:
        return False


async def check_redis(redis: Redis) -> bool:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            return bool(await redis.ping())
    except Exception:
        return False


def health_router() -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        res: Resources = request.app.state.resources
        return {
            "status": "ok",
            "service": res.settings.service_name,
            "version": argus.__version__,
            "git_sha": res.settings.git_sha,
        }

    @router.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        res: Resources = request.app.state.resources
        pg_ok, redis_ok = await asyncio.gather(check_postgres(res.db), check_redis(res.redis))
        ok = pg_ok and redis_ok
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "postgres": pg_ok, "redis": redis_ok},
            status_code=200 if ok else 503,
        )

    return router
