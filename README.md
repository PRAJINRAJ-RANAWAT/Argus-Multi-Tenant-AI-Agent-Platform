# Argus

A multi-tenant AI agent platform for engineering teams. Argus turns GitHub and Slack events into durable, long-running agent jobs: reviewing pull requests against a team's own codebase and conventions, and answering questions about the code with cited sources.

The focus is the distributed-systems layer around the LLM call: effectively-once event processing, durable agent runs that survive crashes, per-tenant fairness and budgets, permission-aware retrieval, guardrails against prompt injection, end-to-end tracing and an eval gate in CI.

- Full specification: [docs/SPEC.md](docs/SPEC.md)
- Architecture decisions: [docs/adr/](docs/adr/)
- Progress: [docs/PROGRESS.md](docs/PROGRESS.md)

## Status

Early development: M0 (skeleton) in progress. See [docs/PROGRESS.md](docs/PROGRESS.md).

## Quick start

```bash
make install   # uv sync + git hooks
make up        # Postgres, Redis, api (:8000), ingest (:8001), worker
make check     # lint, type check, tests (needs Docker)
```
