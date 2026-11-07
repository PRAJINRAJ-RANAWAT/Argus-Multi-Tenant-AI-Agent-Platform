# Progress

## Current milestone

**M3 · Code RAG**: in review. Next: M4 · Tenancy and auth.

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

### M2b · Tool-calling loop over MCP
- MCP servers with the official SDK (v2): `argus-code` (`get_pr_diff`, `list_changed_files`, `read_file`) and `argus-github` (`get_ci_status`, `post_review_comment`, `open_issue`). Built per run and bound to its repo/PR/head SHA; `read_file` rejects traversal.
- `ToolHub` (MCP client) + `AgentToolPolicy`: model tools vs runtime-only tools per agent; everything else denied and recorded as a step. Write tools take a runtime-supplied `idempotency_key` (hidden from the model) and go through the side-effect ledger.
- `run_tool_loop`: each LLM turn and tool call is a checkpointed step; tool output wrapped as untrusted data; supersede checked between steps. Tool crashes (GitHub 5xx) retry the step; deliberate tool errors go back to the model.
- LLM client parses `tool_calls`; mock LLM gains `tool_script` (scripted tool calls per turn; default reads the diff).
- PR review = tool loop + one runtime-posted comment. ADR 0007.
- Tests (118 total): model drives 4 read tools then one comment; `open_issue`/`post_review_comment`/unknown tool from the model denied and audited, no issue created; traversal and missing file are tool errors; GitHub 502 on a tool → retry, step attempts 2, one comment; model never sees write tools or `idempotency_key`; `kill -9` while posting → resume replays 3 steps, one comment.

### M2 · Durable agent runtime (merged, `v0.2.0` + `v0.2.1`)

### M3 · Code RAG
- Migration 0005: `code_files` (per path, guarded by event receipt time, tombstones), `code_chunks` (vector(256) HNSW + `tsvector` GIN), `code_refs` (call graph), per-tenant `embeddings` cache, `repo_index_state`. Migration 0006: run `input` / `requested_by`.
- tree-sitter chunking (Python, JS, TS/TSX, Go): functions, `Class.method`, class headers, module gaps, long-span splits, call edges; docs/text by line windows; vendored/lock/binary/huge files skipped. Hash covers embedded text, not line numbers.
- Indexing on `jobs:index`: `push` to the default branch → changed paths only (full index if ≥20 commits or forced); `installation` / `installation_repositories` → full index. Only cache misses are embedded.
- Embedders: deterministic hashing embedder (also `/v1/embeddings` on the mock LLM), OpenAI-compatible HTTP embedder with `dimensions=256`.
- Hybrid search (full-text + vector, RRF, lexical re-rank) with tenant/repo filters inside SQL; citations `path:start-end@sha` + GitHub links; `find_callers` from the call graph.
- `argus-code` MCP tools `search_code`, `find_callers` (reviewer and Q&A).
- Codebase Q&A: `POST /api/qa` (outbox → `jobs:qa`, Idempotency-Key), `GET /api/runs/{id}`; tool loop over search/callers/read_file; citations verified against the index. Admin token until M4. ADR 0008.
- Tests (140 total): **one-line push embeds exactly one chunk**; push webhook end to end reads only the changed file; installation → full index; late older push never overwrites newer content; deletes leave tombstones that beat late events; 6 concurrent indexers of one file stay consistent; call graph; embedding cache never crosses tenants; search by meaning and identifier; search never crosses tenants or repos (even with a foreign repo id); Q&A end to end with verified citations and idempotent create.

## Next

- **M4 · Tenancy and auth:** GitHub OAuth login, sessions, roles (`owner`/`admin`/`member`/`viewer`), effective permission = role ∧ GitHub repo permission, row-level security with `app.tenant_id`, hashed API keys, append-only audit log (hash-chained), dashboard with live runs over SSE. Done when tests prove tenant A cannot read tenant B's runs, code or answers.

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. **GitHub App** registration and install on a test repo: `docs/github-app.md`. Needed for M1's live "done when".
4. (Optional) Slack app for Q&A in Slack; Q&A is available over the API meanwhile.
5. (Optional, costs money) a real LLM provider key. Until then the bundled mock LLM produces canned reviews.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
