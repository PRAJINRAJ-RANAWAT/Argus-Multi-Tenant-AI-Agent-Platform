# Progress

## Current milestone

**M1 · Webhook to review comment**: M1a (ingest + outbox + queue) in review; M1b (worker, mock LLM, GitHub App) next

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

### M1a · Ingest, outbox, queue
- `POST /webhooks/github`: HMAC-SHA256 verify (constant time) → Redis fast-path dedupe → `events` + `outbox` rows in one transaction → 202. Postgres `UNIQUE (source, delivery_id)` is the dedupe source of truth; Redis is set only after commit (ADR 0003).
- Outbox relay (`argus-relay`): `FOR UPDATE SKIP LOCKED` batches → `XADD` → mark published; woken by `NOTIFY`, polls as a fallback; records errors and backs off.
- Redis Streams queue: consumer groups, ack-after-work, delayed retries (ZSET, exponential backoff with full jitter), DLQ, `XAUTOCLAIM` reclaim, ownership-checked ack (zombie protection), poison-message bound on delivery count. All failure transitions are atomic Lua (ADR 0004).
- Tests: 20 concurrent duplicate deliveries store exactly one event; flushed Redis / Redis down / DB failure mid-request; 6 relays × 500 rows never double-publish; crash between XADD and COMMIT republishes (documented); zombie ack rejected; poison message dead-lettered; 3 competing consumers process 200 messages exactly once; concurrency cap respected.

## Next

- **M1b:** mock LLM server; GitHub App JWT + installation tokens cached in Redis; review worker consuming `jobs:review` with a run keyed on (tenant, repo, pr, head_sha); one LLM call; idempotent PR comment (DB record + hidden marker check). GitHub App registration guide.

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. (Needed for M1 live test) **GitHub App** registration. Instructions will be added with M1.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
