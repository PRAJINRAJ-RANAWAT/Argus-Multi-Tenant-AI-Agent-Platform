"""Minimal OpenAI-compatible chat client.

Interim client for M1: one call, no retries or routing (the queue retries the
whole job with backoff). M5 replaces direct use with the LLM gateway.
"""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class LLMError(Exception):
    def __init__(self, status: int, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(f"LLM error {status}: {message}")
        self.status = status
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    tool_calls: tuple[ToolCall, ...] = ()
    cached_tokens: int = 0


def _parse_tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    out: list[ToolCall] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(args, dict):
                args = {"_value": args}
        except (ValueError, TypeError):
            args = {"_unparsed": str(raw)[:2000]}
        out.append(
            ToolCall(id=str(tc.get("id") or f"call_{i}"), name=str(fn.get("name")), arguments=args)
        )
    return tuple(out)


class ChatModel(Protocol):
    """What agents call. ``model`` is a stable name used in step identity."""

    model: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...


class ModelSource(Protocol):
    """Hands an agent a model bound to its tenant and run (budgets, accounting)."""

    def for_run(self, tenant_id: uuid.UUID, run_id: uuid.UUID, agent: str) -> ChatModel: ...


class LLMClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        model: str,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        started = time.perf_counter()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = await self.http.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": model or self.model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    **({"tools": tools} if tools else {}),
                },
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise LLMError(504, f"timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise LLMError(503, f"transport: {exc}") from exc
        if resp.status_code >= 400:
            retry_after = resp.headers.get("retry-after")
            raise LLMError(
                resp.status_code,
                resp.text[:300],
                float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        body = resp.json()
        usage = body.get("usage") or {}
        message = body["choices"][0]["message"]
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=_parse_tool_calls(message),
            model=body.get("model", model or self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def for_run(self, tenant_id: uuid.UUID, run_id: uuid.UUID, agent: str) -> "LLMClient":
        """A direct client ignores run context (tests, tools); the gateway does not."""
        return self
