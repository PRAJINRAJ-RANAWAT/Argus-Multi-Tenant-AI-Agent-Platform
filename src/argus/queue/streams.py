"""Redis Streams job queue with consumer groups, delayed retries and a dead-letter stream.

Semantics: at-least-once. A message is XACKed only after its handler finished and
its effects were durably recorded. Failure paths are atomic Lua scripts, so a
message is never both acknowledged and lost:

* ``retry``: ZADD into ``<stream>:delayed`` (due time = now + backoff) + XACK.
* ``dead_letter``: XADD into ``<stream>:dlq`` with the error + XACK.
* ``promote_due``: move due retries from the delayed set back onto the stream.

Every ack/retry/dead-letter first checks that this consumer still owns the pending
entry, so a slow consumer whose message was reclaimed cannot ack it from under
the new owner.

Two counters bound poison messages:
* ``attempt`` (message field): handler failures that were explicitly retried.
* delivery count (XPENDING): redeliveries after consumer crashes, checked on reclaim.
"""

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from argus.queue.topics import dlq

STREAM_MAXLEN = 100_000

# Return 1 if ARGV[2] (entry id) is pending and owned by ARGV[3] (consumer).
_OWNS = """
local p = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p == 0 or p[1][2] ~= ARGV[3] then return 0 end
"""

_ACK_LUA = (
    _OWNS
    + """
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""
)

# KEYS: stream, delayed zset. ARGV: group, id, consumer, due_ms, member
_RETRY_LUA = (
    _OWNS
    + """
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[5])
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""
)

# KEYS: stream, dlq. ARGV: group, id, consumer, data, attempt, error, maxlen
_DLQ_LUA = (
    _OWNS
    + """
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[7], '*',
           'data', ARGV[4], 'attempt', ARGV[5], 'error', ARGV[6],
           'origin_stream', KEYS[1], 'origin_id', ARGV[2])
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""
)

# KEYS: stream, delayed zset. ARGV: now_ms, limit, maxlen
_PROMOTE_LUA = """
local items = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, m in ipairs(items) do
  local d = cjson.decode(m)
  redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[3], '*',
             'data', d['data'], 'attempt', tostring(d['attempt']))
  redis.call('ZREM', KEYS[2], m)
end
return #items
"""


@dataclass(frozen=True)
class Message:
    stream: str
    id: str
    data: str
    attempt: int = 0
    deliveries: int = 1
    fields: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.data)


def backoff_s(attempt: int, *, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2**attempt))."""
    ceiling = min(cap, base * (2**attempt))
    return (rng or random).uniform(0, ceiling)


def _delayed(stream: str) -> str:
    return f"{stream}:delayed"


async def publish(redis: Redis, stream: str, payload: str | dict[str, Any], **extra: str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    fields: dict[str, str] = {"data": data, "attempt": "0", **extra}
    msg_id = await redis.xadd(stream, fields, maxlen=STREAM_MAXLEN, approximate=True)  # type: ignore[arg-type]
    return str(msg_id)


class StreamQueue:
    def __init__(
        self,
        redis: Redis,
        stream: str,
        group: str,
        consumer: str,
        *,
        max_attempts: int = 5,
        max_deliveries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 300.0,
        visibility_timeout_ms: int = 60_000,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.max_attempts = max_attempts
        self.max_deliveries = max_deliveries
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.visibility_timeout_ms = visibility_timeout_ms
        self._ack = redis.register_script(_ACK_LUA)
        self._retry = redis.register_script(_RETRY_LUA)
        self._dlq = redis.register_script(_DLQ_LUA)
        self._promote = redis.register_script(_PROMOTE_LUA)

    @property
    def dlq_stream(self) -> str:
        return dlq(self.stream)

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _to_message(self, msg_id: Any, fields: dict[Any, Any], deliveries: int = 1) -> Message:
        f = {_s(k): _s(v) for k, v in fields.items()}
        return Message(
            stream=self.stream,
            id=_s(msg_id),
            data=f.get("data", ""),
            attempt=int(f.get("attempt", "0")),
            deliveries=deliveries,
            fields=f,
        )

    async def read(self, count: int = 10, block_ms: int = 1000) -> list[Message]:
        resp: Any = await self.redis.xreadgroup(
            self.group, self.consumer, {self.stream: ">"}, count=count, block=block_ms
        )
        out: list[Message] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                if fields is None:  # entry trimmed while pending
                    continue
                out.append(self._to_message(msg_id, fields))
        return out

    async def ack(self, msg: Message) -> bool:
        res = await self._ack(keys=[self.stream], args=[self.group, msg.id, self.consumer])
        return bool(res)

    async def retry(
        self,
        msg: Message,
        error: str,
        *,
        min_delay_s: float | None = None,
        count_attempt: bool = True,
    ) -> str:
        """Schedule a retry with backoff, or dead-letter once attempts are exhausted.

        ``min_delay_s`` honours a server's ``Retry-After``; ``count_attempt=False`` is for
        waits that are not failures (rate limits), so they never dead-letter a message.
        Returns ``"retry"``, ``"dead"``, or ``"lost_ownership"``.
        """
        next_attempt = msg.attempt + 1 if count_attempt else msg.attempt
        if next_attempt >= self.max_attempts:
            return await self.dead_letter(msg, error)
        delay = backoff_s(msg.attempt, base=self.backoff_base_s, cap=self.backoff_cap_s)
        if min_delay_s is not None:
            delay = max(delay, min_delay_s)
        due_ms = int((time.time() + delay) * 1000)
        member = json.dumps(
            {"data": msg.data, "attempt": next_attempt, "origin": msg.id, "error": error[:500]}
        )
        res = await self._retry(
            keys=[self.stream, _delayed(self.stream)],
            args=[self.group, msg.id, self.consumer, due_ms, member],
        )
        return "retry" if res else "lost_ownership"

    async def dead_letter(self, msg: Message, error: str) -> str:
        res = await self._dlq(
            keys=[self.stream, self.dlq_stream],
            args=[
                self.group,
                msg.id,
                self.consumer,
                msg.data,
                msg.attempt,
                error[:2000],
                STREAM_MAXLEN,
            ],
        )
        return "dead" if res else "lost_ownership"

    async def promote_due(self, limit: int = 100, now_ms: int | None = None) -> int:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        res = await self._promote(
            keys=[self.stream, _delayed(self.stream)], args=[now, limit, STREAM_MAXLEN]
        )
        return int(cast(int, res))

    async def reclaim(self, count: int = 50, min_idle_ms: int | None = None) -> list[Message]:
        """XAUTOCLAIM messages idle past the visibility timeout (their consumer is presumed
        dead). Messages redelivered too many times go to the DLQ instead of being returned."""
        idle = self.visibility_timeout_ms if min_idle_ms is None else min_idle_ms
        resp: Any = await self.redis.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_time=idle, start_id="0-0", count=count
        )
        entries = resp[1] if resp else []
        out: list[Message] = []
        for msg_id, fields in entries:
            if fields is None:
                continue
            pending = await self.redis.xpending_range(
                self.stream, self.group, min=msg_id, max=msg_id, count=1
            )
            deliveries = int(pending[0]["times_delivered"]) if pending else 1
            msg = self._to_message(msg_id, fields, deliveries)
            if deliveries > self.max_deliveries:
                await self.dead_letter(msg, f"exceeded {self.max_deliveries} deliveries")
                continue
            out.append(msg)
        return out

    async def depth(self) -> dict[str, int]:
        info = await self.redis.xinfo_groups(self.stream)
        group = next((g for g in info if _s(g["name"]) == self.group), None)
        return {
            "pending": int(group["pending"]) if group else 0,
            "lag": int(group.get("lag") or 0) if group else 0,
            "delayed": int(await self.redis.zcard(_delayed(self.stream))),
            "dlq": int(await self.redis.xlen(self.dlq_stream)),
        }


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)
