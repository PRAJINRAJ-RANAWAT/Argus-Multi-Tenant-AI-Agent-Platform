"""M7 'done when': one trace shows webhook to GitHub comment. Plus metrics."""

import time
from collections.abc import Iterator

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY, generate_latest

from argus.agents.pr_review import ReviewDeps
from argus.github.client import GitHubClient
from argus.http_tracing import TracingTransport, route_of
from argus.ingest.app import create_app as create_ingest
from argus.queue.streams import StreamQueue, publish
from tests.conftest import app_client
from tests.factories import pr_payload
from tests.fakes import GITHUB_API
from tests.test_pr_review import Env, env  # noqa: F401 - fixture reuse

pytestmark = pytest.mark.integration

_EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def _ancestors(span: ReadableSpan, by_id: dict[int, ReadableSpan]) -> list[str]:
    names = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        p = by_id[parent.span_id]
        names.append(p.name)
        parent = p.parent
    return names


async def test_one_trace_from_webhook_to_github_comment(
    env: Env,  # noqa: F811
    spans: InMemorySpanExporter,
) -> None:
    traced = httpx.AsyncClient(transport=TracingTransport(env.gh.transport()))
    env.deps = ReviewDeps(
        **{**env.deps.__dict__, "github": GitHubClient(traced, env.deps.github.tokens, GITHUB_API)}
    )
    assert await env.deliver(pr_payload()) == 202
    assert await env.drain() == ["acked"]
    await traced.aclose()

    finished = spans.get_finished_spans()
    [webhook] = [s for s in finished if s.name == "webhook.receive"]
    trace_id = webhook.context.trace_id
    in_trace = [s for s in finished if s.context.trace_id == trace_id]
    names = {s.name for s in in_trace}
    expected = {
        "webhook.receive",
        "outbox.relay",
        "queue.process jobs:review",
        "agent.run pr_review",
        "step.llm_call mock-large",
        "step.tool_call get_pr_diff",
        "tool get_pr_diff",
        "step.tool_call post_review_comment",
        "tool post_review_comment",
        "HTTP GET api.github.test/repos/acme/widgets/pulls/{id}",
        "HTTP POST api.github.test/repos/acme/widgets/issues/{id}/comments",
    }
    assert expected <= names, expected - names

    # The comment POST descends from the webhook through every hop, in order.
    by_id = {s.context.span_id: s for s in in_trace}
    [comment] = [s for s in in_trace if s.name.startswith("HTTP POST") and "comments" in s.name]
    chain = _ancestors(comment, by_id)
    order = [
        "tool post_review_comment",
        "step.tool_call post_review_comment",
        "agent.run pr_review",
        "queue.process jobs:review",
        "outbox.relay",
        "webhook.receive",
    ]
    positions = [chain.index(n) for n in order]
    assert positions == sorted(positions), chain
    assert comment.attributes is not None and comment.attributes["http.status_code"] == 201
    run_span = next(s for s in in_trace if s.name == "agent.run pr_review")
    assert run_span.attributes is not None and run_span.attributes["argus.outcome"] == "succeeded"


async def test_retry_republish_stays_in_the_same_trace(
    env: Env,  # noqa: F811
    spans: InMemorySpanExporter,
) -> None:
    env.gh.fail_next_comment_after_create = True  # first attempt fails after the write
    await env.deliver(pr_payload())
    assert await env.drain() == ["retry", "acked"]
    finished = spans.get_finished_spans()
    [webhook] = [s for s in finished if s.name == "webhook.receive"]
    processes = [s for s in finished if s.name == "queue.process jobs:review"]
    assert len(processes) == 2
    assert {s.context.trace_id for s in processes} == {webhook.context.trace_id}


async def test_metrics_are_exported(env: Env, spans: InMemorySpanExporter) -> None:  # noqa: F811
    await env.deliver(pr_payload())
    await env.drain()
    text = generate_latest(REGISTRY).decode()
    for needle in (
        'argus_runs_total{agent="pr_review",outcome="succeeded"}',
        'argus_webhooks_total{event="pull_request",outcome="202"}',
        'argus_queue_messages_total{outcome="acked",stream="jobs:review"}',
        'argus_run_steps_total{kind="llm_call",result="executed"}',
        'argus_outbox_published_total{topic="jobs:review"}',
    ):
        assert needle in text, needle
    async with app_client(create_ingest(env.settings)) as c:
        r = await c.get("/metrics")
        assert r.status_code == 200 and "argus_http_requests_total" in r.text


async def test_queue_oldest_age(env: Env) -> None:  # noqa: F811
    q = StreamQueue(env.redis, "jobs:age", "g", "c")
    await q.ensure_group()
    assert await q.oldest_age_s() == 0.0
    await publish(env.redis, "jobs:age", {"x": 1})
    now = int(time.time() * 1000) + 5000
    assert 4.5 < await q.oldest_age_s(now_ms=now) < 6  # undelivered
    await q.read(block_ms=10)
    assert 4.5 < await q.oldest_age_s(now_ms=now) < 6  # delivered, not acked


def test_route_templates_are_low_cardinality() -> None:
    assert route_of("/repos/acme/widgets/issues/1234/comments") == (
        "/repos/acme/widgets/issues/{id}/comments"
    )
    assert route_of("/repos/a/b/commits/" + "f" * 40 + "/status") == (
        "/repos/a/b/commits/{id}/status"
    )
