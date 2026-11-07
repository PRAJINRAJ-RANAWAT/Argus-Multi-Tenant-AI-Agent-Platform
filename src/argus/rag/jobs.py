"""``jobs:index`` handler: push → incremental index; install → full index per repo."""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from argus.github.client import GitHubClient
from argus.queue.consumer import PermanentError
from argus.queue.streams import Message
from argus.rag.embed import Embedder
from argus.rag.indexer import Indexer, IndexStats, RepoRef
from argus.runs import upsert_repo, upsert_tenant

log = logging.getLogger("argus.rag.jobs")

# GitHub lists at most 20 commits in a push payload; beyond that the file list is
# incomplete, so fall back to a full index.
PUSH_COMMIT_LIMIT = 20


@dataclass
class IndexDeps:
    pool: asyncpg.Pool
    github: GitHubClient
    embedder: Embedder

    def indexer(self) -> Indexer:
        return Indexer(self.pool, self.github, self.embedder)


def changed_paths(payload: dict[str, Any]) -> list[str] | None:
    """Paths touched by a push, or None when a full index is needed."""
    commits = payload.get("commits") or []
    if not commits or len(commits) >= PUSH_COMMIT_LIMIT or payload.get("forced"):
        return None
    paths: set[str] = set()
    for c in commits:
        for key in ("added", "modified", "removed"):
            paths.update(str(p) for p in c.get(key) or [])
    return sorted(paths)


async def _repo_ref(
    pool: asyncpg.Pool, installation_id: int, account: str, repo: dict[str, Any]
) -> RepoRef:
    tenant_id = await upsert_tenant(pool, installation_id, account)
    repo_id = await upsert_repo(
        pool, tenant_id, int(repo["id"]), str(repo["full_name"]), bool(repo.get("private", True))
    )
    return RepoRef(tenant_id, repo_id, installation_id, str(repo["full_name"]))


async def handle_push(
    deps: IndexDeps, payload: dict[str, Any], received_at: datetime
) -> IndexStats:
    try:
        repo = payload["repository"]
        installation_id = int(payload["installation"]["id"])
        account = str(repo["owner"].get("login") or repo["owner"].get("name"))
        after = str(payload["after"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PermanentError(f"malformed push payload: {exc}") from exc
    ref = await _repo_ref(deps.pool, installation_id, account, repo)
    paths = changed_paths(payload)
    if paths is None:
        return await deps.indexer().full_index(ref, after, received_at, repo.get("default_branch"))
    return await deps.indexer().index_paths(ref, after, received_at, paths)


async def handle_install(
    deps: IndexDeps, payload: dict[str, Any], received_at: datetime
) -> IndexStats:
    try:
        installation_id = int(payload["installation"]["id"])
        account = str(payload["installation"]["account"]["login"])
        repos = payload.get("repositories") or payload.get("repositories_added") or []
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"malformed installation payload: {exc}") from exc
    total = IndexStats()
    for r in repos:
        full = await deps.github.get_repo(installation_id, str(r["full_name"]))
        branch = str(full.get("default_branch") or "main")
        sha = await deps.github.get_branch_sha(installation_id, str(r["full_name"]), branch)
        ref = await _repo_ref(
            deps.pool,
            installation_id,
            account,
            {
                "id": r["id"],
                "full_name": r["full_name"],
                "private": full.get("private", r.get("private", True)),
            },
        )
        total.add(await deps.indexer().full_index(ref, sha, received_at, branch))
    return total


async def handle_index_message(deps: IndexDeps, msg: Message) -> None:
    data = msg.json()
    try:
        event_id = uuid.UUID(str(data["event_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"bad message: {exc}") from exc
    row = await deps.pool.fetchrow(
        "SELECT event_type, payload::text AS payload, received_at FROM events WHERE id = $1",
        event_id,
    )
    if row is None:
        raise PermanentError(f"event {event_id} not found")
    payload = json.loads(row["payload"])
    if row["event_type"] == "push":
        stats = await handle_push(deps, payload, row["received_at"])
    elif row["event_type"] in {"installation", "installation_repositories"}:
        stats = await handle_install(deps, payload, row["received_at"])
    else:
        raise PermanentError(f"unexpected event type {row['event_type']}")
    await deps.pool.execute("UPDATE events SET status = 'processed' WHERE id = $1", event_id)
    log.info(
        "index job done",
        extra={
            "event_id": str(event_id),
            "embedded": stats.embedded,
            "reused": stats.reused,
            "files": stats.files_indexed,
        },
    )
