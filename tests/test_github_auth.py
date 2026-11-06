import time
from datetime import timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis.asyncio import Redis

from argus.github.auth import TOKEN_SAFETY_MARGIN_S, InstallationTokenProvider, app_jwt
from tests.fakes import GITHUB_API, FakeGitHub


@pytest.fixture(scope="session")
def rsa_pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


def test_app_jwt_claims(rsa_pem: tuple[str, str]) -> None:
    priv, pub = rsa_pem
    now = time.time()
    token = app_jwt("42", priv, now=now)
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "42"
    assert claims["iat"] == int(now) - 60
    assert claims["exp"] - claims["iat"] == 600


@pytest.mark.integration
async def test_installation_token_cached_with_safety_margin(
    rsa_pem: tuple[str, str], redis: Redis
) -> None:
    gh = FakeGitHub()
    async with httpx.AsyncClient(transport=gh.transport()) as http:
        p = InstallationTokenProvider(
            http, redis, app_id="42", private_key_pem=rsa_pem[0], api_url=GITHUB_API
        )
        t1 = await p.get(77)
        t2 = await p.get(77)
        assert t1 == t2
        assert gh.tokens_minted == 1
        ttl = await redis.ttl("gh:inst_token:77")
        assert 3600 - TOKEN_SAFETY_MARGIN_S - 5 <= ttl <= 3600 - TOKEN_SAFETY_MARGIN_S
        await p.get(78)
        assert gh.tokens_minted == 2
        await p.invalidate(77)
        assert await p.get(77) != t1


@pytest.mark.integration
async def test_nearly_expired_token_not_cached(rsa_pem: tuple[str, str], redis: Redis) -> None:
    gh = FakeGitHub(token_ttl=timedelta(seconds=60))
    async with httpx.AsyncClient(transport=gh.transport()) as http:
        p = InstallationTokenProvider(
            http, redis, app_id="42", private_key_pem=rsa_pem[0], api_url=GITHUB_API
        )
        await p.get(1)
        await p.get(1)
    assert gh.tokens_minted == 2
    assert await redis.exists("gh:inst_token:1") == 0
