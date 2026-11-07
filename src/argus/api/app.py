"""Public API service: dashboard backend, OAuth, approvals (later milestones)."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from argus.config import Settings, get_settings
from argus.health import Resources
from argus.queue.dlq import list_dead, replay, replay_all
from argus.queue.topics import ALL_STREAMS
from argus.service import build_app


class ReplayRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    all: bool = False
    limit: int = Field(default=1000, ge=1, le=10_000)


def _resources(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    token = _resources(request).settings.admin_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=404)  # admin API disabled
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _stream(topic: str) -> str:
    if topic not in ALL_STREAMS:
        raise HTTPException(status_code=404, detail=f"unknown topic {topic!r}")
    return topic


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@admin.get("/dlq/{topic}")
async def get_dlq(
    request: Request,
    topic: str,
    count: Annotated[int, Query(ge=1, le=500)] = 50,
    after: str = "-",
) -> dict[str, object]:
    stream = _stream(topic)
    items = await list_dead(_resources(request).redis, stream, count=count, after=after)
    return {"topic": stream, "items": [i.__dict__ for i in items]}


@admin.post("/dlq/{topic}/replay")
async def post_replay(request: Request, topic: str, body: ReplayRequest) -> dict[str, object]:
    stream = _stream(topic)
    redis = _resources(request).redis
    if body.all:
        return {"topic": stream, "replayed": await replay_all(redis, stream, limit=body.limit)}
    if not body.ids:
        raise HTTPException(status_code=422, detail="pass ids or all=true")
    done = await replay(redis, stream, body.ids)
    return {
        "topic": stream,
        "replayed": sum(1 for v in done.values() if v is not None),
        "results": done,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings().model_copy(update={"service_name": "argus-api"})

    def configure(app: FastAPI) -> None:
        @app.get("/")
        async def root() -> dict[str, str]:
            return {"name": "Argus", "message": "Hello from Argus"}

        app.include_router(admin)

    return build_app(settings, title="Argus API", configure=configure)
