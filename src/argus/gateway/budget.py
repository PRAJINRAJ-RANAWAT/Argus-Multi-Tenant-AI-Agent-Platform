"""Per-tenant daily token budgets in Postgres.

Before a call, the gateway reserves the estimated tokens; the reservation succeeds only
if ``used + reserved + estimate <= daily_tokens`` (a single conditional UPDATE, so
concurrent calls cannot overspend together). After the call, the estimate is released
and actual usage recorded. Crossing ``alert_pct`` writes one audit entry per day.
Tenants without a budget row are unlimited but still metered.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date

import asyncpg

from argus.auth import audit
from argus.gateway import TenantBudgetExceededError

log = logging.getLogger("argus.gateway.budget")

TODAY = "(now() AT TIME ZONE 'UTC')::date"


@dataclass(frozen=True)
class Reservation:
    tenant_id: uuid.UUID
    day: date
    tokens: int


class Budgets:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def reserve(self, tenant_id: uuid.UUID, estimate: int) -> Reservation:
        async with self.pool.acquire() as conn, conn.transaction():
            day: date = await conn.fetchval(
                f"""
                INSERT INTO tenant_usage_daily (tenant_id, day) VALUES ($1, {TODAY})
                ON CONFLICT (tenant_id, day) DO UPDATE SET day = EXCLUDED.day
                RETURNING day
                """,  # noqa: S608 - constant expression
                tenant_id,
            )
            limit = await conn.fetchval(
                "SELECT daily_tokens FROM tenant_budgets WHERE tenant_id = $1", tenant_id
            )
            if limit is None:
                return Reservation(tenant_id, day, 0)
            ok = await conn.fetchval(
                """
                UPDATE tenant_usage_daily SET reserved = reserved + $3
                WHERE tenant_id = $1 AND day = $2 AND used + reserved + $3 <= $4
                RETURNING true
                """,
                tenant_id,
                day,
                estimate,
                limit,
            )
            if not ok:
                used = await conn.fetchval(
                    "SELECT used FROM tenant_usage_daily WHERE tenant_id = $1 AND day = $2",
                    tenant_id,
                    day,
                )
                raise TenantBudgetExceededError(
                    f"daily token budget exhausted ({used}/{limit} used)"
                )
        return Reservation(tenant_id, day, estimate)

    async def settle(self, r: Reservation, actual_tokens: int, cost_usd: float) -> None:
        row = await self.pool.fetchrow(
            """
            UPDATE tenant_usage_daily u
            SET reserved = greatest(u.reserved - $3, 0), used = u.used + $4,
                cost_usd = u.cost_usd + $5
            WHERE u.tenant_id = $1 AND u.day = $2
            RETURNING u.used, u.alerted_at,
                      (SELECT daily_tokens FROM tenant_budgets b WHERE b.tenant_id = $1) AS lim,
                      (SELECT alert_pct FROM tenant_budgets b WHERE b.tenant_id = $1) AS pct
            """,
            r.tenant_id,
            r.day,
            r.tokens,
            actual_tokens,
            cost_usd,
        )
        if row is None or row["lim"] is None or row["alerted_at"] is not None:
            return
        if row["used"] * 100 >= row["lim"] * row["pct"]:
            first = await self.pool.fetchval(
                """
                UPDATE tenant_usage_daily SET alerted_at = now()
                WHERE tenant_id = $1 AND day = $2 AND alerted_at IS NULL RETURNING true
                """,
                r.tenant_id,
                r.day,
            )
            if first:
                log.warning(
                    "tenant budget alert",
                    extra={"tenant_id": str(r.tenant_id), "used": row["used"], "limit": row["lim"]},
                )
                await audit(
                    self.pool,
                    r.tenant_id,
                    actor_type="system",
                    actor_id="gateway",
                    action="budget.alert",
                    target=str(r.day),
                    decision="info",
                    details={"used": row["used"], "limit": row["lim"], "pct": row["pct"]},
                )
