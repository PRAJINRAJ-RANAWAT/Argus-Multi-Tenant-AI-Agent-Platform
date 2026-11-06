"""Ingest service: verifies, dedupes and persists webhooks (M1)."""

from fastapi import FastAPI

from argus.config import Settings, get_settings
from argus.service import build_app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings().model_copy(update={"service_name": "argus-ingest"})
    return build_app(settings, title="Argus Ingest")
