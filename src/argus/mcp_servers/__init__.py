"""Argus MCP servers (official MCP SDK).

Each server is built per run and bound to that run's tenant, repo, PR and head SHA, so
a tool call can never reach another repository whatever arguments the model passes.
The agent runtime connects to them as an MCP client (in-process today; the same
servers can be served over Streamable HTTP with OAuth in M4).
"""

import uuid
from dataclasses import dataclass

import asyncpg

from argus.github.client import GitHubClient
from argus.rag.embed import Embedder


@dataclass(frozen=True)
class RunContext:
    pool: asyncpg.Pool
    github: GitHubClient
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    lease_version: int
    installation_id: int
    repo: str
    pr_number: int
    head_sha: str
    max_diff_chars: int = 60_000
    max_file_chars: int = 20_000
    side_effect_stale_s: float = 120.0
    repo_id: uuid.UUID | None = None  # set when the repo's code index can be searched
    embedder: Embedder | None = None
