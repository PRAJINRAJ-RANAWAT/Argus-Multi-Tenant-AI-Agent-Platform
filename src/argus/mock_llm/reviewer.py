"""A deterministic, prompt-sensitive code reviewer for the mock LLM.

Evals need a model whose output depends on the prompt, or a worse prompt could never
fail CI. This reviewer reads the system prompt the way an instruction-following model
would: it only reports a category of finding when the prompt asks for it (bugs,
security, tests), and it adds nitpicks and praise unless the prompt says to skip them.
Findings come from real heuristics on the added lines of the diff and cite
``path:line``. Real-model runs of the same eval suites use provider keys instead.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    title: str
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class Instructions:
    bugs: bool
    security: bool
    tests: bool
    skip_nitpicks: bool
    skip_praise: bool

    @classmethod
    def parse(cls, system: str) -> "Instructions":
        s = system.lower()
        return cls(
            bugs=bool(re.search(r"\bbugs?\b|correctness|logic errors", s)),
            security=bool(re.search(r"securit|vulnerab|injection", s)),
            tests=bool(re.search(r"missing tests|test coverage|untested", s)),
            skip_nitpicks=bool(re.search(r"(skip|no|avoid)[^.]{0,30}nitpick", s)),
            skip_praise=bool(re.search(r"(skip|no|avoid)[^.]{0,30}praise", s)),
        )


@dataclass
class Added:
    path: str
    line: int
    text: str


def added_lines(diff: str) -> list[Added]:
    out: list[Added] = []
    path, new_line = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/").strip()
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 1
            continue
        if raw.startswith("---") or not path:
            continue
        if raw.startswith("+"):
            out.append(Added(path, new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1
    return out


SECURITY = [
    (
        "SQL injection",
        re.compile(r"\.execute\(\s*(f[\"']|[\"'][^\"']*[\"']\s*(\+|%)|[^)]*\.format\()"),
    ),
    (
        "Hardcoded secret",
        re.compile(
            r"(?i)\b\w*(password|passwd|secret|token|api_key|apikey)\w*\s*=\s*[\"'][^\"']{8,}[\"']"
        ),
    ),
    ("Command injection", re.compile(r"shell\s*=\s*True|os\.system\(\s*f[\"']")),
    ("Unsafe deserialization", re.compile(r"pickle\.loads?\(|yaml\.load\((?![^)]*Loader)")),
]
BUGS = [
    ("Off-by-one error", re.compile(r"range\(\s*len\([^)]*\)\s*\+\s*1\s*\)|<=\s*len\(")),
    ("Swallowed exception", re.compile(r"except\s*(Exception\s*)?:\s*(pass)?\s*$")),
    ("Mutable default argument", re.compile(r"def \w+\([^)]*=\s*(\[\]|\{\})")),
]
NONE_GET = re.compile(r"^\s*(\w+)\s*=\s*[\w.]+\.get\(")


def find_issues(diff: str, ins: Instructions) -> list[Finding]:
    lines = added_lines(diff)
    findings: list[Finding] = []
    if ins.security:
        for a in lines:
            for title, pattern in SECURITY:
                if pattern.search(a.text):
                    findings.append(Finding(title, a.path, a.line, f"`{a.text.strip()[:80]}`"))
    if ins.bugs:
        for a in lines:
            for title, pattern in BUGS:
                if pattern.search(a.text):
                    findings.append(Finding(title, a.path, a.line, f"`{a.text.strip()[:80]}`"))
        for i, a in enumerate(lines):
            m = NONE_GET.match(a.text)
            if not m:
                continue
            var = m.group(1)
            for b in lines[i + 1 : i + 4]:
                if b.path != a.path:
                    break
                if re.search(rf"\bif\s+(not\s+)?{var}\b|{var}\s+is\s+(not\s+)?None", b.text):
                    break
                if re.search(rf"\b{var}\.\w+|\b{var}\[", b.text):
                    findings.append(
                        Finding(
                            "Missing None check",
                            a.path,
                            b.line,
                            f"`{var}` may be None (from `.get`)",
                        )
                    )
                    break
    if ins.tests:
        code = {a.path for a in lines if a.path.endswith(".py") and "test" not in a.path}
        touched_tests = any("test" in a.path for a in lines)
        new_defs = [a for a in lines if a.path in code and re.match(r"\s*def \w+", a.text)]
        if new_defs and not touched_tests:
            d = new_defs[0]
            findings.append(
                Finding("Missing tests", d.path, d.line, "new functions are added without tests")
            )
    if not ins.skip_nitpicks:
        for a in lines:
            if len(a.text) > 100:
                findings.append(Finding("Nitpick: long line", a.path, a.line, "over 100 chars"))
            if re.search(r"\b[a-z]+[A-Z]\w*\s*=", a.text):
                findings.append(Finding("Nitpick: naming", a.path, a.line, "use snake_case"))
    return findings


def review(system: str, diff: str) -> str:
    ins = Instructions.parse(system)
    findings = find_issues(diff, ins)
    parts = []
    if not ins.skip_praise:
        parts.append("Nice work on this change!")
    if not findings:
        parts.append("No issues found.")
    else:
        parts.append("### Findings")
        parts.extend(f"- **{f.title}** (`{f.path}:{f.line}`): {f.detail}" for f in findings)
    return "\n".join(parts)
