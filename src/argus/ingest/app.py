"""Ingest service: verify → dedupe → persist (event + outbox) → 202. Nothing slow here."""

import json
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from argus.config import Settings, get_settings
from argus.github.webhooks import route_github_event, verify_signature
from argus.health import Resources
from argus.ingest.dedupe import already_processed, mark_processed
from argus.ingest.store import persist_event
from argus.service import build_app

log = logging.getLogger("argus.ingest")

MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def webhook_router() -> APIRouter:
    router = APIRouter(tags=["webhooks"])

    @router.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        res: Resources = request.app.state.resources
        secret = res.settings.github_webhook_secret.get_secret_value()

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(413, "payload too large")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _error(413, "payload too large")

        if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            return _error(401, "invalid signature")

        delivery_id = request.headers.get("x-github-delivery")
        event_type = request.headers.get("x-github-event")
        if not delivery_id or not event_type:
            return _error(400, "missing delivery or event header")
        try:
            payload: Any = json.loads(body)
        except ValueError:
            return _error(400, "invalid JSON")
        if not isinstance(payload, dict):
            return _error(400, "payload must be an object")

        if await already_processed(res.redis, "github", delivery_id):
            return JSONResponse({"status": "duplicate"}, status_code=200)

        topic = route_github_event(event_type, payload)
        result = await persist_event(
            res.db,
            source="github",
            delivery_id=delivery_id,
            event_type=event_type,
            payload=payload,
            topic=topic,
        )
        await mark_processed(res.redis, "github", delivery_id)
        log.info(
            "webhook accepted",
            extra={
                "delivery_id": delivery_id,
                "event_type": event_type,
                "duplicate": result.duplicate,
                "enqueued": result.enqueued,
            },
        )
        if result.duplicate:
            return JSONResponse({"status": "duplicate"}, status_code=200)
        return JSONResponse(
            {"status": "accepted", "event_id": str(result.event_id), "enqueued": result.enqueued},
            status_code=202,
        )

    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings().model_copy(update={"service_name": "argus-ingest"})

    def configure(app: FastAPI) -> None:
        app.include_router(webhook_router())

    return build_app(settings, title="Argus Ingest", configure=configure)
