-- Settle final expired source leases before or during semantic linking.
CREATE OR REPLACE FUNCTION oam_settle_expired_semantic_work() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $function$
    WITH expired AS (
        SELECT w.tenant_id,w.user_id,w.work_unit_id,
               w.stage='index_semantic_links' OR (
                   o.stage_state->>'memory_ready'='true'
                   AND o.stage_state->>'temporal_links_ready'='true'
                   AND o.stage_state->>'links_ready'='false'
                   AND o.stage_state ? 'semantic_link_manifest'
                   AND o.stage_state->>'semantic_links_status' IN ('pending','running')
               ) AS semantic
        FROM public.work_units w
        JOIN public.operations o USING (tenant_id,user_id,operation_id)
        WHERE w.stage IN ('index_source','index_semantic_links')
          AND w.status='running'
          AND w.lease_expires_at<=clock_timestamp()
          AND w.attempts_remaining=0
          AND o.status IN ('accepted','running')
          AND o.cancellation_revision=0
        ORDER BY w.lease_expires_at,w.work_unit_id
        FOR UPDATE OF o,w SKIP LOCKED LIMIT 32
    ), settled AS (
        UPDATE public.work_units w SET status='failed_terminal',updated_at=clock_timestamp()
        FROM expired e
        WHERE (w.tenant_id,w.user_id,w.work_unit_id)=(e.tenant_id,e.user_id,e.work_unit_id)
        RETURNING w.tenant_id,w.user_id,w.work_unit_id,w.operation_id,w.fence,e.semantic
    ), attempts AS (
        -- A claimed direct attempt has no unsettled model stage. Completed calls are in
        -- checkpoints. Keep their references and record zero additional usage for retry.
        UPDATE public.attempts a SET disposition='failed',finished_at=clock_timestamp(),
            request_ref=CASE WHEN s.semantic THEN a.request_ref
                             ELSE COALESCE(a.request_ref,'source_lease_exhausted') END,
            usage=CASE WHEN s.semantic THEN a.usage ELSE COALESCE(a.usage,
                '{"coverage":"complete","input_tokens":0,"output_tokens":0,"total_tokens":0,"reasoning_tokens":0}'::jsonb)
            END
        FROM settled s
        WHERE (a.tenant_id,a.user_id,a.work_unit_id,a.fence)=
              (s.tenant_id,s.user_id,s.work_unit_id,s.fence)
          AND a.disposition='claimed'
    )
    UPDATE public.operations o
    SET status='partial_failed',updated_at=clock_timestamp(),
        stage_state=stage_state||CASE WHEN s.semantic THEN
            '{"links_ready":false,"semantic_links_status":"failed","error":"SemanticLeaseExhausted"}'::jsonb
        ELSE
            '{"error":"SourceLeaseExhausted"}'::jsonb
        END
    FROM settled s
    WHERE (o.tenant_id,o.user_id,o.operation_id)=(s.tenant_id,s.user_id,s.operation_id);
$function$;
