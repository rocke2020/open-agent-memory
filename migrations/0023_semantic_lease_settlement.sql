-- Settle semantic work that has exhausted its lease-recovery attempt budget.
GRANT UPDATE ON operations, attempts TO oam_queue_owner;
CREATE POLICY operations_semantic_settlement_policy ON operations
    FOR UPDATE TO oam_queue_owner USING (true) WITH CHECK (true);

CREATE FUNCTION oam_settle_expired_semantic_work() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $function$
    WITH expired AS (
        SELECT w.tenant_id,w.user_id,w.work_unit_id
        FROM public.work_units w
        JOIN public.operations o USING (tenant_id,user_id,operation_id)
        WHERE w.stage='index_semantic_links' AND w.status='running'
          AND w.lease_expires_at<=clock_timestamp() AND w.attempts_remaining=0
          AND o.status IN ('accepted','running') AND o.cancellation_revision=0
        ORDER BY w.lease_expires_at,w.work_unit_id
        FOR UPDATE OF w SKIP LOCKED LIMIT 32
    ), settled AS (
        UPDATE public.work_units w SET status='failed_terminal',updated_at=clock_timestamp()
        FROM expired e
        WHERE (w.tenant_id,w.user_id,w.work_unit_id)=(e.tenant_id,e.user_id,e.work_unit_id)
        RETURNING w.tenant_id,w.user_id,w.work_unit_id,w.operation_id,w.fence
    ), attempts AS (
        UPDATE public.attempts a SET disposition='failed',finished_at=clock_timestamp()
        FROM settled s
        WHERE (a.tenant_id,a.user_id,a.work_unit_id,a.fence)=
              (s.tenant_id,s.user_id,s.work_unit_id,s.fence)
          AND a.disposition='claimed'
    )
    UPDATE public.operations o
    SET status='partial_failed',updated_at=clock_timestamp(),
        stage_state=stage_state||'{"links_ready":false,"semantic_links_status":"failed","error":"SemanticLeaseExhausted"}'::jsonb
    FROM settled s
    WHERE (o.tenant_id,o.user_id,o.operation_id)=(s.tenant_id,s.user_id,s.operation_id)
      AND o.status IN ('accepted','running') AND o.cancellation_revision=0;
$function$;
ALTER FUNCTION oam_settle_expired_semantic_work() OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_settle_expired_semantic_work() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_settle_expired_semantic_work() TO oam_queue;
