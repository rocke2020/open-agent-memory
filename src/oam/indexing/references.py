"""Locally frozen eligible L1 context for optional additive extraction."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from oam.contracts.evidence import TemporalAnnotation
from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
from oam.indexing.store import IndexStore, input_digest


def eligible_reference_ids(connection, generation_id: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    return {
        row[0]
        for row in connection.execute(
            f"SELECT unit_id FROM ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) eligible "
            "WHERE kind='fact' AND generation_id=%s AND unit_id=ANY(%s)",
            (generation_id, ids),
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
    """Reuse completed extraction; revise only unfinished inputs after reference revocation."""
    identity = (
        *store.key,
        store.source["source_id"],
        store.source["source_version"],
        store.generation_id,
    )
    stage = "references:" + passage_id
    digest = input_digest(inputs)
    with store.transaction() as connection:
        store.guard(connection)
        frozen = connection.execute(
            "SELECT output FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
            "AND source_id=%s AND source_version=%s AND generation_id=%s "
            "AND stage_key=%s AND input_digest=%s",
            (*identity, stage, digest),
        ).fetchone()
        if frozen:
            references = frozen[0]["references"]
            completed = connection.execute(
                "SELECT 1 FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s AND source_version=%s AND generation_id=%s "
                "AND stage_key=%s AND input_digest=%s",
                (
                    *identity,
                    "extract:" + passage_id,
                    input_digest({**inputs, "reference_facts": references}),
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
            distances = ["embedding <=> %s::vector" for _ in queries]
            distance = distances[0] if len(distances) == 1 else "LEAST(" + ",".join(distances) + ")"
            rows = connection.execute(
                "SELECT unit_id,content_text,locator->>'role',mentioned_at,"
                "(SELECT jsonb_agg(detail ORDER BY annotation_id) FROM unit_times times "
                "WHERE (times.tenant_id,times.user_id,times.unit_id)="
                "(eligible.tenant_id,eligible.user_id,eligible.unit_id)) "
                f"FROM ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) eligible "
                "WHERE kind='fact' AND generation_id=%s AND source_id<>%s "
                "ORDER BY " + distance + ",unit_id LIMIT %s",
                (
                    store.generation_id,
                    store.source["source_id"],
                    *(str(list(query)) for query in queries),
                    store.config.extraction.reference_max_facts,
                ),
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
                Jsonb({"references": references}),
                store.claim.work_unit_id,
                store.claim.attempt_id,
            ),
        )
    return references
