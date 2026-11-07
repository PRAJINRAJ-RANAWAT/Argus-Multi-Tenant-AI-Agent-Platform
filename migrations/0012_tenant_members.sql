-- Members with their GitHub logins for the dashboard. ``users`` is row-level-secured to
-- the user's own row, so this SECURITY DEFINER function returns the members of one
-- tenant only; the API calls it with the caller's tenant.
CREATE FUNCTION argus_tenant_members(p_tenant uuid)
    RETURNS TABLE (user_id uuid, github_login text, name text, role text, source text,
                   last_login_at timestamptz, created_at timestamptz)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    AS $$
    SELECT u.id, u.github_login, u.name, m.role, m.source, u.last_login_at, m.created_at
    FROM memberships m JOIN users u ON u.id = m.user_id
    WHERE m.tenant_id = p_tenant
    ORDER BY m.created_at
$$;
GRANT EXECUTE ON FUNCTION argus_tenant_members(uuid) TO argus_app;
