-- M4: users, memberships, API keys, hash-chained audit log, and row-level security.
--
-- The API serves user requests as the role ``argus_app`` (no BYPASSRLS). Every tenant
-- table has a policy comparing ``tenant_id`` with the transaction's ``app.tenant_id``;
-- with no tenant set, nothing is visible. Background services (ingest, relay, worker)
-- keep the owner role. Lookups that must cross tenants before a tenant is known (API
-- key → tenant, user → memberships) go through SECURITY DEFINER functions.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'argus_app') THEN
        CREATE ROLE argus_app NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;
GRANT argus_app TO CURRENT_USER;  -- lets a non-superuser owner SET ROLE argus_app

CREATE FUNCTION argus_current_tenant() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

-- Users and memberships ------------------------------------------------------------

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    github_id     bigint      NOT NULL UNIQUE,
    github_login  text        NOT NULL,
    name          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE memberships (
    tenant_id   uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id     uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role        text        NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    source      text        NOT NULL DEFAULT 'github' CHECK (source IN ('github', 'manual')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name          text        NOT NULL,
    prefix        text        NOT NULL UNIQUE,         -- shown in listings
    key_hash      text        NOT NULL UNIQUE,         -- sha256 of the full token
    role          text        NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    repo_ids      uuid[],                              -- NULL = every repo in the tenant
    created_by    uuid        REFERENCES users (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- Audit log: append-only, hash-chained per tenant ------------------------------------

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    at          timestamptz NOT NULL,
    actor_type  text        NOT NULL,                  -- user, api_key, agent, system
    actor_id    text,
    action      text        NOT NULL,                  -- e.g. qa.create, tool.call
    target      text,
    decision    text        NOT NULL CHECK (decision IN ('allow', 'deny', 'info')),
    details     jsonb,
    prev_hash   text        NOT NULL,
    hash        text        NOT NULL
);
CREATE INDEX audit_log_tenant_idx ON audit_log (tenant_id, id);

CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END
$$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

CREATE FUNCTION argus_audit_digest(
    p_prev text, p_tenant uuid, p_at timestamptz, p_actor_type text, p_actor_id text,
    p_action text, p_target text, p_decision text, p_details jsonb
) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(concat_ws('|',
        p_prev, p_tenant::text,
        to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        p_actor_type, coalesce(p_actor_id, ''), p_action, coalesce(p_target, ''),
        p_decision, coalesce(p_details::text, '')), 'UTF8')), 'hex')
$$;

CREATE FUNCTION argus_audit(
    p_tenant uuid, p_actor_type text, p_actor_id text, p_action text, p_target text,
    p_decision text, p_details jsonb
) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
    AS $$
DECLARE
    v_prev text;
    v_at timestamptz := clock_timestamp();
    v_id bigint;
BEGIN
    -- Serialise appends per tenant so the chain is linear.
    PERFORM pg_advisory_xact_lock(hashtextextended('argus_audit:' || p_tenant::text, 0));
    SELECT hash INTO v_prev FROM audit_log WHERE tenant_id = p_tenant ORDER BY id DESC LIMIT 1;
    v_prev := coalesce(v_prev, repeat('0', 64));
    INSERT INTO audit_log (tenant_id, at, actor_type, actor_id, action, target, decision,
                           details, prev_hash, hash)
    VALUES (p_tenant, v_at, p_actor_type, p_actor_id, p_action, p_target, p_decision,
            p_details, v_prev,
            argus_audit_digest(v_prev, p_tenant, v_at, p_actor_type, p_actor_id, p_action,
                               p_target, p_decision, p_details))
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;

-- First row id where the chain breaks for a tenant, or NULL if intact.
CREATE FUNCTION argus_audit_verify(p_tenant uuid) RETURNS bigint
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public
    AS $$
DECLARE
    r record;
    v_prev text := repeat('0', 64);
BEGIN
    FOR r IN SELECT * FROM audit_log WHERE tenant_id = p_tenant ORDER BY id LOOP
        IF r.prev_hash <> v_prev OR r.hash <> argus_audit_digest(
            r.prev_hash, r.tenant_id, r.at, r.actor_type, r.actor_id, r.action, r.target,
            r.decision, r.details) THEN
            RETURN r.id;
        END IF;
        v_prev := r.hash;
    END LOOP;
    RETURN NULL;
END
$$;

-- Pre-tenant lookups ---------------------------------------------------------------

CREATE FUNCTION argus_auth_api_key(p_hash text)
    RETURNS TABLE (key_id uuid, tenant_id uuid, role text, repo_ids uuid[], name text)
    LANGUAGE sql SECURITY DEFINER SET search_path = public
    AS $$
    UPDATE api_keys SET last_used_at = now()
    WHERE key_hash = p_hash AND revoked_at IS NULL
    RETURNING id, tenant_id, role, repo_ids, name
$$;

CREATE FUNCTION argus_user_memberships(p_user uuid)
    RETURNS TABLE (tenant_id uuid, account_login text, installation_id bigint, role text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    AS $$
    SELECT m.tenant_id, t.github_account_login, t.github_installation_id, m.role
    FROM memberships m JOIN tenants t ON t.id = m.tenant_id
    WHERE m.user_id = p_user AND t.suspended_at IS NULL
    ORDER BY t.github_account_login
$$;

-- run_steps gets its own tenant column so the policy is a plain comparison ----------

ALTER TABLE run_steps ADD COLUMN tenant_id uuid;
UPDATE run_steps s SET tenant_id = r.tenant_id FROM runs r WHERE r.id = s.run_id;
ALTER TABLE run_steps ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX run_steps_tenant_idx ON run_steps (tenant_id);

-- Row-level security -----------------------------------------------------------------

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants USING (id = argus_current_tenant());

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['repos', 'runs', 'run_steps', 'side_effects', 'code_files',
                             'code_chunks', 'code_refs', 'embeddings', 'repo_index_state',
                             'memberships', 'api_keys', 'audit_log']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = argus_current_tenant())'
            ' WITH CHECK (tenant_id = argus_current_tenant())', t);
    END LOOP;
END
$$;

-- users is global: a user sees only their own row through the API role.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY self_only ON users
    USING (id = nullif(current_setting('app.user_id', true), '')::uuid);

-- Privileges for the API role -------------------------------------------------------

GRANT USAGE ON SCHEMA public TO argus_app;
GRANT SELECT ON tenants, repos, runs, run_steps, code_files, code_chunks, code_refs,
                repo_index_state, memberships, api_keys, audit_log, users TO argus_app;
GRANT INSERT ON runs, api_keys TO argus_app;
GRANT UPDATE (revoked_at) ON api_keys TO argus_app;
GRANT UPDATE (role, source, updated_at) ON memberships TO argus_app;
GRANT INSERT ON outbox TO argus_app;
GRANT USAGE ON SEQUENCE outbox_id_seq TO argus_app;
GRANT EXECUTE ON FUNCTION argus_audit(uuid, text, text, text, text, text, jsonb),
                          argus_audit_verify(uuid), argus_auth_api_key(text),
                          argus_user_memberships(uuid), argus_current_tenant()
    TO argus_app;
-- No grants on events, outbox reads, side_effects writes, or audit_log writes: the API
-- role appends to the audit log only through argus_audit().

-- Live run events for the dashboard (SSE) -----------------------------------------

CREATE FUNCTION runs_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM pg_notify('argus_runs', json_build_object(
            'tenant_id', NEW.tenant_id, 'run_id', NEW.id, 'repo_id', NEW.repo_id,
            'agent_type', NEW.agent_type, 'status', NEW.status,
            'pr_number', NEW.pr_number)::text);
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER runs_notify AFTER INSERT OR UPDATE OF status ON runs
    FOR EACH ROW EXECUTE FUNCTION runs_notify();
