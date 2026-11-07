"""Checkpointed steps and lease heartbeats against real Postgres."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import pytest

from argus.db.pool import create_pool
from argus.runs import (
    Lease,
    LeaseKeeper,
    LeaseLostError,
    claim_run,
    get_or_create_run,
    reap_runs,
    renew_lease,
    upsert_tenant,
)
from argus.steps import BudgetExceededError, NonDeterminismError, StepOutcome, StepRunner

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(db: asyncpg.Connection, migrated_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_dsn, max_size=20)
    yield p
    await p.close()


async def _claimed(pool: asyncpg.Pool, lease_s: float = 30.0) -> Lease:
    tenant = await upsert_tenant(pool, 1, "acme")
    run = await get_or_create_run(
        pool,
        tenant_id=tenant,
        repo_id=None,
        agent_type="pr_review",
        idempotency_key=f"k-{uuid.uuid4()}",
        trigger_event_id=None,
    )
    lease = await claim_run(pool, run.id, "w1", lease_s)
    assert lease is not None
    return lease


async def _expire_and_reclaim(pool: asyncpg.Pool, lease: Lease, owner: str = "w2") -> Lease:
    await pool.execute(
        "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
        lease.run_id,
    )
    new = await claim_run(pool, lease.run_id, owner, 30.0)
    assert new is not None and new.version > lease.version
    return new


class Counter:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def fn(
        self, name: str, output: object, tokens: int = 0
    ) -> Callable[[], Awaitable[StepOutcome]]:
        async def run() -> StepOutcome:
            self.calls[name] = self.calls.get(name, 0) + 1
            return StepOutcome(output=output, input_tokens=tokens, output_tokens=tokens)

        return run


async def test_completed_steps_replay_from_storage_after_crash(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool)
    c = Counter()
    first = StepRunner(pool, lease)
    assert await first.step("llm_call", "m", {"q": 1}, c.fn("a", {"text": "A"}, 10)) == {
        "text": "A"
    }
    assert await first.step("tool_call", "read_file", {"p": "x"}, c.fn("b", "B")) == "B"
    # The worker dies here, before step 2. Another worker takes over the run.
    lease2 = await _expire_and_reclaim(pool, lease)
    second = StepRunner(pool, lease2)
    assert await second.step("llm_call", "m", {"q": 1}, c.fn("a", {"text": "X"})) == {"text": "A"}
    assert await second.step("tool_call", "read_file", {"p": "x"}, c.fn("b", "X")) == "B"
    assert await second.step("llm_call", "m", {"q": 2}, c.fn("c", "C")) == "C"
    assert c.calls == {"a": 1, "b": 1, "c": 1}
    assert (second.replayed, second.executed) == (2, 1)
    tokens = await pool.fetchval(
        "SELECT input_tokens + output_tokens FROM runs WHERE id = $1", lease.run_id
    )
    assert tokens == 20  # replay does not double-count usage


async def test_started_step_is_executed_again_on_resume(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool)

    async def dies() -> StepOutcome:
        raise asyncio.CancelledError  # simulates the process dying mid-call

    with pytest.raises(asyncio.CancelledError):
        await StepRunner(pool, lease).step("llm_call", "m", {}, dies)
    assert await pool.fetchval("SELECT status FROM run_steps") == "started"

    lease2 = await _expire_and_reclaim(pool, lease)
    c = Counter()
    assert await StepRunner(pool, lease2).step("llm_call", "m", {}, c.fn("a", 1)) == 1
    row = await pool.fetchrow("SELECT status, attempts, lease_version FROM run_steps")
    assert row is not None
    assert (row["status"], row["attempts"], row["lease_version"]) == ("done", 2, lease2.version)


async def test_changed_input_on_replay_is_non_determinism(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool)
    c = Counter()
    await StepRunner(pool, lease).step("llm_call", "m", {"q": 1}, c.fn("a", 1))
    lease2 = await _expire_and_reclaim(pool, lease)
    with pytest.raises(NonDeterminismError):
        await StepRunner(pool, lease2).step("llm_call", "m", {"q": 2}, c.fn("a", 1))


async def test_zombie_cannot_start_or_finish_a_step(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool)
    zombie = StepRunner(pool, lease)
    release = asyncio.Event()

    async def slow() -> StepOutcome:
        await release.wait()
        return StepOutcome(output="zombie")

    task = asyncio.create_task(zombie.step("llm_call", "m", {}, slow))
    await asyncio.sleep(0.1)
    lease2 = await _expire_and_reclaim(pool, lease)
    c = Counter()
    assert await StepRunner(pool, lease2).step("llm_call", "m", {}, c.fn("a", "fresh")) == "fresh"
    release.set()
    with pytest.raises(LeaseLostError):
        await task
    assert await pool.fetchval("SELECT output #>> '{}' FROM run_steps") == "fresh"
    with pytest.raises(LeaseLostError):
        await zombie.step("llm_call", "m2", {}, c.fn("b", 1))
    assert "b" not in c.calls


async def test_budgets_stop_the_run(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool)
    await pool.execute("UPDATE runs SET max_steps = 2 WHERE id = $1", lease.run_id)
    runner = StepRunner(pool, lease)
    c = Counter()
    await runner.step("llm_call", "m", {"i": 0}, c.fn("a", 0))
    await runner.step("llm_call", "m", {"i": 1}, c.fn("a", 1))
    with pytest.raises(BudgetExceededError, match="max_steps"):
        await runner.step("llm_call", "m", {"i": 2}, c.fn("a", 2))
    assert c.calls["a"] == 2

    lease = await _claimed(pool)
    await pool.execute("UPDATE runs SET max_tokens = 100 WHERE id = $1", lease.run_id)
    runner = StepRunner(pool, lease)
    await runner.step("llm_call", "m", {}, c.fn("t", 0, tokens=60))  # 120 used
    with pytest.raises(BudgetExceededError, match="max_tokens"):
        await runner.step("llm_call", "m", {"n": 2}, c.fn("t", 0))

    lease = await _claimed(pool)
    await pool.execute(
        "UPDATE runs SET deadline_at = now() - interval '1 second' WHERE id = $1", lease.run_id
    )
    with pytest.raises(BudgetExceededError, match="deadline"):
        await StepRunner(pool, lease).step("llm_call", "m", {}, c.fn("d", 0))
    assert "d" not in c.calls


async def test_heartbeat_keeps_short_lease_alive_past_its_ttl(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool, lease_s=0.6)
    async with LeaseKeeper(pool, lease, 0.6) as keeper:
        await asyncio.sleep(1.5)  # 2.5x the lease
        assert await reap_runs(pool, {}) == 0
        assert not keeper.lost.is_set()
        assert keeper.renewals >= 3
    assert await renew_lease(pool, lease, 30.0)


async def test_heartbeat_detects_lost_lease(pool: asyncpg.Pool) -> None:
    lease = await _claimed(pool, lease_s=0.6)
    async with LeaseKeeper(pool, lease, 0.6) as keeper:
        await _expire_and_reclaim(pool, lease)
        await asyncio.wait_for(keeper.lost.wait(), timeout=2)
    assert not await renew_lease(pool, lease, 30.0)
