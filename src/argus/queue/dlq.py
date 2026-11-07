"""Dead-letter inspection and replay.

Replay moves a message from ``<stream>:dlq`` back onto ``<stream>`` with ``attempt``
reset, in one Lua script per message: XADD to the stream and XDEL from the DLQ happen
together, so two concurrent replays of the same entry enqueue it once. Replaying is safe
because handlers are idempotent (runs are keyed, side effects go through the ledger).
"""

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from argus.queue.streams import STREAM_MAXLEN
from argus.queue.topics import dlq

# KEYS: stream, dlq. ARGV: dlq entry id, maxlen. Returns the new id, or false if gone.
_REPLAY_LUA = """
local entries = redis.call('XRANGE', KEYS[2], ARGV[1], ARGV[1])
if #entries == 0 then return false end
local fields = entries[1][2]
local data
for i = 1, #fields, 2 do
  if fields[i] == 'data' then data = fields[i + 1] end
end
if not data then return false end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*',
                      'data', data, 'attempt', '0', 'replayed_from', ARGV[1])
redis.call('XDEL', KEYS[2], ARGV[1])
return id
"""


@dataclass(frozen=True)
class DeadLetter:
    id: str
    data: str
    attempt: int
    error: str
    origin_id: str


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


async def list_dead(
    redis: Redis, stream: str, *, count: int = 50, after: str = "-"
) -> list[DeadLetter]:
    start = after if after == "-" else f"({after}"
    entries: Any = await redis.xrange(dlq(stream), min=start, max="+", count=count)
    out: list[DeadLetter] = []
    for entry_id, raw in entries:
        f = {_s(k): _s(v) for k, v in raw.items()}
        out.append(
            DeadLetter(
                id=_s(entry_id),
                data=f.get("data", ""),
                attempt=int(f.get("attempt", "0")),
                error=f.get("error", ""),
                origin_id=f.get("origin_id", ""),
            )
        )
    return out


async def replay(redis: Redis, stream: str, ids: list[str]) -> dict[str, str | None]:
    """Replay the given DLQ entries. Maps each id to its new stream id, or None if the
    entry no longer exists (already replayed or trimmed)."""
    script = redis.register_script(_REPLAY_LUA)
    result: dict[str, str | None] = {}
    for entry_id in ids:
        new_id = await script(keys=[stream, dlq(stream)], args=[entry_id, STREAM_MAXLEN])
        result[entry_id] = _s(new_id) if new_id else None
    return result


async def replay_all(redis: Redis, stream: str, *, limit: int = 1000) -> int:
    replayed, cursor = 0, "-"
    while replayed < limit:
        batch = await list_dead(redis, stream, count=min(100, limit - replayed), after=cursor)
        if not batch:
            break
        cursor = batch[-1].id  # entries that cannot be replayed are skipped, not retried
        done = await replay(redis, stream, [d.id for d in batch])
        replayed += sum(1 for v in done.values() if v is not None)
    return replayed
