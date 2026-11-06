"""In-memory fakes for external services."""

import itertools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

GITHUB_API = "https://api.github.test"


@dataclass
class FakeGitHub:
    """Enough of the GitHub REST API for the review flow, with failure hooks."""

    diff: str = "diff --git a/app.py b/app.py\n+def retry():\n+    return 3\n"
    comments: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    tokens_minted: int = 0
    token_ttl: timedelta = timedelta(hours=1)
    fail_next_comment_after_create: bool = False
    calls: list[str] = field(default_factory=list)
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1000))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def comments_for(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.comments.setdefault((repo, number), [])

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            assert request.headers["authorization"].startswith("Bearer ")
            self.tokens_minted += 1
            expires = datetime.now(UTC) + self.token_ttl
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_fake_{m.group(1)}_{self.tokens_minted}",
                    "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ghs_fake_"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", path):
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=self.diff)
            return httpx.Response(200, json={"number": int(m.group(2))})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path):
            key = (m.group(1), int(m.group(2)))
            if request.method == "GET":
                page = int(request.url.params.get("page", "1"))
                per = int(request.url.params.get("per_page", "30"))
                items = self.comments_for(*key)[(page - 1) * per : page * per]
                return httpx.Response(200, json=items)
            body = json.loads(request.content)["body"]
            comment = {"id": next(self._ids), "body": body}
            self.comments_for(*key).append(comment)
            if self.fail_next_comment_after_create:
                # The comment was created, but the response never reaches us.
                self.fail_next_comment_after_create = False
                raise httpx.ReadTimeout("connection lost after write", request=request)
            return httpx.Response(201, json=comment)
        return httpx.Response(404, json={"message": f"fake: no route {request.method} {path}"})
