import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from argus.db.pool import create_pool
from argus.runs import (
    LeaseLostError,
    add_usage,
    claim_run,
    finish_run,
    get_or_create_run,
    reap_runs,
    release_run,
    upsert_tenant,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(db: asyncpg.Connection, migrated_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_dsn, max_size=20)
    yield p
    await p.close()


async def _run(pool: asyncpg.Pool, key: str = "k") -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await upsert_tenant(pool, 1, "acme")
    run = await get_or_create_run(
        pool,
        tenant_id=tenant,
        repo_id=None,
        agent_type="pr_review",
        idempotency_key=key,
        trigger_event_id=None,
    )
    return tenant, run.id


async def test_get_or_create_is_idempotent_under_concurrency(pool: asyncpg.Pool) -> None:
    tenant = await upsert_tenant(pool, 1, "acme")
    runs = await asyncio.gather(
        *(
            get_or_create_run(
                pool,
                tenant_id=tenant,
                repo_id=None,
                agent_type="pr_review",
                idempotency_key="same",
                trigger_event_id=None,
            )
            for _ in range(20)
        )
    )
    assert len({r.id for r in runs}) == 1
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1


async def test_only_one_concurrent_claim_wins(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    leases = await asyncio.gather(*(claim_run(pool, run_id, f"w{i}", 60) for i in range(10)))
    assert sum(lease is not None for lease in leases) == 1


async def test_expired_lease_can_be_reclaimed_and_zombie_is_fenced(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    zombie = await claim_run(pool, run_id, "zombie", 0.05)
    assert zombie is not None
    assert await claim_run(pool, run_id, "live", 60) is None  # still leased
    await asyncio.sleep(0.1)
    live = await claim_run(pool, run_id, "live", 60)
    assert live is not None and live.version == zombie.version + 1
    with pytest.raises(LeaseLostError):
        await add_usage(pool, zombie, 10, 10)
    with pytest.raises(LeaseLostError):
        await finish_run(pool, zombie, "succeeded")
    await add_usage(pool, live, 5, 6)
    await finish_run(pool, live, "succeeded", result={"ok": True})
    row = await pool.fetchrow("SELECT * FROM runs WHERE id=$1", run_id)
    assert (row["status"], row["input_tokens"], row["lease_owner"]) == ("succeeded", 5, None)


async def test_terminal_run_cannot_be_claimed(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    lease = await claim_run(pool, run_id, "w", 60)
    assert lease is not None
    await finish_run(pool, lease, "failed", error="x")
    assert await claim_run(pool, run_id, "w2", 60) is None


async def test_release_allows_immediate_reclaim(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    lease = await claim_run(pool, run_id, "w", 600)
    assert lease is not None
    assert await release_run(pool, lease, "boom")
    again = await claim_run(pool, run_id, "w", 600)
    assert again is not None and again.version == lease.version + 1
    assert not await release_run(pool, lease, "stale release is fenced")


async def test_reaper_requeues_expired_and_fences_old_owner(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    lease = await claim_run(pool, run_id, "dead-worker", 0.01)
    assert lease is not None
    await asyncio.sleep(0.05)
    assert await reap_runs(pool, {"pr_review": "jobs:review"}) == 1
    row = await pool.fetchrow("SELECT status, lease_version FROM runs WHERE id=$1", run_id)
    assert row["status"] == "queued"
    assert row["lease_version"] == lease.version + 1
    out = await pool.fetchrow("SELECT topic, payload::text FROM outbox")
    assert out["topic"] == "jobs:review"
    assert json.loads(out["payload"])["run_id"] == str(run_id)
    with pytest.raises(LeaseLostError):
        await finish_run(pool, lease, "succeeded")
    assert await reap_runs(pool, {"pr_review": "jobs:review"}) == 0  # not repeated


async def test_reaper_requeues_stuck_queued_runs(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    await pool.execute("UPDATE runs SET updated_at = now() - interval '1 hour' WHERE id=$1", run_id)
    assert await reap_runs(pool, {"pr_review": "jobs:review"}) == 1
    assert await pool.fetchval("SELECT lease_version FROM runs WHERE id=$1", run_id) == 0


async def test_live_leases_are_not_reaped(pool: asyncpg.Pool) -> None:
    _, run_id = await _run(pool)
    assert await claim_run(pool, run_id, "w", 600) is not None
    assert await reap_runs(pool, {"pr_review": "jobs:review"}) == 0
