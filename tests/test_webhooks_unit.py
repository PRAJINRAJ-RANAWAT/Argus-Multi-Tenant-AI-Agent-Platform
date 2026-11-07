import pytest

from argus.github.webhooks import route_github_event, sign, verify_signature
from argus.queue.topics import JOBS_REVIEW
from tests.factories import pr_payload


def test_valid_signature() -> None:
    body = b'{"a":1}'
    assert verify_signature("s3cret", body, sign("s3cret", body))


@pytest.mark.parametrize(
    "header",
    [None, "", "sha1=abc", "sha256=", "sha256=" + "0" * 64, sign("other", b'{"a":1}')],
)
def test_invalid_signatures(header: str | None) -> None:
    assert not verify_signature("s3cret", b'{"a":1}', header)


def test_tampered_body_rejected() -> None:
    header = sign("s3cret", b'{"a":1}')
    assert not verify_signature("s3cret", b'{"a":2}', header)


def test_empty_secret_never_verifies() -> None:
    assert not verify_signature("", b"x", sign("", b"x"))


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_review_actions_routed(action: str) -> None:
    assert route_github_event("pull_request", pr_payload(action=action)) == JOBS_REVIEW


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("pull_request", pr_payload(action="closed")),
        ("pull_request", pr_payload(draft=True)),
        ("issues", {"action": "opened"}),
        ("ping", {}),
    ],
)
def test_other_events_ignored(event: str, payload: dict[str, object]) -> None:
    assert route_github_event(event, payload) is None


def test_index_routing() -> None:
    from argus.queue.topics import JOBS_INDEX
    from tests.factories import installation_payload, push_payload

    assert route_github_event("push", push_payload()) == JOBS_INDEX
    assert route_github_event("push", push_payload(ref="refs/heads/feature")) is None
    deleted = {**push_payload(), "deleted": True}
    assert route_github_event("push", deleted) is None
    assert route_github_event("installation", installation_payload(repos=[])) == JOBS_INDEX
    added = {"action": "added", "repositories_added": []}
    assert route_github_event("installation_repositories", added) == JOBS_INDEX
    assert route_github_event("installation", {"action": "deleted"}) is None
