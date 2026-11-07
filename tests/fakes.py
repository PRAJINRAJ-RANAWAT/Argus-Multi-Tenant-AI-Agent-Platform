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
    files: dict[str, str] = field(
        default_factory=lambda: {"app.py": "def retry():\n    return 3\n"}
    )
    ci_state: str = "success"
    head_sha: str = "c" * 40
    file_reads: list[str] = field(default_factory=list)
    # OAuth: code -> user {"id", "login", "name", "installations": [(id, login, type)],
    # "org_roles": {org: role}}; user tokens are "gho_<login>".
    oauth_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    oauth_users: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (repo full name, login) -> admin / write / read / none
    repo_permissions: dict[tuple[str, str], str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    fail_diff_with: int | None = None  # HTTP status to return for the next diff fetch
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
        if path == "/login/oauth/access_token":
            code = dict(httpx.QueryParams(request.content.decode())).get("code", "")
            user = self.oauth_codes.pop(code, None)
            if user is None:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            self.oauth_users[user["login"]] = user
            return httpx.Response(200, json={"access_token": f"gho_{user['login']}"})
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer gho_"):
            user = self.oauth_users.get(auth.removeprefix("Bearer gho_"))
            if user is None:
                return httpx.Response(401, json={"message": "Bad credentials"})
            if path == "/user":
                return httpx.Response(200, json={k: user[k] for k in ("id", "login", "name")})
            if path == "/user/installations":
                items = [
                    {"id": i, "account": {"login": a, "type": t}}
                    for i, a, t in user["installations"]
                ]
                return httpx.Response(200, json={"installations": items})
            if m := re.fullmatch(r"/user/memberships/orgs/([^/]+)", path):
                role = user.get("org_roles", {}).get(m.group(1))
                if role is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"role": role, "state": "active"})
            return httpx.Response(404, json={"message": "fake: no user route"})
        if not auth.startswith("Bearer ghs_fake_"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/collaborators/([^/]+)/permission", path):
            perm = self.repo_permissions.get((m.group(1), m.group(2)), "none")
            return httpx.Response(200, json={"permission": perm, "role_name": perm})
        if re.fullmatch(r"/repos/([^/]+/[^/]+)/git/trees/([^/]+)", path):
            tree = [
                {"path": p, "type": "blob", "size": len(c.encode())} for p, c in self.files.items()
            ]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if re.fullmatch(r"/repos/([^/]+/[^/]+)/branches/([^/]+)", path):
            return httpx.Response(200, json={"commit": {"sha": self.head_sha}})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)", path):
            return httpx.Response(
                200, json={"full_name": m.group(1), "default_branch": "main", "private": True}
            )
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/(.+)", path):
            self.file_reads.append(m.group(2))
            content = self.files.get(m.group(2))
            if content is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, text=content)
        if re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)/files", path):
            return httpx.Response(
                200, json=[{"filename": f, "status": "modified"} for f in self.files]
            )
        if re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([^/]+)/status", path):
            return httpx.Response(200, json={"state": self.ci_state, "statuses": []})
        if re.fullmatch(r"/repos/([^/]+/[^/]+)/issues", path) and request.method == "POST":
            issue = {"id": next(self._ids), **json.loads(request.content)}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", path):
            if "diff" in request.headers.get("accept", "") and self.fail_diff_with:
                status, self.fail_diff_with = self.fail_diff_with, None
                return httpx.Response(status, json={"message": "injected"})
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
