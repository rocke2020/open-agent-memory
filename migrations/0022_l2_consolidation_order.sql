-- Serialize L2 consolidation per user and order runnable jobs by stored fact time.
ALTER TABLE work_units ADD COLUMN l2_priority_at timestamptz;
CREATE INDEX work_units_l2_priority ON work_units(tenant_id,user_id,l2_priority_at,work_unit_id)
    WHERE stage='consolidate';

UPDATE work_units AS w
SET l2_priority_at = (
    SELECT min(u.created_at)
    FROM search_units AS u
    JOIN operations AS consolidation
      ON consolidation.tenant_id=w.tenant_id
     AND consolidation.user_id=w.user_id
     AND consolidation.operation_id=w.operation_id
    JOIN operations AS source_operation
      ON source_operation.tenant_id=consolidation.tenant_id
     AND source_operation.user_id=consolidation.user_id
     AND source_operation.operation_id=consolidation.stage_state->>'source_operation_id'
     AND source_operation.source_id=u.source_id
     AND source_operation.source_version=u.source_version
    WHERE u.tenant_id=w.tenant_id
      AND u.user_id=w.user_id
      AND u.kind='fact'
      AND u.generation_id=w.generation_id
)
WHERE w.stage='consolidate' AND w.l2_priority_at IS NULL;

CREATE OR REPLACE FUNCTION oam_claim_work(
    p_worker_id text, p_attempt_id text, p_lease_seconds integer, p_user_concurrency integer
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
          AND w.available_at <= clock_timestamp()
          AND w.attempts_remaining > 0
          AND operation.status IN ('accepted', 'running')
          AND operation.cancellation_revision = 0
        GROUP BY w.tenant_id, w.user_id
        ORDER BY min(CASE WHEN w.stage='consolidate'
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
          AND active.stage = 'consolidate'
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
              AND w.available_at <= clock_timestamp()
              AND w.attempts_remaining > 0
              AND operation.status IN ('accepted', 'running')
              AND operation.cancellation_revision = 0
              AND (operation.plan_sealed OR w.stage = 'plan')
              AND (w.stage <> 'consolidate' OR active_l2 = 0)
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
            ORDER BY CASE WHEN w.stage='consolidate'
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

ALTER FUNCTION oam_claim_work(text, text, integer, integer) OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_claim_work(text, text, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_claim_work(text, text, integer, integer) TO oam_queue;
