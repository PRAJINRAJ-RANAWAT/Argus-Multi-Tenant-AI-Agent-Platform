# 0009. Tenant isolation with row-level security; GitHub as the source of permissions

- Status: accepted
- Date: 2026-11-07

## Context

M4 must prove that tenant A cannot read tenant B's runs, code or answers, and that an agent never has more access than the human it acts for. Application-level `WHERE tenant_id = …` filters are one forgotten clause away from a leak. Users are GitHub users, tenants are GitHub App installations, and repository access is GitHub's to decide.

## Decision

- **Two database roles.** The API serves user requests as `argus_app` (NOBYPASSRLS), passed as a connection startup setting so pool resets keep it. Background services (ingest, relay, workers) keep the owner role; they act for the system, not for a user.
- **RLS on every tenant table** (`tenant_id = argus_current_tenant()`, plus `WITH CHECK`). `tenant_conn()` sets `app.tenant_id` transaction-locally; with no tenant set, nothing is visible. `argus_app` has no grant at all on `events`, `outbox` reads, `side_effects` or `embeddings`.
- **SECURITY DEFINER functions** for the only lookups that must cross tenants before one is known: API key → tenant (`argus_auth_api_key`), user → memberships (`argus_user_memberships`), and audit appends.
- **Login with GitHub OAuth (the App's user flow).** The user token is used once to read identity, reachable installations and org role, and is not stored. Memberships are reconciled at each login, so leaving the org removes access. Sessions are random ids in an HttpOnly, SameSite=Lax cookie; Redis stores only their hash.
- **Effective permission = Argus role ∧ GitHub permission.** Roles gate actions (`viewer` < `member` < `admin` < `owner`). Private repositories additionally need the user's own GitHub permission, checked with the installation token through the collaborator-permission API and cached for 5 minutes. Unknown and forbidden repositories both return 404.
- **API keys** are `argus_<prefix>_<secret>`, stored as SHA-256, shown once, bound to one tenant, no higher than their creator's role, optionally limited to repositories, and revocable.
- **Audit log**: append-only (trigger blocks UPDATE and DELETE; the API role cannot insert directly), hash-chained per tenant under an advisory lock, verifiable with `argus_audit_verify`. Logins, key and role changes, Q&A requests, denials, and every agent tool allow/deny (arguments redacted) are recorded.
- **Live runs** for the dashboard come from a `pg_notify` trigger on `runs`, fanned out per tenant over SSE.

## Consequences

- A missing `WHERE` in an API query returns only the caller's tenant's rows; a missing tenant context returns nothing. Tests check every RLS table both ways.
- Superusers and the owner role bypass RLS, so workers' correctness still depends on their own filters (e.g. search filters by tenant inside SQL). Moving workers to a tenant-scoped role per run is a later hardening step.
- The GitHub permission check costs one API call per (user, repo) per 5 minutes; a revoked GitHub permission can take that long to apply.
- A superuser can still rewrite the audit log, but not without breaking the hash chain.

## When we would revisit

- Enterprise customers needing SSO beyond GitHub → add an OIDC provider while keeping GitHub as the permission source for repositories.
- Many tenants per database becoming a noisy-neighbour or compliance problem → schema- or database-per-tenant for large tenants.
