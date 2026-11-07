# Progress

## Current milestone

**All milestones M0–M9 are complete.** Remaining items need the owner (blockers below).

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

### M3 · Code RAG (merged, `v0.3.0`)

### M4 · Tenancy and auth
- Migration 0007: `users`, `memberships` (role + source), `api_keys` (hashed, role, repo limits, revocable), `audit_log` (append-only trigger, per-tenant hash chain via `argus_audit`, `argus_audit_verify`), `run_steps.tenant_id`, `pg_notify` trigger on runs.
- Row-level security on every tenant table; the API's `argus_app` role (NOBYPASSRLS, startup setting) + `tenant_conn` (transaction-local `app.tenant_id`). SECURITY DEFINER functions for pre-tenant lookups.
- GitHub OAuth login (state in Redis, no open redirects, user token not stored), membership sync from installations and org role, Redis sessions (hashed ids, HttpOnly/SameSite cookie), logout.
- Roles `viewer`/`member`/`admin`/`owner`; private repos also need the user's GitHub permission (installation token, cached 5 min); unknown = forbidden = 404.
- API: `/api/me`, `/api/runs` (+ `/{id}`, `/stream` SSE), `/api/qa`, `/api/keys` (create/list/revoke), `/api/members` (+ role change, last-owner guard), `/api/audit` (with chain verification). `/dashboard` page with live updates. Admin token now only for operator `/admin/dlq`.
- Agents audit every tool allow/deny with redacted arguments. `argus.redact` for secrets. ADR 0009.
- Tests (152 total): **RLS shows each tenant only its rows in every table with no WHERE; no tenant → nothing; cross-tenant insert rejected; tenant setting doesn't leak; API key of A gets 404 for B's runs/repos and never sees B's answers or code**; login creates memberships from GitHub, tenant choice, access removed when GitHub access is lost; bad OAuth state and open redirects rejected; private repo hidden until the user's GitHub permission allows it (denial audited); role gates; key lifecycle (hash only, repo limits, revoke); last owner protected; audit append-only and tamper-evident; audit details redacted; run events only reach their tenant.

### M4 · Tenancy and auth (merged, `v0.4.0`)

### M5 · LLM gateway
- `argus.gateway`: tier routes → model chains, per-tenant daily budgets (reserve/settle in Postgres, alert once at `alert_pct`), Redis Lua token buckets per tenant (tokens) and provider (requests), retries with full jitter honouring `Retry-After`, per-model circuit breaker (rolling window, cooldown, half-open probe) with fallback, total deadline, `llm_calls` accounting with cost.
- Providers: OpenAI-compatible and Anthropic Messages adapters (prompt-cache markers, cached tokens); streaming passthrough with a time-to-first-token deadline.
- Agents use `for_run(tenant, run, agent)`; step identity is the tier. Budget exhaustion fails the run; rate-limit waits re-queue without using attempts.
- Q&A semantic cache (tenant + repo + commit + role scope). Mock LLM gains `/v1/messages` and per-model outages (`fail_models`).
- API: `PUT /api/budget`, `GET /api/usage`. Migration 0008, ADR 0010.
- Tests (168 total): **budget stops a PR review run (failed, not retried, no comment, budget released)**; **outage of the primary fails over to the fallback, breaker opens after 3 failures and skips the dead model, then a probe closes it after recovery**; total outage is retryable and releases the reservation; `Retry-After` honoured and attempts bounded; 4xx not retried; tenant token bucket limits only that tenant; rate-limited messages retry without using attempts; 20 concurrent calls never overspend; alert fires once; Anthropic conversion and tool round trip; streaming and TTFT timeout; every call accounted with cost; semantic cache scoping and reuse.

### M5 · LLM gateway (merged, `v0.5.0`)

### M6 · Guardrails and approvals
- Injection classifier (weighted intent rules incl. wrapper escape, hidden unicode, multilingual overrides); flagged runs get a reduced tool set, a warning in the review, and an echo filter for verbatim-copied injected text.
- Output scanning (secrets + PII) before posting/returning; secret redaction in the JSON log formatter; wrapper-escape neutralisation; repo-relative paths only; egress allowlist transport in the worker.
- Approvals: per-tenant `require_approval_for` (`PUT /api/settings`), `approval` step parks the run in `waiting_approval` (no lease, no worker), `GET/POST /api/approvals` decide (member + repo access, final, audited), the decision re-queues the run which resumes by replay. Migration 0009.
- Injection eval `argus-eval-injection` (30 cases, hijacked-model mock) with ablations; report in `evals/reports/injection.md`. ADR 0011.
- Result: **ASR 3.5% (1/29), 0 false positives**; no classifier 31.0%, no output scanning 17.2%. The first run was 34.5% and found real gaps that were fixed (see ADR 0011).
- Tests (188 total): eval ASR ≤ 5%, no severe category succeeds, each layer matters; classifier positives/negatives; scanner; wrapper; path binding; egress; log redaction; flagged PR → reduced tools + warning; approval parks, viewer cannot decide, approve resumes without new LLM calls and posts once, decisions final; rejection cancels.

### M6 · Guardrails and approvals (merged, `v0.6.0`)

### M7 · Observability
- OpenTelemetry spans across every hop with W3C context in the outbox payload and stream fields: `webhook.receive` → `outbox.relay` → `queue.process` → `agent.run` → `step.*` → `llm.chat` / `tool *` → `HTTP *` (GitHub, providers). Retries stay in the same trace. Logs carry `trace_id`/`span_id`. OTLP/HTTP export (Jaeger/Tempo/collector/Langfuse) via `ARGUS_OTEL_ENDPOINT`.
- Prometheus metrics: HTTP, webhooks, outbox, queue depth + oldest age, messages, runs + duration, steps, LLM calls/tokens/cost/latency, breaker state; `/metrics` on API and ingest, `:9100` worker, `:9101` relay.
- SLO burn-rate alerts (webhook latency, availability, review latency) and symptom alerts, validated with promtool; Grafana dashboard; `docker-compose.observability.yml` (Prometheus, Grafana, Jaeger). ADR 0012.
- Tests (193 total): **one trace from the webhook to the GitHub comment POST, parent chain in order**; retry stays in the same trace; metrics exported; oldest-age gauge; route templates low-cardinality.

### M7 · Observability (merged, `v0.7.0`)

### M8 · Evals in CI
- Prompt-sensitive deterministic mock reviewer (heuristics on added lines; reports only what the prompt asks for; nitpicks/praise unless told to skip) and a mock judge.
- `argus-eval`: PR review golden set (20 PRs, 16 planted issues, 5 clean) with recall/precision/line accuracy; LLM judge + agreement with 10 hand-labelled reviews; Q&A retrieval recall@5 and citation accuracy (14 questions); injection ASR. Baseline in `evals/baseline.json`, history in `evals/history.jsonl`.
- Gate: tolerances per metric + minimum judge agreement; CI `evals` job (Postgres + Redis services) publishes the report to the job summary and fails on regression; the same gate runs in pytest.
- Online: sampled judge scores of production reviews (`jobs:eval`, by prompt version), thumbs up/down (`POST /api/runs/{id}/feedback`), `GET /api/evals/online`. Migration 0010. Prompt version recorded on every review. ADR 0013.
- Baseline: review recall 1.00, precision 1.00, line accuracy 1.00, judge 5.0 (agreement 1.0), Q&A retrieval@5 0.93, citations 0.93, ASR 3.5%.
- Tests (198 total): **deliberately worse prompt fails the gate** (recall 0, judge 2.0); dropping "security problems" from the prompt fails it; current prompt passes; gate rules incl. untrustworthy judge; online sampling, judging and feedback.

### M8 · Evals in CI (merged, `v0.8.0`)
- A deliberately worse prompt failed CI on a throwaway PR (#13, closed): review recall 1.00 → 0.00, judge 5.0 → 2.0.

### M9 · Scale and chaos
- Per-tenant fair scheduling (`FairQueue`): lanes per tenant, round-robin reads, cross-worker concurrency cap with slot leases, work-conserving borrowing, atomic pruning of idle tenants; relay routes fair topics by installation. Load shedding pauses indexing/evals while interactive queues lag. `argus-autoscale` (Little's law). Event reaper for events lost from Redis. Supersede also on newer accepted events. Queue failures while settling leave messages for reclaim. Migration 0011. ADR 0014.
- Local demo: `make demo` (fake GitHub service, generated demo App key, dev-only login, seeded PRs and Q&A in the dashboard) and `argus-demo`.
- k6 webhook burst script. [Load and chaos report](load-and-chaos-report.md): 300 webhooks/s at p99 114.8 ms, 0 failed, 0 lost; noisy neighbour quiet p95 1.9 s vs 5.9 s FIFO; Redis restart 40/40 with one comment each; six incidents found and fixed.
- Tests (211 total): noisy neighbour (done when), round-robin, cap across workers, dead worker cannot leak capacity, capped tenant cannot borrow while others wait, late lanes served, idle pruning, duplicate storm, force-push churn, Redis data loss, queue outage while settling, load shedding, autoscaling policy.

## After M9

- Live deployment and the GitHub App (blockers below), then a nightly real-model eval run.

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. **GitHub App** registration and install on a test repo: `docs/github-app.md`. Needed for M1's live "done when".
4. (Optional) Slack app for Q&A in Slack; Q&A is available over the API meanwhile.
5. (Optional, costs money) a real LLM provider key. Until then the bundled mock LLM produces canned reviews.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
