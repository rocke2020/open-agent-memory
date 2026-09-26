-- New asserted users receive the same personal capabilities as keyed users.
UPDATE grants AS g
SET capabilities = (
    SELECT array_agg(DISTINCT capability ORDER BY capability)
    FROM unnest(g.capabilities || ARRAY[
        'resource:write', 'source:delete', 'debug:read', 'export:read'
    ]) AS added(capability)
)
WHERE g.principal_id = 'user:' || g.user_id
  AND g.capabilities = ARRAY['memory:read', 'memory:append', 'operation:reconcile']
  AND NOT EXISTS (
      SELECT 1 FROM credentials AS c
      WHERE c.tenant_id = g.tenant_id AND c.principal_id = g.principal_id
  );

CREATE OR REPLACE FUNCTION oam_register_asserted_user(p_tenant_id text, p_user_id text)
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
                ARRAY['memory:read', 'memory:append', 'resource:write', 'source:delete',
                      'debug:read', 'export:read', 'operation:reconcile']);
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
