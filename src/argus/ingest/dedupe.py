"""Redis fast-path dedupe on delivery id.

Postgres' unique constraint is the source of truth. Redis only short-circuits
deliveries that are already *committed*, so a crash between "seen" and "stored"
can never drop an event. Redis errors degrade to the DB path instead of failing
the webhook.
"""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

DEDUPE_TTL_S = 86_400


def _key(source: str, delivery_id: str) -> str:
    return f"delivery:{source}:{delivery_id}"


async def already_processed(redis: Redis, source: str, delivery_id: str) -> bool:
    try:
        return bool(await redis.exists(_key(source, delivery_id)))
    except RedisError:
        log.warning("dedupe check failed; falling back to database", exc_info=True)
        return False


async def mark_processed(redis: Redis, source: str, delivery_id: str) -> None:
    try:
        await redis.set(_key(source, delivery_id), 1, nx=True, ex=DEDUPE_TTL_S)
    except RedisError:
        log.warning("dedupe mark failed", exc_info=True)
