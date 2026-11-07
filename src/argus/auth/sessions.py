"""Server-side sessions in Redis. The cookie holds a random id; Redis stores only its
hash, so a Redis dump does not contain usable session cookies."""

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

COOKIE = "argus_session"


@dataclass(frozen=True)
class Session:
    user_id: uuid.UUID
    github_login: str
    demo: bool = False  # public read-only demo visitor


def _key(session_id: str) -> str:
    return "session:" + hashlib.sha256(session_id.encode()).hexdigest()


async def create_session(
    redis: Redis, user_id: uuid.UUID, login: str, ttl_s: int, *, demo: bool = False
) -> str:
    session_id = secrets.token_urlsafe(32)
    await redis.set(
        _key(session_id),
        json.dumps({"user_id": str(user_id), "login": login, "demo": demo}),
        ex=ttl_s,
    )
    return session_id


async def get_session(redis: Redis, session_id: str) -> Session | None:
    if not session_id or len(session_id) > 100:
        return None
    raw = cast(str | None, await redis.get(_key(session_id)))
    if raw is None:
        return None
    data = json.loads(raw)
    return Session(uuid.UUID(data["user_id"]), data["login"], bool(data.get("demo", False)))


async def delete_session(redis: Redis, session_id: str) -> None:
    await redis.delete(_key(session_id))
