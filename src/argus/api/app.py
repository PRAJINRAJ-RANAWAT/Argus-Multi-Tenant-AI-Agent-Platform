"""Public API service: dashboard backend, OAuth, approvals (later milestones)."""

from fastapi import FastAPI

from argus.config import Settings, get_settings
from argus.service import build_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings().model_copy(update={"service_name": "argus-api"})

    def configure(app: FastAPI) -> None:
        @app.get("/")
        async def root() -> dict[str, str]:
            return {"name": "Argus", "message": "Hello from Argus"}

    return build_app(settings, title="Argus API", configure=configure)
