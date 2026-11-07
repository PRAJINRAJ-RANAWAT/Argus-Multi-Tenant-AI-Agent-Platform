"""Rule-based prompt-injection classifier for untrusted text (PR bodies, diffs, code
comments, questions, tool results).

Each rule has a weight; the score is the sum of matched weights. ``flagged`` at
``THRESHOLD``. Rules target the intent of an injection (override instructions, change
role, exfiltrate secrets, call tools, escape the data wrapper, hide text) rather than
specific strings, and are measured by the injection eval set. An LLM classifier can be
layered on top through the gateway's small tier.
"""

import re
import unicodedata
from dataclasses import dataclass, field

THRESHOLD = 3

Rule = tuple[str, int, re.Pattern[str]]


def _rule(name: str, weight: int, *parts: str) -> Rule:
    return (name, weight, re.compile("".join(parts), re.I))


RULES: list[Rule] = [
    _rule(
        "override_instructions",
        3,
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|all|",
        r"your|the|any)\b[^.\n]{0,30}\b(instructions?|rules|guidelines|prompt|directives?|",
        r"system message)",
    ),
    _rule(
        "override_multilingual",
        3,
        r"\b(ignora|ignore[sz]?|ignoriere|ignorar|negeer|ignoruj)\b[^.\n]{0,40}\b(instrucciones|",
        r"instructions|anweisungen|istruzioni|instruções|instructies|instrukcje|consignes)\b",
    ),
    _rule(
        "new_instructions",
        2,
        r"\b(new|updated|secret)\s+(instructions?|orders|directives?)\b",
    ),
    _rule(
        "real_task",
        2,
        r"\b(the\s+)?(real|actual|true|hidden)\s+(task|goal|job|purpose|instructions?)\b",
    ),
    _rule(
        "role_change",
        2,
        r"\b(you are now|act as|pretend (to be|you are)|from now on,? you|roleplay as|",
        r"developer mode|jailbreak|DAN\b)",
    ),
    _rule(
        "no_rules",
        2,
        r"\b(without|no|free of)\s+(any\s+)?(rules|restrictions|limits|filters|guardrails)\b",
    ),
    _rule(
        "addressing_the_model",
        2,
        r"\b(dear|hey|attention|note (to|for)|message (for|to)|instructions? for)\s+(the\s+)?",
        r"(ai|assistant|llm|model|bot|argus|reviewer|claude|gpt)\b",
        r"|\b(ai|llm|assistant|argus) (reviewer|agent),? (please|you must)",
    ),
    _rule(
        "approve_or_skip",
        2,
        r"\b(approve|lgtm|merge) (this|the) (pr|pull request|change)|\b(do not|don't|never) ",
        r"(report|mention|flag|comment on)\b|\bmark (it|this) as (safe|approved)",
    ),
    _rule(
        "exfiltrate_secrets",
        3,
        r"\b(print|post|reveal|show|output|include|send|leak|dump|paste|echo)\b[^.\n]{0,60}",
        r"(\.env\b|secret|api[_ -]?key|token|password|credential|private key|deploy key|",
        r"environment variables?)",
    ),
    _rule(
        "enumerate_private",
        3,
        r"\b(list|enumerate|show|print)\b[^.\n]{0,30}\b(all|every|other)\b[^.\n]{0,30}",
        r"\b(private )?(repos?|repositor(y|ies)|tenants?|organi[sz]ations?|users?)\b",
    ),
    _rule(
        "tool_invocation",
        2,
        r"\b(call|invoke|use|run|execute)\s+(the\s+)?(tool|function)\b|\b(open_issue|",
        r"post_review_comment|create_issue|delete_repo|fetch_url|http_get)\s*\(",
    ),
    _rule(
        "exfil_url",
        2,
        r"(https?://[^\s)]+\?[^\s)]*(=|\{))|\b(curl|wget|fetch|send (it|this|them) to)\b",
        r"[^\n]{0,40}https?://",
    ),
    _rule(
        "wrapper_escape",
        4,
        r"</?\s*(untrusted_data|system|assistant|instructions?)\b[^>]*>|\[/?(INST|SYS)\]|",
        r"<\|(im_start|im_end|system|endoftext)\|>",
    ),
    _rule("hidden_text", 2, "[​‌‍⁠⁢-⁤﻿‪-‮]"),
]


@dataclass(frozen=True)
class Verdict:
    score: int
    rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def flagged(self) -> bool:
        return self.score >= THRESHOLD


def _normalise(text: str) -> str:
    # NFKC folds look-alike characters (full-width letters, ligatures) before matching.
    return unicodedata.normalize("NFKC", text)


def classify(*texts: str) -> Verdict:
    matched: list[str] = []
    score = 0
    for raw in texts:
        if not raw:
            continue
        text = _normalise(raw)
        for name, weight, pattern in RULES:
            if name not in matched and (pattern.search(text) or pattern.search(raw)):
                matched.append(name)
                score += weight
    return Verdict(score, tuple(matched))


_WRAPPER = re.compile(r"<\s*/?\s*untrusted_data[^>]*>", re.I)


def neutralise_wrapper(text: str) -> str:
    """Stop untrusted text from closing (or opening) the data wrapper around it."""
    return _WRAPPER.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)
