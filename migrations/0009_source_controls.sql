-- Give each personal owner the distinct Resource, lifecycle, debug and export grants.
UPDATE grants
SET capabilities = (
    SELECT array_agg(DISTINCT capability ORDER BY capability)
    FROM unnest(capabilities || ARRAY[
        'resource:write', 'source:delete', 'debug:read', 'export:read'
    ]) AS added(capability)
)
WHERE principal_id = 'user:' || user_id
  AND capabilities @> ARRAY['memory:read', 'memory:append'];

CREATE OR REPLACE FUNCTION oam_create_user_with_key(
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
            ARRAY['memory:read', 'memory:append', 'resource:write', 'source:delete',
                  'debug:read', 'export:read', 'operation:reconcile']);
    INSERT INTO public.credentials
        (tenant_id, credential_id, principal_id, secret_digest, state)
    VALUES (actor_tenant, p_credential_id, 'user:' || p_user_id,
            p_secret_digest, 'active');
END
$function$;
