"""Builders for realistic GitHub webhook payloads."""

import json
import uuid
from typing import Any

from argus.github.webhooks import sign


def pr_payload(
    *,
    action: str = "opened",
    installation_id: int = 1001,
    repo_id: int = 5001,
    repo: str = "acme/widgets",
    number: int = 7,
    head_sha: str = "a" * 40,
    private: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    owner = repo.split("/")[0]
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "title": "Add retry to checkout",
            "body": "This PR adds retries.",
            "draft": draft,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "octocat", "id": 1},
        },
        "repository": {
            "id": repo_id,
            "full_name": repo,
            "private": private,
            "owner": {"login": owner, "id": 9001},
        },
        "installation": {"id": installation_id},
        "sender": {"login": "octocat", "id": 1},
    }


def signed_request(
    payload: dict[str, Any],
    secret: str,
    *,
    event: str = "pull_request",
    delivery_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": delivery_id or str(uuid.uuid4()),
        "x-hub-signature-256": sign(secret, body),
    }
    return body, headers


def push_payload(
    *,
    installation_id: int = 1001,
    repo_id: int = 5001,
    repo: str = "acme/widgets",
    after: str = "d" * 40,
    ref: str = "refs/heads/main",
    added: list[str] | None = None,
    modified: list[str] | None = None,
    removed: list[str] | None = None,
    commits: int = 1,
) -> dict[str, Any]:
    owner = repo.split("/")[0]
    commit = {"added": added or [], "modified": modified or [], "removed": removed or []}
    return {
        "ref": ref,
        "before": "0" * 40,
        "after": after,
        "deleted": False,
        "forced": False,
        "commits": [commit] + [{"added": [], "modified": [], "removed": []}] * (commits - 1),
        "repository": {
            "id": repo_id,
            "full_name": repo,
            "private": True,
            "default_branch": "main",
            "owner": {"login": owner, "name": owner, "id": 9001},
        },
        "installation": {"id": installation_id},
        "sender": {"login": "octocat", "id": 1},
    }


def installation_payload(
    *, installation_id: int = 1001, account: str = "acme", repos: list[tuple[int, str]]
) -> dict[str, Any]:
    return {
        "action": "created",
        "installation": {"id": installation_id, "account": {"login": account, "id": 9001}},
        "repositories": [
            {"id": rid, "full_name": name, "name": name.split("/")[1], "private": True}
            for rid, name in repos
        ],
        "sender": {"login": "octocat", "id": 1},
    }
