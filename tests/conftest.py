"""Shared fixtures: real Postgres (pgvector) and Redis via testcontainers."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from argus.config import Settings
from argus.db.migrate import default_migrations_dir, run

PG_IMAGE = "pgvector/pgvector:pg16"
REDIS_IMAGE = "redis:7-alpine"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(PG_IMAGE, username="argus", password="argus", dbname="argus") as pg:
        yield pg


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE) as r:
        yield r


@pytest.fixture(scope="session")
def pg_dsn(pg_container: PostgresContainer) -> str:
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql://argus:argus@{host}:{port}/argus"


@pytest.fixture(scope="session")
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
async def migrated_dsn(pg_dsn: str) -> str:
    await run(pg_dsn, default_migrations_dir())
    return pg_dsn


WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def settings(migrated_dsn: str, redis_url: str) -> Settings:
    return Settings(
        database_url=migrated_dsn,
        redis_url=redis_url,
        git_sha="test",
        github_webhook_secret=WEBHOOK_SECRET,
    )


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an in-process HTTP client."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def db(migrated_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection to a database with every application table emptied."""
    conn = await asyncpg.connect(migrated_dsn)
    tables = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename <> 'schema_migrations'"
    )
    if tables:
        names = ", ".join(f'"{t["tablename"]}"' for t in tables)
        await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
