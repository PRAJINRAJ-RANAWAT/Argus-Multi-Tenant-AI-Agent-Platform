"""asyncpg connection pool helpers."""

import asyncpg


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    if pool is None:  # pragma: no cover - asyncpg only returns None when awaited lazily
        raise RuntimeError("failed to create database pool")
    return pool
