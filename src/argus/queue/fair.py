"""Per-tenant fair scheduling on top of Redis Streams.

A plain FIFO stream lets one tenant's 500-PR burst delay everyone. ``FairQueue`` keeps
one sub-stream per tenant (``<topic>:t:<tenant>``, all with the same consumer group) and
a set of active tenants. Each read:

1. rotates the tenant list (round-robin start, so no tenant is always first);
2. reserves at most one in-flight slot per tenant, skipping tenants already at their
   concurrency cap (slots are leases in a sorted set with an expiry, so a worker that
   dies cannot leak a tenant's capacity);
3. reads at most one message from each reserved tenant's stream in one XREADGROUP call.

So a tenant with 500 queued jobs and one with 5 each get a message per round, and no
tenant holds more than ``tenant_cap`` slots while others have work. The scheduler is
work-conserving: in a round where no other tenant has work, a capped tenant may borrow
idle slots up to ``burst_cap``, and when every tenant is at capacity the read waits for a
slot to free instead of spinning. Ack, retry, dead-letter, promote
and reclaim delegate to a ``StreamQueue`` per sub-stream and release the slot.
"""

import asyncio
import contextlib
import itertools
import logging
import time
import uuid
from typing import Any

from redis.asyncio import Redis

from argus.queue.streams import STREAM_MAXLEN, Message, StreamQueue

log = logging.getLogger("argus.queue.fair")

DEFAULT_LANE = "_shared"

# KEYS: slot zset. ARGV: now_ms, cap, lease_ms, member. 1 = reserved.
_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[3]), ARGV[4])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
return 1
"""


# KEYS: tenants set, lane stream, delayed zset. ARGV: tenant, group. 1 = pruned.
# Atomic, so it cannot race the relay: the relay re-adds the tenant after every XADD.
_PRUNE = """
if redis.call('EXISTS', KEYS[2]) == 0 then
  redis.call('SREM', KEYS[1], ARGV[1])
  return 1
end
if redis.call('ZCARD', KEYS[3]) > 0 then return 0 end
local ok, groups = pcall(redis.call, 'XINFO', 'GROUPS', KEYS[2])
if not ok then return 0 end
for _, g in ipairs(groups) do
  local f = {}
  for i = 1, #g, 2 do f[g[i]] = g[i + 1] end
  if f['name'] == ARGV[2] then
    if tonumber(f['pending']) == 0 and (f['lag'] == false or tonumber(f['lag']) == 0) then
      redis.call('SREM', KEYS[1], ARGV[1])
      return 1
    end
    return 0
  end
end
return 0
"""


def lane_stream(topic: str, tenant: str) -> str:
    return f"{topic}:t:{tenant}"


async def publish_fair(redis: Redis, topic: str, tenant: str | None, fields: dict[str, str]) -> str:
    """XADD into the tenant's sub-stream and mark the tenant active."""
    lane = tenant or DEFAULT_LANE
    pipe = redis.pipeline(transaction=True)
    pipe.xadd(lane_stream(topic, lane), fields, maxlen=STREAM_MAXLEN, approximate=True)  # type: ignore[arg-type]
    pipe.sadd(f"{topic}:tenants", lane)
    res = await pipe.execute()
    return str(res[0])


class FairQueue:
    def __init__(
        self,
        redis: Redis,
        topic: str,
        group: str,
        consumer: str,
        *,
        tenant_cap: int = 4,
        burst_cap: int | None = None,
        slot_lease_ms: int = 300_000,
        **queue_kwargs: Any,
    ) -> None:
        self.redis = redis
        self.stream = topic  # for metrics and logs
        self.topic = topic
        self.group = group
        self.consumer = consumer
        self.tenant_cap = tenant_cap
        # Idle capacity a tenant may borrow when no other tenant has work (work-conserving).
        self.burst_cap = burst_cap if burst_cap is not None else tenant_cap * 4
        self.slot_lease_ms = slot_lease_ms
        self.queue_kwargs = queue_kwargs
        self._lanes: dict[str, StreamQueue] = {}
        self._rr = itertools.count()
        self._acquire = redis.register_script(_ACQUIRE)
        self._prune = redis.register_script(_PRUNE)
        self._slots: dict[str, str] = {}  # message key -> slot member
        self._ready: set[str] = set()  # lanes whose consumer group exists
        self._freed = asyncio.Event()  # set when this worker releases a slot

    def lane(self, tenant: str) -> StreamQueue:
        q = self._lanes.get(tenant)
        if q is None:
            q = StreamQueue(
                self.redis,
                lane_stream(self.topic, tenant),
                self.group,
                self.consumer,
                **self.queue_kwargs,
            )
            self._lanes[tenant] = q
        return q

    async def ready_lane(self, tenant: str) -> StreamQueue:
        """The lane, with its consumer group created. Lanes appear whenever a new tenant
        sends work, after this worker started, so every operation goes through here."""
        q = self.lane(tenant)
        if tenant not in self._ready:
            await q.ensure_group()
            self._ready.add(tenant)
        return q

    def _tenant_of(self, msg: Message) -> str:
        return msg.stream.removeprefix(f"{self.topic}:t:")

    def _slot_key(self, tenant: str) -> str:
        return f"{self.topic}:slots:{tenant}"

    async def ensure_group(self) -> None:
        for tenant in await self.tenants():
            await self.ready_lane(tenant)

    async def tenants(self) -> list[str]:
        members = await self.redis.smembers(f"{self.topic}:tenants")
        return sorted(m.decode() if isinstance(m, bytes) else str(m) for m in members)

    async def _release(self, msg: Message) -> None:
        member = self._slots.pop(f"{msg.stream}|{msg.id}", None)
        if member is not None:
            await self.redis.zrem(self._slot_key(self._tenant_of(msg)), member)
            self._freed.set()

    async def _take(self, tenant: str, n: int, cap: int, now: int) -> list[str]:
        members: list[str] = []
        for _ in range(n):
            member = f"{self.consumer}:{uuid.uuid4().hex}"  # unique: same-ms reads collide
            ok = await self._acquire(
                keys=[self._slot_key(tenant)], args=[now, cap, self.slot_lease_ms, member]
            )
            if not ok:
                break
            members.append(member)
        return members

    async def _read_lanes(
        self, reservations: dict[str, list[str]], block_ms: int | None
    ) -> list[Message]:
        """Read up to len(members) messages from each tenant lane; give back unused slots."""
        out: list[Message] = []
        for tenant in reservations:
            await self.ready_lane(tenant)
        if len(reservations) == 1 or block_ms:
            # One XREADGROUP for every lane (round-robin, one each), optionally blocking.
            streams: dict[Any, Any] = {lane_stream(self.topic, t): ">" for t in reservations}
            counts = {t: 1 for t in reservations}
            if len(reservations) == 1:
                [(only, members)] = reservations.items()
                counts[only] = len(members)
            resp: Any = await self.redis.xreadgroup(
                self.group,
                self.consumer,
                streams,
                count=max(counts.values()),
                block=block_ms or None,
            )
        else:
            streams = {lane_stream(self.topic, t): ">" for t in reservations}
            resp = await self.redis.xreadgroup(self.group, self.consumer, streams, count=1)
        used: dict[str, int] = {}
        for stream_name, entries in resp or []:
            name = stream_name.decode() if isinstance(stream_name, bytes) else str(stream_name)
            tenant = name.removeprefix(f"{self.topic}:t:")
            members = reservations.get(tenant, [])
            for msg_id, fields in entries:
                k = used.get(tenant, 0)
                if k >= len(members):  # more than we reserved: leave it pending for reclaim
                    break
                msg = self.lane(tenant)._to_message(msg_id, fields)
                self._slots[f"{msg.stream}|{msg.id}"] = members[k]
                used[tenant] = k + 1
                out.append(msg)
        for tenant, members in reservations.items():
            for member in members[used.get(tenant, 0) :]:
                await self.redis.zrem(self._slot_key(tenant), member)
        return out

    async def read(self, count: int = 10, block_ms: int = 1000) -> list[Message]:
        tenants = await self.tenants()
        if not tenants:
            if block_ms:
                await _sleep(min(block_ms / 1000, 0.5))
            return []
        start = next(self._rr) % len(tenants)
        order = tenants[start:] + tenants[:start]
        now = int(time.time() * 1000)
        # Fair pass: one message per tenant under its cap, starting at a rotating tenant.
        reserved: dict[str, list[str]] = {}
        capped: list[str] = []
        for tenant in order:
            if len(reserved) >= count:
                break
            members = await self._take(tenant, 1, self.tenant_cap, now)
            if members:
                reserved[tenant] = members
            else:
                capped.append(tenant)
        out: list[Message] = []
        if reserved:
            # Do not block while capped tenants may borrow idle capacity below.
            out = await self._read_lanes(reserved, None if capped else block_ms)
        # Work-conserving pass: if nobody else had work this round, capped tenants may
        # borrow idle slots (up to burst_cap). Others get their own slots next round.
        if capped and not out and len(out) < count:
            remaining = count
            for tenant in capped:
                if remaining <= 0:
                    break
                members = await self._take(tenant, remaining, self.burst_cap, now)
                if members:
                    got = await self._read_lanes({tenant: members}, None)
                    out.extend(got)
                    remaining -= len(got)
        if not out and block_ms and (capped or not reserved):
            # Everyone is at capacity: wait for a slot to free up instead of spinning.
            self._freed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._freed.wait(), timeout=min(block_ms, 500) / 1000)
        return out

    async def ack(self, msg: Message) -> bool:
        try:
            return await self.lane(self._tenant_of(msg)).ack(msg)
        finally:
            await self._release(msg)

    async def retry(
        self,
        msg: Message,
        error: str,
        *,
        min_delay_s: float | None = None,
        count_attempt: bool = True,
    ) -> str:
        try:
            return await self.lane(self._tenant_of(msg)).retry(
                msg, error, min_delay_s=min_delay_s, count_attempt=count_attempt
            )
        finally:
            await self._release(msg)

    async def dead_letter(self, msg: Message, error: str) -> str:
        try:
            return await self.lane(self._tenant_of(msg)).dead_letter(msg, error)
        finally:
            await self._release(msg)

    async def promote_due(self, limit: int = 100, now_ms: int | None = None) -> int:
        """Promote due retries, and drop idle tenants from the active set so reads and
        housekeeping only touch tenants that have work."""
        n = 0
        for tenant in await self.tenants():
            try:
                lane = await self.ready_lane(tenant)
                n += await lane.promote_due(limit, now_ms)
                pruned = await self._prune(
                    keys=[f"{self.topic}:tenants", lane.stream, f"{lane.stream}:delayed"],
                    args=[tenant, self.group],
                )
                if pruned:
                    self._ready.discard(tenant)
            except Exception:  # one broken lane must not stall every tenant
                log.warning("promote failed", extra={"tenant": tenant}, exc_info=True)
        return n

    async def reclaim(self, count: int = 50, min_idle_ms: int | None = None) -> list[Message]:
        out: list[Message] = []
        now = int(time.time() * 1000)
        for tenant in await self.tenants():
            try:
                msgs = await (await self.ready_lane(tenant)).reclaim(count, min_idle_ms)
            except Exception:
                log.warning("reclaim failed", extra={"tenant": tenant}, exc_info=True)
                continue
            for msg in msgs:
                member = f"{self.consumer}:{uuid.uuid4().hex}"
                await self.redis.zadd(self._slot_key(tenant), {member: now + self.slot_lease_ms})
                self._slots[f"{msg.stream}|{msg.id}"] = member
                out.append(msg)
        return out

    async def depth(self) -> dict[str, int]:
        total = {"pending": 0, "lag": 0, "delayed": 0, "dlq": 0}
        for tenant in await self.tenants():
            try:
                d = await self.lane(tenant).depth()
            except Exception:  # lane without a group yet
                log.debug("depth unavailable", extra={"tenant": tenant}, exc_info=True)
                continue
            for k in total:
                total[k] += d.get(k, 0)
        return total

    async def oldest_age_s(self, now_ms: int | None = None) -> float:
        ages = []
        for tenant in await self.tenants():
            try:
                ages.append(await self.lane(tenant).oldest_age_s(now_ms))
            except Exception:
                log.debug("age unavailable", extra={"tenant": tenant}, exc_info=True)
                continue
        return max(ages, default=0.0)

    async def inflight(self, tenant: str) -> int:
        now = int(time.time() * 1000)
        key = self._slot_key(tenant)
        await self.redis.zremrangebyscore(key, "-inf", now)
        return int(await self.redis.zcard(key))


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
