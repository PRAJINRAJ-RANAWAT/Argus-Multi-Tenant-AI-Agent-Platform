"""Provider adapters. One request shape in (OpenAI-style messages and tools), one
``ChatResult`` out.

* ``OpenAIProvider``: any OpenAI-compatible ``/chat/completions`` (and the mock LLM).
* ``AnthropicProvider``: the Messages API. System prompt and tool definitions are sent
  first and marked ``cache_control`` so the stable prefix hits provider prompt caching.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from argus.llm.client import ChatResult, LLMClient, LLMError, ToolCall


class Provider(Protocol):
    name: str

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult: ...


class OpenAIProvider:
    def __init__(
        self, http: httpx.AsyncClient, *, base_url: str, api_key: str, name: str = "openai"
    ) -> None:
        self.name = name
        self.client = LLMClient(http, base_url=base_url, api_key=api_key, model="")

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        return await self.client.chat(
            messages, model=model, tools=tools, max_tokens=max_tokens, temperature=temperature
        )

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        ttft_timeout_s: float,
    ) -> AsyncIterator[str]:
        """Streaming passthrough of content deltas. ``ttft_timeout_s`` bounds the time
        from the request to the first token (connect, headers and first chunk)."""
        c = self.client
        headers = {"Authorization": f"Bearer {c.api_key}"} if c.api_key else {}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True}
        try:
            async with asyncio.timeout(ttft_timeout_s) as first_token:
                async with c.http.stream(
                    "POST", f"{c.base_url}/chat/completions", json=body, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        raise LLMError(resp.status_code, (await resp.aread()).decode()[:300])
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line.removeprefix("data: ")
                        if data == "[DONE]":
                            return
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                        if delta:
                            first_token.reschedule(None)  # no deadline after the first token
                            yield delta
        except TimeoutError as exc:
            raise LLMError(504, "time to first token exceeded") from exc


def to_anthropic(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Convert OpenAI-style messages/tools into a Messages API request body."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m["role"] == "system")
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": str(m.get("content") or ""),
            }
            if (
                out
                and out[-1]["role"] == "user"
                and isinstance(out[-1]["content"], list)
                and out[-1]["content"]
                and out[-1]["content"][0]["type"] == "tool_result"
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant" and m.get("tool_calls"):
            content: list[dict[str, Any]] = []
            if m.get("content"):
                content.append({"type": "text", "text": str(m["content"])})
            for tc in m["tool_calls"]:
                fn = tc["function"]
                args = fn.get("arguments") or "{}"
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": json.loads(args) if isinstance(args, str) else args,
                    }
                )
            out.append({"role": "assistant", "content": content})
            continue
        out.append({"role": role, "content": str(m.get("content") or "")})
    body: dict[str, Any] = {"messages": out}
    if system:
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if tools:
        converted = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters") or {"type": "object"},
            }
            for t in tools
        ]
        converted[-1]["cache_control"] = {"type": "ephemeral"}
        body["tools"] = converted
    return body


class AnthropicProvider:
    def __init__(
        self, http: httpx.AsyncClient, *, base_url: str, api_key: str, name: str = "anthropic"
    ) -> None:
        self.name = name
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        started = time.perf_counter()
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **to_anthropic(messages, tools),
        }
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            resp = await self.http.post(f"{self.base_url}/messages", json=body, headers=headers)
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
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b["type"] == "text")
        calls = tuple(
            ToolCall(id=str(b["id"]), name=str(b["name"]), arguments=dict(b.get("input") or {}))
            for b in data.get("content", [])
            if b["type"] == "tool_use"
        )
        usage = data.get("usage") or {}
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
        return ChatResult(
            content=text,
            model=str(data.get("model", model)),
            input_tokens=int(usage.get("input_tokens", 0)) + cached,
            output_tokens=int(usage.get("output_tokens", 0)),
            latency_ms=(time.perf_counter() - started) * 1000,
            tool_calls=calls,
            cached_tokens=cached,
        )
