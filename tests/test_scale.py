"""M9: fair scheduling, noisy neighbour, chaos scenarios, load shedding, autoscaling.

Done when: the noisy-neighbour test passes (and the report is public).
"""

import asyncio
import json
import random
import time
from collections import defaultdict
from typing import Any

import pytest
from redis.asyncio import Redis

from argus.autoscale import Policy, desired_workers
from argus.queue.consumer import run_consumer
from argus.queue.fair import FairQueue, publish_fair
from argus.queue.relay import relay_once
from argus.queue.streams import Message, StreamQueue, publish
from argus.runs import reap_events
from tests.factories import pr_payload
from tests.test_pr_review import Env, env  # noqa: F401 - fixture reuse

pytestmark = pytest.mark.integration


# --- fair queue mechanics -----------------------------------------------------------


async def test_round_robin_across_tenants(redis: Redis) -> None:
    for i in range(20):
        await publish_fair(redis, "jobs:t", "a", {"data": json.dumps({"i": i}), "attempt": "0"})
    for t in ("b", "c"):
        for i in range(2):
            await publish_fair(redis, "jobs:t", t, {"data": json.dumps({"i": i}), "attempt": "0"})
    q = FairQueue(redis, "jobs:t", "g", "c1", tenant_cap=10)
    await q.ensure_group()
    first = await q.read(count=10, block_ms=10)
    assert sorted(m.stream.rsplit(":", 1)[1] for m in first) == ["a", "b", "c"]  # one each
    for m in first:
        await q.ack(m)


async def test_tenant_cap_limits_in_flight_and_releases_on_ack(redis: Redis) -> None:
    for i in range(10):
        await publish_fair(redis, "jobs:t", "a", {"data": json.dumps({"i": i}), "attempt": "0"})
    q = FairQueue(redis, "jobs:t", "g", "c1", tenant_cap=2, burst_cap=2)
    other = FairQueue(redis, "jobs:t", "g", "c2", tenant_cap=2, burst_cap=2)  # another worker
    await q.ensure_group()
    held = [m for _ in range(5) for m in await q.read(count=10, block_ms=10)]
    held += [m for _ in range(5) for m in await other.read(count=10, block_ms=10)]
    assert len(held) == 2 and await q.inflight("a") == 2  # cap holds across workers
    await q.ack(held[0])
    assert await q.inflight("a") == 1
    assert len(await other.read(count=10, block_ms=10)) == 1


async def test_dead_worker_cannot_leak_a_tenants_capacity(redis: Redis) -> None:
    await publish_fair(redis, "jobs:t", "a", {"data": "{}", "attempt": "0"})
    await publish_fair(redis, "jobs:t", "a", {"data": "{}", "attempt": "0"})
    dead = FairQueue(redis, "jobs:t", "g", "dead", tenant_cap=1, burst_cap=1, slot_lease_ms=200)
    await dead.ensure_group()
    assert len(await dead.read(count=5, block_ms=10)) == 1  # never acked: worker died
    live = FairQueue(redis, "jobs:t", "g", "live", tenant_cap=1, burst_cap=1, slot_lease_ms=200)
    assert await live.read(count=5, block_ms=10) == []
    await asyncio.sleep(0.25)  # the slot lease expires
    assert len(await live.read(count=5, block_ms=10)) == 1


# --- noisy neighbour (done when) ---------------------------------------------------


async def _simulate(
    redis: Redis,
    *,
    fair: bool,
    noisy: int = 200,
    quiet: int = 5,
    work_s: float = 0.05,
    workers: int = 4,
    concurrency: int = 4,
    cap: int = 4,
) -> dict[str, Any]:
    topic = f"jobs:nn:{'fair' if fair else 'fifo'}:{random.randrange(10**9)}"
    jobs = [("noisy", i) for i in range(noisy)] + [
        (t, i) for t in ("quiet1", "quiet2") for i in range(quiet)
    ]
    for tenant, i in jobs:  # worst case for FIFO: the flood arrives first
        data = json.dumps({"tenant": tenant, "i": i})
        if fair:
            await publish_fair(redis, topic, tenant, {"data": data, "attempt": "0"})
        else:
            await publish(redis, topic, data)
    latency: dict[str, list[float]] = defaultdict(list)
    running: dict[str, int] = defaultdict(int)
    peak: dict[str, int] = defaultdict(int)
    stop = asyncio.Event()
    start = time.monotonic()

    async def handler(msg: Message) -> None:
        tenant = msg.json()["tenant"]
        running[tenant] += 1
        peak[tenant] = max(peak[tenant], running[tenant])
        await asyncio.sleep(work_s)  # an I/O-bound agent run
        running[tenant] -= 1
        latency[tenant].append(time.monotonic() - start)
        if sum(len(v) for v in latency.values()) == len(jobs):
            stop.set()

    def make(i: int) -> StreamQueue | FairQueue:
        if fair:
            return FairQueue(redis, topic, "g", f"w{i}", tenant_cap=cap)
        return StreamQueue(redis, topic, "g", f"w{i}")

    consumers = [
        asyncio.create_task(
            run_consumer(make(i), handler, stop, concurrency=concurrency, block_ms=20)
        )
        for i in range(workers)
    ]
    await asyncio.wait_for(stop.wait(), timeout=60)
    await asyncio.gather(*consumers)

    def p95(values: list[float]) -> float:
        s = sorted(values)
        return s[max(0, round(0.95 * len(s)) - 1)]

    quiet_lat = latency["quiet1"] + latency["quiet2"]
    return {
        "quiet_p95_s": round(p95(quiet_lat), 3),
        "noisy_p95_s": round(p95(latency["noisy"]), 3),
        "noisy_peak_concurrency": peak["noisy"],
        "makespan_s": round(time.monotonic() - start, 3),
    }


async def test_noisy_neighbour_does_not_starve_other_tenants(redis: Redis) -> None:
    fifo = await _simulate(redis, fair=False)
    fair = await _simulate(redis, fair=True)
    print(json.dumps({"fifo": fifo, "fair": fair}))
    # With FIFO, quiet tenants wait behind the whole flood.
    assert fifo["quiet_p95_s"] > 0.5
    # With fair scheduling they finish within a few rounds, while the flood continues.
    assert fair["quiet_p95_s"] < 0.3
    assert fair["quiet_p95_s"] * 3 < fifo["quiet_p95_s"]
    # Work-conserving: once the quiet tenants are done, the flood borrows idle slots
    # (up to its burst cap) instead of crawling at its fair share.
    assert fair["noisy_peak_concurrency"] <= 16
    assert fair["makespan_s"] < 2 * fifo["makespan_s"]


async def test_capped_tenant_cannot_borrow_while_others_wait(redis: Redis) -> None:
    for i in range(10):
        await publish_fair(redis, "jobs:b", "flood", {"data": json.dumps({"i": i}), "attempt": "0"})
    q = FairQueue(redis, "jobs:b", "g", "c1", tenant_cap=2, burst_cap=8)
    await q.ensure_group()
    held = [m for _ in range(3) for m in await q.read(count=10, block_ms=10)]
    assert 2 < len(held) <= 8  # alone: borrows idle capacity
    await publish_fair(redis, "jobs:b", "quiet", {"data": "{}", "attempt": "0"})
    nxt = await q.read(count=10, block_ms=10)
    assert [m.stream.rsplit(":", 1)[1] for m in nxt] == ["quiet"]  # the waiting tenant first


# --- chaos scenarios on the real pipeline -------------------------------------------


async def test_duplicate_storm_one_review_per_head_sha(env: Env) -> None:  # noqa: F811
    deliveries = []
    for pr in range(1, 11):
        payload = pr_payload(number=pr, head_sha=f"{pr:040x}")
        deliveries += [(payload, f"d-{pr}")] * 3  # every delivery replayed 3 times
    random.Random(7).shuffle(deliveries)  # out of order
    for payload, delivery in deliveries:
        assert await env.deliver(payload, delivery_id=delivery) in (200, 202)
    outcomes = await asyncio.gather(*(env.drain(f"w{i}") for i in range(3)))
    assert sum(o.count("acked") for o in outcomes) == 10
    assert [len(env.comments(pr)) for pr in range(1, 11)] == [1] * 10


async def test_force_push_churn_leaves_one_final_review(env: Env) -> None:  # noqa: F811
    shas = [f"{i:x}" * 40 for i in range(1, 6)]
    for sha in shas:  # five pushes to one PR in quick succession
        await env.deliver(pr_payload(action="synchronize", head_sha=sha[:40]))
    await asyncio.gather(*(env.drain(f"w{i}") for i in range(3)))
    statuses = dict(
        (r["head_sha"], r["status"])
        for r in await env.pool.fetch("SELECT head_sha, status FROM runs")
    )
    assert statuses[shas[-1][:40]] == "succeeded"
    assert all(statuses[s[:40]] == "superseded" for s in shas[:-1] if s[:40] in statuses)
    [comment] = env.comments()
    assert shas[-1][:12] in comment["body"]


async def test_redis_data_loss_is_recovered_by_the_event_reaper(env: Env) -> None:  # noqa: F811
    await env.deliver(pr_payload())  # accepted, in the outbox, published to the stream
    await env.redis.flushall()  # Redis loses everything before a worker reads it
    assert await env.drain() == []
    assert await reap_events(env.pool, older_than_s=0) == 1
    assert await reap_events(env.pool, older_than_s=0) == 0  # already re-enqueued
    while await relay_once(env.pool, env.redis):
        pass
    assert await env.drain() == ["acked"]
    assert len(env.comments()) == 1


async def test_low_priority_work_pauses_while_interactive_work_lags(redis: Redis) -> None:
    q = StreamQueue(redis, "jobs:low", "g", "c")
    await q.ensure_group()
    await publish(redis, "jobs:low", {"x": 1})
    handled: list[str] = []
    lagging = True

    async def pause() -> bool:
        return lagging

    async def handler(msg: Message) -> None:
        handled.append(msg.id)

    stop = asyncio.Event()
    task = asyncio.create_task(run_consumer(q, handler, stop, block_ms=20, pause_if=pause))
    await asyncio.sleep(0.3)
    assert handled == []  # shed while interactive queues lag
    lagging = False
    for _ in range(50):
        if handled:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await task
    assert len(handled) == 1


def test_autoscaling_policy() -> None:
    p = Policy(min_workers=1, max_workers=20, per_worker=20, avg_job_s=90, target_drain_s=120)
    assert desired_workers(0, 0, 1, p) == 1
    # 600 queued -> 5 starts/s * 90 s = 450 concurrent runs -> 23 workers, capped at 20.
    assert desired_workers(600, 0, 1, p) == 20
    assert desired_workers(80, 10, 2, p) == 4  # 60 + 10 concurrent → 4 workers
    assert desired_workers(80, 10, 5, p) == 5  # mild over-provisioning: no flapping
    assert desired_workers(0, 5, 10, p) == 1  # clearly idle: scale down


async def test_lanes_created_after_the_worker_started_are_served(redis: Redis) -> None:
    """Regression (found on the live stack): a tenant lane created by the relay after the
    worker started had no consumer group, so reclaim raised NOGROUP and stalled every
    tenant's reads."""
    q = FairQueue(redis, "jobs:late", "g", "c1")
    await q.ensure_group()  # no tenants yet
    await publish_fair(redis, "jobs:late", "newcomer", {"data": "{}", "attempt": "0"})
    assert await q.reclaim(count=10, min_idle_ms=0) == []
    assert await q.promote_due() == 0
    [msg] = await q.read(count=5, block_ms=10)
    assert await q.ack(msg)


async def test_idle_tenants_are_pruned_but_never_lost(redis: Redis) -> None:
    q = FairQueue(redis, "jobs:p", "g", "c1")
    await publish_fair(redis, "jobs:p", "a", {"data": "{}", "attempt": "0"})
    await publish_fair(redis, "jobs:p", "b", {"data": "{}", "attempt": "0"})
    await q.ensure_group()
    for m in await q.read(count=10, block_ms=10):
        if m.stream.endswith(":a"):
            await q.ack(m)  # a is done; b's message stays in flight
    await q.promote_due()
    assert await q.tenants() == ["b"]  # idle a pruned, in-flight b kept
    await publish_fair(redis, "jobs:p", "a", {"data": "{}", "attempt": "0"})
    assert "a" in await q.tenants()  # new work re-activates the tenant
    assert any(m.stream.endswith(":a") for m in await q.read(count=10, block_ms=10))


async def test_queue_outage_while_settling_leaves_the_message_for_reclaim(redis: Redis) -> None:
    from argus.queue.consumer import handle_one

    q = StreamQueue(redis, "jobs:o", "g", "c1")
    await q.ensure_group()
    await publish(redis, "jobs:o", {"x": 1})
    [msg] = await q.read(block_ms=10)

    async def ok(_: Message) -> None:
        return None

    async def broken_ack(_: Message) -> bool:
        raise ConnectionError("redis restarting")

    q.ack = broken_ack  # type: ignore[method-assign]
    assert await handle_one(q, ok, msg) == "unsettled"  # no crash, no lost message
    again = StreamQueue(redis, "jobs:o", "g", "c2")
    [reclaimed] = await again.reclaim(min_idle_ms=0)
    assert reclaimed.id == msg.id
