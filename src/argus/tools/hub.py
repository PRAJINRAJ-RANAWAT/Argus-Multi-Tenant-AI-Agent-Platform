"""Agent-side MCP client hub and per-agent tool policy.

``ToolHub`` connects to a set of MCP servers, indexes their tools by name, and calls
them. ``AgentToolPolicy`` says which tools an agent type may use:

* ``model_tools``: offered to the LLM, which decides when to call them.
* ``runtime_tools``: called only by the agent's own code (e.g. posting the final
  review), never offered to the LLM.

Anything else is denied, including tools a model hallucinates or that exist on a
connected server but are not allowlisted (the reviewer cannot open issues).
"""

import contextlib
import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, Tool

# Runtime-supplied arguments, hidden from the model's view of a tool schema.
RUNTIME_ARGS = frozenset({"idempotency_key"})
MAX_RESULT_CHARS = 20_000


@dataclass(frozen=True)
class AgentToolPolicy:
    model_tools: frozenset[str]
    runtime_tools: frozenset[str] = frozenset()


POLICIES: dict[str, AgentToolPolicy] = {
    "pr_review": AgentToolPolicy(
        model_tools=frozenset(
            {
                "get_pr_diff",
                "list_changed_files",
                "read_file",
                "get_ci_status",
                "search_code",
                "find_callers",
            }
        ),
        runtime_tools=frozenset({"post_review_comment"}),
    ),
    "qa": AgentToolPolicy(model_tools=frozenset({"search_code", "find_callers", "read_file"})),
}


class ToolDeniedError(Exception):
    pass


class ToolCrashError(Exception):
    """The tool failed unexpectedly (e.g. GitHub 5xx). Retryable: the step is re-run."""


@dataclass(frozen=True)
class ToolOutput:
    text: str
    is_error: bool


def _result_text(content: list[Any]) -> str:
    parts = [c.text for c in content if isinstance(c, TextContent)]
    return "\n".join(parts)


class ToolHub:
    def __init__(self, servers: list[MCPServer], policy: AgentToolPolicy) -> None:
        self.servers = servers
        self.policy = policy
        self.tools: dict[str, tuple[Client, Tool]] = {}
        self._stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> "ToolHub":
        try:
            for server in self.servers:
                client = await self._stack.enter_async_context(Client(server))
                for tool in (await client.list_tools()).tools:
                    if tool.name in self.tools:
                        raise ValueError(f"duplicate tool name {tool.name!r}")
                    self.tools[tool.name] = (client, tool)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stack.aclose()

    def model_specs(self) -> list[dict[str, Any]]:
        """OpenAI-style function specs for the allowlisted model tools."""
        specs = []
        for name in sorted(self.policy.model_tools & self.tools.keys()):
            tool = self.tools[name][1]
            schema = json.loads(json.dumps(tool.input_schema))
            props = schema.get("properties") or {}
            for arg in RUNTIME_ARGS:
                props.pop(arg, None)
            schema["required"] = [r for r in schema.get("required", []) if r not in RUNTIME_ARGS]
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description or "",
                        "parameters": schema,
                    },
                }
            )
        return specs

    def check(self, name: str, *, by_model: bool) -> None:
        allowed = self.policy.model_tools if by_model else self.policy.runtime_tools
        if name not in allowed:
            raise ToolDeniedError(f"tool {name!r} is not allowed for this agent")
        if name not in self.tools:
            raise ToolDeniedError(f"tool {name!r} is not available")

    async def call(self, name: str, arguments: dict[str, Any], *, by_model: bool) -> ToolOutput:
        self.check(name, by_model=by_model)
        if by_model:
            arguments = {k: v for k, v in arguments.items() if k not in RUNTIME_ARGS}
        client, _ = self.tools[name]
        result = await client.call_tool(name, arguments)
        text = _result_text(list(result.content))
        if result.is_error and text.strip() == f"Error executing tool {name}":
            # The SDK hides an unexpected exception behind this generic text.
            raise ToolCrashError(f"tool {name} crashed")
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n[result truncated]"
        return ToolOutput(text=text, is_error=bool(result.is_error))
