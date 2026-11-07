"""Mock LLM provider (OpenAI-compatible ``/v1/chat/completions``).

Used by every test, load test and chaos test so they cost nothing and are
repeatable. Faults are configurable at startup (env) and at runtime
(``POST /_admin/faults``):

* ``latency_ms`` / ``jitter_ms``: added before responding (and between stream chunks)
* ``error_rate_500`` / ``error_rate_429``: probability of an injected failure
* ``retry_after_s``: ``Retry-After`` header on injected 429s
* ``outage``: every request returns 500 (provider outage drill)
* ``fail_models``: requests for these models return 500 (one provider/model down)
* ``obey_injections``: act like a hijacked model (follow ``ARGUS-TEST-CALL: tool {json}``
  and ``ARGUS-TEST-SAY: text`` markers in untrusted content) for the injection eval
* ``tool_script``: tool calls to make, one per turn, when the request offers tools.
  Turn N (N = tool results so far) calls ``tool_script[N]``; afterwards it answers in
  text. The default script reads the diff if a ``get_pr_diff`` tool is offered.
"""

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from argus.rag.embed import hashing_vector


class Faults(BaseModel):
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    error_rate_500: float = 0.0
    error_rate_429: float = 0.0
    retry_after_s: int = 1
    outage: bool = False
    seed: int | None = None
    tool_script: list[dict[str, Any]] | None = None
    fail_models: list[str] = []  # models that return 500 (per-model outage drill)
    # Simulate a model hijacked by prompt injection: obey ARGUS-TEST-CALL / -SAY markers
    # found anywhere in the conversation (used by the injection eval).
    obey_injections: bool = False


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


DIRECTIVE_CALL = re.compile(
    r"ARGUS-TEST-CALL:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{[^\n]*?\})?(?=\s|$|<)"
)
DIRECTIVE_SAY = re.compile(r"ARGUS-TEST-SAY:\s*([^\n<]+)")


def injected_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        for name, raw in DIRECTIVE_CALL.findall(_text_of(m)):
            try:
                args = json.loads(raw) if raw else {}
            except ValueError:
                args = {}
            call = {"name": name, "arguments": args}
            if call not in seen:
                seen.append(call)
    return seen


def injected_says(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if m.get("role") != "system":
            out.extend(t.strip() for t in DIRECTIVE_SAY.findall(_text_of(m)))
    return list(dict.fromkeys(out))


def next_tool_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    script: list[dict[str, Any]] | None,
    obey: bool = False,
) -> dict[str, Any] | None:
    """The scripted tool call for this turn, as an OpenAI ``tool_calls`` entry, or None."""
    offered = [str((t.get("function") or {}).get("name")) for t in tools]
    if script is None:
        if "get_pr_diff" in offered:
            script = [{"name": "get_pr_diff", "arguments": {}}]
        elif "search_code" in offered:
            users = [_text_of(m) for m in messages if m.get("role") == "user"]
            question = re.sub(r"<[^>]+>", " ", users[-1] if users else "")
            question = " ".join(question.split()[-40:])
            script = [{"name": "search_code", "arguments": {"query": question}}]
        else:
            script = []
    if obey:
        script = [*script, *injected_calls(messages)]
    turn = sum(1 for m in messages if m.get("role") == "tool")
    if turn >= len(script):
        return None
    step = script[turn]
    return {
        "id": f"call_{turn}",
        "type": "function",
        "function": {"name": step["name"], "arguments": json.dumps(step.get("arguments", {}))},
    }


def canned_completion(messages: list[dict[str, Any]], obey: bool = False) -> str:
    """Deterministic, plausible output keyed on the conversation."""
    text = _canned(messages)
    if obey:
        says = injected_says(messages)
        if says:
            text = text + "\n\n" + "\n".join(says)
    return text


def _canned(messages: list[dict[str, Any]]) -> str:
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
    for m in reversed(messages):
        if m.get("role") == "tool":
            cite = re.search(r'"citation":\s*"([^"@]+)@', _text_of(m))
            if cite:
                return f"The relevant code is in {cite.group(1)} (mock answer {digest})."
    return f"This is a mock answer ({digest})."


class FaultState:
    def __init__(self, faults: Faults) -> None:
        self.faults = faults
        self.rng = random.Random(faults.seed)
        self.requests = 0
        self.by_model: dict[str, int] = {}

    def update(self, faults: Faults) -> None:
        self.faults = faults
        self.rng = random.Random(faults.seed)

    async def delay(self) -> None:
        f = self.faults
        ms = max(0.0, f.latency_ms + self.rng.uniform(-f.jitter_ms, f.jitter_ms))
        if ms:
            await asyncio.sleep(ms / 1000)

    def injected_error(self, model: str | None = None) -> JSONResponse | None:
        f = self.faults
        if f.outage or model in f.fail_models or self.rng.random() < f.error_rate_500:
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

    @app.post("/v1/messages", response_model=None)
    async def messages_api(request: Request) -> JSONResponse:
        """Anthropic Messages API shape, driven by the same canned logic."""
        state.requests += 1
        body = await request.json()
        model = str(body.get("model") or "mock-claude")
        state.by_model[model] = state.by_model.get(model, 0) + 1
        await state.delay()
        if (err := state.injected_error(model)) is not None:
            return err
        system = body.get("system") or ""
        if isinstance(system, list):
            system = " ".join(str(b.get("text", "")) for b in system)
        flat: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in body.get("messages") or []:
            content = m.get("content")
            if isinstance(content, str):
                flat.append({"role": m["role"], "content": content})
                continue
            for block in content or []:
                if block.get("type") == "tool_result":
                    flat.append({"role": "tool", "content": str(block.get("content", ""))})
                elif block.get("type") == "text":
                    flat.append({"role": m["role"], "content": block.get("text", "")})
        tools = [{"function": {"name": t["name"]}} for t in body.get("tools") or []]
        call = (
            next_tool_call(flat, tools, state.faults.tool_script, state.faults.obey_injections)
            if tools
            else None
        )
        prompt_tokens = sum(estimate_tokens(_text_of(m)) for m in flat)
        cached = estimate_tokens(system) if body.get("system") else 0
        if call is not None:
            content_blocks: list[dict[str, Any]] = [
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": json.loads(call["function"]["arguments"]),
                }
            ]
            out_tokens = 10
            stop = "tool_use"
        else:
            text = canned_completion(flat, state.faults.obey_injections)
            content_blocks = [{"type": "text", "text": text}]
            out_tokens = estimate_tokens(text)
            stop = "end_turn"
        return JSONResponse(
            {
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content_blocks,
                "stop_reason": stop,
                "usage": {
                    "input_tokens": max(0, prompt_tokens - cached),
                    "output_tokens": out_tokens,
                    "cache_read_input_tokens": cached,
                },
            }
        )

    @app.post("/v1/embeddings", response_model=None)
    async def embeddings(request: Request) -> JSONResponse:
        state.requests += 1
        body = await request.json()
        await state.delay()
        if (err := state.injected_error()) is not None:
            return err
        raw = body.get("input") or []
        texts = [raw] if isinstance(raw, str) else [str(t) for t in raw]
        return JSONResponse(
            {
                "object": "list",
                "model": str(body.get("model") or "mock-embed"),
                "data": [
                    {"object": "embedding", "index": i, "embedding": hashing_vector(t)}
                    for i, t in enumerate(texts)
                ],
                "usage": {
                    "prompt_tokens": sum(estimate_tokens(t) for t in texts),
                    "total_tokens": sum(estimate_tokens(t) for t in texts),
                },
            }
        )

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> JSONResponse | StreamingResponse:
        state.requests += 1
        body = await request.json()
        messages: list[dict[str, Any]] = body.get("messages") or []
        model = str(body.get("model") or "mock-large")
        state.by_model[model] = state.by_model.get(model, 0) + 1
        await state.delay()
        if (err := state.injected_error(model)) is not None:
            return err
        tools = body.get("tools") or []
        call = (
            next_tool_call(messages, tools, state.faults.tool_script, state.faults.obey_injections)
            if tools
            else None
        )
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
        text = canned_completion(messages, state.faults.obey_injections)
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
