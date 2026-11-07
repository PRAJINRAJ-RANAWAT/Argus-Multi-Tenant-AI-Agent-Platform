"""Token buckets and circuit breakers in Redis, each as one atomic Lua script, so every
worker shares the same limits and breaker state.

Token bucket: ``capacity`` tokens refilled at ``rate`` per second. ``take(cost)`` either
takes the tokens or says how long to wait. ``adjust(delta)`` reconciles an estimate with
actual usage afterwards (the bucket may go into debt, which delays later calls).

Circuit breaker per model: counts outcomes in a rolling window. When at least
``min_calls`` calls failed at ``failure_ratio`` or more, it opens for ``cooldown``; then
one probe at a time is let through (half-open). A successful probe closes it, a failed
one reopens it.
"""

import time

from redis.asyncio import Redis

_TAKE = """
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = math.min(tonumber(ARGV[3]), cap)
local now = tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - ts) / 1000 * rate)
local allowed = 0
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  wait = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate * 1000) + 60000)
return {allowed, tostring(wait)}
"""

_ADJUST = """
local cap = tonumber(ARGV[1])
local t = tonumber(redis.call('HGET', KEYS[1], 'tokens') or cap)
t = math.min(cap, t - tonumber(ARGV[2]))
redis.call('HSET', KEYS[1], 'tokens', tostring(t))
return tostring(t)
"""


class TokenBucket:
    def __init__(self, redis: Redis, *, capacity: float, per_second: float) -> None:
        self.redis = redis
        self.capacity = capacity
        self.per_second = per_second
        self._take = redis.register_script(_TAKE)
        self._adjust = redis.register_script(_ADJUST)

    async def take(self, key: str, cost: float, now_ms: int | None = None) -> tuple[bool, float]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        allowed, wait = await self._take(
            keys=[f"bucket:{key}"], args=[self.capacity, self.per_second, cost, now]
        )
        return bool(int(allowed)), float(wait)

    async def adjust(self, key: str, delta: float) -> None:
        """Positive delta takes more (actual > estimate); negative gives back."""
        await self._adjust(keys=[f"bucket:{key}"], args=[self.capacity, delta])


# allow: 0 = rejected (open), 1 = closed, 2 = half-open probe
_ALLOW = """
local now = tonumber(ARGV[1])
local probe_ttl = tonumber(ARGV[2])
local s = redis.call('HMGET', KEYS[1], 'state', 'open_until', 'probe_until')
local state = s[1] or 'closed'
if state == 'open' then
  if now < tonumber(s[2]) then return 0 end
  redis.call('HSET', KEYS[1], 'state', 'half_open', 'probe_until', now + probe_ttl)
  return 2
end
if state == 'half_open' then
  if now < tonumber(s[3]) then return 0 end
  redis.call('HSET', KEYS[1], 'probe_until', now + probe_ttl)
  return 2
end
return 1
"""

_RECORD = """
local ok = ARGV[1] == '1'
local now = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local min_calls = tonumber(ARGV[4])
local ratio = tonumber(ARGV[5])
local cooldown = tonumber(ARGV[6])
local s = redis.call('HMGET', KEYS[1], 'state', 'window_start', 'total', 'failures')
local state = s[1] or 'closed'
if state ~= 'closed' then
  if ok then
    redis.call('DEL', KEYS[1])
    return 'closed'
  end
  redis.call('HSET', KEYS[1], 'state', 'open', 'open_until', now + cooldown)
  return 'open'
end
local start = tonumber(s[2]) or now
local total = tonumber(s[3]) or 0
local failures = tonumber(s[4]) or 0
if now - start > window then start = now; total = 0; failures = 0 end
total = total + 1
if not ok then failures = failures + 1 end
if total >= min_calls and failures / total >= ratio then
  redis.call('HSET', KEYS[1], 'state', 'open', 'open_until', now + cooldown,
             'window_start', now, 'total', 0, 'failures', 0)
  redis.call('PEXPIRE', KEYS[1], cooldown + window + 60000)
  return 'open'
end
redis.call('HSET', KEYS[1], 'window_start', start, 'total', total, 'failures', failures)
redis.call('PEXPIRE', KEYS[1], window + 60000)
return 'closed'
"""


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        *,
        window_s: float = 30.0,
        min_calls: int = 5,
        failure_ratio: float = 0.5,
        cooldown_s: float = 30.0,
        probe_ttl_s: float = 10.0,
    ) -> None:
        self.redis = redis
        self.window_ms = int(window_s * 1000)
        self.min_calls = min_calls
        self.failure_ratio = failure_ratio
        self.cooldown_ms = int(cooldown_s * 1000)
        self.probe_ttl_ms = int(probe_ttl_s * 1000)
        self._allow = redis.register_script(_ALLOW)
        self._record = redis.register_script(_RECORD)

    async def allow(self, model: str, now_ms: int | None = None) -> int:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return int(await self._allow(keys=[f"cb:{model}"], args=[now, self.probe_ttl_ms]))

    async def record(self, model: str, ok: bool, now_ms: int | None = None) -> str:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        res = await self._record(
            keys=[f"cb:{model}"],
            args=[
                "1" if ok else "0",
                now,
                self.window_ms,
                self.min_calls,
                self.failure_ratio,
                self.cooldown_ms,
            ],
        )
        return res.decode() if isinstance(res, bytes) else str(res)

    async def state(self, model: str) -> str:
        s = await self.redis.hget(f"cb:{model}", "state")
        return str(s) if s else "closed"
