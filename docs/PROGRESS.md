# Progress

## Current milestone

**M1 · Webhook to review comment**: code complete (M1a merged as `v0.1.0`, M1b in review). The live "done when" (install the App → a review appears) is waiting on the VPS and GitHub App (blockers below).

## Done

### M0 · Skeleton
- Repo bootstrapped: `main` (production) and `dev` (integration) branches; commit-msg hook strips AI attribution trailers.
- Python 3.12 project with uv, ruff, mypy (strict), pytest + testcontainers (real Postgres+pgvector, Redis).
- Services: `api` and `ingest` (FastAPI, `/healthz` + `/readyz` checking Postgres and Redis), `worker` (heartbeat loop, graceful SIGTERM).
- Forward-only SQL migration runner (advisory lock, per-file transaction, checksum guard), with race and rollback tests.
- JSON structured logging with `trace_id` / `tenant_id` / `run_id` context fields.
- Docker: one image for all services; local `docker-compose.yml`; production `deploy/docker-compose.prod.yml` with Caddy TLS.
- CI: ruff, mypy, pytest, gitleaks, Docker build on every PR and on pushes to dev/main.
- Deploy workflow on push to `main`: build → GHCR → SSH deploy → smoke test on `git_sha`. Skips with a warning until secrets exist.
- ADRs 0001 (monorepo/tooling/raw SQL) and 0002 (single-VPS deploy).

### M0 · Skeleton: merged, tagged `v0.0.0`
(Deploy job runs and skips until the VPS secrets exist.)

### M1a · Ingest, outbox, queue (merged, `v0.1.0`)
- `POST /webhooks/github`: HMAC-SHA256 verify (constant time) → Redis fast-path dedupe → `events` + `outbox` rows in one transaction → 202. Postgres `UNIQUE (source, delivery_id)` is the dedupe source of truth; Redis is set only after commit (ADR 0003).
- Outbox relay (`argus-relay`): `FOR UPDATE SKIP LOCKED` batches → `XADD` → mark published; woken by `NOTIFY`, polls as a fallback; records errors and backs off.
- Redis Streams queue: consumer groups, ack-after-work, delayed retries (ZSET, exponential backoff with full jitter), DLQ, `XAUTOCLAIM` reclaim, ownership-checked ack (zombie protection), poison-message bound on delivery count. All failure transitions are atomic Lua (ADR 0004).
- Tests: 20 concurrent duplicate deliveries store exactly one event; flushed Redis / Redis down / DB failure mid-request; 6 relays × 500 rows never double-publish; crash between XADD and COMMIT republishes (documented); zombie ack rejected; poison message dead-lettered; 3 competing consumers process 200 messages exactly once; concurrency cap respected.

### M1b · Review worker
- Mock LLM server (OpenAI-compatible, streaming, fault injection via env or `POST /_admin/faults`: latency, 500/429 rates, `Retry-After`, full outage). It also runs in the prod compose as a free stand-in provider.
- GitHub App auth: RS256 app JWT; installation tokens cached in Redis with TTL = expiry − 5 min.
- Schema: `tenants` (tenant = installation), `repos`, `runs` (idempotency key, lease, fencing token, usage), `side_effects` ledger.
- Review agent: event → run keyed `review:<repo>:<pr>:<head_sha>` → lease → diff → one LLM call (untrusted content wrapped in `<untrusted_data>`) → comment posted through `perform_once` with a hidden marker → fenced completion.
- Self-healing (ADR 0005): in-process failures release the lease; a run reaper requeues expired leases through the outbox and bumps the fencing token.
- Tests (98 total): replayed and duplicate deliveries → 1 comment; relay republish harmless; 3 concurrent workers → 1 comment; LLM outage retried; lost response after comment creation → no duplicate (marker found); `kill -9` mid-run → reaper recovers, 1 comment; malformed event → DLQ.

## Next

- **M2 · Durable agent runtime:** `run_steps` checkpointing (replay completed steps on resume), lease heartbeats with a short lease, tool-calling loop over MCP (`argus-code`, `argus-github` servers, per-agent allowlists), supersede on a newer head SHA with a Redis lock per (repo, pr), DLQ replay endpoint, run budgets (max steps/tokens/deadline), ADR build vs Temporal.

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. **GitHub App** registration and install on a test repo: `docs/github-app.md`. Needed for M1's live "done when".
4. (Optional, costs money) a real LLM provider key. Until then the bundled mock LLM produces canned reviews.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
