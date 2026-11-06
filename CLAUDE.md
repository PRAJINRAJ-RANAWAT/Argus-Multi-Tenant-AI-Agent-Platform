# Argus: project conventions

Multi-tenant AI agent platform. The full spec is `docs/SPEC.md`; read it before starting a milestone. Current status lives in `docs/PROGRESS.md`; decisions in `docs/adr/`.

## Layout

- `src/argus/`: one Python package, many services (selected by entry point / command)
  - `api/`: dashboard backend, OAuth, approvals
  - `ingest/`: webhook verify → dedupe → event + outbox in one tx
  - `worker/`: queue consumers / agent runtime
  - `db/`: asyncpg pool, migration runner
  - `config.py` (env vars, prefix `ARGUS_`), `logging.py` (JSON logs with trace/tenant/run ids), `health.py`, `service.py` (FastAPI factory)
- `migrations/NNNN_name.sql`: forward-only SQL migrations, each run in one transaction under an advisory lock. Never edit an applied migration; add a new one.
- `tests/`: pytest; integration tests use testcontainers (real Postgres+pgvector and Redis). Docker must be running.
- `docker/Dockerfile`: single image for all services. `docker-compose.yml` = local stack. `deploy/` = production compose, Caddyfile, deploy script.

## Commands

- `make install`: `uv sync` + enable git hooks
- `make check`: ruff lint + format check, mypy (strict), pytest
- `make up` / `make down`: local stack (api :8000, ingest :8001, Postgres :5432, Redis :6379)
- `uv run argus-migrate`: apply migrations

## Engineering rules

- Python 3.12, asyncio, FastAPI, asyncpg (raw SQL; no ORM), redis-py asyncio. mypy strict must pass.
- Write tests with the code. Concurrency-critical code (outbox, leases, fencing, idempotency, rate limiting) needs tests that simulate failures and races against real Postgres/Redis.
- Use the mock LLM server for all tests and load tests. Real provider keys only from env vars.
- Never commit secrets. `.env.example` holds placeholders. gitleaks runs in CI.
- Record significant decisions as ADRs in `docs/adr/NNNN-title.md`.

## Git rules

- `main` = production, always deployable; never commit or push to it directly. Work on `dev`.
- Small Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`, `ci:`). Push dev after each commit or small group.
- Use the configured git identity. Never change user.name/user.email, never use `--author`.
- Checkpoint (end of each milestone or meaningful sub-milestone):
  1. `make check` passes locally.
  2. Update `docs/PROGRESS.md`.
  3. `gh pr create --base main --head dev` with what changed, how it was tested, decisions.
  4. Wait for CI green. Never merge red; never weaken tests to pass.
  5. `gh pr merge --merge` (merge commit only; no squash or rebase).
  6. Tag main `v0.<milestone>.<n>` and push the tag.
  7. Pull main back into dev.
- Attribution: no AI author, co-author, or mention anywhere (commits, PRs, tags, release notes, metadata). `.githooks/commit-msg` strips such trailers (`git config core.hooksPath .githooks`). Before every push run:
  `git log --format=%B origin/main..HEAD | grep -iE "co-authored-by|generated with|claude-session"`. It must print nothing. Check PR bodies the same way.

## Deployment

Push to `main` → `.github/workflows/deploy.yml` builds `ghcr.io/prajinraj-ranawat/argus:<sha>`, SSHes to the VPS, runs `deploy/deploy.sh`, then smoke-tests `https://$ARGUS_DOMAIN/healthz` for the new `git_sha`. See `docs/deploy.md`.
