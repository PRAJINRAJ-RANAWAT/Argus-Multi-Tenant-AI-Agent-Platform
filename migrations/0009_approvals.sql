-- M6: per-tenant guardrail settings and human approval of write actions.

CREATE TABLE tenant_settings (
    tenant_id             uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    require_approval_for  text[]      NOT NULL DEFAULT '{}',   -- tool names
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id       uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    step_key     text        NOT NULL,
    tool         text        NOT NULL,
    preview      jsonb       NOT NULL,
    status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    decided_by   text,
    reason       text,
    CONSTRAINT approvals_run_step_uniq UNIQUE (run_id, step_key)
);
CREATE INDEX approvals_pending_idx ON approvals (tenant_id, requested_at) WHERE status = 'pending';

ALTER TABLE tenant_settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_settings
    USING (tenant_id = argus_current_tenant()) WITH CHECK (tenant_id = argus_current_tenant());
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approvals
    USING (tenant_id = argus_current_tenant()) WITH CHECK (tenant_id = argus_current_tenant());

GRANT SELECT, INSERT, UPDATE ON tenant_settings TO argus_app;
GRANT SELECT ON approvals TO argus_app;
GRANT UPDATE (status, decided_at, decided_by, reason) ON approvals TO argus_app;
-- A decision re-queues the parked run (RLS keeps it to the caller's tenant).
GRANT UPDATE (status, updated_at) ON runs TO argus_app;
