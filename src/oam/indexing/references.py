"""Bounded prior L1 evidence for extraction and witnessed identity continuity.

This selector consumes already computed passage vectors and eligible name support.
It offers context, not support for new facts or an accepted identity decision, and
freezes selected references under the existing indexing checkpoint contract.
Selection is independent of recall modes and their answer-context ranking.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from oam.contracts.evidence import TemporalAnnotation
from oam.db.ann import configure_ann
from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
from oam.indexing.entity_candidates import (
    eligible_entity_name_candidates,
    merge_reference_ids,
    source_query_text,
)
from oam.indexing.store import IndexStore, input_digest


def reference_candidate_ids(connection, authority, config, vectors):
    """Merge at most K owner-qualified ANN results per query, before existing name fusion."""
    if not vectors or not config.extraction.reference_max_facts:
        return []
    planning = connection.execute(
        "SELECT current_setting('enable_seqscan'),current_setting('enable_sort'),"
        "current_setting('plan_cache_mode')"
    ).fetchone()
    configure_ann(connection, config)
    probes = ",".join("(%s::vector)" for _ in vectors)
    rows = connection.execute(
        f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}), "
        f"probes AS MATERIALIZED (SELECT * FROM (VALUES {probes}) v(embedding)) "
        "SELECT n.unit_id,min(n.distance) AS distance FROM probes p CROSS JOIN LATERAL ("
        "SELECT u.unit_id,u.embedding <=> p.embedding AS distance FROM search_units u "
        "WHERE u.tenant_id=%s AND u.user_id=%s AND u.kind='fact' "
        "AND (SELECT true FROM eligible f "
        "WHERE (f.tenant_id,f.user_id,f.unit_id)=(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
        "ORDER BY u.embedding <=> p.embedding LIMIT %s) n "
        "GROUP BY n.unit_id ORDER BY distance,n.unit_id LIMIT %s",
        (
            *(str(list(vector)) for vector in vectors),
            authority.tenant_id,
            authority.user_id,
            config.extraction.reference_max_facts,
            config.extraction.reference_max_facts,
        ),
        prepare=True,
    ).fetchall()
    # Vector planner preferences must not penalize the following entity queries.
    # On SQL failure the enclosing transaction rolls back its local settings.
    connection.execute(
        "SELECT set_config('enable_seqscan',%s,true),set_config('enable_sort',%s,true),"
        "set_config('plan_cache_mode',%s,true)",
        planning,
    )
    return [row[0] for row in rows]


def eligible_reference_ids(connection, generation_id: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    return {
        row[0]
        for row in connection.execute(
            f"SELECT unit_id FROM ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) eligible "
            "WHERE kind='fact' AND unit_id=ANY(%s)",
            (ids,),
        ).fetchall()
    }


def freeze_extraction_references(
    store: IndexStore,
    passage_id: str,
    vector,
    inputs: dict,
    *,
    vectors=None,
) -> list[dict]:
    """Select and freeze prior-fact context before the existing extraction call.

    Offer eligible L1 facts, including previously completed source chunks.
    Reuse completed extraction; revise only unfinished inputs after revocation.
    """
    identity = (
        *store.key,
        store.source["source_id"],
        store.source["source_version"],
        store.generation_id,
    )
    stage = "references:" + passage_id
    digest = input_digest(inputs)
    preferred_name_ids = []
    with store.transaction() as connection:
        store.guard(connection)
        frozen = connection.execute(
            "SELECT output FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
            "AND source_id=%s AND source_version=%s AND generation_id=%s "
            "AND (stage_key=%s OR starts_with(stage_key,%s||':recipe=')) "
            "AND input_digest=%s ORDER BY (stage_key=%s) DESC,stage_key LIMIT 1",
            (*identity, stage, stage, digest, stage),
        ).fetchone()
        if frozen:
            references = frozen[0]["references"]
            preferred_name_ids = frozen[0].get("preferred_name_ids", [])
            completed = connection.execute(
                "SELECT 1 FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s AND source_version=%s AND generation_id=%s "
                "AND (stage_key=%s OR starts_with(stage_key,%s||':recipe=')) "
                "AND input_digest=%s ORDER BY (stage_key=%s) DESC,stage_key LIMIT 1",
                (
                    *identity,
                    "extract:" + passage_id,
                    "extract:" + passage_id,
                    input_digest({**inputs, "reference_facts": references}),
                    "extract:" + passage_id,
                ),
            ).fetchone()
            if completed:
                return references
            eligible = eligible_reference_ids(
                connection, store.generation_id, [ref["id"] for ref in references]
            )
            references = [ref for ref in references if ref["id"] in eligible]
        else:
            queries = vectors if vectors is not None else (vector,)
            dense = reference_candidate_ids(connection, store.authority, store.config, queries)
            name_candidates = eligible_entity_name_candidates(
                connection,
                store.config,
                generation_id=store.generation_id,
                text=source_query_text(inputs),
                excluded_source_id=store.source["source_id"],
            )
            selected_ids = merge_reference_ids(
                name_candidates,
                dense,
                store.config.extraction.reference_max_facts,
            )
            preferred_name_ids = [
                name["name_id"]
                for candidate in name_candidates
                if candidate["names"][0]["memory_id"] in selected_ids
                for name in candidate["names"]
            ]
            rows = connection.execute(
                "SELECT unit_id,content_text,locator->>'role',mentioned_at,"
                "(SELECT jsonb_agg(detail ORDER BY annotation_id) FROM unit_times times "
                "WHERE (times.tenant_id,times.user_id,times.unit_id)="
                "(eligible.tenant_id,eligible.user_id,eligible.unit_id)) "
                f"FROM ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) eligible "
                "WHERE kind='fact' AND unit_id=ANY(%s) "
                "ORDER BY array_position(%s,unit_id)",
                (selected_ids, selected_ids),
            ).fetchall()
            candidates = tuple(
                {
                    "id": row[0],
                    "text": row[1],
                    "role": row[2],
                    "mentioned_at": row[3].isoformat() if row[3] else None,
                    "time_qualifiers": [
                        TemporalAnnotation.model_validate(
                            {
                                key: detail[key]
                                for key in TemporalAnnotation.model_fields
                                if key in detail
                            }
                        ).model_dump(mode="json")
                        for detail in row[4] or []
                    ],
                }
                for row in rows
            )
            references = list(candidates)
        from oam.indexing.entities import EntityRepository

        names = EntityRepository(
            connection, store.config, generation_id=store.generation_id, key=store.key
        ).reference_entities(
            [reference["id"] for reference in references],
            preferred_name_ids=tuple(preferred_name_ids),
        )
        references = [
            {
                **{key: value for key, value in reference.items() if key != "entities"},
                **({"entities": names[reference["id"]]} if names.get(reference["id"]) else {}),
            }
            for reference in references
        ]
        connection.execute(
            "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
            "generation_id,stage_key,input_digest,output,work_unit_id,attempt_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,user_id,source_id,source_version,generation_id,"
            "stage_key,input_digest) DO UPDATE SET output=EXCLUDED.output",
            (
                *identity,
                stage,
                digest,
                Jsonb({"references": references, "preferred_name_ids": preferred_name_ids}),
                store.claim.work_unit_id,
                store.claim.attempt_id,
            ),
        )
    return references
