-- Existing grants remain stored, but only a key bound to the same user can use them.
CREATE OR REPLACE FUNCTION oam_authorize_user(
    p_tenant_id text,
    p_principal_id text,
    p_user_id text,
    p_capability text
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.users AS u
        JOIN public.grants AS g
          ON (g.tenant_id, g.user_id) = (u.tenant_id, u.user_id)
        JOIN public.principals AS p
          ON (p.tenant_id, p.principal_id) = (g.tenant_id, g.principal_id)
        WHERE u.tenant_id = p_tenant_id
          AND u.user_id = p_user_id
          AND u.state = 'active'
          AND g.principal_id = p_principal_id
          AND p.role = 'user'
          AND p.state = 'active'
          AND p.principal_id = 'user:' || p_user_id
          AND p_capability = ANY (g.capabilities)
    )
$function$;

-- Preserve existing collections while preparing an owner principal for each one.
DO $verify_user_principals$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM users AS u
        JOIN principals AS p
          ON p.tenant_id = u.tenant_id AND p.principal_id = 'user:' || u.user_id
    ) THEN
        RAISE EXCEPTION 'principal name collision with personal user identity';
    END IF;
END
$verify_user_principals$;

INSERT INTO principals (tenant_id, principal_id, role, state)
SELECT tenant_id, 'user:' || user_id, 'user', 'active' FROM users
ON CONFLICT (tenant_id, principal_id) DO NOTHING;

INSERT INTO grants (tenant_id, user_id, principal_id, capabilities)
SELECT tenant_id, user_id, 'user:' || user_id,
       ARRAY['memory:read', 'memory:append', 'operation:reconcile']
FROM users
ON CONFLICT (tenant_id, user_id, principal_id) DO NOTHING;

GRANT INSERT ON users, grants, principals TO oam_auth_owner;
CREATE POLICY users_auth_owner_insert_policy ON users
    FOR INSERT TO oam_auth_owner WITH CHECK (true);
CREATE POLICY grants_auth_owner_insert_policy ON grants
    FOR INSERT TO oam_auth_owner WITH CHECK (true);

CREATE FUNCTION oam_create_user_with_key(
    p_actor_secret_digest bytea,
    p_user_id text,
    p_credential_id text,
    p_secret_digest bytea
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE actor_tenant text;
BEGIN
    actor_tenant := public.oam_credential_admin_tenant(p_actor_secret_digest);
    IF NULLIF(btrim(p_user_id), '') IS NULL THEN
        RAISE EXCEPTION 'user_id cannot be empty' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.users (tenant_id, user_id, subject, namespace, lifetime)
    VALUES (actor_tenant, p_user_id, p_user_id, 'personal', 'persistent');
    INSERT INTO public.principals (tenant_id, principal_id, role, state)
    VALUES (actor_tenant, 'user:' || p_user_id, 'user', 'active');
    INSERT INTO public.grants (tenant_id, user_id, principal_id, capabilities)
    VALUES (actor_tenant, p_user_id, 'user:' || p_user_id,
            ARRAY['memory:read', 'memory:append', 'operation:reconcile']);
    INSERT INTO public.credentials
        (tenant_id, credential_id, principal_id, secret_digest, state)
    VALUES (actor_tenant, p_credential_id, 'user:' || p_user_id,
            p_secret_digest, 'active');
END
$function$;
ALTER FUNCTION oam_create_user_with_key(bytea, text, text, bytea) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_create_user_with_key(bytea, text, text, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_create_user_with_key(bytea, text, text, bytea) TO oam_api;
