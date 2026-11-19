-- Profile policy contains scheduling metadata only; generated content stays in profiles.
CREATE TABLE profile_refresh_policies (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    next_check_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    pending_mode text CHECK (pending_mode IN ('incremental', 'full')),
    last_admitted_at timestamptz,
    last_success_at timestamptz,
    last_full_at timestamptz,
    paused boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, user_id)
);

ALTER TABLE profile_refresh_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_refresh_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY profile_refresh_policies_user_policy ON profile_refresh_policies
    FOR ALL TO oam_content
    USING (oam_user_visible(tenant_id, user_id))
    WITH CHECK (oam_user_visible(tenant_id, user_id));
REVOKE ALL ON profile_refresh_policies FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON profile_refresh_policies TO oam_content;
CREATE INDEX profile_refresh_policies_due
    ON profile_refresh_policies (next_check_at, tenant_id, user_id)
    WHERE NOT paused;

DO $roles$
BEGIN
    CREATE ROLE oam_profile_schedule_owner NOLOGIN NOSUPERUSER NOCREATEDB
        NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

GRANT SELECT (tenant_id, user_id, policy, next_check_at, paused)
    ON profile_refresh_policies TO oam_profile_schedule_owner;
GRANT UPDATE (next_check_at) ON profile_refresh_policies TO oam_profile_schedule_owner;
CREATE POLICY profile_refresh_policies_schedule_read ON profile_refresh_policies
    FOR SELECT TO oam_profile_schedule_owner USING (true);
CREATE POLICY profile_refresh_policies_schedule_update ON profile_refresh_policies
    FOR UPDATE TO oam_profile_schedule_owner USING (true) WITH CHECK (true);
GRANT SELECT (tenant_id, user_id, state) ON users TO oam_profile_schedule_owner;
CREATE POLICY users_profile_schedule_read ON users
    FOR SELECT TO oam_profile_schedule_owner USING (true);

-- Discovery advances a bounded rotating batch, releasing these locks before
-- the caller takes a user-content lock and decides whether to admit work.
CREATE FUNCTION oam_due_profile_policies(p_limit integer, p_poll_seconds integer)
RETURNS TABLE (tenant_id text, user_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'profile policy discovery limit must be between 1 and 1000'
            USING ERRCODE = '22023';
    END IF;
    IF p_poll_seconds IS NULL OR p_poll_seconds < 1 OR p_poll_seconds > 3600 THEN
        RAISE EXCEPTION 'profile policy poll seconds must be between 1 and 3600'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH due AS (
        SELECT p.tenant_id, p.user_id
        FROM public.profile_refresh_policies AS p
        JOIN public.users AS u USING (tenant_id, user_id)
        WHERE NOT p.paused
          AND p.next_check_at <= clock_timestamp()
          AND u.state = 'active'
          AND (
              NULLIF(p.policy->>'refresh_cron', '') IS NOT NULL
              OR NULLIF(p.policy->>'full_refresh_cron', '') IS NOT NULL
              OR p.policy->>'refresh_after_consolidation' = 'true'
          )
        ORDER BY p.next_check_at, p.tenant_id, p.user_id
        LIMIT p_limit
        FOR UPDATE OF p SKIP LOCKED
    )
    UPDATE public.profile_refresh_policies AS p
    SET next_check_at = clock_timestamp() + make_interval(secs => p_poll_seconds)
    FROM due AS d
    WHERE (p.tenant_id, p.user_id) = (d.tenant_id, d.user_id)
    RETURNING p.tenant_id, p.user_id;
END
$function$;

ALTER FUNCTION oam_due_profile_policies(integer, integer)
    OWNER TO oam_profile_schedule_owner;
REVOKE ALL ON FUNCTION oam_due_profile_policies(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_due_profile_policies(integer, integer) TO oam_queue;

-- Scheduled admission reuses the authoritative active-principal/grant check.
GRANT EXECUTE ON FUNCTION oam_authorize_user(text, text, text, text) TO oam_content;
