-- Register a first-seen asserted user only inside an existing active tenant.
ALTER TABLE bootstrap_state ADD COLUMN auto_dev_initialized boolean NOT NULL DEFAULT false;
GRANT SELECT, UPDATE ON bootstrap_state TO oam_auth_owner;

CREATE FUNCTION oam_has_active_root()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.credentials AS c
        JOIN public.principals AS p
          ON (p.tenant_id, p.principal_id) = (c.tenant_id, c.principal_id)
        JOIN public.tenants AS t ON t.tenant_id = c.tenant_id
        WHERE c.state = 'active'
          AND (c.expires_at IS NULL OR c.expires_at > statement_timestamp())
          AND p.state = 'active' AND p.role = 'administrator' AND t.state = 'active'
          AND EXISTS (
              SELECT 1 FROM public.bootstrap_state AS b
              WHERE b.singleton = true AND b.phase NOT IN ('committed', 'recovery_required')
          )
    )
$function$;
ALTER FUNCTION oam_has_active_root() OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_has_active_root() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_has_active_root() TO oam_api;

CREATE FUNCTION oam_choose_auto_auth_mode()
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE current_phase text;
DECLARE dev_initialized boolean;
DECLARE any_credentials boolean;
BEGIN
    SELECT phase, auto_dev_initialized INTO current_phase, dev_initialized
    FROM public.bootstrap_state WHERE singleton = true FOR UPDATE;
    SELECT EXISTS (SELECT 1 FROM public.credentials) INTO any_credentials;
    IF current_phase IN ('committed', 'recovery_required') THEN
        RETURN NULL;
    END IF;
    IF current_phase <> 'uninitialized' OR any_credentials THEN
        IF public.oam_has_active_root() THEN
            RETURN 'api_key';
        END IF;
        RETURN NULL;
    END IF;
    IF NOT dev_initialized AND EXISTS (SELECT 1 FROM public.users) THEN
        RETURN NULL;
    END IF;
    UPDATE public.bootstrap_state SET auto_dev_initialized = true WHERE singleton = true;
    RETURN 'dev';
END
$function$;
ALTER FUNCTION oam_choose_auto_auth_mode() OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_choose_auto_auth_mode() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_choose_auto_auth_mode() TO oam_api;

CREATE FUNCTION oam_register_asserted_user(p_tenant_id text, p_user_id text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE created_user text;
BEGIN
    IF NULLIF(btrim(p_tenant_id), '') IS NULL OR NULLIF(btrim(p_user_id), '') IS NULL THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.tenants
        WHERE tenant_id = p_tenant_id AND state = 'active'
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO public.users (tenant_id, user_id, subject, namespace, lifetime)
    VALUES (p_tenant_id, p_user_id, p_user_id, 'personal', 'persistent')
    ON CONFLICT (tenant_id, user_id) DO NOTHING
    RETURNING user_id INTO created_user;
    IF created_user IS NOT NULL THEN
        INSERT INTO public.principals (tenant_id, principal_id, role, state)
        VALUES (p_tenant_id, 'user:' || p_user_id, 'user', 'active');
        INSERT INTO public.grants (tenant_id, user_id, principal_id, capabilities)
        VALUES (p_tenant_id, p_user_id, 'user:' || p_user_id,
                ARRAY['memory:read', 'memory:append', 'operation:reconcile']);
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.users AS u
        JOIN public.principals AS p
          ON p.tenant_id = u.tenant_id AND p.principal_id = 'user:' || u.user_id
        JOIN public.grants AS g
          ON (g.tenant_id, g.user_id, g.principal_id) =
             (u.tenant_id, u.user_id, p.principal_id)
        WHERE u.tenant_id = p_tenant_id AND u.user_id = p_user_id
          AND u.state = 'active' AND p.state = 'active' AND p.role = 'user'
    );
END
$function$;
ALTER FUNCTION oam_register_asserted_user(text, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_register_asserted_user(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_register_asserted_user(text, text) TO oam_api;
