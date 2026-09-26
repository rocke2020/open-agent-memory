"""User-qualified PostgreSQL reads for the shared recall pipeline."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.contracts.search import SearchRequest
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_SUPPORT, ELIGIBLE_UNITS
from oam.retrieval.algorithms import (
    GraphEdge,
    TemporalEdge,
    TemporalNode,
    ordinary_graph_activation,
    reciprocal_rank_fusion,
)
from oam.retrieval.fts import QueryTerms, query_terms

_ANCHOR = re.compile(r'"([^"\n]+)"|\b([A-Za-z][A-Za-z0-9]*-\d+[A-Za-z0-9-]*|[\w]+_[\w-]+)\b')
_QUERY_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "be",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    }
)


def _informative_terms(terms: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (configuration, word)
        for configuration, word in terms
        if configuration != "english" or word not in _QUERY_FUNCTION_WORDS
    )


def _tsquery(terms: tuple[tuple[str, str], ...], operator: str, parameters: dict, name: str):
    parts = []
    for index, (configuration, word) in enumerate(terms):
        key = f"{name}_{index}"
        parameters[key] = word
        parts.append(f"plainto_tsquery('{configuration}',%({key})s)")
    return f"({f' {operator} '.join(parts)})" if parts else "''::tsquery"


def _rank_queries(terms: QueryTerms, parameters: dict) -> tuple[str, str, str, str]:
    candidates = _informative_terms(terms.candidates)
    primary_terms = _informative_terms(terms.primary)
    candidate = _tsquery(candidates, "||", parameters, "candidate")
    primary = _tsquery(primary_terms, "&&", parameters, "primary")
    full = _tsquery((*primary_terms, *terms.modifiers), "&&", parameters, "full")
    term_queries = ",".join(
        _tsquery((term,), "||", parameters, f"term_{index}")
        for index, term in enumerate(candidates)
    )
    return candidate, primary, full, term_queries


class RecallRepository:
    def __init__(
        self,
        connection,
        authority: AuthorizedUser,
        config: OAMConfig,
        *,
        deadline: float,
        request: SearchRequest | None = None,
    ):
        if not authority.allows("memory:read"):
            raise PermissionError("memory:read is required")
        self.connection = connection
        self.authority = authority
        self.config = config
        self.deadline = deadline
        self.request = request
        self.time_parameters = {}
        bind_user(connection.cursor(), authority)
        connection.execute("SELECT set_config('jit','off',true)")

    def _eligible(self, *, temporal: bool = False) -> str:
        predicate = "true"
        request = self.request
        if temporal and request is not None and request.time_window:
            basis = request.time_basis
            window = request.time_window
            self.time_parameters = {
                "time_start": window.start,
                "time_end": window.end,
                "time_role": "date_reference" if basis == "referenced" else "event",
            }

            def direct(alias):
                if basis in {"event", "referenced"}:
                    return (
                        "EXISTS (SELECT 1 FROM unit_times ut WHERE "
                        "(ut.tenant_id,ut.user_id,ut.unit_id)="
                        f"({alias}.tenant_id,{alias}.user_id,{alias}.unit_id) "
                        "AND ut.role=%(time_role)s AND ut.time_span && "
                        "tstzrange(%(time_start)s,%(time_end)s,'[)'))"
                    )
                column = {
                    "mentioned": "mentioned_at",
                    "created": "source_created_at",
                    "updated": "source_updated_at",
                }[basis]
                return f"{alias}.{column} <@ tstzrange(%(time_start)s,%(time_end)s,'[)')"

            predicate = (
                f"((u.kind<>'insight' AND {direct('u')}) OR (u.kind='insight' AND NOT EXISTS ("
                "SELECT 1 FROM insight_support isp WHERE "
                "(isp.tenant_id,isp.user_id,isp.insight_id,isp.insight_revision)="
                "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision) AND NOT EXISTS ("
                "SELECT 1 FROM eligible child WHERE child.kind='fact' AND "
                "(child.tenant_id,child.user_id,child.memory_id)="
                f"(isp.tenant_id,isp.user_id,isp.memory_id) AND {direct('child')}))))"
            )
        return (
            f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}), "
            f"filtered AS NOT MATERIALIZED (SELECT u.* FROM eligible u WHERE {predicate}) "
        )

    def _execute(self, sql: str, parameters=()):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recall deadline exhausted")
        self.connection.execute(
            "SELECT set_config('statement_timeout',%s,true)", (str(max(1, int(remaining * 1000))),)
        )
        cursor = self.connection.cursor(row_factory=dict_row)
        if isinstance(parameters, dict):
            parameters = {**self.time_parameters, **parameters}
        cursor.execute(sql, parameters)
        return cursor.fetchall()

    def user_generation(self) -> str | None:
        return self.user_state()["index_generation_id"]

    def record_trace(self, trace_id: str, policy_epoch: int, decisions: dict, result: dict) -> bool:
        row = self.connection.execute(
            "INSERT INTO traces(tenant_id,user_id,trace_id,read_seq,generation_id,"
            "policy_epoch,decisions,content,expires_at) "
            "SELECT tenant_id,user_id,%s,published_seq,index_generation_id,policy_epoch,"
            "%s,%s,clock_timestamp()+(%s*interval '1 day') FROM users "
            "WHERE tenant_id=%s AND user_id=%s AND state='active' AND policy_epoch=%s "
            "FOR SHARE RETURNING trace_id",
            (
                trace_id,
                Jsonb(decisions),
                Jsonb(result),
                self.config.trace.metadata_retention_days,
                self.authority.tenant_id,
                self.authority.user_id,
                policy_epoch,
            ),
        ).fetchone()
        return row is not None

    def user_state(self) -> dict:
        rows = self._execute(
            "SELECT sc.index_generation_id,sc.policy_epoch,"
            "COALESCE(bool_or(base.stage_state->>'parse_coverage'='partial'),false) "
            "AS parse_partial,CASE "
            "WHEN bool_or(base.status IN ('partial_failed','failed_terminal','cancelled') "
            "OR base.stage_state->>'extraction_coverage'='partial' "
            "OR base.stage_state ? 'error') "
            "THEN 'partial' WHEN bool_or(s.source_id IS NOT NULL AND "
            "(base.status IS NULL OR base.status IN ('accepted','running'))) "
            "THEN 'pending' ELSE 'ready' END AS readiness FROM users sc "
            "LEFT JOIN sources s ON (s.tenant_id,s.user_id)=(sc.tenant_id,sc.user_id) "
            "AND s.status='active' AND s.input_role IN ('interaction','evidence_resource') "
            "LEFT JOIN LATERAL (SELECT o.status,o.stage_state FROM operations o WHERE "
            "(o.tenant_id,o.user_id,o.source_id,o.source_version)="
            "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
            "AND o.kind<>'consolidate' AND EXISTS ("
            "SELECT 1 FROM work_units w WHERE "
            "(w.tenant_id,w.user_id,w.operation_id,w.generation_id)="
            "(o.tenant_id,o.user_id,o.operation_id,sc.index_generation_id)) "
            "ORDER BY o.created_at DESC,o.operation_id DESC LIMIT 1) base ON true "
            "WHERE sc.tenant_id=%s AND sc.user_id=%s AND sc.state='active' "
            "GROUP BY sc.index_generation_id,sc.policy_epoch",
            (self.authority.tenant_id, self.authority.user_id),
        )
        if not rows:
            raise PermissionError("active user is unavailable")
        return rows[0]

    def lexical(self, query: str, limit: int) -> list[dict]:
        # Protected literals are ranked by anchors, including their query remainder.
        if _ANCHOR.search(query):
            return []
        terms = query_terms(query)
        candidates = _informative_terms(terms.candidates)
        parameters = {"limit": limit, "weights": list(self.config.search.fts_rank_weights)}
        if not candidates:
            if not terms.candidates:
                return []
            return self._execute(
                self._eligible()
                + "SELECT u.unit_id,ts_rank_cd(u.lexical,plainto_tsquery('simple',%(query)s)) "
                "AS score FROM filtered u WHERE "
                "u.lexical @@ plainto_tsquery('simple',%(query)s) "
                "ORDER BY score DESC,u.mentioned_at DESC NULLS LAST,u.unit_id "
                "LIMIT %(limit)s",
                {"query": query, "limit": limit},
            )
        candidate, primary, full, term_queries = _rank_queries(terms, parameters)
        rows = self._execute(
            "WITH queries AS (SELECT "
            f"{candidate} AS candidate,{primary} AS primary,{full} AS full), "
            "hits AS MATERIALIZED (SELECT u.unit_id,u.lexical_bilingual,u.mentioned_at,"
            "u.lexical_bilingual @@ q.full AS full_match,"
            "u.lexical_bilingual @@ q.primary AS primary_match "
            "FROM search_units u,queries q WHERE "
            "u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            "AND u.lexical_bilingual @@ q.candidate "
            "AND EXISTS (SELECT 1 FROM ("
            + ELIGIBLE_UNITS
            + ") eligible WHERE (eligible.tenant_id,eligible.user_id,eligible.unit_id)="
            "(u.tenant_id,u.user_id,u.unit_id))) "
            "SELECT t.term_order,hit.unit_id,hit.mentioned_at,hit.full_match,hit.primary_match "
            f"FROM unnest(ARRAY[{term_queries}]) WITH ORDINALITY AS t(term,term_order) "
            "CROSS JOIN LATERAL (SELECT u.unit_id,u.mentioned_at,u.full_match,u.primary_match,"
            "ts_rank_cd(%(weights)s::float4[],u.lexical_bilingual,t.term) AS score "
            "FROM hits u WHERE u.lexical_bilingual @@ t.term "
            "ORDER BY u.full_match DESC,u.primary_match DESC,score DESC,"
            "u.mentioned_at DESC NULLS LAST,u.unit_id LIMIT %(limit)s) hit "
            "ORDER BY t.term_order,hit.full_match DESC,hit.primary_match DESC,hit.score DESC,"
            "hit.mentioned_at DESC NULLS LAST,hit.unit_id",
            parameters,
        )
        arms = [[] for _ in candidates]
        evidence = {}
        for row in rows:
            arms[row["term_order"] - 1].append(row["unit_id"])
            evidence[row["unit_id"]] = row
        ranked = reciprocal_rank_fusion(arms, rrf_k=self.config.search.rrf_k)
        ranked = sorted(
            ranked,
            key=lambda item: (
                -evidence[item.node_id]["full_match"],
                -evidence[item.node_id]["primary_match"],
                -item.score,
                -(
                    evidence[item.node_id]["mentioned_at"].timestamp()
                    if evidence[item.node_id]["mentioned_at"]
                    else float("-inf")
                ),
                item.node_id,
            ),
        )
        return [{"unit_id": item.node_id, "score": item.score} for item in ranked[:limit]]

    def anchors(self, query: str, limit: int) -> list[dict]:
        matches = _ANCHOR.findall(query)
        phrases = [quoted.casefold() for quoted, _ in matches if quoted]
        identifiers = [
            f"(^|[^[:alnum:]_-]){re.escape(identifier)}($|[^[:alnum:]_-])"
            for _, identifier in matches
            if identifier
        ]
        if not phrases and not identifiers:
            return []
        terms = query_terms(_ANCHOR.sub(" ", query))
        candidates = _informative_terms(terms.candidates)
        parameters = {
            "phrases": phrases,
            "identifiers": identifiers,
            "limit": limit,
            "weights": list(self.config.search.fts_rank_weights),
        }
        relevance_order = ""
        if candidates:
            candidate, primary, full, term_queries = _rank_queries(terms, parameters)
            relevance_order = (
                f"(u.lexical_bilingual @@ {full}) DESC,"
                f"(u.lexical_bilingual @@ {primary}) DESC,"
                f"(SELECT count(*) FROM unnest(ARRAY[{term_queries}]) t(term) "
                "WHERE u.lexical_bilingual @@ t.term) DESC,"
                f"ts_rank_cd(%(weights)s::float4[],u.lexical_bilingual,{candidate}) DESC,"
            )
        return self._execute(
            self._eligible() + "SELECT unit_id,1.0 AS score FROM filtered u WHERE EXISTS ("
            "SELECT 1 FROM unnest(%(phrases)s::text[]) p "
            "WHERE strpos(lower(u.content_text),p)>0) OR EXISTS ("
            "SELECT 1 FROM unnest(%(identifiers)s::text[]) i "
            "WHERE u.content_text ~* i) "
            "ORDER BY "
            + relevance_order
            + "u.mentioned_at DESC NULLS LAST,u.unit_id LIMIT %(limit)s",
            parameters,
        )

    def dense(self, vector: Sequence[float], limit: int) -> list[dict]:
        self.connection.execute(
            "SELECT set_config('hnsw.iterative_scan',%s,true), "
            "set_config('hnsw.max_scan_tuples',%s,true)",
            (self.config.search.ann_iterative_scan, str(self.config.search.ann_scan_max_tuples)),
        )
        # Keep the distance-ordered unit scan outside the support joins. The
        # unique scalar lookup still applies all eligibility before admission.
        return self._execute(
            self._eligible()
            + "SELECT u.unit_id,u.kind,1-(u.embedding <=> %(vector)s::vector) AS score "
            "FROM search_units u WHERE u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            "AND (SELECT true FROM filtered f WHERE "
            "(f.tenant_id,f.user_id,f.unit_id)=(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
            "ORDER BY u.embedding <=> %(vector)s::vector LIMIT %(limit)s",
            {"vector": json.dumps(tuple(vector)), "limit": limit},
        )

    def hydrate(self, unit_ids: Sequence[str]) -> list[dict]:
        rows = self._execute(
            self._eligible().rstrip() + ", "
            f"valid_support AS NOT MATERIALIZED ({ELIGIBLE_SUPPORT}) "
            "SELECT u.unit_id,u.kind,u.origin,u.content_text AS text,u.locator, "
            "u.mentioned_at,u.source_created_at,u.source_updated_at, "
            "(SELECT i.subtype FROM insights i WHERE "
            "(i.tenant_id,i.user_id,i.insight_id,i.revision)="
            "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision)) AS subtype, "
            "COALESCE((SELECT jsonb_agg(jsonb_build_object('memory_id',isp.memory_id,"
            "'support_revision',isp.support_revision) ORDER BY isp.memory_id) "
            "FROM insight_support isp WHERE "
            "(isp.tenant_id,isp.user_id,isp.insight_id,isp.insight_revision)="
            "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision)), '[]') AS supports, "
            "COALESCE((SELECT jsonb_agg(jsonb_build_object('source_id',sp.source_id,"
            "'source_version',sp.source_version,'evidence_id',sp.evidence_id,"
            "'passage_id',sp.passage_id,'span',sp.span) "
            "ORDER BY sp.source_id,sp.passage_id,sp.memory_id) "
            "FROM valid_support sp WHERE sp.memory_id=ANY(CASE WHEN u.kind='insight' "
            "THEN ARRAY(SELECT isp.memory_id FROM insight_support isp WHERE "
            "(isp.tenant_id,isp.user_id,isp.insight_id,isp.insight_revision)="
            "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision)) "
            "ELSE ARRAY[u.memory_id] END)), "
            "jsonb_build_array(u.locator)) AS citations, "
            "COALESCE((SELECT jsonb_agg(jsonb_build_object("
            "'role',ut.role,'range',ut.time_span::text,"
            "'detail',ut.detail) ORDER BY ut.annotation_id) FROM unit_times ut WHERE "
            "(ut.tenant_id,ut.user_id,ut.unit_id)=(u.tenant_id,u.user_id,u.unit_id)), "
            "'[]') AS time_evidence "
            "FROM filtered u WHERE unit_id=ANY(%(ids)s) ORDER BY array_position(%(ids)s,unit_id)",
            {"ids": list(unit_ids)},
        )
        for row in rows:
            if row["kind"] != "insight":
                row.pop("subtype")
                row.pop("supports")
            for column, role in (
                ("mentioned_at", "mentioned"),
                ("source_created_at", "created"),
                ("source_updated_at", "updated"),
            ):
                instant = row.pop(column)
                if instant is not None:
                    row["time_evidence"].append({"role": role, "at": instant.isoformat()})
        return rows

    def ordinary(self, seed_ids: Sequence[str], vector: Sequence[float]):
        config = self.config.search
        parameters = {
            "seeds": list(seed_ids),
            "vector": json.dumps(tuple(vector)),
            "entities": config.entities_per_seed,
            "per_entity": config.entity_candidates_per_entity,
            "links": config.links_per_seed,
        }
        prefix = (
            self._eligible()
            + ", seeds AS (SELECT * FROM filtered WHERE kind='fact' AND unit_id=ANY(%(seeds)s)) "
        )
        entities = self._execute(
            prefix + "SELECT s.unit_id AS seed_id,e.entity_key,c.unit_id FROM seeds s "
            "CROSS JOIN LATERAL (SELECT entity_key FROM unit_entities ue WHERE "
            "(ue.tenant_id,ue.user_id,ue.unit_id)=(s.tenant_id,s.user_id,s.unit_id) "
            "ORDER BY entity_key LIMIT %(entities)s) e CROSS JOIN LATERAL ("
            "SELECT u.unit_id FROM filtered u JOIN unit_entities ue "
            "USING(tenant_id,user_id,unit_id) "
            "WHERE u.kind='fact' AND ue.entity_key=e.entity_key AND u.unit_id<>s.unit_id "
            "ORDER BY u.embedding <=> %(vector)s::vector,u.unit_id LIMIT %(per_entity)s) c",
            parameters,
        )
        directions = []
        for source, target in (("source_id", "target_id"), ("target_id", "source_id")):
            directions.append(
                "(SELECT l.source_id,l.target_id,l.weight FROM unit_links l JOIN filtered u ON "
                f"(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.{target}) "
                f"WHERE (l.tenant_id,l.user_id,l.{source})=(s.tenant_id,s.user_id,s.unit_id) "
                f"AND l.kind='semantic' AND u.kind='fact' ORDER BY l.weight DESC,l.{target} "
                "LIMIT %(links)s)"
            )
        semantic = self._execute(
            prefix
            + "SELECT e.* FROM seeds s CROSS JOIN LATERAL ("
            + " UNION ALL ".join(directions)
            + ") e",
            parameters,
        )
        causal = self._execute(
            prefix.rstrip() + ", first_edges AS (SELECT s.unit_id AS seed_id,l.* FROM seeds s "
            "CROSS JOIN LATERAL (SELECT l.* FROM unit_links l JOIN filtered u ON "
            "(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.target_id) "
            "WHERE (l.tenant_id,l.user_id,l.source_id)=(s.tenant_id,s.user_id,s.unit_id) "
            "AND l.kind='caused_by' AND u.kind='fact' "
            "ORDER BY l.weight DESC,l.target_id LIMIT %(links)s) l) "
            "SELECT source_id,target_id,weight,ARRAY[source_id,target_id] AS nodes "
            "FROM first_edges UNION ALL SELECT e.source_id,e.target_id,e.weight,"
            "ARRAY[f.seed_id,e.source_id,e.target_id] "
            "FROM first_edges f CROSS JOIN LATERAL (SELECT l.* FROM unit_links l "
            "JOIN filtered u ON "
            "(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.target_id) "
            "WHERE (l.tenant_id,l.user_id,l.source_id)=(f.tenant_id,f.user_id,f.target_id) "
            "AND l.kind='caused_by' AND u.kind='fact' AND l.target_id<>f.seed_id "
            "ORDER BY l.weight DESC,l.target_id LIMIT %(links)s) e",
            parameters,
        )
        matches = defaultdict(set)
        paths = defaultdict(list)
        for row in entities:
            matches[row["unit_id"]].add(row["entity_key"])
            paths[row["unit_id"]].append(
                {
                    "arm": "graph",
                    "relation": "entity",
                    "nodes": [row["seed_id"], row["unit_id"]],
                    "entity": row["entity_key"],
                }
            )
        for row in semantic:
            source, target = row["source_id"], row["target_id"]
            if target in seed_ids:
                source, target = target, source
            paths[target].append(
                {"arm": "graph", "relation": "semantic", "nodes": [source, target]}
            )
        for row in causal:
            paths[row["target_id"]].append(
                {"arm": "graph", "relation": "caused_by", "nodes": row["nodes"]}
            )
        ranked = ordinary_graph_activation(
            seed_ids,
            entity_matches=matches,
            semantic_edges=tuple(
                GraphEdge(r["source_id"], r["target_id"], r["weight"]) for r in semantic
            ),
            causal_edges=tuple(
                GraphEdge(r["source_id"], r["target_id"], r["weight"]) for r in causal
            ),
        )[: config.relation_candidates]
        return tuple(item.node_id for item in ranked), {
            item.node_id: paths[item.node_id] for item in ranked
        }

    def hierarchy(self, seed_ids: Sequence[str], vector: Sequence[float]):
        rows = self._execute(
            self._eligible().rstrip()
            + """,
            seeds AS MATERIALIZED (
                SELECT s.tenant_id,s.user_id,s.unit_id,s.memory_id,s.generation_id
                FROM search_units s
                WHERE s.tenant_id=current_setting('oam.tenant_id',true)
                  AND s.user_id=current_setting('oam.user_id',true)
                  AND s.kind='fact' AND s.unit_id=ANY(%(seeds)s)
                  AND (SELECT true FROM filtered f WHERE (f.tenant_id,f.user_id,f.unit_id)=
                      (s.tenant_id,s.user_id,s.unit_id)) IS TRUE
            ),
            parent_edges AS MATERIALIZED (
                SELECT seed.unit_id AS seed_id,seed.generation_id,
                       sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision
                FROM seeds seed JOIN insight_support sp ON
                    (sp.tenant_id,sp.user_id,sp.memory_id)=
                    (seed.tenant_id,seed.user_id,seed.memory_id)
            ),
            parent_candidates AS MATERIALIZED (
                SELECT e.seed_id,p.tenant_id,p.user_id,p.unit_id,p.insight_id,
                       p.insight_revision,p.generation_id,
                       p.embedding <=> %(vector)s::vector AS distance
                FROM parent_edges e JOIN search_units p ON
                    (p.tenant_id,p.user_id,p.insight_id,p.insight_revision,p.generation_id)=
                    (e.tenant_id,e.user_id,e.insight_id,e.insight_revision,e.generation_id)
                WHERE p.kind='insight' AND
                    (SELECT true FROM filtered f WHERE (f.tenant_id,f.user_id,f.unit_id)=
                        (p.tenant_id,p.user_id,p.unit_id)) IS TRUE
            ),
            ranked_parents AS MATERIALIZED (
                SELECT pc.*,row_number() OVER (
                    PARTITION BY seed_id ORDER BY distance,unit_id) AS parent_rank
                FROM parent_candidates pc
            ),
            selected_parents AS MATERIALIZED (
                SELECT * FROM ranked_parents WHERE parent_rank<=%(parents)s
            ),
            child_edges AS MATERIALIZED (
                SELECT p.seed_id,p.unit_id AS parent_id,p.parent_rank,p.generation_id,
                       sp.tenant_id,sp.user_id,sp.memory_id
                FROM selected_parents p JOIN insight_support sp ON
                    (sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision)=
                    (p.tenant_id,p.user_id,p.insight_id,p.insight_revision)
            ),
            child_candidates AS MATERIALIZED (
                SELECT e.seed_id,e.parent_id,e.parent_rank,c.unit_id,
                       c.embedding <=> %(vector)s::vector AS distance
                FROM child_edges e JOIN search_units c ON
                    (c.tenant_id,c.user_id,c.memory_id,c.generation_id)=
                    (e.tenant_id,e.user_id,e.memory_id,e.generation_id)
                WHERE c.kind='fact' AND c.unit_id<>e.seed_id AND
                    (SELECT true FROM filtered f WHERE (f.tenant_id,f.user_id,f.unit_id)=
                        (c.tenant_id,c.user_id,c.unit_id)) IS TRUE
            ),
            ranked_children AS (
                SELECT c.*,row_number() OVER (
                    PARTITION BY seed_id,parent_id ORDER BY distance,unit_id) AS child_rank
                FROM child_candidates c
            )
            SELECT seed_id,parent_id,parent_rank,unit_id,1-distance AS score
            FROM ranked_children WHERE child_rank<=%(children)s
            ORDER BY parent_rank,score DESC,unit_id,seed_id""",
            {
                "seeds": list(seed_ids),
                "vector": json.dumps(tuple(vector)),
                "parents": self.config.search.hierarchy_parents_per_seed,
                "children": self.config.search.hierarchy_children_per_parent,
            },
        )
        paths = defaultdict(list)
        for row in rows:
            paths[row["unit_id"]].append(
                {"arm": "hierarchy", "nodes": [row["seed_id"], row["parent_id"], row["unit_id"]]}
            )
        return tuple(dict.fromkeys(row["unit_id"] for row in rows)), dict(paths)

    def _basis_date(self, alias: str) -> str:
        basis = self.request.time_basis
        if basis in {"event", "referenced"}:
            return (
                "(SELECT greatest(lower(ut.time_span),%(time_start)s::timestamptz) + "
                "(least(upper(ut.time_span),%(time_end)s::timestamptz) - "
                "greatest(lower(ut.time_span),%(time_start)s::timestamptz))/2 "
                "FROM unit_times ut WHERE "
                "(ut.tenant_id,ut.user_id,ut.unit_id)="
                f"({alias}.tenant_id,{alias}.user_id,{alias}.unit_id) "
                "AND ut.role=%(time_role)s AND ut.time_span && "
                "tstzrange(%(time_start)s,%(time_end)s,'[)') "
                "ORDER BY greatest(lower(ut.time_span),%(time_start)s::timestamptz),"
                "least(upper(ut.time_span),%(time_end)s::timestamptz),ut.annotation_id LIMIT 1)"
            )
        return (
            alias
            + "."
            + {
                "mentioned": "mentioned_at",
                "created": "source_created_at",
                "updated": "source_updated_at",
            }[basis]
        )

    def temporal_matches(self, unit_ids: Sequence[str]) -> tuple[str, ...]:
        """Qualify bounded direct candidates without inventing an Insight event time."""
        rows = self._execute(
            self._eligible(temporal=True)
            + "SELECT unit_id FROM filtered WHERE unit_id=ANY(%(ids)s) "
            "ORDER BY array_position(%(ids)s,unit_id)",
            {"ids": list(unit_ids)},
        )
        return tuple(row["unit_id"] for row in rows)

    def temporal_nodes(self, unit_ids: Sequence[str], vector: Sequence[float]):
        rows = self._execute(
            self._eligible(temporal=True) + "SELECT u.unit_id,LEAST(1.0,GREATEST(-1.0,"
            "1-(u.embedding <=> %(vector)s::vector))) AS cosine,"
            + self._basis_date("u")
            + " AS basis_date FROM filtered u WHERE kind='fact' AND unit_id=ANY(%(ids)s)",
            {"ids": list(unit_ids), "vector": json.dumps(tuple(vector))},
        )
        return tuple(TemporalNode(row["unit_id"], row["cosine"], row["basis_date"]) for row in rows)

    def _temporal_edges(self, source_ids, vector, *, limit=None, edge_ids=None):
        prefix = self._eligible(temporal=True)
        adjacent = []
        for source, target in (("left_id", "right_id"), ("right_id", "left_id")):
            adjacent.append(
                f"SELECT t.tenant_id,t.user_id,t.{source} AS source_id,t.{target} AS target_id,"
                "t.weight,'temporal' AS relation,t.left_id,t.right_id,"
                "t.left_annotation_id,t.right_annotation_id,"
                "jsonb_build_array('temporal',t.time_basis,t.left_id,t.right_id,"
                "t.left_annotation_id,t.right_annotation_id)::text AS edge_id "
                f"FROM temporal_links t WHERE t.{source}=ANY(%(sources)s) "
                "AND t.time_basis=%(basis)s"
            )
        adjacent.append(
            "SELECT l.tenant_id,l.user_id,l.source_id,l.target_id,l.weight,'caused_by',"
            "NULL::text,NULL::text,NULL::text,NULL::text,"
            "jsonb_build_array('caused_by',l.source_id,l.target_id)::text FROM unit_links l "
            "WHERE l.kind='caused_by' AND l.source_id=ANY(%(sources)s)"
        )
        witnesses = "true"
        if self.request.time_basis in {"event", "referenced"}:
            conditions = []
            for side in ("left", "right"):
                conditions.append(
                    "EXISTS(SELECT 1 FROM unit_times witness WHERE "
                    f"(witness.tenant_id,witness.user_id,witness.unit_id,witness.annotation_id)="
                    f"(edge.tenant_id,edge.user_id,edge.{side}_id,edge.{side}_annotation_id) "
                    "AND witness.role=%(time_role)s AND witness.time_span && "
                    "tstzrange(%(time_start)s,%(time_end)s,'[)'))"
                )
            witnesses = "(edge.relation='caused_by' OR (" + " AND ".join(conditions) + "))"
        query = (
            prefix + ", adjacent AS (" + " UNION ALL ".join(adjacent) + ") "
            "SELECT target.unit_id,LEAST(1.0,GREATEST(-1.0,"
            "1-(target.embedding <=> %(vector)s::vector))) AS cosine,"
            + self._basis_date("target")
            + " AS basis_date,edge.weight,edge.relation,edge.edge_id "
            "FROM adjacent edge JOIN filtered source ON "
            "(source.tenant_id,source.user_id,source.unit_id)="
            "(edge.tenant_id,edge.user_id,edge.source_id) "
            "JOIN filtered target ON "
            "(target.tenant_id,target.user_id,target.unit_id)="
            "(edge.tenant_id,edge.user_id,edge.target_id) "
            "WHERE source.kind='fact' AND target.kind='fact' "
            "AND 1-(target.embedding <=> %(vector)s::vector)>=%(floor)s AND " + witnesses
        )
        parameters = {
            "sources": list(source_ids),
            "vector": json.dumps(tuple(vector)),
            "basis": self.request.time_basis.value,
            "floor": self.config.search.temporal_semantic_floor,
        }
        if edge_ids is not None:
            query += " AND edge.edge_id=ANY(%(edges)s)"
            parameters["edges"] = list(edge_ids)
        query += " ORDER BY edge.weight DESC,target.unit_id,edge.edge_id"
        if limit is not None:
            query += " LIMIT %(limit)s"
            parameters["limit"] = limit
        return self._execute(query, parameters)

    def temporal_neighbors(self, source_id: str, vector: Sequence[float], limit: int):
        rows = self._temporal_edges((source_id,), vector, limit=limit)
        return tuple(
            TemporalEdge(
                TemporalNode(row["unit_id"], row["cosine"], row["basis_date"]),
                row["weight"],
                row["relation"],
                row["edge_id"],
            )
            for row in rows
        )

    def valid_paths(self, paths: dict[str, list[dict]], vector: Sequence[float]):
        node_ids = sorted(
            {node for group in paths.values() for path in group for node in path["nodes"]}
        )
        rows = self._execute(
            self._eligible() + "SELECT unit_id FROM filtered WHERE unit_id=ANY(%(ids)s)",
            {"ids": node_ids},
        )
        eligible = {row["unit_id"] for row in rows}
        temporal_ids = {
            node
            for group in paths.values()
            for path in group
            if path["arm"] == "temporal"
            for node in path["nodes"]
        }
        temporal_eligible = (
            set(self.temporal_matches(sorted(temporal_ids))) if temporal_ids else set()
        )
        edge_ids = {
            edge for group in paths.values() for path in group for edge in path.get("edge_ids", ())
        }
        valid_edges = set()
        if edge_ids:
            valid_edges = {
                row["edge_id"] for row in self._temporal_edges(node_ids, vector, edge_ids=edge_ids)
            }
        return {
            node_id: [
                path
                for path in group
                if set(path["nodes"]) <= eligible
                and (path["arm"] != "temporal" or set(path["nodes"]) <= temporal_eligible)
                and set(path.get("edge_ids", ())) <= valid_edges
            ]
            for node_id, group in paths.items()
        }
