"""Shared fixtures: real Postgres (pgvector) and Redis via testcontainers."""

from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
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


@pytest.fixture
def settings(migrated_dsn: str, redis_url: str) -> Settings:
    return Settings(database_url=migrated_dsn, redis_url=redis_url, git_sha="test")


@pytest.fixture
async def db(migrated_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_dsn)
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
