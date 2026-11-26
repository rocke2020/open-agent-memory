"""Current fact support owns every entity-name binding and membership."""

from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS

_NAME_SUPPORT = f"""
SELECT ns.*,n.entity_id,n.name_text,n.name_key,n.name_kind,
       f.generation_id,f.source_id,f.source_version,f.locator
FROM entity_name_support ns
JOIN entity_names n USING(tenant_id,user_id,name_id)
JOIN ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f
  ON (f.tenant_id,f.user_id,f.memory_id)=(ns.tenant_id,ns.user_id,ns.memory_id)
JOIN entity_units e ON (e.tenant_id,e.user_id,e.entity_id)=(n.tenant_id,n.user_id,n.entity_id)
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
SELECT membership.tenant_id,membership.user_id,membership.unit_id,membership.entity_key,
       membership.entity_id,membership.name_id,membership.observed_mention,
       membership.mention_quote,membership.generation_id FROM unit_entities membership
JOIN ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f
  ON (f.tenant_id,f.user_id,f.unit_id,f.generation_id)=
     (membership.tenant_id,membership.user_id,membership.unit_id,membership.generation_id)
WHERE f.kind='fact' AND membership.entity_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) binding
    WHERE (binding.tenant_id,binding.user_id,binding.entity_id,binding.name_id)=
          (membership.tenant_id,membership.user_id,membership.entity_id,membership.name_id)
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


def fact_only_eligible() -> str:
    return (
        "WITH filtered AS NOT MATERIALIZED (SELECT * FROM ("
        + ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
        + ") eligible WHERE kind='fact') "
    )


def scoped_graph_facts(
    *,
    include_semantic: bool,
    include_entities: bool = True,
    include_causal: bool = True,
    require_nonseed_fact: bool = True,
) -> str:
    """Select eligible seed facts and the facts needed to prove their graph neighbors."""
    eligible = fact_only_eligible()
    seed_gate = (
        ""
        if include_semantic or not require_nonseed_fact
        else (
            " AND EXISTS ("
            + eligible
            + "SELECT 1 FROM filtered other WHERE other.unit_id<>ALL(%(seeds)s))"
        )
    )
    candidates = ["SELECT unit_id FROM seeds"]
    if include_entities:
        # Spell out the fact kind so the name-support join can use the partial
        # index on scoped memory IDs.
        candidates.extend(
            [
                "SELECT ue.unit_id FROM unit_entities ue JOIN raw_seed_entities r "
                "USING(tenant_id,user_id,entity_id)",
                "SELECT u.unit_id FROM raw_seed_entities r JOIN entity_names n "
                "USING(tenant_id,user_id,entity_id) JOIN entity_name_support ns "
                "USING(tenant_id,user_id,name_id) JOIN search_units u "
                "USING(tenant_id,user_id,memory_id) WHERE u.kind='fact'",
            ]
        )
    if include_causal:
        candidates.extend(
            [
                "SELECT l.target_id FROM seeds s JOIN unit_links l ON "
                "(l.tenant_id,l.user_id,l.source_id)=(s.tenant_id,s.user_id,s.unit_id) "
                "WHERE l.kind='caused_by'",
                "SELECT second.target_id FROM seeds s JOIN unit_links first ON "
                "(first.tenant_id,first.user_id,first.source_id)=(s.tenant_id,s.user_id,s.unit_id) "
                "JOIN unit_links second ON (second.tenant_id,second.user_id,second.source_id)="
                "(first.tenant_id,first.user_id,first.target_id) "
                "WHERE first.kind='caused_by' AND second.kind='caused_by'",
            ]
        )
    if include_semantic:
        for source, target in (("source_id", "target_id"), ("target_id", "source_id")):
            candidates.append(
                f"SELECT l.{target} AS unit_id FROM seeds s JOIN unit_links l ON "
                f"(l.tenant_id,l.user_id,l.{source})=(s.tenant_id,s.user_id,s.unit_id) "
                "WHERE l.kind='semantic'"
            )
    candidate_eligible = eligible.replace(
        "FROM search_units u",
        "FROM (SELECT * FROM search_units "
        "WHERE tenant_id=current_setting('oam.tenant_id',true) "
        "AND user_id=current_setting('oam.user_id',true) AND kind='fact' "
        "AND unit_id IN (SELECT unit_id FROM graph_candidate_ids) "
        "AND unit_id<>ALL(%(seeds)s) OFFSET 0) u",
        1,
    )
    # Stored identities and links provide an uncapped superset. Validate seeds
    # before identity reads, retain nonseed name anchors, and apply every support
    # predicate before the ordinary graph's existing ranking and branch limits.
    return (
        "WITH seeds AS MATERIALIZED ("
        + eligible
        + "SELECT * FROM filtered WHERE unit_id=ANY(%(seeds)s)"
        + seed_gate
        + ")"
        + (
            ", raw_seed_entities AS MATERIALIZED ("
            "SELECT DISTINCT s.tenant_id,s.user_id,ue.entity_id FROM seeds s "
            "JOIN unit_entities ue USING(tenant_id,user_id,unit_id,generation_id) "
            "WHERE ue.entity_id IS NOT NULL)"
            if include_entities
            else ""
        )
        + ", graph_candidate_ids AS MATERIALIZED ("
        + " UNION ".join(candidates)
        + "), filtered AS MATERIALIZED (SELECT * FROM seeds UNION ALL "
        "SELECT * FROM (" + candidate_eligible + "SELECT * FROM filtered) candidate_facts) "
    )
