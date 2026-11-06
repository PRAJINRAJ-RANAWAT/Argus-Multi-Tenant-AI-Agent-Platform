"""Minimal OpenAI-compatible chat client.

Interim client for M1: one call, no retries or routing (the queue retries the
whole job with backoff). M5 replaces direct use with the LLM gateway.
"""

import time
from dataclasses import dataclass
from typing import Any

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
class ChatResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


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
        return ChatResult(
            content=body["choices"][0]["message"]["content"] or "",
            model=body.get("model", model or self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
