"""GitHub App authentication: app JWT and cached installation tokens."""

import logging
import time
from datetime import datetime

import httpx
import jwt
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

# Refresh installation tokens this long before GitHub's 1 h expiry.
TOKEN_SAFETY_MARGIN_S = 300


def app_jwt(app_id: str, private_key_pem: str, *, now: float | None = None) -> str:
    """RS256 JWT for the App itself. Backdated 60 s for clock skew; valid 9 minutes."""
    issued = int(now if now is not None else time.time()) - 60
    claims = {"iat": issued, "exp": issued + 600, "iss": str(app_id)}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


class InstallationTokenProvider:
    """Mints installation tokens and caches them in Redis with a TTL shorter than expiry."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        *,
        app_id: str,
        private_key_pem: str,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.http = http
        self.redis = redis
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.api_url = api_url.rstrip("/")

    @staticmethod
    def _key(installation_id: int) -> str:
        return f"gh:inst_token:{installation_id}"

    async def get(self, installation_id: int) -> str:
        try:
            cached = await self.redis.get(self._key(installation_id))
        except RedisError:
            log.warning("token cache read failed", exc_info=True)
            cached = None
        if cached:
            return str(cached)
        token, ttl = await self._mint(installation_id)
        if ttl > 0:
            try:
                await self.redis.set(self._key(installation_id), token, ex=ttl)
            except RedisError:
                log.warning("token cache write failed", exc_info=True)
        return token

    async def invalidate(self, installation_id: int) -> None:
        await self.redis.delete(self._key(installation_id))

    async def _mint(self, installation_id: int) -> tuple[str, int]:
        resp = await self.http.post(
            f"{self.api_url}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(self.app_id, self.private_key_pem)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        ttl = int(expires_at.timestamp() - time.time()) - TOKEN_SAFETY_MARGIN_S
        return body["token"], ttl
