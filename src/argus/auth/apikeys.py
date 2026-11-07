"""API keys: ``argus_<prefix>_<secret>``, stored as a SHA-256 hash, shown once.

A key belongs to one tenant, carries a role no higher than its creator's, and can be
limited to specific repositories. Revoked keys stop working immediately.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

import asyncpg

from argus.auth import RANK, Principal


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class NewKey:
    id: uuid.UUID
    token: str  # returned once, never stored
    prefix: str


async def create_key(
    conn: asyncpg.Connection,
    creator: Principal,
    *,
    name: str,
    role: str,
    repo_ids: list[uuid.UUID] | None,
) -> NewKey:
    if role not in ("admin", "member", "viewer") or RANK[role] > RANK[creator.role]:
        raise PermissionError(f"cannot create a {role!r} key as {creator.role!r}")
    prefix = secrets.token_hex(4)
    token = f"argus_{prefix}_{secrets.token_urlsafe(32)}"
    key_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO api_keys (tenant_id, name, prefix, key_hash, role, repo_ids, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """,
        creator.tenant_id,
        name,
        prefix,
        hash_key(token),
        role,
        repo_ids,
        creator.user_id,
    )
    return NewKey(key_id, token, prefix)


async def principal_for_key(system_pool: asyncpg.Pool, token: str) -> Principal | None:
    if not token.startswith("argus_") or len(token) > 200:
        return None
    row = await system_pool.fetchrow(
        """
        SELECT k.key_id, k.tenant_id, k.role, k.repo_ids, t.github_account_login,
               t.github_installation_id
        FROM argus_auth_api_key($1) k JOIN tenants t ON t.id = k.tenant_id
        WHERE t.suspended_at IS NULL
        """,
        hash_key(token),
    )
    if row is None:
        return None
    return Principal(
        tenant_id=row["tenant_id"],
        tenant_login=row["github_account_login"],
        installation_id=int(row["github_installation_id"]),
        role=row["role"],
        api_key_id=row["key_id"],
        repo_ids=frozenset(row["repo_ids"]) if row["repo_ids"] is not None else None,
    )
