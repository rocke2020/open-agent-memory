-- Preserve existing personal collections while making user identity authoritative.
DO $verify_users$
BEGIN
    IF EXISTS (
        SELECT 1 FROM scopes
        WHERE namespace <> 'personal' OR lifetime <> 'persistent'
           OR run_id IS NOT NULL OR subject <> scope_id
    ) THEN
        RAISE EXCEPTION 'legacy collection cannot be mapped to a personal user';
    END IF;
END
$verify_users$;

ALTER TABLE scopes RENAME TO users;

DO $rename_identity$
DECLARE record_name record;
BEGIN
    FOR record_name IN
        SELECT c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND a.attname = 'scope_id' AND NOT a.attisdropped
    LOOP
        EXECUTE format('ALTER TABLE public.%I RENAME COLUMN scope_id TO user_id', record_name.table_name);
    END LOOP;
END
$rename_identity$;

CREATE FUNCTION oam_user_visible(row_tenant_id text, row_user_id text)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $function$
    SELECT NULLIF(current_setting('oam.tenant_id', true), '') = row_tenant_id
       AND NULLIF(current_setting('oam.user_id', true), '') = row_user_id
$function$;
REVOKE ALL ON FUNCTION oam_user_visible(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_user_visible(text, text) TO oam_content;

DO $replace_policies$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['users','grants','sources','source_versions','evidence','passages','memories','support','insights','insight_support','profiles','operations','work_units','attempts','usage_records','turn_deadlines','traces','exports','index_stage_results','search_units','unit_times','unit_entities','unit_links','temporal_links'] LOOP
        EXECUTE format('DROP POLICY %I ON %I',
            CASE WHEN table_name = 'users' THEN 'scopes_scope_policy'
                 ELSE table_name || '_scope_policy' END, table_name);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO oam_content USING '
            || '(oam_user_visible(tenant_id,user_id)) WITH CHECK '
            || '(oam_user_visible(tenant_id,user_id))', table_name || '_user_policy', table_name);
    END LOOP;
END
$replace_policies$;
DROP FUNCTION oam_scope_visible(text, text);

CREATE FUNCTION oam_authorize_user(
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
        WHERE u.tenant_id = p_tenant_id
          AND u.user_id = p_user_id
          AND u.state = 'active'
          AND g.principal_id = p_principal_id
          AND p_capability = ANY (g.capabilities)
    )
$function$;
ALTER FUNCTION oam_authorize_user(text, text, text, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_authorize_user(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_authorize_user(text, text, text, text) TO oam_api;
DROP FUNCTION oam_authorize_scope(text, text, text, text);

DROP FUNCTION oam_claim_work(text, text, integer);
CREATE FUNCTION oam_claim_work(p_worker_id text, p_attempt_id text, p_lease_seconds integer)
RETURNS TABLE (
    tenant_id text,
    user_id text,
    work_unit_id text,
    operation_id text,
    stage text,
    attempt_id text,
    fence bigint,
    lease_expires_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    WITH candidate AS (
        SELECT w.tenant_id, w.user_id, w.work_unit_id, w.fence
        FROM public.work_units AS w
        JOIN public.operations AS operation
          ON (operation.tenant_id, operation.user_id, operation.operation_id) =
             (w.tenant_id, w.user_id, w.operation_id)
        WHERE (
                w.status IN ('queued', 'retry_wait')
                OR (w.status = 'running' AND w.lease_expires_at <= clock_timestamp())
              )
          AND w.available_at <= clock_timestamp()
          AND w.attempts_remaining > 0
          AND operation.status IN ('accepted', 'running')
          AND operation.cancellation_revision = 0
          AND (operation.plan_sealed OR w.stage = 'plan')
          AND NOT EXISTS (
              SELECT 1 FROM public.attempts AS unresolved
              WHERE unresolved.tenant_id = w.tenant_id
                AND unresolved.user_id = w.user_id
                AND unresolved.work_unit_id = w.work_unit_id
                AND unresolved.disposition = 'unknown_external_outcome'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(w.dependency_ids) AS dependency(work_unit_id)
              LEFT JOIN public.work_units AS prior
                ON prior.tenant_id = w.tenant_id
               AND prior.user_id = w.user_id
               AND prior.work_unit_id = dependency.work_unit_id
              WHERE prior.status IS DISTINCT FROM 'succeeded'
          )
        ORDER BY w.available_at, w.created_at, w.tenant_id, w.user_id, w.work_unit_id
        FOR UPDATE OF w SKIP LOCKED
        LIMIT 1
    )
    , claimed AS (
        UPDATE public.work_units AS work
        SET status = 'running',
            owner_id = p_worker_id,
            fence = candidate.fence + 1,
            lease_expires_at = clock_timestamp() + (p_lease_seconds * interval '1 second'),
            attempts_remaining = work.attempts_remaining - 1,
            updated_at = clock_timestamp()
        FROM candidate
        WHERE (work.tenant_id, work.user_id, work.work_unit_id) =
              (candidate.tenant_id, candidate.user_id, candidate.work_unit_id)
        RETURNING work.tenant_id, work.user_id, work.work_unit_id,
                  work.operation_id, work.stage, work.fence, work.lease_expires_at
    )
    , numbered AS (
        SELECT claimed.*,
               COALESCE((
                   SELECT max(existing.ordinal)
                   FROM public.attempts AS existing
                   WHERE existing.tenant_id = claimed.tenant_id
                     AND existing.user_id = claimed.user_id
                     AND existing.work_unit_id = claimed.work_unit_id
               ), 0) + 1 AS ordinal
        FROM claimed
    )
    , inserted AS (
        INSERT INTO public.attempts
            (tenant_id, user_id, work_unit_id, attempt_id, ordinal, owner_id, fence, disposition)
        SELECT tenant_id, user_id, work_unit_id, p_attempt_id, ordinal,
               p_worker_id, fence, 'claimed'
        FROM numbered
        RETURNING tenant_id, user_id, work_unit_id, attempt_id
    )
    SELECT numbered.tenant_id, numbered.user_id, numbered.work_unit_id,
           numbered.operation_id, numbered.stage, inserted.attempt_id,
           numbered.fence, numbered.lease_expires_at
    FROM numbered
    JOIN inserted USING (tenant_id, user_id, work_unit_id)
$function$;

ALTER FUNCTION oam_claim_work(text, text, integer) OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_claim_work(text, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_claim_work(text, text, integer) TO oam_queue;
