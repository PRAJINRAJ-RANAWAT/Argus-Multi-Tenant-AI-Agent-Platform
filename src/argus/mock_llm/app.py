"""Mock LLM provider (OpenAI-compatible ``/v1/chat/completions``).

Used by every test, load test and chaos test so they cost nothing and are
repeatable. Faults are configurable at startup (env) and at runtime
(``POST /_admin/faults``):

* ``latency_ms`` / ``jitter_ms``: added before responding (and between stream chunks)
* ``error_rate_500`` / ``error_rate_429``: probability of an injected failure
* ``retry_after_s``: ``Retry-After`` header on injected 429s
* ``outage``: every request returns 500 (provider outage drill)
* ``tool_script``: tool calls to make, one per turn, when the request offers tools.
  Turn N (N = tool results so far) calls ``tool_script[N]``; afterwards it answers in
  text. The default script reads the diff if a ``get_pr_diff`` tool is offered.
"""

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Faults(BaseModel):
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    error_rate_500: float = 0.0
    error_rate_429: float = 0.0
    retry_after_s: int = 1
    outage: bool = False
    seed: int | None = None
    tool_script: list[dict[str, Any]] | None = None


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_LLM_")
    latency_ms: float = 50.0
    jitter_ms: float = 20.0
    error_rate_500: float = 0.0
    error_rate_429: float = 0.0
    retry_after_s: int = 1


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def next_tool_call(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], script: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The scripted tool call for this turn, as an OpenAI ``tool_calls`` entry, or None."""
    offered = [str((t.get("function") or {}).get("name")) for t in tools]
    if script is None:
        script = [{"name": "get_pr_diff", "arguments": {}}] if "get_pr_diff" in offered else []
    turn = sum(1 for m in messages if m.get("role") == "tool")
    if turn >= len(script):
        return None
    step = script[turn]
    return {
        "id": f"call_{turn}",
        "type": "function",
        "function": {"name": step["name"], "arguments": json.dumps(step.get("arguments", {}))},
    }


def canned_completion(messages: list[dict[str, Any]]) -> str:
    """Deterministic, plausible output keyed on the conversation."""
    system = " ".join(_text_of(m) for m in messages if m.get("role") == "system").lower()
    last = _text_of(messages[-1]) if messages else ""
    digest = hashlib.sha256(last.encode()).hexdigest()[:8]
    if "review" in system:
        return (
            "### Summary\n"
            "The change looks reasonable overall. A few observations:\n\n"
            "- Consider adding a test that covers the new error path.\n"
            "- Check that retries are bounded and use backoff with jitter.\n\n"
            f"_mock review {digest}_"
        )
    return f"This is a mock answer ({digest})."


class FaultState:
    def __init__(self, faults: Faults) -> None:
        self.faults = faults
        self.rng = random.Random(faults.seed)
        self.requests = 0

    def update(self, faults: Faults) -> None:
        self.faults = faults
        self.rng = random.Random(faults.seed)

    async def delay(self) -> None:
        f = self.faults
        ms = max(0.0, f.latency_ms + self.rng.uniform(-f.jitter_ms, f.jitter_ms))
        if ms:
            await asyncio.sleep(ms / 1000)

    def injected_error(self) -> JSONResponse | None:
        f = self.faults
        if f.outage or self.rng.random() < f.error_rate_500:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "mock: injected 500"}},
                status_code=500,
            )
        if self.rng.random() < f.error_rate_429:
            return JSONResponse(
                {"error": {"type": "rate_limit_error", "message": "mock: injected 429"}},
                status_code=429,
                headers={"Retry-After": str(f.retry_after_s)},
            )
        return None


def create_app(faults: Faults | None = None) -> FastAPI:
    if faults is None:
        s = MockSettings()
        faults = Faults(**s.model_dump())
    state = FaultState(faults)
    app = FastAPI(title="Argus mock LLM")
    app.state.faults = state

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/_admin/faults")
    async def get_faults() -> dict[str, Any]:
        return {**state.faults.model_dump(), "requests": state.requests}

    @app.post("/_admin/faults")
    async def set_faults(new: Faults) -> dict[str, Any]:
        state.update(new)
        return state.faults.model_dump()

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> JSONResponse | StreamingResponse:
        state.requests += 1
        body = await request.json()
        messages: list[dict[str, Any]] = body.get("messages") or []
        model = str(body.get("model") or "mock-large")
        await state.delay()
        if (err := state.injected_error()) is not None:
            return err
        tools = body.get("tools") or []
        call = next_tool_call(messages, tools, state.faults.tool_script) if tools else None
        if call is not None and not body.get("stream"):
            prompt_tokens = sum(estimate_tokens(_text_of(m)) for m in messages)
            return JSONResponse(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": 10,
                        "total_tokens": prompt_tokens + 10,
                    },
                }
            )
        text = canned_completion(messages)
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            text = text[: max_tokens * 4]
        prompt_tokens = sum(estimate_tokens(_text_of(m)) for m in messages)
        completion_tokens = estimate_tokens(text)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if body.get("stream"):
            return StreamingResponse(
                _stream(state, cid, created, model, text, usage), media_type="text/event-stream"
            )
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
        )

    return app


async def _stream(
    state: FaultState, cid: str, created: int, model: str, text: str, usage: dict[str, int]
) -> AsyncIterator[bytes]:
    words = text.split(" ")
    for i, word in enumerate(words):
        piece = word if i == 0 else " " + word
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        await asyncio.sleep(state.faults.latency_ms / 1000 / max(1, len(words)))
    final = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": usage,
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"
