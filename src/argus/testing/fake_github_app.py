"""The in-memory fake GitHub as an HTTP service, for the local demo stack.

The worker and API point ``ARGUS_GITHUB_API_URL`` at it, so reviews, indexing and
permission checks run end to end without a real GitHub App. ``/_fake/*`` endpoints let
the demo script register PR diffs and repository files and read posted comments.
Never deployed to production.
"""

from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from argus.testing.fakes import FakeGitHub


class PRDiff(BaseModel):
    repo: str
    number: int
    diff: str


class Files(BaseModel):
    files: dict[str, str]


def create_app() -> FastAPI:
    gh = FakeGitHub(files={}, default_permission="admin")  # demo users can read the repo
    app = FastAPI(title="Fake GitHub (demo only)")

    @app.post("/_fake/pr")
    async def add_pr(body: PRDiff) -> dict[str, str]:
        gh.pr_diffs[(body.repo, body.number)] = body.diff
        return {"status": "ok"}

    @app.post("/_fake/files")
    async def set_files(body: Files) -> dict[str, int]:
        gh.files.update(body.files)
        return {"files": len(gh.files)}

    @app.get("/_fake/comments")
    async def comments() -> dict[str, Any]:
        return {f"{repo}#{n}": items for (repo, n), items in gh.comments.items()}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request) -> Response:
        req = httpx.Request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=await request.body(),
        )
        try:
            resp = gh.handle(req)
        except httpx.ReadTimeout:
            return Response(status_code=504)
        return Response(
            resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    return app
