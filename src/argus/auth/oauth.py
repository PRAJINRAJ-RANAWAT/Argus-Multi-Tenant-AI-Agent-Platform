"""GitHub OAuth login (the GitHub App's user authorization flow) and membership sync.

The user's GitHub token is used only during login: to identify the user, list the
App installations they can access, and read their org role. It is not stored.
Tenant memberships are synced from that list: installations the user lost access to
are removed, so leaving the GitHub org removes Argus access at the next login.
"""

import secrets
import uuid
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import asyncpg
import httpx
from redis.asyncio import Redis

from argus.runs import upsert_tenant

STATE_TTL_S = 600


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str
    name: str | None


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    web_url: str
    api_url: str
    redirect_uri: str


async def begin_login(redis: Redis, cfg: OAuthConfig, next_path: str) -> str:
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"  # no open redirects
    state = secrets.token_urlsafe(32)
    await redis.set(f"oauth_state:{state}", next_path, ex=STATE_TTL_S)
    query = urlencode(
        {"client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "state": state}
    )
    return f"{cfg.web_url.rstrip('/')}/login/oauth/authorize?{query}"


async def consume_state(redis: Redis, state: str) -> str:
    if not state or len(state) > 100:
        raise OAuthError("missing state")
    next_path = cast(str | None, await redis.getdel(f"oauth_state:{state}"))
    if next_path is None:
        raise OAuthError("unknown or expired state")
    return next_path


async def exchange_code(http: httpx.AsyncClient, cfg: OAuthConfig, code: str) -> str:
    resp = await http.post(
        f"{cfg.web_url.rstrip('/')}/login/oauth/access_token",
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "redirect_uri": cfg.redirect_uri,
        },
        headers={"Accept": "application/json"},
    )
    body = resp.json() if resp.status_code == 200 else {}
    token = body.get("access_token")
    if not token:
        raise OAuthError(f"code exchange failed: {body.get('error', resp.status_code)}")
    return str(token)


async def _get(http: httpx.AsyncClient, cfg: OAuthConfig, token: str, path: str) -> Any:
    resp = await http.get(
        f"{cfg.api_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def fetch_identity(
    http: httpx.AsyncClient, cfg: OAuthConfig, token: str
) -> tuple[GitHubUser, list[tuple[int, str, str]]]:
    """The user and their reachable installations as (installation id, account, role)."""
    me = await _get(http, cfg, token, "/user")
    user = GitHubUser(int(me["id"]), str(me["login"]), me.get("name"))
    data = await _get(http, cfg, token, "/user/installations") or {}
    out: list[tuple[int, str, str]] = []
    for inst in data.get("installations", []):
        account = inst["account"]
        login = str(account["login"])
        if account.get("type") == "User":
            role = "owner" if login.lower() == user.login.lower() else "member"
        else:
            m = await _get(http, cfg, token, f"/user/memberships/orgs/{login}") or {}
            active = m.get("state", "active") == "active"
            role = "admin" if active and m.get("role") == "admin" else "member"
        out.append((int(inst["id"]), login, role))
    return user, out


async def sync_user(
    pool: asyncpg.Pool, user: GitHubUser, installations: list[tuple[int, str, str]]
) -> uuid.UUID:
    """Upsert the user and reconcile memberships with what GitHub says now."""
    async with pool.acquire() as conn, conn.transaction():
        user_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO users (github_id, github_login, name, last_login_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (github_id) DO UPDATE
            SET github_login = EXCLUDED.github_login, name = EXCLUDED.name,
                last_login_at = now()
            RETURNING id
            """,
            user.id,
            user.login,
            user.name,
        )
        tenant_ids: list[uuid.UUID] = []
        for installation_id, login, role in installations:
            tenant_id = await upsert_tenant(pool, installation_id, login)
            tenant_ids.append(tenant_id)
            await conn.execute(
                """
                INSERT INTO memberships (tenant_id, user_id, role) VALUES ($1, $2, $3)
                ON CONFLICT (tenant_id, user_id) DO UPDATE
                SET role = EXCLUDED.role, updated_at = now()
                WHERE memberships.source = 'github'
                """,
                tenant_id,
                user_id,
                role,
            )
        await conn.execute(
            "DELETE FROM memberships WHERE user_id = $1 AND tenant_id <> ALL($2::uuid[])",
            user_id,
            tenant_ids,
        )
    return user_id
