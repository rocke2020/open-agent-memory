"""Eligible name support for the bounded extraction-reference selector.

Shared lexical helpers do not make this a recall mode. Name matches propose prior
evidence; grounded per-mention continuity decides whether an identity is reused.
"""

from itertools import zip_longest
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from oam.config import OAMConfig
from oam.db.entity_eligibility import ELIGIBLE_ENTITY_NAME_SUPPORT
from oam.retrieval.fts import query_terms
from oam.retrieval.repository import _tsquery


def source_query_text(inputs: dict) -> str:
    """Use owned source text, preserving grouped order and excluding context-only text."""
    if "passages" in inputs:
        return "\n".join(passage["center"]["text"] for passage in inputs["passages"])
    if "center" in inputs:
        return inputs["center"]["text"]
    return inputs["text"]


def merge_reference_ids(candidates: list[dict], dense_ids: list[str], limit: int) -> list[str]:
    """Interleave name-first and dense proposals within one reference-fact cap.

    This ordering is the current extraction selection policy, not recall fusion or
    proof of identity. Its coverage and noise require separate evaluation.
    """
    if limit <= 0:
        return []
    name_ids = list(dict.fromkeys(candidate["names"][0]["memory_id"] for candidate in candidates))
    selected = []
    for pair in zip_longest(name_ids, dense_ids):
        for memory_id in pair:
            if memory_id is not None and memory_id not in selected:
                selected.append(memory_id)
                if len(selected) == limit:
                    return selected
    return selected


def eligible_entity_name_candidates(
    connection: Connection,
    config: OAMConfig,
    *,
    generation_id: str,
    text: str,
    excluded_source_id: str,
) -> list[dict[str, Any]]:
    """Names rank candidates; only later source-backed references resolve identity."""
    parameters = {
        "generation": generation_id,
        "excluded": excluded_source_id,
        "normalization": config.index.entity_search_rank_normalization,
        "entities": config.index.entity_resolution_candidates,
        "names": config.index.entity_search_names_per_entity,
    }
    terms = query_terms(text).candidates
    if not terms:
        return []
    query = _tsquery(terms, "||", parameters, "entity_term")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "WITH name_support AS NOT MATERIALIZED ("
            + ELIGIBLE_ENTITY_NAME_SUPPORT
            + "), matched_names AS MATERIALIZED ("
            "SELECT n.entity_id,n.name_id,n.name_text,s.memory_id,s.quote,"
            f"ts_rank_cd(n.lexical,{query},%(normalization)s) AS rank "
            "FROM entity_names n CROSS JOIN LATERAL ("
            "SELECT memory_id,quote FROM name_support s WHERE "
            "(s.tenant_id,s.user_id,s.entity_id,s.name_id)="
            "(n.tenant_id,n.user_id,n.entity_id,n.name_id) "
            "AND s.generation_id=%(generation)s AND s.source_id<>%(excluded)s "
            "ORDER BY support_id LIMIT 1) s "
            "WHERE n.tenant_id=current_setting('oam.tenant_id',true) "
            "AND n.user_id=current_setting('oam.user_id',true) "
            f"AND n.lexical @@ {query}), entities AS MATERIALIZED ("
            "SELECT entity_id,max(rank) AS rank FROM matched_names GROUP BY entity_id "
            "ORDER BY max(rank) DESC,entity_id LIMIT %(entities)s), ranked_names AS ("
            "SELECT matched_names.*,row_number() OVER (PARTITION BY entity_id "
            "ORDER BY matched_names.rank DESC,name_id) AS name_rank FROM matched_names "
            "JOIN entities USING(entity_id)) "
            "SELECT e.entity_id,e.rank,jsonb_agg(jsonb_build_object("
            "'name_id',n.name_id,'name',n.name_text,'memory_id',n.memory_id,'quote',n.quote) "
            "ORDER BY n.rank DESC,n.name_id) AS names FROM entities e "
            "JOIN ranked_names n USING(entity_id) WHERE n.name_rank<=%(names)s "
            "GROUP BY e.entity_id,e.rank ORDER BY e.rank DESC,e.entity_id",
            parameters,
        )
        return cursor.fetchall()
