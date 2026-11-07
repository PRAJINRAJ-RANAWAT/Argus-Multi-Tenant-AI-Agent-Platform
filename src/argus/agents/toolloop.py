"""LLM tool-calling loop on top of checkpointed steps.

Each LLM turn and each tool call is a step, so after a crash the conversation is
rebuilt by replaying stored outputs: the LLM is not called again for turns that
finished, and finished tools are not re-run. Denied tool calls are recorded as steps
too (an audit trail of allow/deny decisions) and returned to the model as errors.
Tool output is wrapped as untrusted data before it re-enters the conversation.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from argus.llm.client import LLMClient
from argus.steps import StepOutcome, StepRunner
from argus.tools.hub import ToolDeniedError, ToolHub

log = logging.getLogger("argus.agents.toolloop")


def wrap_untrusted(source: str, text: str) -> str:
    return f'<untrusted_data source="{source}">\n{text}\n</untrusted_data>'


async def run_tool_loop(
    steps: StepRunner,
    llm: LLMClient,
    hub: ToolHub,
    messages: list[dict[str, Any]],
    *,
    between_steps: Callable[[], Awaitable[None]] | None = None,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Run until the model answers without tool calls. Returns that final LLM output
    (``content``, ``model``). Bounded by the run's step and token budgets."""
    specs = hub.model_specs()
    tool_names = [s["function"]["name"] for s in specs]

    while True:

        async def call_llm() -> StepOutcome:
            res = await llm.chat(messages, tools=specs or None, max_tokens=max_tokens)
            return StepOutcome(
                output={
                    "content": res.content,
                    "model": res.model,
                    "tool_calls": [tc.to_dict() for tc in res.tool_calls],
                },
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
            )

        turn: dict[str, Any] = await steps.step(
            "llm_call",
            llm.model,
            {"messages": messages, "tools": tool_names},
            call_llm,
            store_input=False,  # rebuildable from earlier steps; avoids O(n^2) storage
        )
        if between_steps is not None:
            await between_steps()
        calls: list[dict[str, Any]] = turn.get("tool_calls") or []
        if not calls:
            return turn

        messages.append(
            {
                "role": "assistant",
                "content": turn.get("content") or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                    for c in calls
                ],
            }
        )
        for c in calls:
            name, args = str(c["name"]), dict(c["arguments"])

            async def call_tool(name: str = name, args: dict[str, Any] = args) -> StepOutcome:
                try:
                    out = await hub.call(name, args, by_model=True)
                except ToolDeniedError as exc:
                    log.warning("tool denied", extra={"tool": name, "reason": str(exc)})
                    return StepOutcome(
                        output={"text": f"error: {exc}", "is_error": True, "denied": True}
                    )
                return StepOutcome(output={"text": out.text, "is_error": out.is_error})

            result: dict[str, Any] = await steps.step("tool_call", name, args, call_tool)
            if between_steps is not None:
                await between_steps()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": wrap_untrusted(f"tool:{name}", result["text"]),
                }
            )
