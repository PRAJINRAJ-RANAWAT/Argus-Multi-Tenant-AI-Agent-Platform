"""Public API service: GitHub login, tenant-scoped API, dashboard, operator endpoints.

Authentication: a session cookie (GitHub OAuth) or ``Authorization: Bearer argus_…``
(API key). Every tenant read goes through ``tenant_conn`` on the ``argus_app`` pool,
so row-level security filters it even if a query forgets its ``WHERE``. Repository
access additionally requires the user's own GitHub permission. Allow and deny
decisions on sensitive actions are written to the audit log.
"""

import asyncio
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from argus.agents.qa import QAError, create_qa_run
from argus.approvals import decide
from argus.auth import RANK, Principal, audit, tenant_conn
from argus.auth.apikeys import create_key, principal_for_key
from argus.auth.oauth import (
    OAuthConfig,
    OAuthError,
    begin_login,
    consume_state,
    exchange_code,
    fetch_identity,
    sync_user,
)
from argus.auth.repoperm import RepoAccess, RepoInfo
from argus.auth.sessions import COOKIE, create_session, delete_session, get_session
from argus.config import Settings, get_settings
from argus.db.pool import APP_ROLE, create_pool
from argus.github.auth import InstallationTokenProvider
from argus.github.client import GitHubClient, TokenGetter
from argus.health import Resources
from argus.queue.dlq import list_dead, replay, replay_all
from argus.queue.topics import ALL_STREAMS
from argus.service import build_app

from .events import RunEvents

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
SSE_HEARTBEAT_S = 15.0


def _resources(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


def _x(request: Request, name: str) -> Any:
    return _resources(request).extras[name]


# --- authentication -----------------------------------------------------------------


async def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_argus_tenant: Annotated[str | None, Header()] = None,
    argus_session: Annotated[str | None, Cookie()] = None,
) -> Principal:
    res = _resources(request)
    if authorization:
        token = authorization.removeprefix("Bearer ").strip()
        principal = await principal_for_key(res.db, token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        return principal
    session = await get_session(res.redis, argus_session or "")
    if session is None:
        raise HTTPException(status_code=401, detail="not signed in")
    rows = await res.db.fetch("SELECT * FROM argus_user_memberships($1)", session.user_id)
    if not rows:
        raise HTTPException(status_code=403, detail="no Argus installation for this user")
    if x_argus_tenant:
        rows = [r for r in rows if r["account_login"].lower() == x_argus_tenant.lower()]
        if not rows:
            raise HTTPException(status_code=403, detail="not a member of that tenant")
    elif len(rows) > 1:
        raise HTTPException(status_code=400, detail="choose a tenant with X-Argus-Tenant")
    row = rows[0]
    return Principal(
        tenant_id=row["tenant_id"],
        tenant_login=row["account_login"],
        installation_id=int(row["installation_id"]),
        role=row["role"],
        user_id=session.user_id,
        github_login=session.github_login,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def _deny(request: Request, p: Principal, action: str, target: str | None) -> None:
    await audit(
        _resources(request).db,
        p.tenant_id,
        actor_type=p.actor_type,
        actor_id=p.actor_id,
        action=action,
        target=target,
        decision="deny",
    )


async def require(request: Request, p: Principal, permission: str, action: str) -> None:
    if not p.can(permission):
        await _deny(request, p, action, None)
        raise HTTPException(status_code=403, detail=f"{permission} requires a higher role")


async def _repo_for(
    request: Request,
    p: Principal,
    conn: Any,
    *,
    full_name: str | None = None,
    repo_id: uuid.UUID | None = None,
    action: str,
) -> RepoInfo:
    """Load a repo under RLS and check the caller can read it. Unknown and forbidden
    look the same (404), so existence of private repos is not revealed."""
    row = await conn.fetchrow(
        "SELECT id, full_name, private FROM repos WHERE full_name = $1 OR id = $2",
        full_name,
        repo_id,
    )
    repo = RepoInfo(row["id"], row["full_name"], row["private"]) if row else None
    access: RepoAccess = _x(request, "repo_access")
    if repo is None or not await access.can_read(p, repo):
        if repo is not None:
            await _deny(request, p, action, repo.full_name)
        raise HTTPException(status_code=404, detail="repository not found")
    return repo


# --- operator endpoints (DLQ) --------------------------------------------------------


def require_admin_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    token = _resources(request).settings.admin_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=404)  # admin API disabled
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _stream(topic: str) -> str:
    if topic not in ALL_STREAMS:
        raise HTTPException(status_code=404, detail=f"unknown topic {topic!r}")
    return topic


class ReplayRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    all: bool = False
    limit: int = Field(default=1000, ge=1, le=10_000)


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_token)])


@admin.get("/dlq/{topic}")
async def get_dlq(
    request: Request,
    topic: str,
    count: Annotated[int, Query(ge=1, le=500)] = 50,
    after: str = "-",
) -> dict[str, object]:
    stream = _stream(topic)
    items = await list_dead(_resources(request).redis, stream, count=count, after=after)
    return {"topic": stream, "items": [i.__dict__ for i in items]}


@admin.post("/dlq/{topic}/replay")
async def post_replay(request: Request, topic: str, body: ReplayRequest) -> dict[str, object]:
    stream = _stream(topic)
    redis = _resources(request).redis
    if body.all:
        return {"topic": stream, "replayed": await replay_all(redis, stream, limit=body.limit)}
    if not body.ids:
        raise HTTPException(status_code=422, detail="pass ids or all=true")
    done = await replay(redis, stream, body.ids)
    return {
        "topic": stream,
        "replayed": sum(1 for v in done.values() if v is not None),
        "results": done,
    }


# --- login ---------------------------------------------------------------------------

auth = APIRouter(prefix="/auth")


def _oauth(request: Request) -> OAuthConfig:
    s = _resources(request).settings
    if not s.github_oauth_client_id:
        raise HTTPException(status_code=503, detail="GitHub login is not configured")
    return OAuthConfig(
        client_id=s.github_oauth_client_id,
        client_secret=s.github_oauth_client_secret.get_secret_value(),
        web_url=s.github_web_url,
        api_url=s.github_api_url,
        redirect_uri=f"{s.public_url.rstrip('/')}/auth/github/callback",
    )


@auth.get("/github/login")
async def login(request: Request, next: str = "/dashboard") -> RedirectResponse:
    url = await begin_login(_resources(request).redis, _oauth(request), next)
    return RedirectResponse(url, status_code=302)


@auth.get("/github/callback")
async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    res = _resources(request)
    cfg = _oauth(request)
    http: httpx.AsyncClient = _x(request, "http")
    try:
        next_path = await consume_state(res.redis, state)
        token = await exchange_code(http, cfg, code)
        user, installations = await fetch_identity(http, cfg, token)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user_id = await sync_user(res.db, user, installations)
    for row in await res.db.fetch("SELECT * FROM argus_user_memberships($1)", user_id):
        await audit(
            res.db,
            row["tenant_id"],
            actor_type="user",
            actor_id=str(user_id),
            action="auth.login",
            target=user.login,
            decision="allow",
            details={"role": row["role"]},
        )
    session_id = await create_session(res.redis, user_id, user.login, res.settings.session_ttl_s)
    resp = RedirectResponse(next_path, status_code=302)
    resp.set_cookie(
        COOKIE,
        session_id,
        max_age=res.settings.session_ttl_s,
        httponly=True,
        secure=res.settings.cookie_secure,
        samesite="lax",
    )
    return resp


@auth.post("/logout")
async def logout(
    request: Request, argus_session: Annotated[str | None, Cookie()] = None
) -> Response:
    if argus_session:
        await delete_session(_resources(request).redis, argus_session)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE)
    return resp


# --- tenant API ----------------------------------------------------------------------

api = APIRouter(prefix="/api")


@api.get("/me")
async def me(request: Request, p: CurrentPrincipal) -> dict[str, object]:
    tenants = []
    if p.user_id is not None:
        rows = await _resources(request).db.fetch(
            "SELECT * FROM argus_user_memberships($1)", p.user_id
        )
        tenants = [{"login": r["account_login"], "role": r["role"]} for r in rows]
    return {
        "actor": {"type": p.actor_type, "id": p.actor_id, "github_login": p.github_login},
        "tenant": {"login": p.tenant_login, "role": p.role},
        "tenants": tenants,
    }


class QARequest(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    question: str = Field(min_length=1, max_length=2000)


@api.post("/qa", status_code=202)
async def post_qa(
    request: Request,
    p: CurrentPrincipal,
    body: QARequest,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> dict[str, object]:
    await require(request, p, "qa:create", "qa.create")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        repo = await _repo_for(request, p, conn, full_name=body.repo, action="qa.create")
        try:
            run_id, created = await create_qa_run(
                conn,
                tenant_id=p.tenant_id,
                repo_id=repo.id,
                question=body.question,
                requested_by=f"{p.actor_type}:{p.actor_id}",
                idempotency_key=idempotency_key,
                role=p.role,
            )
        except QAError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if created:
            await audit(
                conn,
                p.tenant_id,
                actor_type=p.actor_type,
                actor_id=p.actor_id,
                action="qa.create",
                target=repo.full_name,
                decision="allow",
                details={"run_id": str(run_id)},
            )
    return {"run_id": str(run_id), "created": created, "status_url": f"/api/runs/{run_id}"}


def _run_json(row: Any, repo: str | None) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "agent_type": row["agent_type"],
        "repo": repo,
        "pr_number": row["pr_number"],
        "status": row["status"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
        "steps": row["steps"],
        "tokens": {"input": row["input_tokens"], "output": row["output_tokens"]},
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


_RUN_SELECT = """
    SELECT r.id, r.agent_type, r.repo_id, r.pr_number, r.status, r.result::text AS result,
           r.error, r.input_tokens, r.output_tokens, r.created_at, r.finished_at,
           (SELECT count(*) FROM run_steps s WHERE s.run_id = r.id) AS steps,
           p.full_name, p.private
    FROM runs r LEFT JOIN repos p ON p.id = r.repo_id
"""


async def _visible(request: Request, p: Principal, rows: list[Any]) -> list[Any]:
    access: RepoAccess = _x(request, "repo_access")
    allowed: dict[uuid.UUID | None, bool] = {None: p.repo_ids is None}
    out = []
    for r in rows:
        rid = r["repo_id"]
        if rid not in allowed:
            allowed[rid] = await access.can_read(p, RepoInfo(rid, r["full_name"], r["private"]))
        if allowed[rid]:
            out.append(r)
    return out


@api.get("/runs")
async def list_runs(
    request: Request, p: CurrentPrincipal, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> dict[str, object]:
    await require(request, p, "runs:read", "runs.list")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(_RUN_SELECT + " ORDER BY r.created_at DESC LIMIT $1", limit)
    rows = await _visible(request, p, rows)
    return {"runs": [_run_json(r, r["full_name"]) for r in rows]}


@api.get("/runs/stream")
async def stream_runs(request: Request, p: CurrentPrincipal) -> StreamingResponse:
    await require(request, p, "runs:read", "runs.stream")
    events: RunEvents = _x(request, "events")
    access: RepoAccess = _x(request, "repo_access")
    app_db = _x(request, "app_db")

    async def can_see(repo_id: str | None) -> bool:
        if repo_id is None:
            return p.repo_ids is None
        async with tenant_conn(app_db, p.tenant_id, p.user_id) as conn:
            row = await conn.fetchrow(
                "SELECT id, full_name, private FROM repos WHERE id = $1", uuid.UUID(repo_id)
            )
        return row is not None and await access.can_read(
            p, RepoInfo(row["id"], row["full_name"], row["private"])
        )

    async def body() -> AsyncIterator[bytes]:
        seen: dict[str | None, bool] = {}
        async with events.subscribe(p.tenant_id) as queue:
            yield b": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield b": ping\n\n"
                    continue
                rid = event.get("repo_id")
                if rid not in seen:
                    seen[rid] = await can_see(rid)
                if seen[rid]:
                    yield f"event: run\ndata: {json.dumps(event)}\n\n".encode()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/runs/{run_id}")
async def get_run(request: Request, p: CurrentPrincipal, run_id: uuid.UUID) -> dict[str, object]:
    await require(request, p, "runs:read", "runs.get")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        row = await conn.fetchrow(_RUN_SELECT + " WHERE r.id = $1", run_id)
    if row is None or not await _visible(request, p, [row]):
        raise HTTPException(status_code=404, detail="run not found")
    return _run_json(row, row["full_name"])


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="member", pattern="^(admin|member|viewer)$")
    repos: list[str] | None = Field(default=None, max_length=100)


@api.post("/keys", status_code=201)
async def post_key(request: Request, p: CurrentPrincipal, body: KeyRequest) -> dict[str, object]:
    await require(request, p, "keys:manage", "keys.create")
    if p.user_id is None:
        raise HTTPException(status_code=403, detail="API keys cannot create API keys")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        repo_ids = None
        if body.repos is not None:
            repo_ids = [
                (await _repo_for(request, p, conn, full_name=r, action="keys.create")).id
                for r in body.repos
            ]
        try:
            key = await create_key(conn, p, name=body.name, role=body.role, repo_ids=repo_ids)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action="keys.create",
            target=str(key.id),
            decision="allow",
            details={"name": body.name, "role": body.role, "repos": body.repos},
        )
    return {
        "id": str(key.id),
        "token": key.token,
        "prefix": key.prefix,
        "note": "Store this token now; it is not shown again.",
    }


@api.get("/keys")
async def list_keys(request: Request, p: CurrentPrincipal) -> dict[str, object]:
    await require(request, p, "keys:manage", "keys.list")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(
            "SELECT id, name, prefix, role, created_at, last_used_at, revoked_at"
            " FROM api_keys ORDER BY created_at DESC"
        )
    return {
        "keys": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "prefix": r["prefix"],
                "role": r["role"],
                "created_at": r["created_at"].isoformat(),
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                "revoked": r["revoked_at"] is not None,
            }
            for r in rows
        ]
    }


@api.delete("/keys/{key_id}", status_code=204)
async def revoke_key(request: Request, p: CurrentPrincipal, key_id: uuid.UUID) -> Response:
    await require(request, p, "keys:manage", "keys.revoke")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        tag = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
            key_id,
        )
        if tag != "UPDATE 1":
            raise HTTPException(status_code=404, detail="key not found")
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action="keys.revoke",
            target=str(key_id),
            decision="allow",
        )
    return Response(status_code=204)


@api.get("/members")
async def list_members(request: Request, p: CurrentPrincipal) -> dict[str, object]:
    await require(request, p, "keys:manage", "members.list")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch("SELECT user_id, role, source FROM memberships ORDER BY created_at")
    return {
        "members": [
            {"user_id": str(r["user_id"]), "role": r["role"], "source": r["source"]} for r in rows
        ]
    }


class RoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|admin|member|viewer)$")


@api.put("/members/{user_id}")
async def set_role(
    request: Request, p: CurrentPrincipal, user_id: uuid.UUID, body: RoleRequest
) -> dict[str, object]:
    await require(request, p, "members:manage", "members.set_role")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        owners = await conn.fetchval("SELECT count(*) FROM memberships WHERE role = 'owner'")
        current = await conn.fetchval("SELECT role FROM memberships WHERE user_id = $1", user_id)
        if current is None:
            raise HTTPException(status_code=404, detail="member not found")
        if current == "owner" and body.role != "owner" and owners <= 1:
            raise HTTPException(status_code=409, detail="a tenant needs at least one owner")
        await conn.execute(
            "UPDATE memberships SET role = $2, source = 'manual', updated_at = now()"
            " WHERE user_id = $1",
            user_id,
            body.role,
        )
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action="members.set_role",
            target=str(user_id),
            decision="allow",
            details={"from": current, "to": body.role},
        )
    return {"user_id": str(user_id), "role": body.role}


@api.get("/audit")
async def list_audit(
    request: Request, p: CurrentPrincipal, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, object]:
    await require(request, p, "audit:read", "audit.read")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(
            "SELECT id, at, actor_type, actor_id, action, target, decision,"
            " details::text AS details, hash FROM audit_log ORDER BY id DESC LIMIT $1",
            limit,
        )
        broken = await conn.fetchval("SELECT argus_audit_verify($1)", p.tenant_id)
    return {
        "chain_intact": broken is None,
        "first_broken_id": broken,
        "entries": [
            {
                "id": r["id"],
                "at": r["at"].isoformat(),
                "actor": f"{r['actor_type']}:{r['actor_id']}",
                "action": r["action"],
                "target": r["target"],
                "decision": r["decision"],
                "details": json.loads(r["details"]) if r["details"] else None,
                "hash": r["hash"],
            }
            for r in rows
        ],
    }


WRITE_TOOLS = frozenset({"post_review_comment", "open_issue"})


class SettingsRequest(BaseModel):
    require_approval_for: list[str] = Field(default_factory=list, max_length=20)


@api.put("/settings")
async def put_settings(
    request: Request, p: CurrentPrincipal, body: SettingsRequest
) -> dict[str, object]:
    await require(request, p, "budgets:manage", "settings.set")
    unknown = set(body.require_approval_for) - WRITE_TOOLS
    if unknown:
        raise HTTPException(status_code=422, detail=f"not write tools: {sorted(unknown)}")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        await conn.execute(
            """
            INSERT INTO tenant_settings (tenant_id, require_approval_for) VALUES ($1, $2)
            ON CONFLICT (tenant_id) DO UPDATE
            SET require_approval_for = EXCLUDED.require_approval_for, updated_at = now()
            """,
            p.tenant_id,
            sorted(set(body.require_approval_for)),
        )
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action="settings.set",
            target=None,
            decision="allow",
            details=body.model_dump(),
        )
    return {"require_approval_for": sorted(set(body.require_approval_for))}


@api.get("/approvals")
async def list_approvals(
    request: Request, p: CurrentPrincipal, status: str = "pending"
) -> dict[str, object]:
    await require(request, p, "runs:read", "approvals.list")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.run_id, a.tool, a.preview::text AS preview, a.status,
                   a.requested_at, a.decided_by, r.repo_id, p.full_name, p.private
            FROM approvals a JOIN runs r ON r.id = a.run_id LEFT JOIN repos p ON p.id = r.repo_id
            WHERE a.status = $1 ORDER BY a.requested_at
            """,
            status,
        )
    rows = await _visible(request, p, rows)
    return {
        "approvals": [
            {
                "id": str(r["id"]),
                "run_id": str(r["run_id"]),
                "tool": r["tool"],
                "repo": r["full_name"],
                "status": r["status"],
                "preview": json.loads(r["preview"]),
                "requested_at": r["requested_at"].isoformat(),
                "decided_by": r["decided_by"],
            }
            for r in rows
        ]
    }


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=500)


@api.post("/approvals/{approval_id}")
async def decide_approval(
    request: Request, p: CurrentPrincipal, approval_id: uuid.UUID, body: DecisionRequest
) -> dict[str, object]:
    await require(request, p, "approvals:decide", "approvals.decide")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT a.status, r.repo_id, p.full_name, p.private
            FROM approvals a JOIN runs r ON r.id = a.run_id LEFT JOIN repos p ON p.id = r.repo_id
            WHERE a.id = $1
            """,
            approval_id,
        )
        if row is None or not await _visible(request, p, [row]):
            raise HTTPException(status_code=404, detail="approval not found")
        run_id = await decide(
            conn,
            approval_id,
            approve=body.decision == "approve",
            actor=f"{p.actor_type}:{p.actor_id}",
            reason=body.reason,
        )
        if run_id is None:
            raise HTTPException(status_code=409, detail=f"already {row['status']}")
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action=f"approval.{body.decision}",
            target=str(approval_id),
            decision="allow",
            details={"run_id": str(run_id), "reason": body.reason},
        )
    return {"approval_id": str(approval_id), "run_id": str(run_id), "decision": body.decision}


class BudgetRequest(BaseModel):
    daily_tokens: int = Field(gt=0, le=10_000_000_000)
    alert_pct: int = Field(default=80, ge=1, le=100)


@api.put("/budget")
async def put_budget(request: Request, p: CurrentPrincipal, body: BudgetRequest) -> dict[str, int]:
    await require(request, p, "budgets:manage", "budget.set")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        await conn.execute(
            """
            INSERT INTO tenant_budgets (tenant_id, daily_tokens, alert_pct) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id) DO UPDATE
            SET daily_tokens = EXCLUDED.daily_tokens, alert_pct = EXCLUDED.alert_pct,
                updated_at = now()
            """,
            p.tenant_id,
            body.daily_tokens,
            body.alert_pct,
        )
        await audit(
            conn,
            p.tenant_id,
            actor_type=p.actor_type,
            actor_id=p.actor_id,
            action="budget.set",
            target=None,
            decision="allow",
            details=body.model_dump(),
        )
    return body.model_dump()


@api.get("/usage")
async def get_usage(
    request: Request, p: CurrentPrincipal, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, object]:
    await require(request, p, "runs:read", "usage.read")
    async with tenant_conn(_x(request, "app_db"), p.tenant_id, p.user_id) as conn:
        budget = await conn.fetchrow("SELECT daily_tokens, alert_pct FROM tenant_budgets")
        daily = await conn.fetch(
            "SELECT day, used, reserved, cost_usd FROM tenant_usage_daily"
            " WHERE day > (now() AT TIME ZONE 'UTC')::date - $1::int ORDER BY day DESC",
            days,
        )
        models = await conn.fetch(
            """
            SELECT model, count(*) AS calls, sum(input_tokens) AS input,
                   sum(output_tokens) AS output, sum(cached_tokens) AS cached,
                   sum(cost_usd) AS cost, count(*) FILTER (WHERE status <> 'ok') AS errors
            FROM llm_calls WHERE created_at > now() - make_interval(days => $1)
            GROUP BY model ORDER BY model
            """,
            days,
        )
    return {
        "budget": dict(budget) if budget else None,
        "daily": [
            {
                "day": r["day"].isoformat(),
                "used": r["used"],
                "reserved": r["reserved"],
                "cost_usd": float(r["cost_usd"]),
            }
            for r in daily
        ],
        "models": [
            {
                "model": r["model"],
                "calls": r["calls"],
                "input_tokens": r["input"],
                "output_tokens": r["output"],
                "cached_tokens": r["cached"],
                "cost_usd": float(r["cost"]),
                "errors": r["errors"],
            }
            for r in models
        ],
    }


# --- app -----------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    github_tokens: TokenGetter | None = None,
) -> FastAPI:
    settings = settings or get_settings().model_copy(update={"service_name": "argus-api"})

    @asynccontextmanager
    async def extras(res: Resources) -> AsyncIterator[None]:
        app_db = await create_pool(settings.database_url, role=APP_ROLE)
        events = RunEvents(settings.database_url)
        await events.start()
        async with httpx.AsyncClient(transport=http_transport, timeout=15.0) as http:
            tokens = github_tokens
            if tokens is None:
                provider = InstallationTokenProvider(
                    http,
                    res.redis,
                    app_id=settings.github_app_id,
                    private_key_pem=settings.github_private_key_pem(),
                    api_url=settings.github_api_url,
                )
                tokens = provider.get
            github = GitHubClient(http, tokens, settings.github_api_url)
            res.extras.update(
                app_db=app_db,
                events=events,
                http=http,
                github=github,
                repo_access=RepoAccess(github, res.redis, settings.repo_permission_cache_s),
            )
            try:
                yield
            finally:
                await events.stop()
                await app_db.close()

    def configure(app: FastAPI) -> None:
        @app.get("/")
        async def root() -> dict[str, str]:
            return {"name": "Argus", "message": "Hello from Argus", "dashboard": "/dashboard"}

        @app.get("/dashboard", response_class=HTMLResponse)
        async def dashboard() -> HTMLResponse:
            return HTMLResponse(DASHBOARD_HTML.read_text())

        app.include_router(admin)
        app.include_router(auth)
        app.include_router(api)

    return build_app(settings, title="Argus API", configure=configure, extra_lifespan=extras)


__all__ = ["RANK", "create_app"]
