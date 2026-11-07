"""DLQ listing and replay: atomic under concurrency, admin-only over the API."""

import asyncio
import json

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from argus.api.app import create_app as create_api
from argus.config import Settings
from argus.queue.dlq import list_dead, replay, replay_all
from argus.queue.streams import StreamQueue, publish
from argus.queue.topics import JOBS_REVIEW
from tests.conftest import app_client

pytestmark = pytest.mark.integration

ADMIN = "test-admin-token"


async def _dead_letter(redis: Redis, n: int) -> StreamQueue:
    q = StreamQueue(redis, JOBS_REVIEW, "g", "c1")
    await q.ensure_group()
    for i in range(n):
        await publish(redis, JOBS_REVIEW, {"i": i})
    for m in await q.read(count=n, block_ms=10):
        assert await q.dead_letter(m, "boom") == "dead"
    return q


async def test_replay_moves_entries_back_with_attempt_reset(redis: Redis) -> None:
    q = await _dead_letter(redis, 3)
    dead = await list_dead(redis, JOBS_REVIEW)
    assert [json.loads(d.data)["i"] for d in dead] == [0, 1, 2]
    assert all(d.error == "boom" for d in dead)

    result = await replay(redis, JOBS_REVIEW, [dead[1].id, "0-1"])
    assert result[dead[1].id] is not None and result["0-1"] is None
    [m] = await q.read(count=10, block_ms=10)
    assert m.json() == {"i": 1} and m.attempt == 0
    assert m.fields["replayed_from"] == dead[1].id
    assert len(await list_dead(redis, JOBS_REVIEW)) == 2


async def test_concurrent_replays_enqueue_each_entry_once(redis: Redis) -> None:
    q = await _dead_letter(redis, 20)
    ids = [d.id for d in await list_dead(redis, JOBS_REVIEW, count=100)]
    results = await asyncio.gather(*(replay(redis, JOBS_REVIEW, ids) for _ in range(8)))
    assert sum(1 for r in results for v in r.values() if v is not None) == 20
    msgs = await q.read(count=100, block_ms=10)
    assert sorted(m.json()["i"] for m in msgs) == list(range(20))
    assert await redis.xlen(f"{JOBS_REVIEW}:dlq") == 0


async def test_replay_all_pages_and_respects_limit(redis: Redis) -> None:
    await _dead_letter(redis, 250)
    assert await replay_all(redis, JOBS_REVIEW, limit=120) == 120
    assert await replay_all(redis, JOBS_REVIEW) == 130
    assert await replay_all(redis, JOBS_REVIEW) == 0


async def test_admin_api_requires_token(settings: Settings, redis: Redis) -> None:
    await _dead_letter(redis, 1)
    off = create_api(settings)
    async with app_client(off) as c:
        assert (await c.get(f"/admin/dlq/{JOBS_REVIEW}")).status_code == 404

    on = create_api(settings.model_copy(update={"admin_token": SecretStr(ADMIN)}))
    auth = {"authorization": f"Bearer {ADMIN}"}
    async with app_client(on) as c:
        assert (await c.get(f"/admin/dlq/{JOBS_REVIEW}")).status_code == 401
        bad = {"authorization": "Bearer nope"}
        assert (await c.get(f"/admin/dlq/{JOBS_REVIEW}", headers=bad)).status_code == 401
        assert (await c.get("/admin/dlq/jobs:nope", headers=auth)).status_code == 404

        r = await c.get(f"/admin/dlq/{JOBS_REVIEW}", headers=auth)
        assert r.status_code == 200
        [item] = r.json()["items"]
        r = await c.post(
            f"/admin/dlq/{JOBS_REVIEW}/replay", json={"ids": [item["id"]]}, headers=auth
        )
        assert r.json()["replayed"] == 1
        r = await c.post(f"/admin/dlq/{JOBS_REVIEW}/replay", json={}, headers=auth)
        assert r.status_code == 422
        r = await c.post(f"/admin/dlq/{JOBS_REVIEW}/replay", json={"all": True}, headers=auth)
        assert r.json()["replayed"] == 0
