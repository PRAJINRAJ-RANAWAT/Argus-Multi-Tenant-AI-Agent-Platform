import asyncio
import json

import asyncpg
import pytest
from redis.asyncio import Redis

from argus.db.pool import create_pool
from argus.queue import relay as relay_mod
from argus.queue.relay import relay_once, run_relay

pytestmark = pytest.mark.integration


async def _insert(db: asyncpg.Connection, n: int, topic: str = "jobs:review") -> None:
    await db.executemany(
        "INSERT INTO outbox (topic, payload) VALUES ($1, $2::jsonb)",
        [(topic, json.dumps({"i": i})) for i in range(n)],
    )


async def test_publishes_and_marks_rows(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str
) -> None:
    await _insert(db, 3)
    pool = await create_pool(migrated_dsn)
    try:
        assert await relay_once(pool, redis) == 3
        assert await relay_once(pool, redis) == 0
    finally:
        await pool.close()
    entries = await redis.xrange("jobs:review")
    assert [json.loads(f["data"])["i"] for _, f in entries] == [0, 1, 2]
    assert await db.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 0


async def test_concurrent_relays_never_double_publish(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str
) -> None:
    await _insert(db, 500)
    pool = await create_pool(migrated_dsn, max_size=10)
    try:

        async def drain() -> None:
            while await relay_once(pool, redis, batch=25):
                pass

        await asyncio.gather(*(drain() for _ in range(6)))
    finally:
        await pool.close()
    entries = await redis.xrange("jobs:review")
    ids = [json.loads(f["data"])["i"] for _, f in entries]
    assert sorted(ids) == list(range(500))


async def test_redis_outage_leaves_rows_for_next_pass(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str
) -> None:
    await _insert(db, 2)
    pool = await create_pool(migrated_dsn)
    broken = Redis.from_url("redis://127.0.0.1:1/0")
    try:
        with pytest.raises(Exception):  # noqa: B017 - any connection error
            await relay_once(pool, broken)
        assert await db.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 2
        assert await relay_once(pool, redis) == 2
    finally:
        await broken.aclose()
        await pool.close()


async def test_crash_after_publish_before_commit_republishes(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented at-least-once window: consumers must dedupe."""
    await _insert(db, 1)
    pool = await create_pool(migrated_dsn)
    original_execute = asyncpg.connection.Connection.execute

    async def crash_on_mark(self: asyncpg.Connection, query: str, *args: object) -> str:
        if query.startswith("UPDATE outbox SET published_at"):
            raise ConnectionError("died before commit")
        return await original_execute(self, query, *args)

    try:
        monkeypatch.setattr(asyncpg.connection.Connection, "execute", crash_on_mark)
        with pytest.raises(ConnectionError):
            await relay_once(pool, redis)
        monkeypatch.setattr(asyncpg.connection.Connection, "execute", original_execute)
        assert await relay_once(pool, redis) == 1
    finally:
        await pool.close()
    assert await redis.xlen("jobs:review") == 2  # duplicate, by design


async def test_notify_wakes_relay_without_waiting_for_poll(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str
) -> None:
    pool = await create_pool(migrated_dsn)
    stop = asyncio.Event()
    task = asyncio.create_task(run_relay(pool, redis, migrated_dsn, stop, poll_s=30))
    try:
        await asyncio.sleep(0.2)
        await _insert(db, 1)
        for _ in range(40):
            if await redis.xlen("jobs:review"):
                break
            await asyncio.sleep(0.025)
        assert await redis.xlen("jobs:review") == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await pool.close()


async def test_relay_records_error_and_recovers(
    db: asyncpg.Connection, redis: Redis, migrated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert(db, 1)
    pool = await create_pool(migrated_dsn)
    real = relay_mod.relay_once
    calls = {"n": 0}

    async def flaky(*a: object, **k: object) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("redis blip")
        return await real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(relay_mod, "relay_once", flaky)
    stop = asyncio.Event()
    task = asyncio.create_task(run_relay(pool, redis, migrated_dsn, stop, poll_s=0.05))
    try:
        for _ in range(100):
            if await redis.xlen("jobs:review"):
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await pool.close()
    assert await redis.xlen("jobs:review") == 1
    row = await db.fetchrow("SELECT last_error, published_at FROM outbox")
    assert "redis blip" in row["last_error"]
    assert row["published_at"] is not None
