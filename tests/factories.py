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
