-- Tenants (GitHub App installations), repos, agent runs and side-effect ledger.

CREATE TABLE tenants (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    github_installation_id  bigint      NOT NULL UNIQUE,
    github_account_login    text        NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    suspended_at            timestamptz
);

CREATE TABLE repos (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    github_repo_id  bigint      NOT NULL,
    full_name       text        NOT NULL,
    private         boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT repos_tenant_github_uniq UNIQUE (tenant_id, github_repo_id)
);

CREATE TABLE runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    repo_id           uuid        REFERENCES repos (id) ON DELETE CASCADE,
    agent_type        text        NOT NULL,                  -- 'pr_review', 'qa', ...
    trigger_event_id  uuid        REFERENCES events (id) ON DELETE SET NULL,
    idempotency_key   text        NOT NULL,                  -- e.g. review:<repo>:<pr>:<sha>
    pr_number         integer,
    head_sha          text,
    status            text        NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded',
                                        'failed', 'cancelled', 'superseded')),
    attempts          integer     NOT NULL DEFAULT 0,
    lease_owner       text,
    lease_expires_at  timestamptz,
    lease_version     bigint      NOT NULL DEFAULT 0,        -- fencing token
    input_tokens      bigint      NOT NULL DEFAULT 0,
    output_tokens     bigint      NOT NULL DEFAULT 0,
    cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
    error             text,
    result            jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz,
    CONSTRAINT runs_tenant_idem_uniq UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX runs_tenant_created_idx ON runs (tenant_id, created_at DESC);
CREATE INDEX runs_pr_idx ON runs (repo_id, pr_number, created_at DESC);
-- Lease reaper scans: expired running runs, and queued runs nobody picked up.
CREATE INDEX runs_active_idx ON runs (status, lease_expires_at)
    WHERE status IN ('queued', 'running');

-- Ledger of external side effects (GitHub comments, Slack messages) keyed by idempotency key.
CREATE TABLE side_effects (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id           uuid        REFERENCES runs (id) ON DELETE SET NULL,
    idempotency_key  text        NOT NULL,
    kind             text        NOT NULL,                   -- 'github.issue_comment', ...
    status           text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    lease_version    bigint,                                 -- fencing token of the claimer
    external_id      text,
    claimed_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    CONSTRAINT side_effects_tenant_idem_uniq UNIQUE (tenant_id, idempotency_key)
);
