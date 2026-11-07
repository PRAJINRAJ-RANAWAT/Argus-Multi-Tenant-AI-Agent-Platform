"""LLM judge for review quality (offline evals and online sampling)."""

import json
import re

from argus.llm.client import ChatModel

JUDGE_PROMPT = """You are an evaluation judge for code reviews. Given the issues a PR is known
to contain and the review that was posted, rate the review from 1 (useless or wrong) to
5 (finds every known issue, no noise). Reply with JSON {"score": n, "reason": "..."}."""


async def judge(llm: ChatModel, expected: list[str], review: str) -> int:
    payload = json.dumps({"expected": expected, "review": review})
    res = await llm.chat(
        [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"<judge_input>{payload}</judge_input>"},
        ],
        max_tokens=200,
        temperature=0,
    )
    try:
        return int(json.loads(res.content)["score"])
    except (ValueError, KeyError, TypeError):
        m = re.search(r"[1-5]", res.content)
        return int(m.group(0)) if m else 1
