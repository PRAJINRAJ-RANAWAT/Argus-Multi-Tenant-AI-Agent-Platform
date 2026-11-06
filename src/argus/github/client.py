"""Thin GitHub REST client authenticated with installation tokens."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

TokenGetter = Callable[[int], Awaitable[str]]

MAX_COMMENT_PAGES = 10


class GitHubClient:
    def __init__(
        self, http: httpx.AsyncClient, tokens: TokenGetter, api_url: str = "https://api.github.com"
    ) -> None:
        self.http = http
        self.tokens = tokens
        self.api_url = api_url.rstrip("/")

    async def _headers(self, installation_id: int, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.tokens(installation_id)}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_pr_diff(self, installation_id: int, repo: str, number: int) -> str:
        resp = await self.http.get(
            f"{self.api_url}/repos/{repo}/pulls/{number}",
            headers=await self._headers(installation_id, "application/vnd.github.diff"),
        )
        resp.raise_for_status()
        return resp.text

    async def get_pull(self, installation_id: int, repo: str, number: int) -> dict[str, Any]:
        resp = await self.http.get(
            f"{self.api_url}/repos/{repo}/pulls/{number}",
            headers=await self._headers(installation_id, "application/vnd.github+json"),
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def list_issue_comments(
        self, installation_id: int, repo: str, number: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        headers = await self._headers(installation_id, "application/vnd.github+json")
        for page in range(1, MAX_COMMENT_PAGES + 1):
            resp = await self.http.get(
                f"{self.api_url}/repos/{repo}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
                headers=headers,
            )
            resp.raise_for_status()
            batch = resp.json()
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    async def create_issue_comment(
        self, installation_id: int, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        resp = await self.http.post(
            f"{self.api_url}/repos/{repo}/issues/{number}/comments",
            json={"body": body},
            headers=await self._headers(installation_id, "application/vnd.github+json"),
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
