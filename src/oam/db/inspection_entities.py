"""Bounded entity browsing over current eligible fact projections."""

import json

from psycopg.rows import dict_row

from oam.contracts.memory import MEMORY_TYPE_BY_KIND
from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
from oam.db.entity_eligibility import ENTITY_PROJECTIONS_FROM_FILTERED_FACTS
from oam.db.inspection import _MEMORY_ITEMS, INSPECTION_PAGE_SIZE, SourceInspector

_PROJECTIONS = (
    f"WITH filtered AS MATERIALIZED ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS})"
    + ENTITY_PROJECTIONS_FROM_FILTERED_FACTS
    + ", canonical AS MATERIALIZED ("
    "SELECT DISTINCT ON (n.tenant_id,n.user_id,n.entity_id) "
    "n.tenant_id,n.user_id,n.entity_id,"
    "CASE WHEN e.canonical_key IS NOT NULL AND e.canonical_base_key=n.name_key "
    "THEN e.canonical_name ELSE n.name_text END AS name "
    "FROM name_support n JOIN entity_units e USING(tenant_id,user_id,entity_id) "
    "WHERE n.support_kind='canonical' "
    "ORDER BY n.tenant_id,n.user_id,n.entity_id,"
    "(n.name_key=e.canonical_base_key) DESC,n.name_text,n.name_id), "
    "entity_facts AS MATERIALIZED ("
    "SELECT DISTINCT c.entity_id,c.name,f.* FROM canonical c "
    "JOIN memberships m USING(tenant_id,user_id,entity_id) "
    "JOIN filtered f USING(tenant_id,user_id,unit_id,generation_id) "
    "WHERE f.kind='fact') "
)


_MATCH = """(%s='' OR strpos(lower(ef.name),lower(%s))>0 OR EXISTS (
SELECT 1 FROM name_support ns WHERE
(ns.tenant_id,ns.user_id,ns.entity_id,ns.generation_id)=
(ef.tenant_id,ef.user_id,ef.entity_id,ef.generation_id)
AND strpos(lower(ns.name_text),lower(%s))>0))"""
_STATS = """SELECT ef.entity_id,min(ef.name) AS name,
count(DISTINCT ef.memory_id) AS memory_count,
min(COALESCE(ef.mentioned_at,ef.created_at)) AS first_mentioned_at,
max(COALESCE(ef.mentioned_at,ef.created_at)) AS last_mentioned_at
FROM entity_facts ef """


def list_entities(connection, authority, *, query="", after=None):
    with connection.transaction():
        epoch = SourceInspector._admit(connection, authority)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                _PROJECTIONS
                + ", matches AS MATERIALIZED ("
                + _STATS
                + "WHERE "
                + _MATCH
                + " GROUP BY ef.entity_id), boundary AS ("
                "SELECT memory_count FROM matches WHERE entity_id=%s), "
                "totals AS (SELECT count(*) AS total FROM matches), "
                "page AS (SELECT * FROM matches WHERE %s::text IS NULL OR "
                "memory_count<(SELECT memory_count FROM boundary) OR "
                "(memory_count=(SELECT memory_count FROM boundary) AND entity_id>%s) "
                "ORDER BY memory_count DESC,entity_id LIMIT %s) "
                "SELECT totals.total,EXISTS(SELECT 1 FROM boundary) AS cursor_exists,page.* "
                "FROM totals LEFT JOIN page ON true ORDER BY page.memory_count DESC,page.entity_id",
                (query, query, query, after, after, after, INSPECTION_PAGE_SIZE + 1),
            )
            result = cursor.fetchall()
            total = result[0]["total"]
            if after is not None and not result[0]["cursor_exists"]:
                raise ValueError("invalid entity cursor")
            rows = [
                {key: value for key, value in row.items() if key not in ("total", "cursor_exists")}
                for row in result
                if row["entity_id"] is not None
            ]
        items = rows[:INSPECTION_PAGE_SIZE]
        SourceInspector._check_policy(connection, authority, epoch)
        return {
            "items": items,
            "total": total,
            "next_after": items[-1]["entity_id"] if len(rows) > INSPECTION_PAGE_SIZE else None,
            "policy_epoch": epoch,
        }


def entity_graph(connection, authority, *, query=""):
    with connection.transaction():
        epoch = SourceInspector._admit(connection, authority)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                _PROJECTIONS
                + _STATS
                + "WHERE "
                + _MATCH
                + " GROUP BY ef.entity_id ORDER BY memory_count DESC,ef.entity_id LIMIT 501",
                (query, query, query),
            )
            rows = cursor.fetchall()
            cursor.execute(
                _PROJECTIONS + "SELECT count(DISTINCT ef.entity_id) AS total "
                "FROM entity_facts ef WHERE " + _MATCH,
                (query, query, query),
            )
            total = cursor.fetchone()["total"]
            nodes = rows[:500]
            ids = [node["entity_id"] for node in nodes]
            cursor.execute(
                _PROJECTIONS + "SELECT a.entity_id AS source,b.entity_id AS target,"
                "count(DISTINCT a.memory_id) AS weight FROM entity_facts a "
                "JOIN entity_facts b USING(tenant_id,user_id,memory_id) "
                "WHERE a.entity_id<b.entity_id AND a.entity_id=ANY(%s) "
                "AND b.entity_id=ANY(%s) GROUP BY a.entity_id,b.entity_id "
                "ORDER BY weight DESC,a.entity_id,b.entity_id LIMIT 5001",
                (ids, ids),
            )
            edges = cursor.fetchall()
        SourceInspector._check_policy(connection, authority, epoch)
        return {
            "nodes": nodes,
            "edges": edges[:5000],
            "total": total,
            "truncated": len(rows) > 500,
            "edges_truncated": len(edges) > 5000,
            "policy_epoch": epoch,
        }


def get_entity_memories(connection, authority, entity_id, *, after=None, names_after=None):
    """Read supporting facts chronologically, using creation time for undated mentions."""
    with connection.transaction():
        epoch = SourceInspector._admit(connection, authority)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                _PROJECTIONS + _STATS + "WHERE ef.entity_id=%s GROUP BY ef.entity_id",
                (entity_id,),
            )
            metadata = cursor.fetchone()
            if metadata is None:
                SourceInspector._check_policy(connection, authority, epoch)
                return None
            names = _entity_names(cursor, entity_id, names_after)
            if names_after is not None and after is None:
                SourceInspector._check_policy(connection, authority, epoch)
                return {**metadata, **names, "items": [], "next_after": None, "policy_epoch": epoch}
            boundary_time = None
            if after:
                cursor.execute(
                    _PROJECTIONS + "SELECT COALESCE(mentioned_at,created_at) AS mention_time "
                    "FROM entity_facts WHERE entity_id=%s AND unit_id=%s",
                    (entity_id, after),
                )
                boundary = cursor.fetchone()
                if boundary is None:
                    raise ValueError("invalid entity memory cursor")
                boundary_time = boundary["mention_time"]
            cursor.execute(
                _PROJECTIONS + ", selected AS (SELECT * FROM entity_facts "
                "WHERE entity_id=%s AND (%s::timestamptz IS NULL OR "
                "(COALESCE(mentioned_at,created_at),unit_id)>(%s::timestamptz,%s)) "
                "ORDER BY COALESCE(mentioned_at,created_at),unit_id LIMIT %s), "
                "items AS (" + _MEMORY_ITEMS + ") "
                "SELECT items.*,COALESCE(s.mentioned_at,s.created_at) AS mention_time, "
                "COALESCE((SELECT jsonb_agg(jsonb_build_object("
                "'source_id',e.source_id,'source_version',e.source_version,"
                "'passage_id',p.passage_id,'text',p.normalized_text,"
                "'mentions',jsonb_build_array(m.observed_mention),'quote',m.mention_quote,"
                "'mention_spans',(SELECT jsonb_agg(mention->'span') "
                "FROM jsonb_array_elements(raw.source_mentions) mention "
                "WHERE mention->'span' IS NOT NULL AND mention->'span'<>'null'::jsonb "
                "AND EXISTS (SELECT 1 FROM name_support ns WHERE ns.entity_id=raw.entity_id "
                "AND ns.name_id=mention->>'name_id'))) "
                "ORDER BY e.source_id,p.passage_id) FROM memberships m "
                "JOIN unit_entities raw USING(tenant_id,user_id,unit_id,entity_key) "
                "JOIN support sp ON (sp.tenant_id,sp.user_id,sp.memory_id)="
                "(s.tenant_id,s.user_id,s.memory_id) "
                "JOIN passages p ON (p.tenant_id,p.user_id,p.passage_id)="
                "(sp.tenant_id,sp.user_id,sp.passage_id) "
                "JOIN evidence e ON (e.tenant_id,e.user_id,e.evidence_id)="
                "(sp.tenant_id,sp.user_id,sp.evidence_id) "
                "JOIN source_versions v ON (v.tenant_id,v.user_id,v.source_id,v.source_version)="
                "(e.tenant_id,e.user_id,e.source_id,e.source_version) "
                "JOIN sources src ON (src.tenant_id,src.user_id,src.source_id)="
                "(e.tenant_id,e.user_id,e.source_id) "
                "WHERE (m.tenant_id,m.user_id,m.unit_id,m.generation_id,m.entity_id)="
                "(s.tenant_id,s.user_id,s.unit_id,s.generation_id,s.entity_id) "
                "AND p.passage_id=s.locator->>'passage_id' "
                "AND p.generation_id=s.generation_id AND p.evidence_id=e.evidence_id "
                "AND p.visible_to IS NULL AND e.status='active' AND e.learning_eligible "
                "AND v.status='active' AND src.status='active' "
                "AND src.active_version=e.source_version AND src.input_role='interaction'),"
                "'[]'::jsonb) AS source_chunks "
                "FROM items JOIN selected s USING(unit_id) "
                "ORDER BY mention_time,items.unit_id",
                (entity_id, boundary_time, boundary_time, after or "", INSPECTION_PAGE_SIZE + 1),
            )
            rows = cursor.fetchall()
        items = rows[:INSPECTION_PAGE_SIZE]
        for item in items:
            item["memory_type"] = MEMORY_TYPE_BY_KIND[item["kind"]]
        SourceInspector._check_policy(connection, authority, epoch)
        return {
            **metadata,
            **names,
            "items": items,
            "next_after": items[-1]["unit_id"] if len(rows) > INSPECTION_PAGE_SIZE else None,
            "policy_epoch": epoch,
        }


def _entity_names(cursor, entity_id, after):
    boundary = None
    if after is not None:
        try:
            boundary = json.loads(after)
            if (
                not isinstance(boundary, list)
                or len(boundary) != 2
                or boundary[0] not in ("alias", "original")
                or not isinstance(boundary[1], str)
            ):
                raise ValueError("invalid names cursor")
        except (ValueError, TypeError) as error:
            raise ValueError("invalid names cursor") from error
    cursor.execute(
        _PROJECTIONS + ", names AS MATERIALIZED ("
        "SELECT DISTINCT CASE WHEN name_kind='alias' THEN 'alias' ELSE 'original' END AS kind,"
        "name_text AS name FROM name_support WHERE entity_id=%s UNION "
        "SELECT CASE WHEN mention->>'kind'='alias' THEN 'alias' ELSE 'original' END,"
        "mention->>'name' FROM memberships m "
        "JOIN unit_entities raw USING(tenant_id,user_id,unit_id,entity_key) "
        "CROSS JOIN LATERAL jsonb_array_elements(raw.source_mentions) mention "
        "WHERE m.entity_id=%s AND EXISTS (SELECT 1 FROM name_support ns "
        "WHERE ns.entity_id=m.entity_id AND ns.name_id=mention->>'name_id')), totals AS ("
        "SELECT count(*) FILTER(WHERE kind='original') AS original_names_total,"
        "count(*) FILTER(WHERE kind='alias') AS aliases_total FROM names), page AS ("
        "SELECT * FROM names WHERE %s::text IS NULL OR (kind,name)>(%s,%s) "
        "ORDER BY kind,name LIMIT %s) SELECT totals.*,page.*,"
        "EXISTS(SELECT 1 FROM names WHERE (kind,name)=(%s,%s)) AS cursor_exists "
        "FROM totals LEFT JOIN page ON true ORDER BY kind,name",
        (
            entity_id,
            entity_id,
            after,
            *(boundary or (None, None)),
            INSPECTION_PAGE_SIZE + 1,
            *(boundary or (None, None)),
        ),
    )
    rows = cursor.fetchall()
    if boundary is not None and not rows[0]["cursor_exists"]:
        raise ValueError("invalid names cursor")
    values = [row for row in rows if row["name"] is not None]
    page = values[:INSPECTION_PAGE_SIZE]
    return {
        "original_names": [row["name"] for row in page if row["kind"] == "original"],
        "aliases": [row["name"] for row in page if row["kind"] == "alias"],
        "original_names_total": rows[0]["original_names_total"],
        "aliases_total": rows[0]["aliases_total"],
        "names_next_after": json.dumps([page[-1]["kind"], page[-1]["name"]], ensure_ascii=False)
        if len(values) > INSPECTION_PAGE_SIZE
        else None,
    }
