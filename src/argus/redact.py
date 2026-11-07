"""Secret redaction for audit details and logs.

Redacts values under sensitive keys and any string that looks like a credential
(GitHub, Slack, OpenAI/Anthropic-style keys, AWS access keys, JWTs, private keys).
M6 extends this into the output scanner for PII as well.
"""

import re
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|credential)",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bargus_[a-z0-9]{8}_[A-Za-z0-9_\-]{20,}"),
]


def redact_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, *, max_str: int = 2000) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if SENSITIVE_KEYS.search(str(k)) else redact(v, max_str=max_str)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, max_str=max_str) for v in value]
    if isinstance(value, str):
        out = redact_text(value)
        return out if len(out) <= max_str else out[:max_str] + "…"
    return value
