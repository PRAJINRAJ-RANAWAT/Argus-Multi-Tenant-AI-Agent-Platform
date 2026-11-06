# Progress

## Current milestone

**M0 · Skeleton** (in review)

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

## Next

- **M1 · Webhook to review comment:** GitHub App webhook endpoint with HMAC verify, `X-GitHub-Delivery` dedupe in Redis, event + outbox in one transaction, outbox relay → Redis Streams (`jobs:review`), one worker with consumer group, mock LLM server, a single LLM call that posts a PR comment via an installation token. Idempotency on (tenant, repo, pr, head_sha).

## Blockers waiting on the owner

1. **VPS + domain + DNS** for the deployed hello-world (M0 "done when"). Steps in `docs/deploy.md` §1–4.
2. **Repo secrets** `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and variable `ARGUS_DOMAIN` (`docs/deploy.md` §5).
3. (Needed for M1 live test) **GitHub App** registration. Instructions will be added with M1.

## Open questions

- None blocking. Default LLM provider for production (needed by M1 live run, not tests) to be decided when keys are available.
