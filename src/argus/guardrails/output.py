"""Output scanning before anything leaves Argus (PR comments, Q&A answers).

Secrets (the redaction patterns plus generic assignments of long random values) are
always redacted. PII (emails, phone numbers, card numbers passing Luhn, US SSNs) is
redacted too. Findings are returned so the caller can audit them.
"""

import re
from dataclasses import dataclass, field

from argus.guardrails import SWITCHES
from argus.redact import REDACTED, SECRET_PATTERNS

GENERIC_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY)[A-Z0-9_]*)\s*[:=]\s*"
    r"['\"]?([A-Za-z0-9/+_\-.]{12,})['\"]?"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])(\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
ALLOWED_EMAILS = re.compile(
    r"(noreply|no-reply)@|@(example\.(com|org)|users\.noreply\.github\.com)$"
)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class ScanResult:
    text: str
    findings: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.findings

    def _hit(self, kind: str) -> None:
        self.findings[kind] = self.findings.get(kind, 0) + 1


def scan(text: str, *, pii: bool = True) -> ScanResult:
    result = ScanResult(text)
    if not SWITCHES.output_scan:
        return result

    def sub(kind: str, pattern: re.Pattern[str], repl: str = REDACTED) -> None:
        def _r(m: re.Match[str]) -> str:
            result._hit(kind)
            return repl

        result.text = pattern.sub(_r, result.text)

    for pattern in SECRET_PATTERNS:
        sub("secret", pattern)

    def generic(m: re.Match[str]) -> str:
        result._hit("secret")
        return f"{m.group(1)}={REDACTED}"

    result.text = GENERIC_SECRET.sub(generic, result.text)
    if pii:

        def email(m: re.Match[str]) -> str:
            if ALLOWED_EMAILS.search(m.group(0)):
                return m.group(0)
            result._hit("email")
            return "[EMAIL]"

        result.text = EMAIL.sub(email, result.text)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn(digits):
                result._hit("card")
                return "[CARD]"
            return m.group(0)

        result.text = CARD.sub(card, result.text)
        sub("ssn", SSN, "[SSN]")
        sub("phone", PHONE, "[PHONE]")
    return result
