import asyncio
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from argus.db.pool import create_pool
from argus.runs import upsert_tenant
from argus.tools.idempotency import SideEffectInProgressError, perform_once

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(db: asyncpg.Connection, migrated_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_dsn, max_size=20)
    yield p
    await p.close()


@pytest.fixture
async def tenant(pool: asyncpg.Pool) -> uuid.UUID:
    return await upsert_tenant(pool, 1, "acme")


class External:
    """Stands in for GitHub: records calls, can 'lose' the response after acting."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.lose_response = False

    async def perform(self) -> str:
        await asyncio.sleep(0.01)
        self.posted.append("x")
        ext_id = f"c{len(self.posted)}"
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost")
        return ext_id

    async def find(self) -> str | None:
        return f"c{len(self.posted)}" if self.posted else None


async def _call(
    pool: asyncpg.Pool, tenant: uuid.UUID, ext: External, **kw: object
) -> tuple[str, bool]:
    r = await perform_once(
        pool,
        tenant_id=tenant,
        run_id=None,
        key="k1",
        kind="test",
        perform=ext.perform,
        find_existing=ext.find,
        **kw,  # type: ignore[arg-type]
    )
    return r.external_id, r.performed


async def test_second_call_returns_recorded_result(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    ext = External()
    assert await _call(pool, tenant, ext) == ("c1", True)
    assert await _call(pool, tenant, ext) == ("c1", False)
    assert len(ext.posted) == 1


async def test_concurrent_callers_perform_once(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    ext = External()
    results = await asyncio.gather(
        *(_call(pool, tenant, ext) for _ in range(10)), return_exceptions=True
    )
    assert len(ext.posted) == 1
    ok = [r for r in results if isinstance(r, tuple)]
    busy = [r for r in results if isinstance(r, SideEffectInProgressError)]
    assert len(ok) + len(busy) == 10
    assert sum(1 for r in ok if r[1]) == 1


async def test_fresh_pending_claim_is_in_progress(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    ext = External()
    ext.lose_response = True
    with pytest.raises(TimeoutError):
        await _call(pool, tenant, ext)
    with pytest.raises(SideEffectInProgressError):
        await _call(pool, tenant, ext)
    assert len(ext.posted) == 1


async def test_stale_claim_found_externally_is_not_repeated(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    ext = External()
    ext.lose_response = True
    with pytest.raises(TimeoutError):
        await _call(pool, tenant, ext)
    assert await _call(pool, tenant, ext, stale_after_s=0) == ("c1", False)
    assert len(ext.posted) == 1
    assert await _call(pool, tenant, ext) == ("c1", False)


async def test_stale_claim_not_found_is_taken_over(pool: asyncpg.Pool, tenant: uuid.UUID) -> None:
    ext = External()
    # Simulate a claimer that crashed *before* calling out.
    await pool.execute(
        "INSERT INTO side_effects (tenant_id, idempotency_key, kind, claimed_at) "
        "VALUES ($1, 'k1', 'test', now() - interval '1 hour')",
        tenant,
    )
    assert await _call(pool, tenant, ext) == ("c1", True)
    assert len(ext.posted) == 1


async def test_claim_from_older_lease_of_same_run_is_stale_immediately(
    pool: asyncpg.Pool, tenant: uuid.UUID
) -> None:
    run_id = await pool.fetchval(
        "INSERT INTO runs (tenant_id, agent_type, idempotency_key) VALUES ($1,'t','r') "
        "RETURNING id",
        tenant,
    )
    ext = External()
    ext.lose_response = True

    async def call(version: int) -> tuple[str, bool]:
        r = await perform_once(
            pool,
            tenant_id=tenant,
            run_id=run_id,
            key="k1",
            kind="test",
            perform=ext.perform,
            find_existing=ext.find,
            lease_version=version,
        )
        return r.external_id, r.performed

    with pytest.raises(TimeoutError):
        await call(1)
    with pytest.raises(SideEffectInProgressError):
        await call(1)  # same lease, fresh claim: genuinely ambiguous
    assert await call(2) == ("c1", False)  # newer lease proves the claimer is gone
    assert len(ext.posted) == 1
