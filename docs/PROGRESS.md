# Progress

## Current milestone

**M2 · Durable agent runtime**: M2a (checkpointed steps, heartbeats, supersede, budgets, DLQ replay) in review; M2b (tool-calling loop over MCP) next.

M1's live "done when" (install the App → a review appears) is still waiting on the VPS and GitHub App (blockers below).

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

### M2a · Durable runtime core
- `run_steps` (migration 0004): each LLM/tool step is written `started` (fenced on `lease_version`), executed, then marked `done` with output and tokens in one fenced transaction. On resume, `done` steps replay from storage; a changed input hash is a `NonDeterminismError`.
- Short lease (30 s) renewed by `LeaseKeeper` every 10 s; a lost lease is detected and every later write is fenced off.
- Budgets per run (`max_steps`, `max_tokens`, `deadline_at`) checked before each step; exceeding one fails the run without retrying.
- PR review rewritten as three steps (fetch diff → LLM → post comment via the side-effect ledger).
- Supersede: between steps a run checks for a newer run of the same PR (ordered by the trigger's first receipt) and stops as `superseded`; a stale queued run is superseded before it starts. Owner-checked Redis lock per (repo, PR) keeps one live review.
- DLQ: `GET /admin/dlq/{topic}`, `POST /admin/dlq/{topic}/replay` (ids or all), atomic Lua move, guarded by `ARGUS_ADMIN_TOKEN`.
- ADR 0006: build the runtime on Postgres vs adopt Temporal, and when we would switch.
- Tests (113 total): crash between steps replays without re-calling the LLM; crash mid-step re-executes; zombie cannot start or finish a step; heartbeat keeps a 0.6 s lease alive for 1.5 s; `kill -9` while posting the comment → reaper → resume replays 2 steps, 1 comment; new push mid-review → old run superseded, PR lock busy until it stops, 1 comment for the new SHA; late old message superseded before starting; step budget fails the run; 8 concurrent DLQ replays enqueue each entry once.

## Next

- **M2b · Tool-calling loop over MCP:** `argus-code` and `argus-github` MCP servers (official SDK), the agent runtime as MCP client, per-agent tool allowlists with read/write tags, write tools keyed by step through the side-effect ledger. The review agent moves from a fixed pipeline to an LLM-driven tool loop (mock LLM gains tool calls).

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. **GitHub App** registration and install on a test repo: `docs/github-app.md`. Needed for M1's live "done when".
4. (Optional, costs money) a real LLM provider key. Until then the bundled mock LLM produces canned reviews.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
