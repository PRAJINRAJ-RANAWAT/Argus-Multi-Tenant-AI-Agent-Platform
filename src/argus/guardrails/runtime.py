"""Per-run guardrail state: classify untrusted inputs as they arrive and, on the first
likely injection, narrow the run's tools for the rest of the run."""

from collections.abc import Awaitable, Callable

from argus.guardrails import SWITCHES
from argus.guardrails.injection import Verdict, classify
from argus.tools.hub import ToolHub

OnFlag = Callable[[str, Verdict], Awaitable[None]]

WARNING = (
    "> **Note:** Argus found text in this pull request that looks like instructions aimed at "
    "an AI reviewer ({rules}). It was treated as data, and this review ran with a reduced "
    "tool set."
)


class InjectionGuard:
    def __init__(
        self, hub: ToolHub, reduced: frozenset[str], on_flag: OnFlag | None = None
    ) -> None:
        self.hub = hub
        self.reduced = reduced
        self.on_flag = on_flag
        self.rules: list[str] = []
        self.sources: list[str] = []
        self.untrusted: list[str] = []  # everything inspected, for echo detection

    @property
    def flagged(self) -> bool:
        return bool(self.sources)

    async def inspect(self, source: str, *texts: str) -> Verdict:
        self.untrusted.extend(t for t in texts if t)
        if not SWITCHES.classifier:
            return Verdict(0)
        verdict = classify(*texts)
        if verdict.flagged:
            first = not self.flagged
            self.sources.append(source)
            self.rules.extend(r for r in verdict.rules if r not in self.rules)
            if first:
                self.hub.restrict(self.reduced)
                if self.on_flag is not None:
                    await self.on_flag(source, verdict)
        return verdict

    def strip_echoes(self, output: str) -> tuple[str, int]:
        """On a flagged run, drop output lines copied verbatim from untrusted input (an
        injected "say X" that the model parroted). Code blocks are kept: reviews quote
        code legitimately."""
        if not self.flagged or not SWITCHES.classifier:
            return output, 0
        corpus = "\n".join(" ".join(t.split()) for t in self.untrusted)
        kept, removed, in_code = [], 0, False
        for line in output.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
            norm = " ".join(line.split())
            if not in_code and len(norm) >= 12 and norm in corpus:
                removed += 1
                continue
            kept.append(line)
        return "\n".join(kept), removed

    def summary(self) -> dict[str, object]:
        return {"injection_flagged": self.flagged, "rules": self.rules, "sources": self.sources}

    def warning(self) -> str:
        return WARNING.format(rules=", ".join(r.replace("_", " ") for r in self.rules))
