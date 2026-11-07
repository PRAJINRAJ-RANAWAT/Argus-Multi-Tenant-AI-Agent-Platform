"""OpenTelemetry tracing across the async hops.

One trace follows a webhook through every hop: ingest (``webhook.receive``) → outbox row
(the W3C ``traceparent`` is stored in the payload) → relay (``outbox.relay``, which
publishes its own ``traceparent`` as a stream field) → worker (``queue.process``) →
agent run, steps, LLM calls, MCP tool calls and outgoing HTTP requests (GitHub, LLM).

Without a configured exporter the API hands out non-recording spans, which cost almost
nothing. ``configure_tracing`` sets up OTLP/HTTP export (Jaeger, Tempo, an OTel
collector, or Langfuse's OTLP endpoint).
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

log = logging.getLogger("argus.telemetry")

tracer = trace.get_tracer("argus")


def configure_tracing(
    service: str,
    *,
    endpoint: str = "",
    exporter: SpanExporter | None = None,
    headers: Mapping[str, str] | None = None,
) -> TracerProvider | None:
    """Install a global tracer provider (once per process)."""
    if exporter is None and not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=endpoint.rstrip("/") + "/v1/traces", headers=dict(headers or {})
        )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    log.info("tracing enabled", extra={"otel_endpoint": endpoint or "custom exporter"})
    return provider


def inject() -> dict[str, str]:
    """The current trace context as a carrier dict (``traceparent``/``tracestate``)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract(carrier: Mapping[str, Any] | None) -> otel_context.Context:
    clean = {k: str(v) for k, v in (carrier or {}).items() if isinstance(v, str)}
    return propagate.extract(clean)


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    parent: otel_context.Context | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Start a span (optionally under an extracted parent) and record exceptions."""
    attrs = {k: v for k, v in (attributes or {}).items() if v is not None}
    with tracer.start_as_current_span(
        name,
        context=parent,
        kind=kind,
        attributes=attrs,
        record_exception=True,
        set_status_on_exception=True,
    ) as s:
        yield s


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def mark_error(s: Span, message: str) -> None:
    s.set_status(Status(StatusCode.ERROR, message))
