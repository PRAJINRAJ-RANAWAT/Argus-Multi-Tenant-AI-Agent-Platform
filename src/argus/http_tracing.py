"""httpx transport that records a client span per outgoing request (GitHub, LLM
providers, embeddings). It does not forward trace headers to third parties."""

import re

import httpx
from opentelemetry.trace import SpanKind

from argus.telemetry import mark_error, span

# Collapse ids in paths so span names stay low-cardinality.
_IDS = re.compile(r"/(\d+|[0-9a-f]{7,40})(?=/|$)")


def route_of(path: str) -> str:
    return _IDS.sub("/{id}", path)


class TracingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        name = f"HTTP {request.method} {request.url.host}{route_of(request.url.path)}"
        with span(
            name,
            kind=SpanKind.CLIENT,
            attributes={
                "http.method": request.method,
                "server.address": request.url.host,
                "url.path": route_of(request.url.path),
            },
        ) as s:
            response = await self.inner.handle_async_request(request)
            s.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                mark_error(s, f"HTTP {response.status_code}")
            return response

    async def aclose(self) -> None:
        await self.inner.aclose()
