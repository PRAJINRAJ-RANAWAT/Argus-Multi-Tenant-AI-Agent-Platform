from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from argus.api.app import create_app as create_api
from argus.config import Settings
from argus.ingest.app import create_app as create_ingest

pytestmark = pytest.mark.integration


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


@pytest.mark.parametrize("factory", [create_api, create_ingest])
async def test_healthz(factory: Callable[[Settings], FastAPI], settings: Settings) -> None:
    resp = await _get(factory(settings), "/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["git_sha"] == "test"


@pytest.mark.parametrize("factory", [create_api, create_ingest])
async def test_readyz_ok(factory: Callable[[Settings], FastAPI], settings: Settings) -> None:
    resp = await _get(factory(settings), "/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "postgres": True, "redis": True}


async def test_readyz_degraded_when_redis_down(settings: Settings) -> None:
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    resp = await _get(create_api(broken), "/readyz")
    assert resp.status_code == 503
    assert resp.json()["redis"] is False
    assert resp.json()["postgres"] is True


async def test_api_root(settings: Settings) -> None:
    resp = await _get(create_api(settings), "/")
    assert resp.status_code == 200
    if resp.headers["content-type"].startswith("text/html"):  # landing page is built
        assert "__PUBLIC_URL__" not in resp.text
        assert f'href="{settings.public_url.rstrip("/")}/"' in resp.text
    else:
        assert resp.json()["name"] == "Argus"
