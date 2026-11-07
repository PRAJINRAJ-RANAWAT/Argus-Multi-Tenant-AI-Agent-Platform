"""Owner-checked Redis locks: at most one live review per (repo, PR).

The lock value is the run id. Renew and release are Lua compare-and-act scripts, so a
worker whose lock expired (and was taken by another run) cannot extend or delete the new
owner's lock. The lock is an efficiency guard, not the correctness guarantee: fencing
tokens on the run and the side-effect ledger still protect writes if Redis loses it.
"""

from typing import cast

from redis.asyncio import Redis

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def pr_lock_key(repo_id: object, pr_number: int) -> str:
    return f"lock:pr:{repo_id}:{pr_number}"


class OwnedLock:
    def __init__(self, redis: Redis, key: str, owner: str, ttl_s: float) -> None:
        self.redis = redis
        self.key = key
        self.owner = owner
        self.ttl_ms = max(1, int(ttl_s * 1000))

    async def acquire(self) -> str | None:
        """Take the lock (or keep it, if we already own it). Returns None on success,
        otherwise the current holder."""
        if await self.redis.set(self.key, self.owner, nx=True, px=self.ttl_ms):
            return None
        holder = cast(str | None, await self.redis.get(self.key))
        if holder == self.owner:
            # Our own earlier attempt crashed while holding it: keep it and refresh.
            await self.renew()
            return None
        if holder is None:  # expired between SET and GET
            return await self.acquire()
        return holder

    async def renew(self) -> bool:
        res = await self.redis.eval(_RENEW, 1, self.key, self.owner, self.ttl_ms)
        return bool(res)

    async def release(self) -> bool:
        res = await self.redis.eval(_RELEASE, 1, self.key, self.owner)
        return bool(res)
