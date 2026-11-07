-- M5: LLM gateway accounting, per-tenant daily budgets, Q&A semantic cache.

CREATE TABLE tenant_budgets (
    tenant_id     uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    daily_tokens  bigint      NOT NULL CHECK (daily_tokens > 0),
    alert_pct     integer     NOT NULL DEFAULT 80 CHECK (alert_pct BETWEEN 1 AND 100),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ``reserved`` holds estimates for calls in flight; reconciled to ``used`` afterwards.
CREATE TABLE tenant_usage_daily (
    tenant_id   uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    day         date        NOT NULL,
    used        bigint      NOT NULL DEFAULT 0,
    reserved    bigint      NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    cost_usd    numeric(14, 6) NOT NULL DEFAULT 0,
    alerted_at  timestamptz,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE llm_calls (
    id              bigserial PRIMARY KEY,
    tenant_id       uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id          uuid        REFERENCES runs (id) ON DELETE SET NULL,
    agent           text,
    tier            text        NOT NULL,
    provider        text        NOT NULL,
    model           text        NOT NULL,
    route_reason    text        NOT NULL,          -- primary, fallback:breaker_open, ...
    status          text        NOT NULL,          -- ok, error, rate_limited, budget, cache_hit
    attempts        integer     NOT NULL DEFAULT 1,
    input_tokens    integer     NOT NULL DEFAULT 0,
    output_tokens   integer     NOT NULL DEFAULT 0,
    cached_tokens   integer     NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    latency_ms      integer,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_tenant_idx ON llm_calls (tenant_id, created_at DESC);
CREATE INDEX llm_calls_run_idx ON llm_calls (run_id);

-- Q&A semantic cache: answers are reused only within the same tenant, repo, indexed
-- commit and requester role, so a cached answer never crosses permissions.
CREATE TABLE qa_cache (
    id           bigserial PRIMARY KEY,
    tenant_id    uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    repo_id      uuid        NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    commit_sha   text        NOT NULL,
    role_scope   text        NOT NULL,
    question     text        NOT NULL,
    embedding    vector(256) NOT NULL,
    result       jsonb       NOT NULL,
    source_run   uuid        REFERENCES runs (id) ON DELETE SET NULL,
    hits         integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX qa_cache_scope_idx ON qa_cache (tenant_id, repo_id, commit_sha, role_scope);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenant_budgets', 'tenant_usage_daily', 'llm_calls', 'qa_cache']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = argus_current_tenant())'
            ' WITH CHECK (tenant_id = argus_current_tenant())', t);
    END LOOP;
END
$$;

GRANT SELECT ON tenant_budgets, tenant_usage_daily, llm_calls TO argus_app;
GRANT INSERT, UPDATE ON tenant_budgets TO argus_app;
