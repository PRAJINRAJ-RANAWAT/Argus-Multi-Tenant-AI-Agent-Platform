"""LLM tool-calling loop on top of checkpointed steps.

Each LLM turn and each tool call is a step, so after a crash the conversation is
rebuilt by replaying stored outputs: the LLM is not called again for turns that
finished, and finished tools are not re-run. Denied tool calls are recorded as steps
too (an audit trail of allow/deny decisions) and returned to the model as errors.
Tool output is wrapped as untrusted data before it re-enters the conversation.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from argus.auth import audit
from argus.guardrails.injection import neutralise_wrapper
from argus.llm.client import ChatModel
from argus.steps import StepOutcome, StepRunner
from argus.tools.hub import ToolDeniedError, ToolHub

log = logging.getLogger("argus.agents.toolloop")

# (tool name, arguments, "allow" | "deny"); used to write the audit log. Called inside the
# step, so a replayed step does not audit twice.
ToolDecisionHook = Callable[[str, dict[str, Any], str], Awaitable[None]]


def audit_hook(
    pool: asyncpg.Pool, tenant_id: uuid.UUID, agent: str, run_id: uuid.UUID
) -> ToolDecisionHook:
    async def hook(name: str, args: dict[str, Any], decision: str) -> None:
        await audit(
            pool,
            tenant_id,
            actor_type="agent",
            actor_id=f"{agent}:{run_id}",
            action="tool.call",
            target=name,
            decision=decision,
            details={"args": args},
        )

    return hook


def wrap_untrusted(source: str, text: str) -> str:
    return f'<untrusted_data source="{source}">\n{neutralise_wrapper(text)}\n</untrusted_data>'


async def run_tool_loop(
    steps: StepRunner,
    llm: ChatModel,
    hub: ToolHub,
    messages: list[dict[str, Any]],
    *,
    between_steps: Callable[[], Awaitable[None]] | None = None,
    max_tokens: int = 1024,
    on_tool_decision: ToolDecisionHook | None = None,
    on_tool_result: Callable[[str, str], Awaitable[object]] | None = None,
) -> dict[str, Any]:
    """Run until the model answers without tool calls. Returns that final LLM output
    (``content``, ``model``). Bounded by the run's step and token budgets.

    ``on_tool_result(tool, text)`` sees every tool result (also replayed ones, so a
    guardrail that narrows the tool set decides the same way on resume). The offered
    tools are recomputed each turn, so a narrowed set applies from the next turn on."""
    while True:
        specs = hub.model_specs()
        tool_names = [s["function"]["name"] for s in specs]

        async def call_llm(specs: list[dict[str, Any]] = specs) -> StepOutcome:
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
                    hub.check(name, by_model=True)
                except ToolDeniedError as exc:
                    log.warning("tool denied", extra={"tool": name, "reason": str(exc)})
                    if on_tool_decision is not None:
                        await on_tool_decision(name, args, "deny")
                    return StepOutcome(
                        output={"text": f"error: {exc}", "is_error": True, "denied": True}
                    )
                if on_tool_decision is not None:
                    await on_tool_decision(name, args, "allow")
                out = await hub.call(name, args, by_model=True)
                return StepOutcome(output={"text": out.text, "is_error": out.is_error})

            result: dict[str, Any] = await steps.step("tool_call", name, args, call_tool)
            if on_tool_result is not None:
                await on_tool_result(name, str(result["text"]))
            if between_steps is not None:
                await between_steps()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": wrap_untrusted(f"tool:{name}", result["text"]),
                }
            )
