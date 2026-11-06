# 0001. Monorepo, single Python package, uv/ruff/mypy, raw SQL

- Status: accepted
- Date: 2026-11-07

## Context

Argus has several services (ingest, API, agent workers, outbox relay, indexer, gateway, mock LLM, MCP servers) that share config, logging, DB access and domain types. One developer builds them, and the spec asks for "one language for most services".

## Decision

- One repository, one Python package `argus` under `src/`, one container image. Each service is an entry point (`uvicorn argus.api.app:create_app`, `argus-worker`, ...) chosen by the compose `command`. Services scale independently because they are separate containers, even though they share an image.
- **uv** for dependency management and a lockfile (`uv.lock`); **ruff** for lint and format; **mypy --strict**; **pytest** + **testcontainers** against real Postgres (pgvector image) and Redis.
- **asyncpg with raw SQL**, no ORM. The interesting code (outbox, leases, fencing tokens, `SELECT ... FOR UPDATE SKIP LOCKED`, RLS `SET LOCAL`) is easier to reason about and review as SQL.
- **Forward-only numbered SQL migrations** with a small in-house runner: one transaction per file, a Postgres advisory lock so concurrent containers never double-apply, and a checksum to detect edits to applied migrations. This avoids Alembic's model/autogenerate machinery we would not use.

## Consequences

- Shared code changes ship to all services at once (simple, but a bad change affects everything; CI covers it).
- No ORM means hand-written queries and row mapping. We accept that for clarity on concurrency-critical paths.
- No down-migrations; rollbacks are new forward migrations.

## When we would revisit

- A second team or language (e.g. the Go gateway stretch goal) → split that service into its own directory/image.
- Image size or cold start matters for a specific service → per-service images with narrower dependency groups.
