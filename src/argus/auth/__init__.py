"""Identity and authorization.

* ``Principal``: who is calling (a user via session, or an API key), in which tenant,
  with which Argus role.
* Effective permission = the role allows the action **and** (for a user) GitHub says
  the user can read the repository. A member without access to a private repo in
  GitHub cannot ask Argus about it either.
* Tenant data is read through ``tenant_conn``: a transaction on the ``argus_app`` pool
  with ``app.tenant_id`` set, so row-level security filters every query.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import asyncpg

from argus.redact import redact

ROLES = ("viewer", "member", "admin", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}

# Minimum role per permission.
PERMISSIONS = {
    "runs:read": "viewer",
    "qa:create": "member",
    "approvals:decide": "member",
    "keys:manage": "admin",
    "audit:read": "admin",
    "budgets:manage": "admin",
    "members:manage": "owner",
}

READ_PERMISSIONS = frozenset({"admin", "maintain", "write", "triage", "read"})


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    tenant_login: str
    installation_id: int
    role: str
    user_id: uuid.UUID | None = None
    github_login: str | None = None
    api_key_id: uuid.UUID | None = None
    repo_ids: frozenset[uuid.UUID] | None = None  # API keys may be limited to repos
    demo: bool = False  # public demo visitor: read-only, pinned to the demo tenant

    @property
    def actor_type(self) -> str:
        return "user" if self.user_id is not None else "api_key"

    @property
    def actor_id(self) -> str:
        return str(self.user_id if self.user_id is not None else self.api_key_id)

    def can(self, permission: str) -> bool:
        need = PERMISSIONS[permission]
        return RANK[self.role] >= RANK[need]


@asynccontextmanager
async def tenant_conn(
    app_pool: asyncpg.Pool, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction in which row-level security sees only ``tenant_id``'s rows."""
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, true), set_config('app.user_id', $2, true)",
            str(tenant_id),
            str(user_id) if user_id else "",
        )
        yield cast(asyncpg.Connection, conn)


async def audit(
    conn: asyncpg.Connection | asyncpg.Pool,
    tenant_id: uuid.UUID,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target: str | None,
    decision: str,
    details: dict[str, object] | None = None,
) -> int:
    """Append to the tenant's hash-chained audit log (details are redacted first)."""
    row_id: int = await conn.fetchval(
        "SELECT argus_audit($1, $2, $3, $4, $5, $6, $7::jsonb)",
        tenant_id,
        actor_type,
        actor_id,
        action,
        target,
        decision,
        json.dumps(redact(details)) if details is not None else None,
    )
    return row_id
