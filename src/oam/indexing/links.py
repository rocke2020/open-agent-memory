"""Bounded fact-link proposals inside the caller's fenced publication transaction."""

from __future__ import annotations

from collections.abc import Sequence

from psycopg.pq import TransactionStatus

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS

_FACTS = f"""
WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}),
facts AS MATERIALIZED (
    SELECT * FROM eligible
    WHERE tenant_id=%(tenant)s AND user_id=%(user)s
      AND kind='fact' AND generation_id=%(generation)s
), seeds AS NOT MATERIALIZED (
    SELECT * FROM facts WHERE unit_id=ANY(%(ids)s)
)
"""

_SEMANTIC = (
    _FACTS
    + """
, inserted AS (
    INSERT INTO unit_links(tenant_id,user_id,source_id,target_id,kind,weight)
    SELECT seed.tenant_id,seed.user_id,seed.unit_id,target.unit_id,'semantic',target.weight
    FROM seeds seed
    CROSS JOIN LATERAL (
        SELECT candidate.unit_id,1-(candidate.embedding <=> seed.embedding) AS weight
        FROM facts candidate
        WHERE candidate.tenant_id=seed.tenant_id AND candidate.user_id=seed.user_id
          AND candidate.unit_id<>seed.unit_id
          AND 1-(candidate.embedding <=> seed.embedding)>0
          AND 1-(candidate.embedding <=> seed.embedding)>=%(similarity)s
          AND NOT EXISTS (
              SELECT 1 FROM unit_links prior
              WHERE prior.tenant_id=seed.tenant_id AND prior.user_id=seed.user_id
                AND prior.source_id=seed.unit_id AND prior.target_id=candidate.unit_id
                AND prior.kind='semantic'
          )
        ORDER BY candidate.embedding <=> seed.embedding,candidate.unit_id
        LIMIT GREATEST(0,%(semantic_neighbors)s-(
            SELECT count(*) FROM unit_links prior
            WHERE prior.tenant_id=seed.tenant_id AND prior.user_id=seed.user_id
              AND prior.source_id=seed.unit_id AND prior.kind='semantic'
        ))
    ) target
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""
)

_TEMPORAL_INSERT = """
, inserted AS (
    INSERT INTO temporal_links(tenant_id,user_id,time_basis,left_id,right_id,
                              left_annotation_id,right_annotation_id,gap_seconds,weight)
    SELECT tenant_id,user_id,%(basis)s,LEAST(source_id,target_id),GREATEST(source_id,target_id),
           CASE WHEN source_id<target_id THEN source_annotation ELSE target_annotation END,
           CASE WHEN source_id<target_id THEN target_annotation ELSE source_annotation END,
           gap,1-gap/%(horizon)s
    FROM proposals
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""

_INTERVAL = (
    _FACTS
    + """
, proposals AS (
    SELECT seed.tenant_id,seed.user_id,seed.unit_id AS source_id,
           target.unit_id AS target_id,target.source_annotation,target.target_annotation,target.gap
    FROM seeds seed
    CROSS JOIN LATERAL (
        SELECT candidate.unit_id,witness.source_annotation,witness.target_annotation,witness.gap
        FROM facts candidate
        CROSS JOIN LATERAL (
            SELECT source_time.annotation_id AS source_annotation,
                   target_time.annotation_id AS target_annotation,
                   GREATEST(0,
                       EXTRACT(EPOCH FROM
                           lower(target_time.time_span)-upper(source_time.time_span)),
                       EXTRACT(EPOCH FROM
                           lower(source_time.time_span)-upper(target_time.time_span))
                   )::double precision AS gap
            FROM unit_times source_time
            JOIN unit_times target_time
              ON target_time.tenant_id=source_time.tenant_id
             AND target_time.user_id=source_time.user_id
             AND target_time.role=source_time.role
             AND target_time.unit_id=candidate.unit_id
            WHERE source_time.tenant_id=seed.tenant_id AND source_time.user_id=seed.user_id
              AND source_time.unit_id=seed.unit_id AND source_time.role=%(role)s
              AND target_time.time_span && tstzrange(
                  lower(source_time.time_span)-%(horizon)s*interval '1 second',
                  upper(source_time.time_span)+%(horizon)s*interval '1 second','[]')
            ORDER BY gap,
                     CASE WHEN seed.unit_id<candidate.unit_id
                          THEN source_time.annotation_id ELSE target_time.annotation_id END,
                     CASE WHEN seed.unit_id<candidate.unit_id
                          THEN target_time.annotation_id ELSE source_time.annotation_id END
            LIMIT 1
        ) witness
        WHERE candidate.tenant_id=seed.tenant_id AND candidate.user_id=seed.user_id
          AND candidate.unit_id<>seed.unit_id AND witness.gap<%(horizon)s
        ORDER BY witness.gap,candidate.unit_id
        LIMIT %(temporal_neighbors)s
    ) target
)
"""
    + _TEMPORAL_INSERT
)

_POINT_COLUMNS = {
    "mentioned": "mentioned_at",
    "created": "source_created_at",
    "updated": "source_updated_at",
}


def _point_query(column: str) -> str:
    return (
        _FACTS
        + f"""
    , proposals AS (
        SELECT seed.tenant_id,seed.user_id,seed.unit_id AS source_id,
               target.unit_id AS target_id,NULL::text AS source_annotation,
               NULL::text AS target_annotation,target.gap
        FROM seeds seed
        CROSS JOIN LATERAL (
            SELECT candidate.unit_id,
                   abs(EXTRACT(EPOCH FROM
                       candidate.{column}-seed.{column}))::double precision AS gap
            FROM facts candidate
            WHERE candidate.tenant_id=seed.tenant_id AND candidate.user_id=seed.user_id
              AND candidate.unit_id<>seed.unit_id
              AND candidate.{column}>seed.{column}-%(horizon)s*interval '1 second'
              AND candidate.{column}<seed.{column}+%(horizon)s*interval '1 second'
            ORDER BY gap,candidate.unit_id
            LIMIT %(temporal_neighbors)s
        ) target
        WHERE seed.{column} IS NOT NULL
    )
    """
        + _TEMPORAL_INSERT
    )


def build_links(
    connection,
    authority: AuthorizedUser,
    config: OAMConfig,
    generation_id: str,
    new_fact_ids: Sequence[str],
) -> dict[str, int | str]:
    """Propose links from newly published fact unit IDs; never commit or delete."""
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ValueError("link construction requires the caller's publication transaction")
    if not generation_id or any(
        not isinstance(identity, str) or not identity for identity in new_fact_ids
    ):
        raise ValueError("link generation and fact identities must be nonempty strings")
    result: dict[str, int | str] = {
        "semantic_links": 0,
        "temporal_links": 0,
        "coverage": "complete",
    }
    if not new_fact_ids:
        return result
    parameters = {
        "tenant": authority.tenant_id,
        "user": authority.user_id,
        "generation": generation_id,
        "ids": sorted(set(new_fact_ids)),
        "similarity": config.index.semantic_link_min_similarity,
        "semantic_neighbors": config.index.semantic_link_max_neighbors,
        "temporal_neighbors": config.index.temporal_link_max_neighbors,
        "horizon": config.index.temporal_link_horizon_seconds,
    }
    with connection.cursor() as cursor:
        bind_user(cursor, authority)
        cursor.execute("SET LOCAL enable_nestloop = off")
        cursor.execute(_SEMANTIC, parameters)
        result["semantic_links"] = cursor.fetchone()[0]
        temporal_count = 0
        for basis, role in (("event", "event"), ("referenced", "date_reference")):
            cursor.execute(_INTERVAL, {**parameters, "basis": basis, "role": role})
            temporal_count += cursor.fetchone()[0]
        for basis, column in _POINT_COLUMNS.items():
            cursor.execute(_point_query(column), {**parameters, "basis": basis})
            temporal_count += cursor.fetchone()[0]
        result["temporal_links"] = temporal_count
    return result
