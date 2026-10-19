"""Read stored connections without inferring new relationships."""

from psycopg.rows import dict_row

from oam.db.eligibility import ELIGIBLE_UNITS
from oam.db.entity_eligibility import ENTITY_PROJECTIONS_FROM_SEED_ENTITIES

RELATIONSHIP_LIMIT = 50


def memory_connections(connection, item: dict) -> dict:
    support_ids = (
        [support["memory_id"] for support in item["supports"]]
        if item["kind"] == "insight"
        else [item["memory_id"]]
    )
    query = (
        f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}),"
        "filtered AS MATERIALIZED (SELECT * FROM eligible WHERE kind='fact'),"
        "seeds AS MATERIALIZED (SELECT * FROM filtered WHERE memory_id=ANY(%(supports)s))"
        + ENTITY_PROJECTIONS_FROM_SEED_ENTITIES
        + """
        , entities AS (
            SELECT m.entity_id,e.canonical_name AS name,min(m.observed_mention) AS mention
            FROM seeds s JOIN memberships m USING(tenant_id,user_id,unit_id,generation_id)
            JOIN entity_units e USING(tenant_id,user_id,entity_id,generation_id)
            GROUP BY m.entity_id,e.canonical_name
        ), connections AS (
            SELECT l.kind,
                   CASE WHEN l.source_id=%(unit)s THEN 'outgoing' ELSE 'incoming' END
                       AS direction,
                   CASE WHEN l.source_id=%(unit)s THEN l.target_id ELSE l.source_id END
                       AS related_id,
                   NULL::text AS entity_id,NULL::text AS entity_name,
                   NULL::text AS time_basis,NULL::double precision AS gap_seconds,
                   NULL::jsonb AS time
            FROM unit_links l
            WHERE l.tenant_id=current_setting('oam.tenant_id',true)
              AND l.user_id=current_setting('oam.user_id',true)
              AND (l.source_id=%(unit)s OR l.target_id=%(unit)s)
            UNION ALL
            SELECT 'temporal','undirected',
                   CASE WHEN t.left_id=%(unit)s THEN t.right_id ELSE t.left_id END,
                   NULL,NULL,t.time_basis,t.gap_seconds,
                   jsonb_build_object(
                       'memory_start',COALESCE(lower(own_time.time_span),
                           CASE t.time_basis WHEN 'mentioned' THEN own.mentioned_at
                           WHEN 'created' THEN own.source_created_at
                           WHEN 'updated' THEN own.source_updated_at END),
                       'memory_end',upper(own_time.time_span),
                       'related_start',COALESCE(lower(peer_time.time_span),
                           CASE t.time_basis WHEN 'mentioned' THEN peer.mentioned_at
                           WHEN 'created' THEN peer.source_created_at
                           WHEN 'updated' THEN peer.source_updated_at END),
                       'related_end',upper(peer_time.time_span))
            FROM temporal_links t
            JOIN search_units own ON (own.tenant_id,own.user_id,own.unit_id)=
                (t.tenant_id,t.user_id,%(unit)s)
            JOIN search_units peer ON (peer.tenant_id,peer.user_id,peer.unit_id)=
                (t.tenant_id,t.user_id,
                 CASE WHEN t.left_id=%(unit)s THEN t.right_id ELSE t.left_id END)
            LEFT JOIN unit_times own_time ON
                (own_time.tenant_id,own_time.user_id,own_time.unit_id,own_time.annotation_id)=
                (t.tenant_id,t.user_id,%(unit)s,
                 CASE WHEN t.left_id=%(unit)s THEN t.left_annotation_id
                      ELSE t.right_annotation_id END)
            LEFT JOIN unit_times peer_time ON
                (peer_time.tenant_id,peer_time.user_id,peer_time.unit_id,peer_time.annotation_id)=
                (t.tenant_id,t.user_id,peer.unit_id,
                 CASE WHEN t.left_id=%(unit)s THEN t.right_annotation_id
                      ELSE t.left_annotation_id END)
            WHERE t.tenant_id=current_setting('oam.tenant_id',true)
              AND t.user_id=current_setting('oam.user_id',true)
              AND (t.left_id=%(unit)s OR t.right_id=%(unit)s)
            UNION ALL
            SELECT DISTINCT 'shared_entity','undirected',peer.unit_id,
                   seed.entity_id,e.canonical_name,NULL::text,NULL::double precision,NULL::jsonb
            FROM memberships seed JOIN memberships peer
                USING(tenant_id,user_id,entity_id,generation_id)
            JOIN entity_units e USING(tenant_id,user_id,entity_id,generation_id)
            WHERE seed.unit_id=%(unit)s AND peer.unit_id<>seed.unit_id
        ), related AS (
            SELECT c.*,p.memory_id,p.insight_id,p.kind AS memory_kind,
                   p.content_text AS text
            FROM connections c JOIN eligible p ON p.unit_id=c.related_id
            WHERE p.kind IN ('fact','insight')
            ORDER BY CASE c.kind WHEN 'caused_by' THEN 0 WHEN 'updates' THEN 1
                WHEN 'temporal' THEN 2 WHEN 'shared_entity' THEN 3 ELSE 4 END,
                c.related_id,c.kind,c.direction,c.time_basis,c.entity_id
            LIMIT %(limit)s
        )
        SELECT COALESCE((SELECT jsonb_agg(e ORDER BY e.name,e.entity_id) FROM entities e),
                        '[]'::jsonb) AS entities,
               COALESCE((SELECT jsonb_agg(r) FROM related r),'[]'::jsonb) AS relationships
        """
    )
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            query,
            {"unit": item["unit_id"], "supports": support_ids, "limit": RELATIONSHIP_LIMIT + 1},
        )
        result = cursor.fetchone()
    relationships = result["relationships"]
    return {
        "entities": result["entities"],
        "relationships": relationships[:RELATIONSHIP_LIMIT],
        "relationships_truncated": len(relationships) > RELATIONSHIP_LIMIT,
    }
