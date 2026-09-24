"""Authoritative current-source and support eligibility for retrieval projections."""

ELIGIBLE_SUPPORT = """
SELECT sp.*, p.generation_id, e.source_id, e.source_version, e.event_time
FROM support sp
JOIN passages p USING (tenant_id, scope_id, passage_id)
JOIN evidence e ON (e.tenant_id,e.scope_id,e.evidence_id) =
                   (sp.tenant_id,sp.scope_id,sp.evidence_id)
JOIN source_versions v ON (v.tenant_id,v.scope_id,v.source_id,v.source_version) =
                         (e.tenant_id,e.scope_id,e.source_id,e.source_version)
JOIN sources s ON (s.tenant_id,s.scope_id,s.source_id) =
                 (e.tenant_id,e.scope_id,e.source_id)
WHERE sp.tenant_id=current_setting('oam.tenant_id',true)
  AND sp.scope_id=current_setting('oam.scope_id',true)
  AND p.evidence_id=e.evidence_id AND p.visible_to IS NULL
  AND e.status='active' AND e.learning_eligible
  AND v.status='active' AND s.status='active' AND s.active_version=v.source_version
  AND s.input_role='interaction'
""".strip()

_ELIGIBLE_MEMORIES = f"""
    SELECT m.* FROM memories m
    WHERE m.tenant_id=current_setting('oam.tenant_id',true)
      AND m.scope_id=current_setting('oam.scope_id',true)
      AND m.lifecycle_state='active' AND m.visible_to IS NULL
      AND EXISTS (SELECT 1 FROM ({ELIGIBLE_SUPPORT}) sp
                  WHERE (sp.tenant_id,sp.scope_id,sp.memory_id,sp.generation_id) =
                        (m.tenant_id,m.scope_id,m.memory_id,m.generation_id))
""".strip()

ELIGIBLE_UNITS = f"""
SELECT u.* FROM search_units u
JOIN scopes scope ON (scope.tenant_id,scope.scope_id)=(u.tenant_id,u.scope_id)
WHERE u.tenant_id=current_setting('oam.tenant_id',true)
  AND u.scope_id=current_setting('oam.scope_id',true)
  AND scope.state='active' AND scope.index_generation_id=u.generation_id
  AND (
    (u.kind IN ('passage','resource_passage') AND EXISTS (
        SELECT 1 FROM passages p
        JOIN evidence e USING (tenant_id,scope_id,evidence_id)
        JOIN source_versions v USING (tenant_id,scope_id,source_id,source_version)
        JOIN sources s USING (tenant_id,scope_id,source_id)
        WHERE (p.tenant_id,p.scope_id,p.passage_id)=(u.tenant_id,u.scope_id,u.passage_id)
          AND p.visible_to IS NULL AND p.generation_id=u.generation_id
          AND e.status='active' AND v.status='active' AND s.status='active'
          AND s.active_version=e.source_version
          AND ((u.origin='memory' AND s.input_role='interaction' AND e.learning_eligible)
               OR (u.origin='evidence' AND s.input_role='evidence_resource'))
    ))
    OR (u.kind='fact' AND EXISTS (
        SELECT 1 FROM ({_ELIGIBLE_MEMORIES}) m
        WHERE (m.tenant_id,m.scope_id,m.memory_id,m.generation_id) =
              (u.tenant_id,u.scope_id,u.memory_id,u.generation_id)
    ))
    OR (u.kind='insight' AND EXISTS (
        SELECT 1 FROM insights i
        WHERE (i.tenant_id,i.scope_id,i.insight_id,i.revision,i.generation_id) =
              (u.tenant_id,u.scope_id,u.insight_id,u.insight_revision,u.generation_id)
          AND i.visible_to IS NULL
          AND (SELECT count(*) FROM insight_support sp
               WHERE (sp.tenant_id,sp.scope_id,sp.insight_id,sp.insight_revision) =
                     (i.tenant_id,i.scope_id,i.insight_id,i.revision)) >= 2
          AND NOT EXISTS (
              SELECT 1 FROM insight_support sp
              WHERE (sp.tenant_id,sp.scope_id,sp.insight_id,sp.insight_revision) =
                    (i.tenant_id,i.scope_id,i.insight_id,i.revision)
                AND (sp.support_revision<>1 OR NOT EXISTS (
                    SELECT 1 FROM ({_ELIGIBLE_MEMORIES}) m
                    WHERE (m.tenant_id,m.scope_id,m.memory_id,m.generation_id) =
                          (sp.tenant_id,sp.scope_id,sp.memory_id,i.generation_id)
                ))
          )
    ))
)
""".strip()
