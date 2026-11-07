"""Public read-only demo.

``GET /demo`` signs a visitor in, without GitHub, as a shared ``demo-visitor`` user that
is a *viewer* of one dedicated tenant (``ARGUS_DEMO_TENANT``) and of nothing else. The
session is marked ``demo``, which the API enforces on the server:

* every write is refused (``demo_read_only`` on the API router), except asking the
  codebase a question;
* questions are rate limited per client IP and per day for all visitors, and stop when
  the demo tenant's daily token budget is spent, with a friendly 429.

Row-level security still applies, so the visitor can only ever read the demo tenant.
"""

import time
import uuid
from datetime import UTC, datetime

import asyncpg
from fastapi import Request
from redis.asyncio import Redis

DEMO_USER_GITHUB_ID = -424242  # negative: never collides with a real GitHub user id
DEMO_USER_LOGIN = "demo-visitor"


class DemoLimitError(Exception):
    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


async def demo_tenant(pool: asyncpg.Pool, login: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT id, github_account_login FROM tenants WHERE lower(github_account_login) ="
        " lower($1) AND suspended_at IS NULL",
        login,
    )


async def ensure_demo_member(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> uuid.UUID:
    """The shared demo user, a viewer of the demo tenant only (idempotent)."""
    async with pool.acquire() as conn, conn.transaction():
        user_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO users (github_id, github_login, name, last_login_at)
            VALUES ($1, $2, 'Demo visitor', now())
            ON CONFLICT (github_id) DO UPDATE SET last_login_at = now()
            RETURNING id
            """,
            DEMO_USER_GITHUB_ID,
            DEMO_USER_LOGIN,
        )
        await conn.execute(
            """
            INSERT INTO memberships (tenant_id, user_id, role, source)
            VALUES ($1, $2, 'viewer', 'manual')
            ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = 'viewer', updated_at = now()
            """,
            tenant_id,
            user_id,
        )
        # Never a member of anything else, whatever happened before.
        await conn.execute(
            "DELETE FROM memberships WHERE user_id = $1 AND tenant_id <> $2", user_id, tenant_id
        )
    return user_id


async def _hit(redis: Redis, key: str, limit: int, window_s: int) -> int | None:
    """Fixed-window counter. Returns seconds to wait when over the limit, else None."""
    bucket = int(time.time() // window_s)
    full = f"{key}:{bucket}"
    pipe = redis.pipeline(transaction=True)
    pipe.incr(full)
    pipe.expire(full, window_s + 5)
    count, _ = await pipe.execute()
    if int(count) > limit:
        return int(window_s - time.time() % window_s) + 1
    return None


async def check_session_limit(redis: Redis, ip: str, per_hour: int) -> None:
    wait = await _hit(redis, f"demo:session:{ip}", per_hour, 3600)
    if wait:
        raise DemoLimitError("Too many demo sessions from your network. Try again later.", wait)


async def check_question_limits(
    redis: Redis,
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    ip: str,
    per_10min: int,
    per_day_ip: int,
    per_day_all: int,
) -> None:
    """Raise ``DemoLimitError`` with a friendly message if this question would go over."""
    used = await pool.fetchrow(
        """
        SELECT u.used + u.reserved AS spent, b.daily_tokens
        FROM tenant_budgets b
        LEFT JOIN tenant_usage_daily u
               ON u.tenant_id = b.tenant_id AND u.day = (now() AT TIME ZONE 'UTC')::date
        WHERE b.tenant_id = $1
        """,
        tenant_id,
    )
    if used is not None and used["spent"] is not None and used["spent"] >= used["daily_tokens"]:
        raise DemoLimitError(
            "The demo has used today's AI budget. Browse the existing reviews and answers, "
            "or come back tomorrow (UTC).",
            _seconds_to_utc_midnight(),
        )
    if wait := await _hit(redis, f"demo:qa:ip10m:{ip}", per_10min, 600):
        raise DemoLimitError(
            f"You can ask {per_10min} questions every 10 minutes in the demo. "
            "Try again in a few minutes.",
            wait,
        )
    if wait := await _hit(redis, f"demo:qa:ipday:{ip}", per_day_ip, 86400):
        raise DemoLimitError(
            f"You have asked the {per_day_ip} questions the demo allows per day. "
            "Thanks for trying Argus!",
            wait,
        )
    if wait := await _hit(redis, "demo:qa:all", per_day_all, 86400):
        raise DemoLimitError(
            "The demo is very popular today and has reached its question limit. "
            "Please come back tomorrow.",
            wait,
        )


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(UTC)
    return 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
