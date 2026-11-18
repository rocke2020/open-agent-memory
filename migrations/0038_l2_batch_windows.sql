-- User-owned L2 windows retain source obligations and immutable dispatched groups.
CREATE TABLE l2_batch_members (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    source_id text NOT NULL,
    source_version integer NOT NULL,
    generation_id text NOT NULL,
    batch_id text NOT NULL,
    source_operation_id text NOT NULL,
    admitted_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','excluded','failed')),
    error text,
    PRIMARY KEY(tenant_id,user_id,source_id,source_version,generation_id),
    FOREIGN KEY(tenant_id,user_id,source_id,source_version)
        REFERENCES source_versions(tenant_id,user_id,source_id,source_version),
    FOREIGN KEY(tenant_id,user_id,batch_id) REFERENCES operations(tenant_id,user_id,operation_id),
    FOREIGN KEY(tenant_id,user_id,source_operation_id) REFERENCES operations(tenant_id,user_id,operation_id)
);
CREATE INDEX l2_batch_members_pending ON l2_batch_members(tenant_id,user_id,batch_id,status);
CREATE INDEX operations_l2_open_windows ON operations(tenant_id,user_id,created_at,operation_id)
    WHERE status='accepted' AND retry_of_operation_id IS NULL;
CREATE TABLE l2_batch_groups (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    batch_id text NOT NULL,
    stage_key text NOT NULL,
    input_digest bytea NOT NULL CHECK(octet_length(input_digest)=32),
    inputs jsonb NOT NULL,
    output jsonb,
    usage jsonb NOT NULL DEFAULT '{}',
    allocation jsonb NOT NULL DEFAULT '{}',
    publication_seq bigint,
    published boolean NOT NULL DEFAULT false,
    invalidated boolean NOT NULL DEFAULT false,
    work_unit_id text NOT NULL,
    attempt_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,user_id,batch_id,stage_key,input_digest),
    FOREIGN KEY(tenant_id,user_id,batch_id) REFERENCES operations(tenant_id,user_id,operation_id),
    FOREIGN KEY(tenant_id,user_id,work_unit_id,attempt_id)
        REFERENCES attempts(tenant_id,user_id,work_unit_id,attempt_id)
);
CREATE INDEX l2_batch_groups_pending ON l2_batch_groups
    (tenant_id,user_id,batch_id,created_at,input_digest)
    WHERE stage_key='insight' AND NOT published AND NOT invalidated;
DO $policies$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['l2_batch_members','l2_batch_groups'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO oam_content USING '
            ||'(oam_user_visible(tenant_id,user_id)) WITH CHECK '
            ||'(oam_user_visible(tenant_id,user_id))',table_name||'_user_policy',table_name);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON %I TO oam_content',table_name);
    END LOOP;
END
$policies$;

-- Queue maintenance changes work/readiness only, never learned content.
GRANT SELECT,UPDATE ON l2_batch_members TO oam_queue_owner;
CREATE POLICY l2_batch_members_queue_settlement ON l2_batch_members
    FOR ALL TO oam_queue_owner USING (true) WITH CHECK (true);
CREATE FUNCTION oam_settle_expired_l2_batch_work() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $function$
    WITH expired AS (
        SELECT w.tenant_id,w.user_id,w.work_unit_id,w.operation_id,w.fence,
               o.stage_state->>'l2_batch_id' AS batch_id
        FROM public.work_units w JOIN public.operations o USING(tenant_id,user_id,operation_id)
        WHERE w.stage='consolidate_batch' AND w.status='running'
          AND w.lease_expires_at<=clock_timestamp() AND w.attempts_remaining=0
          AND o.status IN ('accepted','running') AND o.cancellation_revision=0
        ORDER BY w.lease_expires_at,w.work_unit_id
        FOR UPDATE OF o,w SKIP LOCKED LIMIT 32
    ), settled AS (
        UPDATE public.work_units w SET status='failed_terminal',updated_at=clock_timestamp()
        FROM expired e
        WHERE (w.tenant_id,w.user_id,w.work_unit_id)=(e.tenant_id,e.user_id,e.work_unit_id)
        RETURNING w.tenant_id,w.user_id,w.work_unit_id,w.operation_id,w.fence,e.batch_id
    ), attempts AS (
        -- claimed means every dispatch has settled into its durable checkpoint.
        -- The failed worker adds no generation beyond those saved group results.
        UPDATE public.attempts a SET disposition='failed',finished_at=clock_timestamp(),
            usage='{"input_tokens":0,"cached_input_tokens":0,"uncached_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":0,"total_tokens":0,"reasoning_tokens":0,"coverage":"complete"}'::jsonb
        FROM settled s
        WHERE (a.tenant_id,a.user_id,a.work_unit_id,a.fence)=
              (s.tenant_id,s.user_id,s.work_unit_id,s.fence) AND a.disposition='claimed'
    ), members AS (
        UPDATE public.l2_batch_members bm SET status='failed',error='BatchLeaseExhausted'
        FROM settled s WHERE (bm.tenant_id,bm.user_id,bm.batch_id)=
                            (s.tenant_id,s.user_id,s.batch_id) AND bm.status='pending'
        RETURNING bm.tenant_id,bm.user_id,bm.source_operation_id
    ), parents AS (
        UPDATE public.operations o SET updated_at=clock_timestamp(),
            stage_state=stage_state||'{"insights_ready":false,"consolidation_status":"failed", "consolidation_error":"BatchLeaseExhausted"}'::jsonb
        FROM members m WHERE (o.tenant_id,o.user_id,o.operation_id)=
                             (m.tenant_id,m.user_id,m.source_operation_id)
    )
    UPDATE public.operations o SET status='partial_failed',updated_at=clock_timestamp(),
        stage_state=stage_state||jsonb_build_object('error','BatchLeaseExhausted',
            'insights_ready',false,'l2_member_counts',(
                SELECT jsonb_build_object('pending',0,
                    'succeeded',count(*) FILTER(WHERE bm.status='succeeded'),
                    'excluded',count(*) FILTER(WHERE bm.status='excluded'),
                    'failed',count(*) FILTER(WHERE bm.status IN ('pending','failed')))
                FROM public.l2_batch_members bm
                WHERE (bm.tenant_id,bm.user_id,bm.batch_id)=(s.tenant_id,s.user_id,s.batch_id)
            ),'failed_sources',(
                SELECT COALESCE(jsonb_agg(jsonb_build_object('source_id',bm.source_id,
                    'source_version',bm.source_version,'error',CASE WHEN bm.status='pending'
                    THEN 'BatchLeaseExhausted' ELSE bm.error END)
                    ORDER BY bm.source_id,bm.source_version),'[]'::jsonb)
                FROM public.l2_batch_members bm
                WHERE (bm.tenant_id,bm.user_id,bm.batch_id)=(s.tenant_id,s.user_id,s.batch_id)
                  AND bm.status IN ('pending','failed')
            ))
    FROM settled s WHERE (o.tenant_id,o.user_id,o.operation_id)=
                         (s.tenant_id,s.user_id,s.operation_id);
$function$;
ALTER FUNCTION oam_settle_expired_l2_batch_work() OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_settle_expired_l2_batch_work() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_settle_expired_l2_batch_work() TO oam_queue;

CREATE OR REPLACE FUNCTION oam_claim_work(
    p_worker_id text, p_attempt_id text, p_lease_seconds integer, p_user_concurrency integer,
    p_claim_semantic_links boolean, p_claim_consolidation_batches boolean
)
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
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    candidate_user record;
    active_claims bigint;
    active_l2 bigint;
BEGIN
    IF p_user_concurrency <= 0 OR p_lease_seconds <= 0 THEN
        RAISE EXCEPTION 'claim limits must be positive';
    END IF;
    IF p_claim_consolidation_batches THEN
        PERFORM public.oam_settle_expired_l2_batch_work();
    END IF;
    FOR candidate_user IN
        SELECT w.tenant_id, w.user_id
        FROM public.work_units AS w
        JOIN public.operations AS operation
          ON (operation.tenant_id, operation.user_id, operation.operation_id) =
             (w.tenant_id, w.user_id, w.operation_id)
        WHERE (
                w.status IN ('queued', 'retry_wait')
                OR (w.status = 'running' AND w.lease_expires_at <= clock_timestamp())
              )
          AND (p_claim_semantic_links OR w.stage <> 'index_semantic_links')
          AND (p_claim_consolidation_batches OR w.stage <> 'consolidate_batch')
          AND (w.stage NOT IN ('consolidate','consolidate_batch') OR NOT EXISTS (
              SELECT 1 FROM public.l2_claim_holds AS h
              WHERE (h.tenant_id,h.user_id)=(w.tenant_id,w.user_id) AND h.held
          ))
          AND w.available_at <= clock_timestamp()
          AND w.attempts_remaining > 0
          AND operation.status IN ('accepted', 'running')
          AND operation.cancellation_revision = 0
        GROUP BY w.tenant_id, w.user_id
        ORDER BY min(CASE WHEN w.stage IN ('consolidate','consolidate_batch')
                          THEN COALESCE(w.l2_priority_at,w.created_at)
                          ELSE w.available_at END),
                 min(w.created_at), w.tenant_id, w.user_id
    LOOP
        -- Serialize claim admission only; never acquire content row privileges.
        IF NOT pg_try_advisory_xact_lock(hashtextextended(
            'oam:user-claims:' || jsonb_build_array(
                candidate_user.tenant_id, candidate_user.user_id
            )::text, 0
        )) THEN
            CONTINUE;
        END IF;
        -- This statement runs after the user lock with a fresh committed view.
        SELECT count(*) INTO active_claims
        FROM public.work_units AS active
        WHERE active.tenant_id = candidate_user.tenant_id
          AND active.user_id = candidate_user.user_id
          AND active.status = 'running'
          AND active.lease_expires_at > clock_timestamp();
        IF active_claims >= p_user_concurrency THEN
            CONTINUE;
        END IF;
        SELECT count(*) INTO active_l2
        FROM public.work_units AS active
        WHERE active.tenant_id = candidate_user.tenant_id
          AND active.user_id = candidate_user.user_id
          AND active.stage IN ('consolidate','consolidate_batch')
          AND active.status = 'running'
          AND active.lease_expires_at > clock_timestamp();
        RETURN QUERY
        WITH candidate AS (
            SELECT w.tenant_id, w.user_id, w.work_unit_id, w.fence
            FROM public.work_units AS w
            JOIN public.operations AS operation
              ON (operation.tenant_id, operation.user_id, operation.operation_id) =
                 (w.tenant_id, w.user_id, w.operation_id)
            WHERE w.tenant_id = candidate_user.tenant_id
              AND w.user_id = candidate_user.user_id
              AND (
                    w.status IN ('queued', 'retry_wait')
                    OR (w.status = 'running' AND w.lease_expires_at <= clock_timestamp())
                  )
              AND (p_claim_semantic_links OR w.stage <> 'index_semantic_links')
          AND (p_claim_consolidation_batches OR w.stage <> 'consolidate_batch')
              AND (w.stage NOT IN ('consolidate','consolidate_batch') OR NOT EXISTS (
                  SELECT 1 FROM public.l2_claim_holds AS h
                  WHERE (h.tenant_id,h.user_id)=(w.tenant_id,w.user_id) AND h.held
              ))
              AND w.available_at <= clock_timestamp()
              AND w.attempts_remaining > 0
              AND operation.status IN ('accepted', 'running')
              AND operation.cancellation_revision = 0
              AND (operation.plan_sealed OR w.stage = 'plan')
              AND (w.stage NOT IN ('consolidate','consolidate_batch') OR active_l2 = 0)
              AND NOT EXISTS (
                  SELECT 1 FROM public.attempts AS unresolved
                  WHERE unresolved.tenant_id = w.tenant_id
                    AND unresolved.user_id = w.user_id
                    AND unresolved.work_unit_id = w.work_unit_id
                    AND unresolved.disposition = 'unknown_external_outcome'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(w.dependency_ids) AS dependency(work_unit_id)
                  LEFT JOIN public.work_units AS prior
                    ON prior.tenant_id = w.tenant_id
                   AND prior.user_id = w.user_id
                   AND prior.work_unit_id = dependency.work_unit_id
                  WHERE prior.status IS DISTINCT FROM 'succeeded'
                    AND (w.stage <> 'refresh_profile' OR prior.status IS NULL
                         OR prior.status NOT IN ('failed_terminal','cancelled'))
              )
            ORDER BY CASE WHEN w.stage IN ('consolidate','consolidate_batch')
                              THEN COALESCE(w.l2_priority_at,w.created_at)
                              ELSE w.available_at END,
                     w.created_at, w.work_unit_id
            FOR UPDATE OF w SKIP LOCKED
            LIMIT 1
        ), claimed AS (
            UPDATE public.work_units AS work
            SET status = 'running', owner_id = p_worker_id,
                fence = candidate.fence + 1,
                lease_expires_at = clock_timestamp() + (p_lease_seconds * interval '1 second'),
                attempts_remaining = work.attempts_remaining - 1,
                updated_at = clock_timestamp()
            FROM candidate
            WHERE (work.tenant_id, work.user_id, work.work_unit_id) =
                  (candidate.tenant_id, candidate.user_id, candidate.work_unit_id)
            RETURNING work.tenant_id, work.user_id, work.work_unit_id,
                      work.operation_id, work.stage, work.fence, work.lease_expires_at
        ), numbered AS (
            SELECT claimed.*,
                   COALESCE((
                       SELECT max(existing.ordinal) FROM public.attempts AS existing
                       WHERE existing.tenant_id = claimed.tenant_id
                         AND existing.user_id = claimed.user_id
                         AND existing.work_unit_id = claimed.work_unit_id
                   ), 0) + 1 AS ordinal
            FROM claimed
        ), inserted AS (
            INSERT INTO public.attempts
                (tenant_id, user_id, work_unit_id, attempt_id, ordinal, owner_id, fence, disposition)
            SELECT numbered.tenant_id, numbered.user_id, numbered.work_unit_id,
                   p_attempt_id, numbered.ordinal, p_worker_id, numbered.fence, 'claimed'
            FROM numbered
            RETURNING attempts.tenant_id, attempts.user_id, attempts.work_unit_id, attempts.attempt_id
        )
        SELECT numbered.tenant_id, numbered.user_id, numbered.work_unit_id,
               numbered.operation_id, numbered.stage, inserted.attempt_id,
               numbered.fence, numbered.lease_expires_at
        FROM numbered
        JOIN inserted ON (inserted.tenant_id, inserted.user_id, inserted.work_unit_id) =
                         (numbered.tenant_id, numbered.user_id, numbered.work_unit_id);
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$function$;

ALTER FUNCTION oam_claim_work(text, text, integer, integer, boolean, boolean) OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_claim_work(text, text, integer, integer, boolean, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_claim_work(text, text, integer, integer, boolean, boolean) TO oam_queue;

-- Old workers retain their exact entry point and never receive a batch claim.
CREATE OR REPLACE FUNCTION oam_claim_work(
    p_worker_id text, p_attempt_id text, p_lease_seconds integer, p_user_concurrency integer,
    p_claim_semantic_links boolean DEFAULT true
)
RETURNS TABLE(tenant_id text,user_id text,work_unit_id text,operation_id text,stage text,
              attempt_id text,fence bigint,lease_expires_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog
AS $function$
    SELECT * FROM public.oam_claim_work(p_worker_id,p_attempt_id,p_lease_seconds,
        p_user_concurrency,p_claim_semantic_links,false)
$function$;
ALTER FUNCTION oam_claim_work(text,text,integer,integer,boolean) OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_claim_work(text,text,integer,integer,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_claim_work(text,text,integer,integer,boolean) TO oam_queue;
