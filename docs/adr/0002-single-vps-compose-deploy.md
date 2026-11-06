# 0002. Deploy with Docker Compose on one VPS behind Caddy, images in GHCR

- Status: accepted
- Date: 2026-11-07

## Context

M0 requires "a push to main deploys automatically" to a public domain. The spec's deployment path starts with Docker Compose on one VPS behind Cloudflare with TLS, then splits services and moves to managed Postgres, and as a stretch goes to k3s + KEDA.

## Decision

- GitHub Actions builds one image per commit on `main` and pushes `ghcr.io/prajinraj-ranawat/argus:<git sha>` (and `:latest`).
- The deploy job copies `deploy/` (compose file, Caddyfile, `deploy.sh`) to `/opt/argus` over SSH (pinned host key via `DEPLOY_KNOWN_HOSTS`), logs in to GHCR with the short-lived workflow token sent over stdin, pulls, runs migrations as a one-off container, then `up -d`.
- **Caddy** terminates TLS (automatic Let's Encrypt) and routes `/webhooks/*` to ingest and everything else to the API. Cloudflare sits in front in "Full (strict)" mode.
- A smoke test polls `https://$ARGUS_DOMAIN/healthz` until it reports the deployed `git_sha`, so a deploy only goes green when the new revision is actually serving.
- If deploy secrets are not configured yet, the job warns and skips instead of failing, so `main` stays green before the VPS exists.
- Postgres and Redis run on the same VPS with named volumes (backups: see `docs/deploy.md`).

## Consequences

- Single point of failure, with a short restart blip on each deploy (`up -d` recreates containers). Fine for the 99.5% single-region target at demo scale.
- Migrations must stay backward compatible with the previous revision (expand/contract), because old containers may briefly run against the new schema.

## When we would revisit

- Need zero-downtime deploys or more than one host → separate services and managed Postgres (spec step 2), then k3s with KEDA scaling workers on stream lag (step 3).
