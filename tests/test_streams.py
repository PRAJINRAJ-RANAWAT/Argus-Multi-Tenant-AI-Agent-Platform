import asyncio
import random
import time

import pytest
from redis.asyncio import Redis

from argus.queue.consumer import PermanentError, handle_one, run_consumer
from argus.queue.streams import Message, StreamQueue, backoff_s, publish

pytestmark = pytest.mark.integration

S = "jobs:test"


def q(redis: Redis, consumer: str = "c1", **kw: object) -> StreamQueue:
    opts: dict[str, object] = {"backoff_base_s": 0.01, "backoff_cap_s": 0.05}
    opts.update(kw)
    return StreamQueue(redis, S, "g", consumer, **opts)  # type: ignore[arg-type]


def test_backoff_is_bounded_full_jitter() -> None:
    rng = random.Random(1)
    for attempt in range(12):
        d = backoff_s(attempt, base=1.0, cap=30.0, rng=rng)
        assert 0 <= d <= min(30.0, 2**attempt)


async def test_publish_read_ack(redis: Redis) -> None:
    queue = q(redis)
    await queue.ensure_group()
    await queue.ensure_group()  # idempotent
    await publish(redis, S, {"k": 1})
    [msg] = await queue.read(block_ms=10)
    assert msg.json() == {"k": 1}
    assert msg.attempt == 0
    assert await queue.ack(msg)
    assert await queue.read(block_ms=10) == []
    assert (await queue.depth())["pending"] == 0


async def test_retry_goes_through_delayed_set_with_incremented_attempt(redis: Redis) -> None:
    queue = q(redis)
    await queue.ensure_group()
    await publish(redis, S, {"k": 1})
    [msg] = await queue.read(block_ms=10)
    assert await queue.retry(msg, "boom") == "retry"
    assert (await queue.depth())["pending"] == 0
    assert (await queue.depth())["delayed"] == 1
    # Not due yet when "now" is in the past.
    assert await queue.promote_due(now_ms=0) == 0
    assert await queue.promote_due(now_ms=int(time.time() * 1000) + 60_000) == 1
    [again] = await queue.read(block_ms=10)
    assert again.attempt == 1
    assert again.json() == {"k": 1}


async def test_exhausted_attempts_dead_letter_with_error(redis: Redis) -> None:
    queue = q(redis, max_attempts=2)
    await queue.ensure_group()
    await publish(redis, S, {"k": 1})
    [m] = await queue.read(block_ms=10)
    assert await queue.retry(m, "first") == "retry"
    await queue.promote_due(now_ms=int(time.time() * 1000) + 60_000)
    [m2] = await queue.read(block_ms=10)
    assert await queue.retry(m2, "second") == "dead"
    entries = await redis.xrange(queue.dlq_stream)
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["error"] == "second"
    assert fields["origin_stream"] == S
    assert fields["data"] == '{"k": 1}'


async def test_crashed_consumer_message_is_reclaimed(redis: Redis) -> None:
    dead, live = q(redis, "dead"), q(redis, "live")
    await dead.ensure_group()
    await publish(redis, S, {"k": 1})
    [m] = await dead.read(block_ms=10)  # "dead" never acks
    assert await live.reclaim(min_idle_ms=60_000) == []  # not idle long enough
    await asyncio.sleep(0.05)
    [claimed] = await live.reclaim(min_idle_ms=10)
    assert claimed.id == m.id
    assert claimed.deliveries == 2
    assert await live.ack(claimed)


async def test_zombie_cannot_ack_after_reclaim(redis: Redis) -> None:
    zombie, live = q(redis, "zombie"), q(redis, "live")
    await zombie.ensure_group()
    await publish(redis, S, {"k": 1})
    [m] = await zombie.read(block_ms=10)
    await asyncio.sleep(0.02)
    [claimed] = await live.reclaim(min_idle_ms=10)
    assert await zombie.ack(m) is False
    assert await zombie.retry(m, "late") == "lost_ownership"
    assert await zombie.dead_letter(m, "late") == "lost_ownership"
    assert (await live.depth())["pending"] == 1
    assert await live.ack(claimed)


async def test_poison_message_that_crashes_consumers_is_dead_lettered(redis: Redis) -> None:
    queue = q(redis, max_deliveries=3)
    await queue.ensure_group()
    await publish(redis, S, {"k": "poison"})
    await queue.read(block_ms=10)  # delivery 1, then "crash"
    for _ in range(2):  # deliveries 2 and 3 via reclaim, each "crashing"
        await asyncio.sleep(0.02)
        assert len(await queue.reclaim(min_idle_ms=10)) == 1
    await asyncio.sleep(0.02)
    assert await queue.reclaim(min_idle_ms=10) == []  # 4th delivery → DLQ
    assert await redis.xlen(queue.dlq_stream) == 1
    assert (await queue.depth())["pending"] == 0


async def test_permanent_error_skips_retries(redis: Redis) -> None:
    queue = q(redis)
    await queue.ensure_group()
    await publish(redis, S, {"k": 1})
    [m] = await queue.read(block_ms=10)

    async def handler(_: Message) -> None:
        raise PermanentError("bad payload")

    assert await handle_one(queue, handler, m) == "dead"
    assert (await queue.depth())["delayed"] == 0


async def test_consumer_retries_until_success(redis: Redis) -> None:
    queue = q(redis)
    await publish(redis, S, {"k": 1})
    seen: list[int] = []
    done = asyncio.Event()

    async def handler(m: Message) -> None:
        seen.append(m.attempt)
        if len(seen) < 3:
            raise RuntimeError("transient")
        done.set()

    stop = asyncio.Event()
    task = asyncio.create_task(run_consumer(queue, handler, stop, block_ms=20, housekeeping_s=0.02))
    await asyncio.wait_for(done.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert seen == [0, 1, 2]
    depth = await queue.depth()
    assert (depth["pending"], depth["delayed"], depth["dlq"]) == (0, 0, 0)


async def test_competing_consumers_process_each_message_once(redis: Redis) -> None:
    n = 200
    for i in range(n):
        await publish(redis, S, {"i": i})
    processed: list[int] = []
    stop = asyncio.Event()

    async def handler(m: Message) -> None:
        await asyncio.sleep(random.uniform(0, 0.005))
        processed.append(m.json()["i"])

    consumers = [
        asyncio.create_task(
            run_consumer(q(redis, f"c{i}"), handler, stop, concurrency=8, block_ms=20)
        )
        for i in range(3)
    ]
    for _ in range(200):
        if len(processed) >= n:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.gather(*consumers)
    assert sorted(processed) == list(range(n))


async def test_concurrency_limit_respected(redis: Redis) -> None:
    for i in range(30):
        await publish(redis, S, {"i": i})
    active = 0
    peak = 0
    count = 0
    stop = asyncio.Event()

    async def handler(_: Message) -> None:
        nonlocal active, peak, count
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        count += 1
        if count == 30:
            stop.set()

    await asyncio.wait_for(
        run_consumer(q(redis), handler, stop, concurrency=4, block_ms=20), timeout=10
    )
    assert peak <= 4
