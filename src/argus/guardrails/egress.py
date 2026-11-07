"""Egress control: HTTP clients used by agents and tools can reach only allowlisted
hosts, so an injected instruction cannot make Argus fetch an attacker's URL with data in
the query string. Enforced in the transport, below every client that uses it."""

from urllib.parse import urlparse

import httpx


class EgressDeniedError(httpx.TransportError):
    pass


def hosts_from_urls(*urls: str) -> set[str]:
    return {h for u in urls if u and (h := urlparse(u).hostname)}


class EgressGuard(httpx.AsyncBaseTransport):
    def __init__(
        self, allowed_hosts: set[str], inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.allowed = {h.lower() for h in allowed_hosts}
        self.inner = inner or httpx.AsyncHTTPTransport()
        self.denied: list[str] = []

    def permits(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for rule in self.allowed:
            if rule.startswith("*.") and (host.endswith(rule[1:]) or host == rule[2:]):
                return True
            if host == rule:
                return True
        return False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.permits(request.url.host):
            self.denied.append(request.url.host)
            raise EgressDeniedError(f"egress to {request.url.host} is not allowed", request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
