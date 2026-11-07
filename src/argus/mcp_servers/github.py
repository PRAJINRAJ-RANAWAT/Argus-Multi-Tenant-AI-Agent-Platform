"""``argus-github``: CI status (read) and comments/issues (write).

Write tools require an ``idempotency_key`` supplied by the runtime (never by the model:
the runtime strips it from the schema it shows the model). Each write goes through the
side-effect ledger, and comments carry a hidden marker so a crash after GitHub
accepted the call is detected instead of duplicated.
"""

import hashlib
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from argus.mcp_servers import RunContext
from argus.tools.idempotency import perform_once

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

MAX_BODY_CHARS = 60_000


def comment_marker(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return f"<!-- argus:review:{digest} -->"


def _check_key(key: str) -> None:
    if not key or len(key) > 300:
        raise ToolError("idempotency_key is required")


def build_github_server(ctx: RunContext) -> MCPServer:
    server = MCPServer("argus-github", instructions=f"GitHub actions on {ctx.repo}.")

    @server.tool(annotations=READ)
    async def get_ci_status() -> dict[str, Any]:
        """Combined CI status of the PR's head commit."""
        status = await ctx.github.get_combined_status(ctx.installation_id, ctx.repo, ctx.head_sha)
        return {
            "state": status.get("state"),
            "checks": [
                {"context": s.get("context"), "state": s.get("state")}
                for s in status.get("statuses") or []
            ],
        }

    @server.tool(annotations=WRITE)
    async def post_review_comment(body: str, idempotency_key: str) -> dict[str, Any]:
        """Post the review as a comment on the pull request (at most once per key)."""
        _check_key(idempotency_key)
        marker = comment_marker(idempotency_key)
        text = f"{marker}\n{body[:MAX_BODY_CHARS]}"

        async def post() -> str:
            c = await ctx.github.create_issue_comment(
                ctx.installation_id, ctx.repo, ctx.pr_number, text
            )
            return str(c["id"])

        async def find_existing() -> str | None:
            for c in await ctx.github.list_issue_comments(
                ctx.installation_id, ctx.repo, ctx.pr_number
            ):
                if marker in (c.get("body") or ""):
                    return str(c["id"])
            return None

        effect = await perform_once(
            ctx.pool,
            tenant_id=ctx.tenant_id,
            run_id=ctx.run_id,
            key=f"{idempotency_key}:comment",
            kind="github.issue_comment",
            perform=post,
            find_existing=find_existing,
            lease_version=ctx.lease_version,
            stale_after_s=ctx.side_effect_stale_s,
        )
        return {"comment_id": effect.external_id, "reused": not effect.performed}

    @server.tool(annotations=WRITE)
    async def open_issue(title: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """Open an issue in the repository (at most once per key)."""
        _check_key(idempotency_key)

        async def create() -> str:
            issue = await ctx.github.create_issue(
                ctx.installation_id, ctx.repo, title[:250], body[:MAX_BODY_CHARS]
            )
            return str(issue["id"])

        async def find_existing() -> str | None:
            return None  # issues are not marker-searchable yet; the ledger still dedupes

        effect = await perform_once(
            ctx.pool,
            tenant_id=ctx.tenant_id,
            run_id=ctx.run_id,
            key=f"{idempotency_key}:issue",
            kind="github.issue",
            perform=create,
            find_existing=find_existing,
            lease_version=ctx.lease_version,
            stale_after_s=ctx.side_effect_stale_s,
        )
        return {"issue_id": effect.external_id, "reused": not effect.performed}

    return server
