-- M8: online evaluation of production runs (sampled judge scores) and user feedback.

CREATE TABLE eval_samples (
    run_id       uuid PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    tenant_id    uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    agent        text        NOT NULL,
    prompt_version text,
    status       text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    judge_score  integer CHECK (judge_score BETWEEN 1 AND 5),
    reason       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    scored_at    timestamptz
);

CREATE TABLE run_feedback (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id      uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    actor       text        NOT NULL,
    rating      text        NOT NULL CHECK (rating IN ('up', 'down')),
    comment     text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_feedback_once UNIQUE (run_id, actor)
);

ALTER TABLE eval_samples ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON eval_samples
    USING (tenant_id = argus_current_tenant()) WITH CHECK (tenant_id = argus_current_tenant());
ALTER TABLE run_feedback ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_feedback
    USING (tenant_id = argus_current_tenant()) WITH CHECK (tenant_id = argus_current_tenant());

GRANT SELECT ON eval_samples TO argus_app;
GRANT SELECT, INSERT ON run_feedback TO argus_app;
GRANT UPDATE (rating, comment, created_at) ON run_feedback TO argus_app;
GRANT USAGE ON SEQUENCE run_feedback_id_seq TO argus_app;
