"""``argus-code``: read-only tools over the repository's code.

``search_code`` and ``find_callers`` query the code index (eventually consistent with
the default branch); ``read_file`` and the PR tools read GitHub at the run's commit.
"""

import posixpath
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from argus.mcp_servers import RunContext
from argus.rag.search import LexicalReranker, hybrid_search
from argus.rag.search import find_callers as find_callers_q

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def safe_path(path: str) -> str:
    """Normalise a repo-relative path; reject absolute paths and traversal."""
    if not path or len(path) > 500 or "\x00" in path:
        raise ToolError("invalid path")
    norm = posixpath.normpath(path.strip().lstrip("/"))
    if norm.startswith("..") or norm == ".":
        raise ToolError("path must stay inside the repository")
    return norm


def build_code_server(ctx: RunContext) -> MCPServer:
    server = MCPServer(
        "argus-code",
        instructions=f"Read-only access to {ctx.repo} at {ctx.head_sha[:12]}.",
    )

    @server.tool(annotations=READ)
    async def get_pr_diff() -> str:
        """Unified diff of the pull request under review (may be truncated)."""
        diff = await ctx.github.get_pr_diff(ctx.installation_id, ctx.repo, ctx.pr_number)
        if len(diff) > ctx.max_diff_chars:
            return diff[: ctx.max_diff_chars] + "\n[diff truncated]"
        return diff

    @server.tool(annotations=READ)
    async def list_changed_files() -> list[dict[str, Any]]:
        """Files changed by the pull request, with their change status."""
        files = await ctx.github.list_pr_files(ctx.installation_id, ctx.repo, ctx.pr_number)
        return [{"path": f.get("filename"), "status": f.get("status")} for f in files]

    @server.tool(annotations=READ)
    async def read_file(path: str) -> str:
        """Contents of a file in the repository at the PR's head commit."""
        clean = safe_path(path)
        content = await ctx.github.get_file(ctx.installation_id, ctx.repo, clean, ctx.head_sha)
        if content is None:
            raise ToolError(f"file not found: {clean}")
        if len(content) > ctx.max_file_chars:
            return content[: ctx.max_file_chars] + "\n[file truncated]"
        return content

    @server.tool(annotations=READ)
    async def search_code(query: str, k: int = 8) -> list[dict[str, Any]]:
        """Hybrid (keyword + semantic) search over the repository's indexed code.
        Returns chunks with a citation (path:start-end@commit) and a GitHub link."""
        if ctx.repo_id is None or ctx.embedder is None:
            raise ToolError("code index is not available for this repository")
        if not query.strip() or len(query) > 500:
            raise ToolError("query must be 1-500 characters")
        hits = await hybrid_search(
            ctx.pool,
            ctx.embedder,
            tenant_id=ctx.tenant_id,
            repo_ids=[ctx.repo_id],
            query=query,
            k=max(1, min(k, 12)),
            reranker=LexicalReranker(),
        )
        return [h.to_dict() for h in hits]

    @server.tool(annotations=READ)
    async def find_callers(symbol: str) -> list[dict[str, Any]]:
        """Call sites of a function or method (``name`` or ``Class.method``) in the index."""
        if ctx.repo_id is None:
            raise ToolError("code index is not available for this repository")
        sites = await find_callers_q(
            ctx.pool, tenant_id=ctx.tenant_id, repo_id=ctx.repo_id, symbol=symbol
        )
        return [
            {"citation": f"{s.path}:{s.line}@{s.commit_sha[:12]}", "caller": s.caller_symbol}
            for s in sites
        ]

    return server
