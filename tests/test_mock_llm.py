import json

import httpx
import pytest

from argus.mock_llm.app import Faults, create_app


def client(faults: Faults | None = None) -> httpx.AsyncClient:
    app = create_app(faults or Faults(seed=1))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://llm")


async def test_completion_has_openai_shape_and_usage() -> None:
    async with client() as c:
        r = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [
                    {"role": "system", "content": "You review code"},
                    {"role": "user", "content": "x" * 400},
                ],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "Summary" in body["choices"][0]["message"]["content"]
    assert body["usage"]["prompt_tokens"] >= 100
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


async def test_deterministic_for_same_input() -> None:
    req = {"messages": [{"role": "user", "content": "hello"}]}
    async with client() as c:
        a = (await c.post("/v1/chat/completions", json=req)).json()
        b = (await c.post("/v1/chat/completions", json=req)).json()
    assert a["choices"][0]["message"] == b["choices"][0]["message"]


async def test_outage_and_429_injection_via_admin() -> None:
    async with client() as c:
        await c.post("/_admin/faults", json={"outage": True})
        r = await c.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 500
        await c.post("/_admin/faults", json={"error_rate_429": 1.0, "retry_after_s": 7})
        r = await c.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 429
        assert r.headers["retry-after"] == "7"
        await c.post("/_admin/faults", json={})
        r = await c.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 200
        assert (await c.get("/_admin/faults")).json()["requests"] == 3


@pytest.mark.parametrize("rate", [0.3])
async def test_error_rate_is_roughly_honoured(rate: float) -> None:
    async with client(Faults(error_rate_500=rate, seed=42)) as c:
        codes = [
            (await c.post("/v1/chat/completions", json={"messages": []})).status_code
            for _ in range(200)
        ]
    share = codes.count(500) / len(codes)
    assert 0.2 < share < 0.4


async def test_streaming_emits_sse_chunks_and_done() -> None:
    async with client() as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    lines = [ln for ln in r.text.split("\n\n") if ln]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln.removeprefix("data: ")) for ln in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text.startswith("This is a mock answer")
    assert chunks[-1]["usage"]["completion_tokens"] > 0
