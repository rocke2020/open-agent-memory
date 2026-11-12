ALTER TABLE users ADD COLUMN last_write_at timestamptz;

UPDATE users AS u
SET last_write_at = activity.last_write_at
FROM (
    SELECT sv.tenant_id, sv.user_id, max(sv.received_at) AS last_write_at
    FROM source_versions AS sv
    JOIN sources AS s USING (tenant_id, user_id, source_id)
    WHERE s.input_role IN ('interaction', 'evidence_resource', 'profile_resource')
    GROUP BY sv.tenant_id, sv.user_id
) AS activity
WHERE (u.tenant_id, u.user_id) = (activity.tenant_id, activity.user_id);

DO $roles$
BEGIN
    CREATE ROLE oam_user_list_owner NOLOGIN INHERIT NOSUPERUSER NOCREATEDB
        NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

GRANT SELECT (tenant_id, user_id, state, last_write_at) ON users TO oam_user_list_owner;
CREATE POLICY users_list_owner_policy ON users
    FOR SELECT TO oam_user_list_owner
    USING (tenant_id = NULLIF(current_setting('oam.admin_tenant_id', true), ''));
GRANT EXECUTE ON FUNCTION oam_credential_admin_tenant(bytea) TO oam_user_list_owner;

CREATE FUNCTION oam_list_users(
    p_actor_secret_digest bytea,
    p_limit integer,
    p_offset bigint,
    p_last_write_at_gte timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    actor_tenant text;
    result jsonb;
BEGIN
    actor_tenant := public.oam_credential_admin_tenant(p_actor_secret_digest);
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 10000 THEN
        RAISE EXCEPTION 'user page limit must be between 1 and 10000'
            USING ERRCODE = '22023';
    END IF;
    IF p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION 'user page offset must be non-negative' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_catalog.set_config('oam.admin_tenant_id', actor_tenant, true);
    SELECT pg_catalog.jsonb_build_object(
        'items', COALESCE(
            (SELECT pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'user_id', page.user_id,
                    'last_write_at', page.last_write_at
                ) ORDER BY page.user_id COLLATE "C"
            ) FROM (
                SELECT u.user_id, u.last_write_at
                FROM public.users AS u
                WHERE u.tenant_id = actor_tenant
                  AND u.state = 'active'
                  AND (p_last_write_at_gte IS NULL OR u.last_write_at >= p_last_write_at_gte)
                ORDER BY u.user_id COLLATE "C"
                LIMIT p_limit OFFSET p_offset
            ) AS page), '[]'::jsonb
        ),
        'total', (
            SELECT count(*)
            FROM public.users AS u
            WHERE u.tenant_id = actor_tenant
              AND u.state = 'active'
              AND (p_last_write_at_gte IS NULL OR u.last_write_at >= p_last_write_at_gte)
        )
    ) INTO result;
    RETURN result;
END
$function$;

ALTER FUNCTION oam_list_users(bytea, integer, bigint, timestamptz) OWNER TO oam_user_list_owner;
REVOKE ALL ON FUNCTION oam_list_users(bytea, integer, bigint, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_list_users(bytea, integer, bigint, timestamptz) TO oam_api;
