"""Authoritative current-source and support eligibility for retrieval projections."""

_ACTIVE_SUPPORT_PREDICATE = """
p.evidence_id=e.evidence_id AND p.visible_to IS NULL
AND e.status='active' AND e.learning_eligible
AND v.status='active' AND s.status='active' AND s.active_version=v.source_version
AND s.input_role='interaction'
""".strip()

ELIGIBLE_SUPPORT = f"""
SELECT sp.*, p.generation_id, e.source_id, e.source_version, e.event_time
FROM support sp
JOIN passages p USING (tenant_id, user_id, passage_id)
JOIN evidence e ON (e.tenant_id,e.user_id,e.evidence_id) =
                   (sp.tenant_id,sp.user_id,sp.evidence_id)
JOIN source_versions v ON (v.tenant_id,v.user_id,v.source_id,v.source_version) =
                         (e.tenant_id,e.user_id,e.source_id,e.source_version)
JOIN sources s ON (s.tenant_id,s.user_id,s.source_id) =
                 (e.tenant_id,e.user_id,e.source_id)
WHERE sp.tenant_id=current_setting('oam.tenant_id',true)
  AND sp.user_id=current_setting('oam.user_id',true)
  AND {_ACTIVE_SUPPORT_PREDICATE}
""".strip()

_ALL_CURRENT_MEMORY_SUPPORT = f"""
EXISTS (
    SELECT 1 FROM support sp
    LEFT JOIN passages p USING (tenant_id,user_id,passage_id)
    LEFT JOIN evidence e ON (e.tenant_id,e.user_id,e.evidence_id) =
                            (sp.tenant_id,sp.user_id,sp.evidence_id)
    LEFT JOIN source_versions v ON (v.tenant_id,v.user_id,v.source_id,v.source_version) =
                                   (e.tenant_id,e.user_id,e.source_id,e.source_version)
    LEFT JOIN sources s ON (s.tenant_id,s.user_id,s.source_id) =
                        (e.tenant_id,e.user_id,e.source_id)
    WHERE (sp.tenant_id,sp.user_id,sp.memory_id) =
          (m.tenant_id,m.user_id,m.memory_id)
    HAVING count(*)>0 AND bool_and(COALESCE(
        p.generation_id=m.generation_id AND {_ACTIVE_SUPPORT_PREDICATE},false))
    OFFSET 0
)
""".strip()

_ELIGIBLE_MEMORIES = f"""
    SELECT m.* FROM memories m
    WHERE m.tenant_id=current_setting('oam.tenant_id',true)
      AND m.user_id=current_setting('oam.user_id',true)
      AND m.lifecycle_state='active' AND m.visible_to IS NULL
      AND {_ALL_CURRENT_MEMORY_SUPPORT}
""".strip()

_FACT_UNIT_ELIGIBILITY = f"""
OR (u.kind='fact' AND EXISTS (
    SELECT 1 FROM ({_ELIGIBLE_MEMORIES}) m
    WHERE (m.tenant_id,m.user_id,m.memory_id,m.generation_id) =
          (u.tenant_id,u.user_id,u.memory_id,u.generation_id)
))
""".strip()

_INSIGHT_UNIT_ELIGIBILITY = f"""
OR (u.kind='insight' AND EXISTS (
    SELECT 1 FROM insights i
    WHERE (i.tenant_id,i.user_id,i.insight_id,i.revision,i.generation_id) =
          (u.tenant_id,u.user_id,u.insight_id,u.insight_revision,u.generation_id)
      AND i.visible_to IS NULL
      AND (SELECT count(*) FROM insight_support sp
           WHERE (sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision) =
                 (i.tenant_id,i.user_id,i.insight_id,i.revision)) >= 2
      AND NOT EXISTS (
          SELECT 1 FROM insight_support sp
          WHERE (sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision) =
                (i.tenant_id,i.user_id,i.insight_id,i.revision)
            AND (sp.support_revision<>1 OR NOT EXISTS (
                SELECT 1 FROM ({_ELIGIBLE_MEMORIES}) m
                WHERE (m.tenant_id,m.user_id,m.memory_id,m.generation_id) =
                      (sp.tenant_id,sp.user_id,sp.memory_id,i.generation_id)
            ))
      )
))
""".strip()

ELIGIBLE_UNITS = f"""
SELECT u.* FROM search_units u
JOIN users owner ON (owner.tenant_id,owner.user_id)=(u.tenant_id,u.user_id)
WHERE u.tenant_id=current_setting('oam.tenant_id',true)
  AND u.user_id=current_setting('oam.user_id',true)
  AND owner.state='active' AND owner.index_generation_id=u.generation_id
  AND (
    (u.kind IN ('passage','resource_passage') AND EXISTS (
        SELECT 1 FROM passages p
        JOIN evidence e USING (tenant_id,user_id,evidence_id)
        JOIN source_versions v USING (tenant_id,user_id,source_id,source_version)
        JOIN sources s USING (tenant_id,user_id,source_id)
        WHERE (p.tenant_id,p.user_id,p.passage_id)=(u.tenant_id,u.user_id,u.passage_id)
          AND p.visible_to IS NULL AND p.generation_id=u.generation_id
          AND e.status='active' AND v.status='active' AND s.status='active'
          AND s.active_version=e.source_version
          AND ((u.origin='memory' AND s.input_role='interaction' AND e.learning_eligible)
               OR (u.origin='evidence' AND s.input_role='evidence_resource'))
    ))
    {_FACT_UNIT_ELIGIBILITY}
    {_INSIGHT_UNIT_ELIGIBILITY}
)
""".strip()

# Candidate-ordered reads must check source support per candidate before their LIMIT.
# The OFFSET 0 barriers keep PostgreSQL from precomputing every user's support set.
_CANDIDATE_FACT_UNIT_ELIGIBILITY = f"""
OR (u.kind='fact' AND EXISTS (
    SELECT 1 FROM memories m
    WHERE (m.tenant_id,m.user_id,m.memory_id,m.generation_id) =
          (u.tenant_id,u.user_id,u.memory_id,u.generation_id)
      AND m.lifecycle_state='active' AND m.visible_to IS NULL
      AND {_ALL_CURRENT_MEMORY_SUPPORT}
))
""".strip()

ELIGIBLE_UNITS_CANDIDATE = ELIGIBLE_UNITS.replace(
    _FACT_UNIT_ELIGIBILITY, _CANDIDATE_FACT_UNIT_ELIGIBILITY
)
ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS = ELIGIBLE_UNITS_CANDIDATE.replace(
    _INSIGHT_UNIT_ELIGIBILITY, "OR false"
)

ELIGIBLE_PASSAGE_UNITS = ELIGIBLE_UNITS.replace(_FACT_UNIT_ELIGIBILITY, "").replace(
    _INSIGHT_UNIT_ELIGIBILITY, ""
)
