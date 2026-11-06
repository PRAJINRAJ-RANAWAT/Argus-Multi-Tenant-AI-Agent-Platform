import asyncio
import json
import uuid

import asyncpg
import pytest
from redis.asyncio import Redis

from argus.config import Settings
from argus.ingest import app as ingest_app
from argus.ingest.app import create_app
from tests.conftest import WEBHOOK_SECRET, app_client
from tests.factories import pr_payload, signed_request

pytestmark = pytest.mark.integration


async def _counts(db: asyncpg.Connection) -> tuple[int, int]:
    events = await db.fetchval("SELECT count(*) FROM events")
    outbox = await db.fetchval("SELECT count(*) FROM outbox")
    return events, outbox


async def test_pr_opened_persists_event_and_outbox(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(settings)) as client:
        resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["enqueued"] is True
    event = await db.fetchrow("SELECT * FROM events")
    assert event["delivery_id"] == headers["x-github-delivery"]
    assert event["installation_id"] == 1001
    assert event["status"] == "received"
    outbox = await db.fetchrow("SELECT * FROM outbox")
    assert outbox["topic"] == "jobs:review"
    assert json.loads(outbox["payload"])["event_id"] == str(event["id"])
    assert outbox["published_at"] is None


async def test_bad_signature_rejected_and_nothing_stored(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request(pr_payload(), "wrong-secret")
    async with app_client(create_app(settings)) as client:
        resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 401
    assert await _counts(db) == (0, 0)


async def test_missing_headers_400(settings: Settings, db: asyncpg.Connection) -> None:
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    del headers["x-github-delivery"]
    async with app_client(create_app(settings)) as client:
        resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 400


async def test_ignored_event_stored_without_outbox(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request({"action": "opened"}, WEBHOOK_SECRET, event="issues")
    async with app_client(create_app(settings)) as client:
        resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["enqueued"] is False
    assert await _counts(db) == (1, 0)
    assert await db.fetchval("SELECT status FROM events") == "ignored"


async def test_redelivery_is_deduplicated(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(settings)) as client:
        first = await client.post("/webhooks/github", content=body, headers=headers)
        second = await client.post("/webhooks/github", content=body, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["status"] == "duplicate"
    assert await _counts(db) == (1, 1)


async def test_concurrent_duplicates_store_exactly_one(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(settings)) as client:
        responses = await asyncio.gather(
            *(client.post("/webhooks/github", content=body, headers=headers) for _ in range(20))
        )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(202) == 1
    assert codes.count(200) == 19
    assert await _counts(db) == (1, 1)


async def test_db_dedupe_holds_when_redis_was_flushed(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(settings)) as client:
        await client.post("/webhooks/github", content=body, headers=headers)
        await redis.flushdb()
        resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 200
    assert await _counts(db) == (1, 1)


async def test_accepts_and_dedupes_when_redis_is_down(
    settings: Settings, db: asyncpg.Connection
) -> None:
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(broken)) as client:
        first = await client.post("/webhooks/github", content=body, headers=headers)
        second = await client.post("/webhooks/github", content=body, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert await _counts(db) == (1, 1)


async def test_failed_persist_does_not_poison_dedupe(
    settings: Settings, db: asyncpg.Connection, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before commit must leave the delivery retryable (no lost event)."""
    real = ingest_app.persist_event
    calls = {"n": 0}

    async def flaky(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db went away")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ingest_app, "persist_event", flaky)
    body, headers = signed_request(pr_payload(), WEBHOOK_SECRET)
    async with app_client(create_app(settings)) as client:
        failed = await client.post("/webhooks/github", content=body, headers=headers)
        retried = await client.post("/webhooks/github", content=body, headers=headers)
    assert failed.status_code == 500
    assert retried.status_code == 202
    assert await _counts(db) == (1, 1)


async def test_distinct_deliveries_for_same_pr_both_stored(
    settings: Settings, db: asyncpg.Connection, redis: Redis
) -> None:
    """Dedupe is per delivery; run-level idempotency (same head SHA) is the worker's job."""
    async with app_client(create_app(settings)) as client:
        for _ in range(2):
            body, headers = signed_request(
                pr_payload(), WEBHOOK_SECRET, delivery_id=str(uuid.uuid4())
            )
            assert (
                await client.post("/webhooks/github", content=body, headers=headers)
            ).status_code == 202
    assert await _counts(db) == (2, 2)
