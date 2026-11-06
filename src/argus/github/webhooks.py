"""GitHub webhook signature verification and event routing."""

import hashlib
import hmac
from typing import Any

from argus.queue.topics import JOBS_REVIEW

SIGNATURE_PREFIX = "sha256="

REVIEW_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def sign(secret: str, body: bytes) -> str:
    """Compute the X-Hub-Signature-256 header value for ``body``."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256. Empty secret never verifies."""
    if not secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(secret, body), header)


def route_github_event(event_type: str, payload: dict[str, Any]) -> str | None:
    """Return the job stream for this event, or None if Argus ignores it."""
    action = payload.get("action")
    if event_type == "pull_request" and action in REVIEW_ACTIONS:
        pr = payload.get("pull_request") or {}
        if pr.get("draft"):
            return None
        return JOBS_REVIEW
    return None
