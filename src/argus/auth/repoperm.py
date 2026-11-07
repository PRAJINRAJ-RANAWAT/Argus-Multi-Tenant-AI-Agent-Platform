"""The user's own GitHub permission on a repository, cached in Redis.

Checked with the App's installation token through the collaborator-permission API, so
Argus never needs to keep user tokens. Public repositories are readable by anyone.
"""

import uuid
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

from argus.auth import READ_PERMISSIONS, Principal
from argus.github.client import GitHubClient


@dataclass(frozen=True)
class RepoInfo:
    id: uuid.UUID
    full_name: str
    private: bool


class RepoAccess:
    def __init__(self, github: GitHubClient, redis: Redis, cache_s: int = 300) -> None:
        self.github = github
        self.redis = redis
        self.cache_s = cache_s

    async def github_permission(self, installation_id: int, repo: str, login: str) -> str:
        key = f"repoperm:{installation_id}:{repo}:{login.lower()}"
        cached = cast(str | None, await self.redis.get(key))
        if cached is not None:
            return cached
        perm = await self.github.get_collaborator_permission(installation_id, repo, login)
        await self.redis.set(key, perm, ex=self.cache_s)
        return perm

    async def can_read(self, principal: Principal, repo: RepoInfo) -> bool:
        if principal.repo_ids is not None and repo.id not in principal.repo_ids:
            return False
        if principal.github_login is None:  # API key: bounded by its repo list and role
            return True
        if not repo.private:
            return True
        perm = await self.github_permission(
            principal.installation_id, repo.full_name, principal.github_login
        )
        return perm in READ_PERMISSIONS
