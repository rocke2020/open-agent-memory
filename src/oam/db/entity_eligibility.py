"""Current fact support owns every entity-name binding and membership."""

from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS

_NAME_SUPPORT = f"""
SELECT ns.*,n.entity_id,n.name_text,n.name_key,n.name_kind,
       f.generation_id,f.source_id,f.source_version,f.locator
FROM entity_name_support ns
JOIN entity_names n USING(tenant_id,user_id,name_id)
JOIN ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f
  ON (f.tenant_id,f.user_id,f.memory_id)=(ns.tenant_id,ns.user_id,ns.memory_id)
JOIN entity_units e ON (e.tenant_id,e.user_id,e.entity_id,e.generation_id)=
                      (n.tenant_id,n.user_id,n.entity_id,f.generation_id)
WHERE f.kind='fact' AND f.locator->>'passage_id'=ns.passage_id
  AND position(ns.quote in f.locator->>'quote')>0
""".strip()

ELIGIBLE_ENTITY_NAME_SUPPORT = f"""
SELECT binding.* FROM ({_NAME_SUPPORT}) binding
WHERE binding.support_kind='canonical' OR EXISTS (
    SELECT 1 FROM ({_NAME_SUPPORT}) anchor
    WHERE (anchor.tenant_id,anchor.user_id,anchor.entity_id,anchor.name_id,anchor.generation_id)=
          (binding.tenant_id,binding.user_id,binding.entity_id,binding.anchor_name_id,binding.generation_id)
      AND anchor.support_kind='canonical'
)
""".strip()

ELIGIBLE_ENTITY_MEMBERSHIPS = f"""
SELECT membership.* FROM unit_entities membership
JOIN ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f
  ON (f.tenant_id,f.user_id,f.unit_id,f.generation_id)=
     (membership.tenant_id,membership.user_id,membership.unit_id,membership.generation_id)
WHERE f.kind='fact' AND membership.entity_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) binding
    WHERE (binding.tenant_id,binding.user_id,binding.entity_id,binding.name_id,
           binding.generation_id)=
          (membership.tenant_id,membership.user_id,membership.entity_id,membership.name_id,
           membership.generation_id)
)
""".strip()

# Consumers already materialize scoped eligible facts as `filtered`. Share
# that proof across name bindings and memberships instead of re-reading support.
ENTITY_PROJECTIONS_FROM_FILTERED_FACTS = (
    ", raw_name_support AS MATERIALIZED ("
    + _NAME_SUPPORT.replace(f"({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f", "filtered f")
    + "), name_support AS MATERIALIZED ("
    + ELIGIBLE_ENTITY_NAME_SUPPORT.replace(
        f"({_NAME_SUPPORT}) binding", "raw_name_support binding"
    ).replace(f"({_NAME_SUPPORT}) anchor", "raw_name_support anchor")
    + "), memberships AS MATERIALIZED ("
    + ELIGIBLE_ENTITY_MEMBERSHIPS.replace(
        f"({ELIGIBLE_ENTITY_NAME_SUPPORT}) binding", "name_support binding"
    ).replace(f"({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f", "filtered f")
    + ")"
)

# `seeds` already proves fact eligibility. Raw identities are an uncapped
# superset; validation below can remove them but cannot discover another ID.
# Retain every name/support within that set, including nonseed alias anchors.
ENTITY_PROJECTIONS_FROM_SEED_ENTITIES = (
    ", seed_entity_ids AS MATERIALIZED ("
    "SELECT DISTINCT s.tenant_id,s.user_id,ue.entity_id FROM seeds s "
    "JOIN unit_entities ue USING(tenant_id,user_id,unit_id,generation_id) "
    "WHERE ue.entity_id IS NOT NULL), "
    "scoped_name_bindings AS MATERIALIZED ("
    "SELECT ns.*,n.entity_id,n.name_text,n.name_key,n.name_kind "
    "FROM seed_entity_ids scoped JOIN entity_names n USING(tenant_id,user_id,entity_id) "
    "JOIN entity_name_support ns USING(tenant_id,user_id,name_id))"
    + ENTITY_PROJECTIONS_FROM_FILTERED_FACTS.replace(
        "SELECT ns.*,n.entity_id,n.name_text,n.name_key,n.name_kind,", "SELECT ns.*,"
    )
    .replace(
        "FROM entity_name_support ns\nJOIN entity_names n USING(tenant_id,user_id,name_id)",
        "FROM scoped_name_bindings ns",
    )
    .replace("n.tenant_id", "ns.tenant_id")
    .replace("n.user_id", "ns.user_id")
    .replace("n.entity_id", "ns.entity_id")
)
