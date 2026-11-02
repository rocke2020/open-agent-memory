"""User-qualified PostgreSQL reads for the shared recall pipeline."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from contextlib import nullcontext

from psycopg import Pipeline
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.contracts.search import RecallLevel, SearchRequest
from oam.db.context import SET_USER_CONTEXT_SQL
from oam.db.eligibility import (
    ELIGIBLE_PASSAGE_UNITS,
    ELIGIBLE_SUPPORT,
    ELIGIBLE_UNITS,
    ELIGIBLE_UNITS_CANDIDATE,
    ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS,
)
from oam.db.entity_eligibility import (
    ELIGIBLE_ENTITY_MEMBERSHIPS,
    ELIGIBLE_ENTITY_NAME_SUPPORT,
    ENTITY_PROJECTIONS_FROM_SEED_ENTITIES,
)
from oam.indexing.entity_names import normalize_entity_text
from oam.retrieval.algorithms import (
    GraphEdge,
    TemporalEdge,
    TemporalNode,
    ordinary_graph_activation,
    reciprocal_rank_fusion,
)
from oam.retrieval.fts import QueryTerms, query_terms


def _causal_ctes() -> str:
    """Existing eligible outgoing links, bounded separately at each of two hops."""
    return (
        "first_edges AS (SELECT s.unit_id AS seed_id,l.* FROM seeds s "
        "CROSS JOIN LATERAL (SELECT l.* FROM unit_links l JOIN filtered u ON "
        "(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.target_id) "
        "WHERE (l.tenant_id,l.user_id,l.source_id)=(s.tenant_id,s.user_id,s.unit_id) "
        "AND l.kind='caused_by' AND u.kind='fact' "
        "ORDER BY l.weight DESC,l.target_id LIMIT %(links)s) l), "
        "causal_rows AS (SELECT source_id,target_id,weight,"
        "ARRAY[source_id,target_id] AS nodes,weight AS path_score FROM first_edges "
        "UNION ALL SELECT e.source_id,e.target_id,e.weight,"
        "ARRAY[f.seed_id,e.source_id,e.target_id],f.weight*e.weight "
        "FROM first_edges f CROSS JOIN LATERAL (SELECT l.* FROM unit_links l "
        "JOIN filtered u ON "
        "(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.target_id) "
        "WHERE (l.tenant_id,l.user_id,l.source_id)=(f.tenant_id,f.user_id,f.target_id) "
        "AND l.kind='caused_by' AND u.kind='fact' AND l.target_id<>ALL(%(seeds)s::text[]) "
        "ORDER BY l.weight DESC,l.target_id LIMIT %(links)s) e)"
    )


def _fact_only_eligible() -> str:
    return (
        "WITH filtered AS NOT MATERIALIZED (SELECT * FROM ("
        + ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
        + ") eligible WHERE kind='fact') "
    )


def _graph_fact_eligibility(
    *, include_semantic: bool, include_entities: bool = True, include_causal: bool = True
) -> str:
    eligible = _fact_only_eligible()
    seed_gate = (
        ""
        if include_semantic
        else (
            " AND EXISTS ("
            + eligible
            + "SELECT 1 FROM filtered other WHERE other.unit_id<>ALL(%(seeds)s))"
        )
    )
    candidates = ["SELECT unit_id FROM seeds"]
    if include_entities:
        candidates.extend(
            [
                "SELECT ue.unit_id FROM unit_entities ue JOIN raw_seed_entities r "
                "USING(tenant_id,user_id,entity_id)",
                "SELECT u.unit_id FROM raw_seed_entities r JOIN entity_names n "
                "USING(tenant_id,user_id,entity_id) JOIN entity_name_support ns "
                "USING(tenant_id,user_id,name_id) JOIN search_units u "
                "USING(tenant_id,user_id,memory_id)",
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


def _causal_paths(rows):
    paths = defaultdict(list)
    for row in rows:
        paths[row["target_id"]].append(
            {
                "arm": "graph",
                "relation": "caused_by",
                "nodes": row["nodes"],
                "score": row["path_score"],
            }
        )
    return paths


_ANCHOR = re.compile(r'"([^"\n]+)"|\b([A-Za-z][A-Za-z0-9]*-\d+[A-Za-z0-9-]*|[\w]+_[\w-]+)\b')
EXACT_USER_ORDER_MIN_CANDIDATES = 64
LEXICAL_DOCUMENT_LENGTH_NORMALIZATION = 2
LEXICAL_WHOLE_QUERY_NORMALIZATION = 0
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
        read_committed: bool = False,
    ):
        if not authority.allows("memory:read"):
            raise PermissionError("memory:read is required")
        self.connection = connection
        self.authority = authority
        self.config = config
        self.deadline = deadline
        self.request = request
        self.time_parameters = {}
        # Scoped cardinality depends on current_setting; pooled generic plans can
        # retain another user's small-history estimates for large fact collections.
        with connection.pipeline() if read_committed and Pipeline.is_supported() else nullcontext():
            if read_committed:
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            connection.execute(
                SET_USER_CONTEXT_SQL + ", set_config('jit','off',true), "
                "set_config('plan_cache_mode','force_custom_plan',true)",
                (authority.tenant_id, authority.user_id),
            )
        self._has_insights: bool | None = None

    @property
    def has_insights(self) -> bool:
        if self._has_insights is None:
            self._has_insights = bool(
                self.connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM insights WHERE tenant_id=%s AND user_id=%s)",
                    (self.authority.tenant_id, self.authority.user_id),
                ).fetchone()[0]
            )
        return self._has_insights

    @property
    def candidate_units_sql(self) -> str:
        return (
            ELIGIBLE_UNITS_CANDIDATE if self.has_insights else ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
        )

    @property
    def lexical_candidate_predicate(self) -> str:
        return "true"

    def _eligible(self, *, temporal: bool = False, candidate_facts: bool = False) -> str:
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
            f"WITH eligible AS NOT MATERIALIZED "
            f"({self.candidate_units_sql if candidate_facts else ELIGIBLE_UNITS}), "
            f"filtered AS NOT MATERIALIZED (SELECT u.* FROM eligible u WHERE {predicate}) "
        )

    def _execute(self, sql: str, parameters=(), *, configure_ann: bool = False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recall deadline exhausted")
        cursor = self.connection.cursor(row_factory=dict_row)
        if isinstance(parameters, dict):
            parameters = {**self.time_parameters, **parameters}
        settings = "SELECT set_config('statement_timeout',%s,true)"
        setting_parameters = (str(max(1, int(remaining * 1000))),)
        if configure_ann:
            settings += (
                ", set_config('hnsw.iterative_scan',%s,true), "
                "set_config('hnsw.max_scan_tuples',%s,true)"
            )
            setting_parameters += (
                self.config.search.ann_iterative_scan,
                str(self.config.search.ann_scan_max_tuples),
            )
        # Preserve backend command order while sending setup and query together.
        # Exit drains results before fetch or the owning transaction's rollback.
        with self.connection.pipeline() if Pipeline.is_supported() else nullcontext():
            self.connection.execute(settings, setting_parameters)
            cursor.execute(sql, parameters)
        return cursor.fetchall()

    def user_generation(self) -> str | None:
        return self.user_state()["index_generation_id"]

    @property
    def causal_graph_enabled(self) -> bool:
        mode = self.request.recall_level.value if self.request is not None else "normal"
        return getattr(self.config.search, mode).causal_graph

    def record_trace(self, trace_id: str, policy_epoch: int, decisions: dict, result: dict) -> bool:
        rows = self._execute(
            "INSERT INTO traces(tenant_id,user_id,trace_id,read_seq,generation_id,"
            "policy_epoch,decisions,content,expires_at) "
            "SELECT tenant_id,user_id,%s,published_seq,"
            "COALESCE(index_generation_id,%s),policy_epoch,"
            "%s,%s,clock_timestamp()+(%s*interval '1 day') FROM users "
            "WHERE tenant_id=%s AND user_id=%s AND state='active' AND policy_epoch=%s "
            "FOR SHARE RETURNING trace_id",
            (
                trace_id,
                decisions.get("generation_id"),
                Jsonb(decisions),
                Jsonb(result),
                self.config.trace.metadata_retention_days,
                self.authority.tenant_id,
                self.authority.user_id,
                policy_epoch,
            ),
        )
        return bool(rows)

    def user_state(self) -> dict:
        rows = self._execute(
            "WITH active_user AS MATERIALIZED ("
            "SELECT tenant_id,user_id,index_generation_id,policy_epoch FROM users "
            "WHERE tenant_id=%s AND user_id=%s AND state='active'), "
            "current_sources AS MATERIALIZED ("
            "SELECT s.tenant_id,s.user_id,s.source_id,s.active_version "
            "FROM active_user sc JOIN sources s ON "
            "(s.tenant_id,s.user_id)=(sc.tenant_id,sc.user_id) "
            "WHERE s.status='active' AND s.input_role IN ('interaction','evidence_resource')), "
            "current_operations AS MATERIALIZED ("
            "SELECT DISTINCT o.tenant_id,o.user_id,o.operation_id,o.source_id,"
            "o.source_version,o.status,o.stage_state,o.created_at "
            "FROM active_user sc JOIN work_units w ON "
            "(w.tenant_id,w.user_id)=(sc.tenant_id,sc.user_id) "
            "JOIN operations o ON (o.tenant_id,o.user_id,o.operation_id)="
            "(w.tenant_id,w.user_id,w.operation_id) WHERE o.kind<>'consolidate'), "
            "latest_operations AS MATERIALIZED ("
            "SELECT DISTINCT ON (o.tenant_id,o.user_id,o.source_id,o.source_version) "
            "o.tenant_id,o.user_id,o.source_id,o.source_version,o.status,o.stage_state "
            "FROM current_sources s JOIN current_operations o ON "
            "(o.tenant_id,o.user_id,o.source_id,o.source_version)="
            "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
            "ORDER BY o.tenant_id,o.user_id,o.source_id,o.source_version,"
            "o.created_at DESC,o.operation_id DESC) "
            "SELECT sc.index_generation_id,sc.policy_epoch,"
            "COALESCE(bool_or(base.stage_state->>'parse_coverage'='partial'),false) "
            "AS parse_partial,CASE "
            "WHEN bool_or(base.status IN ('partial_failed','failed_terminal','cancelled') "
            "OR base.stage_state->>'extraction_coverage'='partial' "
            "OR base.stage_state ? 'error') "
            "THEN 'partial' WHEN bool_or(s.source_id IS NOT NULL AND "
            "(base.status IS NULL OR base.status IN ('accepted','running'))) "
            "THEN 'pending' ELSE 'ready' END AS readiness FROM active_user sc "
            "LEFT JOIN current_sources s ON (s.tenant_id,s.user_id)=(sc.tenant_id,sc.user_id) "
            "LEFT JOIN latest_operations base ON "
            "(base.tenant_id,base.user_id,base.source_id,base.source_version)="
            "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
            "GROUP BY sc.index_generation_id,sc.policy_epoch",
            (self.authority.tenant_id, self.authority.user_id),
        )
        if not rows:
            raise PermissionError("active user is unavailable")
        return rows[0]

    def _query_entity_names(self, query: str) -> list[dict]:
        terms = query_terms(query, max_terms=None)
        normalized = normalize_entity_text(query)
        if not normalized:
            return []
        parameters = {
            "entity_query": normalized,
            "query_name_keys": list(
                dict.fromkeys(
                    [normalized, *normalized.split(), *re.findall(r"\w+(?:[.-]\w+)*", normalized)]
                )
            ),
        }
        candidate = _tsquery((*terms.candidates, *terms.modifiers), "||", parameters, "name_term")
        return self._execute(
            "WITH matched_query_names AS MATERIALIZED ("
            "SELECT n.* FROM entity_names n WHERE "
            "n.tenant_id=current_setting('oam.tenant_id',true) "
            "AND n.user_id=current_setting('oam.user_id',true) "
            f"AND (n.lexical @@ {candidate} OR n.name_key=ANY(%(query_name_keys)s) "
            "OR (n.lexical=''::tsvector "
            "AND split_part(n.name_key,' ',1)=ANY(%(query_name_keys)s))) "
            "AND %(entity_query)s ~ ("
            r"CASE WHEN left(n.name_key,1) ~ '[[:alnum:]_]' "
            r"AND left(n.name_key,1) !~ '[㐀-鿿]' THEN '\m' ELSE '' END || "
            r"regexp_replace(n.name_key,'([\\.^$|?*+(){}\[\]])','\\\1','g') || "
            r"CASE WHEN right(n.name_key,1) ~ '[[:alnum:]_]' "
            r"AND right(n.name_key,1) !~ '[㐀-鿿]' THEN '\M' ELSE '' END)) "
            "SELECT DISTINCT n.entity_id,n.name_text FROM matched_query_names n "
            "WHERE EXISTS (SELECT 1 FROM ("
            + ELIGIBLE_ENTITY_NAME_SUPPORT
            + ") binding WHERE (binding.tenant_id,binding.user_id,binding.entity_id,"
            "binding.name_id)=(n.tenant_id,n.user_id,n.entity_id,n.name_id)) "
            "ORDER BY n.entity_id,n.name_text",
            parameters,
        )

    def lexical(self, query: str, limit: int) -> list[dict]:
        # Protected literals are ranked by anchors, including their query remainder.
        if _ANCHOR.search(query):
            return []
        entity_names = (
            self._query_entity_names(query) if self.config.search.query_entity_boost else []
        )
        terms = query_terms(query, prioritized_names=[row["name_text"] for row in entity_names])
        candidates = _informative_terms(terms.candidates)
        parameters = {
            "limit": limit,
            "weights": list(self.config.search.fts_rank_weights),
            "normalization": LEXICAL_DOCUMENT_LENGTH_NORMALIZATION,
            "entity_boost": self.config.search.query_entity_boost,
        }
        fallback = not candidates
        if fallback:
            if not terms.candidates and not entity_names:
                return []
            if not entity_names:
                return self._execute(
                    self._eligible(candidate_facts=True)
                    + "SELECT u.unit_id,ts_rank_cd(u.lexical,plainto_tsquery('simple',%(query)s)) "
                    "AS score,true AS strict_match FROM filtered u WHERE "
                    "u.lexical @@ plainto_tsquery('simple',%(query)s) "
                    "ORDER BY score DESC,u.mentioned_at DESC NULLS LAST,u.unit_id "
                    "LIMIT %(limit)s",
                    {"query": query, "limit": limit},
                )
            candidates = (("simple", query),)
            terms = QueryTerms(candidates, candidates, ())
        request = self.request or SearchRequest(query=query)
        whole_query = fallback or (
            getattr(self.config.search, request.recall_level.value).lexical_ranking == "whole_query"
        )
        if whole_query:
            parameters["normalization"] = LEXICAL_WHOLE_QUERY_NORMALIZATION
        candidate, primary, full, term_queries = _rank_queries(terms, parameters)
        if fallback:
            candidate = primary = full = _tsquery(candidates, "&&", parameters, "fallback")
        lexical_vector = "u.lexical" if fallback else "u.lexical_bilingual"
        entity_prefix = ""
        entity_match = "false"
        hits_source = "search_units u"
        hits_predicate = f"{lexical_vector} @@ q.candidate"
        if entity_names:
            parameters["query_entities"] = sorted({row["entity_id"] for row in entity_names})
            memberships = ELIGIBLE_ENTITY_MEMBERSHIPS.replace(
                "FROM unit_entities membership", "FROM query_entity_memberships membership"
            )
            entity_prefix = (
                ", query_entity_memberships AS MATERIALIZED ("
                "SELECT * FROM unit_entities WHERE "
                "tenant_id=current_setting('oam.tenant_id',true) "
                "AND user_id=current_setting('oam.user_id',true) "
                "AND entity_id=ANY(%(query_entities)s)), "
                "query_entity_units AS MATERIALIZED (SELECT DISTINCT unit_id FROM ("
                + memberships
                + ") eligible_memberships)"
            )
            entity_match = "u.unit_id IN (SELECT unit_id FROM query_entity_units)"
            entity_prefix += (
                ", lexical_candidate_ids AS MATERIALIZED ("
                "SELECT u.unit_id FROM search_units u,queries q WHERE "
                "u.tenant_id=current_setting('oam.tenant_id',true) "
                "AND u.user_id=current_setting('oam.user_id',true) "
                f"AND {self.lexical_candidate_predicate} "
                f"AND {lexical_vector} @@ q.candidate "
                "UNION SELECT unit_id FROM query_entity_units)"
            )
            hits_source = "lexical_candidate_ids ids JOIN search_units u ON u.unit_id=ids.unit_id"
            hits_predicate = "true"
        score = "u.partial_score*(1+%(entity_boost)s*u.entity_match::int)"
        # Order matches before checking support so a common term does not
        # validate the entire history. Eligibility still precedes every cap.
        eligible_ordered = self.candidate_units_sql.replace("FROM search_units u", "FROM ordered u")
        hits_sql = (
            "WITH queries AS (SELECT "
            f"{candidate} AS candidate,{primary} AS primary,{full} AS full)" + entity_prefix + ", "
            "hits AS MATERIALIZED (SELECT u.tenant_id,u.user_id,u.unit_id,u.kind,u.origin,"
            "u.source_id,u.source_version,u.evidence_id,u.passage_id,u.memory_id,"
            "u.insight_id,u.insight_revision,u.generation_id,"
            f"{lexical_vector} AS lexical_bilingual,u.mentioned_at,"
            f"ts_rank_cd(%(weights)s::float4[],{lexical_vector},q.candidate,"
            "%(normalization)s) AS partial_score,"
            f"{lexical_vector} @@ q.full AS full_match,"
            f"{lexical_vector} @@ q.primary AS primary_match,"
            f"({entity_match}) AS entity_match "
            f"FROM {hits_source},queries q WHERE "
            "u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            f"AND {self.lexical_candidate_predicate} AND {hits_predicate})"
        )
        if whole_query:
            return self._execute(
                hits_sql + f", ordered AS (SELECT u.*,{score} AS score FROM hits u "
                "ORDER BY u.full_match DESC,u.primary_match DESC,score DESC,"
                "u.mentioned_at DESC NULLS LAST,u.unit_id OFFSET 0) "
                "SELECT u.unit_id,u.score,u.entity_match,"
                "(u.full_match OR u.primary_match) AS strict_match FROM ("
                + eligible_ordered
                + ") u ORDER BY u.full_match DESC,u.primary_match DESC,u.score DESC,"
                "u.mentioned_at DESC NULLS LAST,u.unit_id LIMIT %(limit)s",
                parameters,
            )
        entity_tail = ",NULL::tsquery" if entity_names else ""
        rows = self._execute(
            hits_sql + " "
            "SELECT t.term_order,hit.unit_id,hit.mentioned_at,hit.full_match,hit.primary_match,"
            "hit.entity_match "
            f"FROM unnest(ARRAY[{term_queries}{entity_tail}]) "
            "WITH ORDINALITY AS t(term,term_order) "
            "CROSS JOIN LATERAL (WITH ordered AS (SELECT u.*,"
            f"{score} AS score "
            "FROM hits u WHERE u.lexical_bilingual @@ t.term "
            "OR (t.term IS NULL AND u.entity_match AND u.partial_score=0) "
            "ORDER BY u.full_match DESC,u.primary_match DESC,score DESC,"
            "u.mentioned_at DESC NULLS LAST,u.unit_id OFFSET 0) "
            "SELECT u.unit_id,u.mentioned_at,u.full_match,u.primary_match,u.score,"
            "u.entity_match FROM ("
            + eligible_ordered
            + ") u ORDER BY u.full_match DESC,u.primary_match DESC,u.score DESC,"
            "u.mentioned_at DESC NULLS LAST,u.unit_id LIMIT %(limit)s) hit "
            "ORDER BY t.term_order,hit.full_match DESC,hit.primary_match DESC,hit.score DESC,"
            "hit.mentioned_at DESC NULLS LAST,hit.unit_id",
            parameters,
        )
        arms = [[] for _ in candidates]
        evidence = {}
        tail = []
        for row in rows:
            if row["term_order"] > len(arms):
                tail.append(
                    {
                        "unit_id": row["unit_id"],
                        "score": 0.0,
                        "strict_match": False,
                        "entity_match": True,
                    }
                )
                continue
            arms[row["term_order"] - 1].append(row["unit_id"])
            evidence[row["unit_id"]] = row
        ranked = reciprocal_rank_fusion(arms, rrf_k=self.config.search.rrf_k)
        boosted_scores = {
            item.node_id: item.score
            * (1 + self.config.search.query_entity_boost * evidence[item.node_id]["entity_match"])
            for item in ranked
        }
        ranked = sorted(
            ranked,
            key=lambda item: (
                -evidence[item.node_id]["full_match"],
                -evidence[item.node_id]["primary_match"],
                -boosted_scores[item.node_id],
                -(
                    evidence[item.node_id]["mentioned_at"].timestamp()
                    if evidence[item.node_id]["mentioned_at"]
                    else float("-inf")
                ),
                item.node_id,
            ),
        )
        selected = [
            {
                "unit_id": item.node_id,
                "score": boosted_scores[item.node_id],
                "entity_match": evidence[item.node_id]["entity_match"],
                "strict_match": bool(
                    evidence[item.node_id]["full_match"] or evidence[item.node_id]["primary_match"]
                ),
            }
            for item in ranked[:limit]
        ]
        return [*selected, *tail[: max(0, limit - len(selected))]]

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
        scope = (
            "u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
        )
        exact_user_order = self.request is not None and (
            self.request.recall_level != RecallLevel.FAST
            or limit >= EXACT_USER_ORDER_MIN_CANDIDATES
        )
        if exact_user_order:
            # For larger pools, rank this user's vectors before the support
            # check. The outer limit still admits eligible units only.
            source = (
                "(SELECT u.*,u.embedding <=> %(vector)s::vector AS distance "
                "FROM search_units u WHERE " + scope + "ORDER BY distance OFFSET 0) u "
            )
            distance = "u.distance"
            predicate = ""
        else:
            source = "search_units u "
            distance = "u.embedding <=> %(vector)s::vector"
            predicate = scope + "AND "
        return self._execute(
            self._eligible(candidate_facts=True)
            + "SELECT u.unit_id,u.kind,u.source_id,u.source_version,u.passage_id,"
            + f"1-({distance}) AS score FROM {source} WHERE "
            + predicate
            + "(SELECT true FROM filtered f WHERE "
            "(f.tenant_id,f.user_id,f.unit_id)=(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
            f"ORDER BY {distance}{',u.unit_id' if exact_user_order else ''} LIMIT %(limit)s",
            {"vector": json.dumps(tuple(vector)), "limit": limit},
            configure_ann=True,
        )

    def related_user_fact(self, unit_ids: Sequence[str]) -> dict | None:
        if not unit_ids:
            return None
        scope = (
            "u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
        )
        user_fact = (
            "u.kind='fact' AND EXISTS (SELECT 1 FROM support sp WHERE "
            "(sp.tenant_id,sp.user_id,sp.memory_id)=(u.tenant_id,u.user_id,u.memory_id) "
            "AND sp.span->>'role'='user') "
        )
        eligible = (
            "(SELECT true FROM filtered f WHERE "
            "(f.tenant_id,f.user_id,f.unit_id)=(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
        )
        rows = self._execute(
            self._eligible(candidate_facts=True)
            + ", seed AS MATERIALIZED (SELECT u.unit_id,u.embedding FROM ("
            "SELECT u.tenant_id,u.user_id,u.unit_id,u.embedding,"
            "array_position(%(ids)s,u.unit_id) AS position FROM search_units u WHERE "
            + scope
            + "AND "
            + user_fact
            + "AND u.unit_id=ANY(%(ids)s) ORDER BY position OFFSET 0) u WHERE "
            + eligible
            + "ORDER BY u.position LIMIT 1), "
            "ranked AS MATERIALIZED (SELECT u.tenant_id,u.user_id,u.unit_id,"
            "u.source_id,u.source_version,u.distance FROM seed s CROSS JOIN LATERAL ("
            "SELECT u.* FROM (SELECT u.tenant_id,u.user_id,u.unit_id,u.memory_id,"
            "u.source_id,u.source_version,u.embedding <=> s.embedding AS distance "
            "FROM search_units u WHERE "
            + scope
            + "AND "
            + user_fact
            + "AND u.unit_id<>s.unit_id AND u.source_id IS NOT NULL "
            "ORDER BY distance,u.unit_id OFFSET 0) u WHERE "
            + eligible
            + "ORDER BY u.distance,u.unit_id LIMIT 1) u) "
            "SELECT s.unit_id AS seed_id,u.unit_id,u.source_id,u.source_version "
            "FROM seed s JOIN ranked u ON true",
            {"ids": list(unit_ids)},
        )
        return rows[0] if rows else None

    def hydrate(self, unit_ids: Sequence[str]) -> list[dict]:
        rows = self._execute(
            self._eligible(candidate_facts=True).rstrip() + ", "
            f"valid_support AS MATERIALIZED (SELECT sp.* FROM ({ELIGIBLE_SUPPORT}) sp "
            "WHERE sp.memory_id IN ("
            "SELECT u.memory_id FROM search_units u WHERE "
            "u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            "AND u.unit_id=ANY(%(ids)s) AND u.memory_id IS NOT NULL "
            "UNION SELECT isp.memory_id FROM insight_support isp JOIN search_units u ON "
            "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision)="
            "(isp.tenant_id,isp.user_id,isp.insight_id,isp.insight_revision) "
            "WHERE u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            "AND u.unit_id=ANY(%(ids)s) AND u.kind='insight')) "
            "SELECT u.unit_id,u.kind,u.memory_id,u.insight_id,u.insight_revision,"
            "u.origin,u.content_text AS text,u.locator, "
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

    def insight_support_units(
        self, unit_ids: Sequence[str], vector: Sequence[float] | None, limit: int
    ) -> list[dict]:
        """Rank bounded supports of current eligible parents, independent of direct hits."""
        if not unit_ids:
            return []
        return self._execute(
            self._eligible(candidate_facts=True).rstrip()
            + " SELECT parent.unit_id AS insight_unit_id,child.* FROM filtered parent "
            "CROSS JOIN LATERAL (SELECT fact.unit_id,fact.memory_id,sp.support_revision,"
            "CASE WHEN %(vector)s::vector IS NOT NULL THEN "
            "fact.embedding <=> %(vector)s::vector END AS distance "
            "FROM insight_support sp JOIN filtered fact ON fact.kind='fact' AND "
            "(fact.tenant_id,fact.user_id,fact.memory_id,fact.generation_id)="
            "(sp.tenant_id,sp.user_id,sp.memory_id,parent.generation_id) "
            "WHERE (sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision)="
            "(parent.tenant_id,parent.user_id,parent.insight_id,parent.insight_revision) "
            "AND sp.support_revision=1 ORDER BY distance NULLS LAST,fact.unit_id "
            "LIMIT %(limit)s) child WHERE parent.kind='insight' "
            "AND parent.unit_id=ANY(%(ids)s) "
            "ORDER BY array_position(%(ids)s,parent.unit_id),"
            "child.distance NULLS LAST,child.unit_id",
            {
                "ids": list(unit_ids),
                "vector": json.dumps(tuple(vector)) if vector else None,
                "limit": limit,
            },
        )

    def source_passage_locators(self, source_ids: Sequence[str]) -> list[dict]:
        if not source_ids:
            return []
        return self._execute(
            "WITH filtered AS NOT MATERIALIZED ("
            + ELIGIBLE_PASSAGE_UNITS
            + ") SELECT u.unit_id,u.passage_id,u.source_id,u.source_version,"
            "u.generation_id,u.locator "
            "FROM filtered u WHERE u.kind='passage' "
            "AND u.source_id=ANY(%(source_ids)s) "
            "ORDER BY u.source_id,u.source_version,u.ordinal,u.unit_id",
            {"source_ids": list(source_ids)},
        )

    def source_user_passage_ids(
        self,
        source_keys: Sequence[tuple[str, int]],
        vector: Sequence[float],
        limit: int,
        excluded_ids: Sequence[str],
    ) -> list[dict]:
        """Rank current user originals inside query-selected interaction sources."""
        if not source_keys:
            return []
        return self._execute(
            "WITH eligible AS NOT MATERIALIZED ("
            + ELIGIBLE_PASSAGE_UNITS
            + ") SELECT u.unit_id FROM eligible u WHERE u.kind='passage' "
            "AND u.origin='memory' AND u.locator->>'role'='user' "
            "AND u.embedding IS NOT NULL AND (u.source_id,u.source_version) IN ("
            "SELECT * FROM unnest(%(source_ids)s::text[],%(source_versions)s::integer[])) "
            "AND NOT (u.unit_id=ANY(%(excluded_ids)s::text[])) "
            "ORDER BY u.embedding <=> %(vector)s::vector,u.unit_id LIMIT %(limit)s",
            {
                "source_ids": [key[0] for key in source_keys],
                "source_versions": [key[1] for key in source_keys],
                "vector": json.dumps(tuple(vector)),
                "limit": limit,
                "excluded_ids": list(excluded_ids),
            },
        )

    def supported_fact_ids(
        self,
        passage_ids: Sequence[str],
        vector: Sequence[float],
        limit: int,
        excluded_ids: Sequence[str],
    ) -> list[dict]:
        if not passage_ids:
            return []
        return self._execute(
            self._eligible(candidate_facts=True) + ", selected AS MATERIALIZED ("
            "SELECT u.tenant_id,u.user_id,u.unit_id,u.evidence_id,u.passage_id,u.generation_id "
            "FROM filtered u WHERE u.kind='passage' AND u.unit_id=ANY(%(ids)s)), "
            "ranked AS (SELECT selected.unit_id AS passage_id, fact.unit_id, "
            "row_number() OVER (PARTITION BY selected.unit_id "
            "ORDER BY fact.embedding <=> %(vector)s::vector, fact.unit_id) AS rank "
            "FROM selected JOIN support sp ON "
            "(sp.tenant_id,sp.user_id,sp.evidence_id,sp.passage_id)="
            "(selected.tenant_id,selected.user_id,selected.evidence_id,selected.passage_id) "
            "JOIN filtered fact ON "
            "(fact.tenant_id,fact.user_id,fact.memory_id,fact.generation_id)="
            "(sp.tenant_id,sp.user_id,sp.memory_id,selected.generation_id) "
            "WHERE fact.kind='fact' AND sp.span->>'role'='user' "
            "AND fact.unit_id<>ALL(%(excluded)s::text[])) "
            "SELECT passage_id,unit_id FROM ranked WHERE rank=1 "
            "ORDER BY array_position(%(ids)s,passage_id) LIMIT %(limit)s",
            {
                "ids": list(passage_ids),
                "vector": json.dumps(tuple(vector)),
                "limit": limit,
                "excluded": list(excluded_ids),
            },
        )

    def fast_graph(
        self,
        seed_ids: Sequence[str],
        vector: Sequence[float],
        *,
        include_entities: bool = True,
        include_causal: bool = False,
    ):
        """Bounded independent identity/causal expansion without semantic discovery."""
        if not seed_ids:
            return (), {}
        # A negative raw-superset check proves the existing nonseed eligibility
        # gate cannot admit any seed. A positive check keeps the full graph proof.
        if not self._execute(
            "SELECT unit_id FROM search_units WHERE tenant_id=%(tenant)s "
            "AND user_id=%(user)s AND kind='fact' "
            "AND unit_id<>ALL(%(seeds)s::text[]) LIMIT 1",
            {
                "tenant": self.authority.tenant_id,
                "user": self.authority.user_id,
                "seeds": list(seed_ids),
            },
        ):
            return (), {}
        return self.ordinary(
            seed_ids,
            vector,
            include_semantic=False,
            include_entities=include_entities,
            include_causal=include_causal,
        )

    def ordinary(
        self,
        seed_ids: Sequence[str],
        vector: Sequence[float],
        *,
        include_semantic: bool = True,
        include_entities: bool = True,
        include_causal: bool = True,
    ):
        if not (include_entities or include_semantic or include_causal):
            return (), {}
        config = self.config.search
        parameters = {
            "seeds": list(seed_ids),
            "vector": json.dumps(tuple(vector)),
            "entities": config.entities_per_seed,
            "per_entity": config.entity_candidates_per_entity,
            "links": config.links_per_seed,
        }
        prefix = _graph_fact_eligibility(
            include_semantic=include_semantic,
            include_entities=include_entities,
            include_causal=include_causal,
        ) + (ENTITY_PROJECTIONS_FROM_SEED_ENTITIES if include_entities else "")
        directions = []
        for source, target in (("source_id", "target_id"), ("target_id", "source_id")):
            directions.append(
                "(SELECT l.source_id,l.target_id,l.weight FROM unit_links l JOIN filtered u ON "
                f"(u.tenant_id,u.user_id,u.unit_id)=(l.tenant_id,l.user_id,l.{target}) "
                f"WHERE (l.tenant_id,l.user_id,l.{source})=(s.tenant_id,s.user_id,s.unit_id) "
                "AND l.kind='semantic' AND u.kind='fact' "
                f"AND u.unit_id<>ALL(%(seeds)s::text[]) ORDER BY l.weight DESC,l.{target} "
                "LIMIT %(links)s)"
            )
        ctes = [_causal_ctes()] if include_causal else []
        if include_entities:
            ctes.append(
                "seed_entities AS MATERIALIZED (SELECT s.unit_id AS seed_id,e.entity_id,e.name_id "
                "FROM seeds s CROSS JOIN LATERAL (SELECT entity_id,name_id "
                "FROM memberships ue WHERE "
                "(ue.tenant_id,ue.user_id,ue.unit_id)=(s.tenant_id,s.user_id,s.unit_id) "
                "ORDER BY entity_id LIMIT %(entities)s) e), "
                "entity_candidates AS MATERIALIZED ("
                "SELECT e.entity_id,c.unit_id,c.name_id,c.distance "
                "FROM (SELECT DISTINCT entity_id FROM seed_entities) e "
                "CROSS JOIN LATERAL (SELECT u.unit_id,ue.name_id,"
                "u.embedding <=> %(vector)s::vector "
                "AS distance FROM filtered u JOIN memberships ue "
                "USING(tenant_id,user_id,unit_id) WHERE u.kind='fact' "
                "AND ue.entity_id=e.entity_id AND u.unit_id<>ALL(%(seeds)s::text[]) "
                "ORDER BY u.embedding <=> %(vector)s::vector,u.unit_id "
                "LIMIT %(per_entity)s) c), "
                "entity_rows AS (SELECT s.seed_id,s.entity_id,s.name_id AS seed_name_id,"
                "c.name_id AS target_name_id,c.unit_id FROM seed_entities s "
                "CROSS JOIN LATERAL (SELECT unit_id,name_id FROM entity_candidates c "
                "WHERE c.entity_id=s.entity_id AND c.unit_id<>s.seed_id "
                "ORDER BY c.distance,c.unit_id LIMIT %(per_entity)s) c)"
            )
        if include_semantic:
            ctes.append(
                "semantic_rows AS (SELECT e.* FROM seeds s CROSS JOIN LATERAL ("
                + " UNION ALL ".join(directions)
                + ") e)"
            )
        branches = []
        if include_entities:
            branches.append(
                "SELECT 'entity' AS source,seed_id,entity_id AS entity_key,unit_id,"
                "NULL::text AS source_id,NULL::text AS target_id,NULL::double precision AS weight,"
                "NULL::text[] AS nodes,NULL::double precision AS path_score,"
                "jsonb_build_object('name_ids',"
                "ARRAY[seed_name_id,target_name_id],'alias_witnesses',COALESCE(w.witnesses,'[]')) "
                "AS entity_evidence FROM entity_rows e LEFT JOIN LATERAL ("
                "SELECT jsonb_agg(jsonb_build_object('support_id',support_id,'name_id',name_id,"
                "'entity_id',entity_id,'quote',quote,'citation',jsonb_build_object("
                "'source_id',source_id,'source_version',source_version,"
                "'span',locator||jsonb_build_object('quote',quote))) "
                "ORDER BY name_id) AS witnesses "
                "FROM (SELECT DISTINCT ON(name_id) * FROM name_support ns "
                "WHERE ns.entity_id=e.entity_id AND ns.name_id=ANY("
                "ARRAY[e.seed_name_id,e.target_name_id]) AND ns.support_kind='alias' "
                "ORDER BY name_id,support_id) ns) w ON true"
            )
        if include_semantic:
            branches.append(
                "SELECT 'semantic' AS source,NULL::text AS seed_id,NULL::text AS entity_key,"
                "NULL::text AS unit_id,"
                "source_id,target_id,weight,NULL::text[] AS nodes,weight AS path_score,"
                "NULL::jsonb AS entity_evidence FROM semantic_rows"
            )
        if include_causal:
            branches.append(
                "SELECT 'causal' AS source,NULL::text AS seed_id,NULL::text AS entity_key,"
                "NULL::text AS unit_id,source_id,target_id,weight,nodes,path_score,"
                "NULL::jsonb AS entity_evidence FROM causal_rows"
            )
        graph = self._execute(
            prefix.rstrip()
            + (", " + ", ".join(ctes) if ctes else "")
            + " "
            + " UNION ALL ".join(branches)
            + " ORDER BY source,source_id,target_id,seed_id,unit_id",
            parameters,
        )
        entities = [row for row in graph if row["source"] == "entity"]
        semantic = [row for row in graph if row["source"] == "semantic"]
        causal = [row for row in graph if row["source"] == "causal"]
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
                    **row["entity_evidence"],
                }
            )
        for row in semantic:
            source, target = row["source_id"], row["target_id"]
            if target in seed_ids:
                source, target = target, source
            paths[target].append(
                {
                    "arm": "graph",
                    "relation": "semantic",
                    "nodes": [source, target],
                    "score": row["path_score"],
                }
            )
        for node, group in _causal_paths(causal).items():
            paths[node].extend(group)
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

    def temporal_candidates(self, vector: Sequence[float], limit: int):
        """Rank eligible facts inside the requested window before the pool cap."""
        if self.request is None or self.request.time_window is None:
            return ()
        prefix = self._eligible(temporal=True, candidate_facts=True)
        if self.request.time_basis in {"event", "referenced"}:
            time_predicate = (
                "EXISTS(SELECT 1 FROM unit_times ut WHERE "
                "(ut.tenant_id,ut.user_id,ut.unit_id)=(u.tenant_id,u.user_id,u.unit_id) "
                "AND ut.role=%(time_role)s AND ut.time_span && "
                "tstzrange(%(time_start)s,%(time_end)s,'[)'))"
            )
        else:
            time_predicate = (
                self._basis_date("u") + " <@ tstzrange(%(time_start)s,%(time_end)s,'[)')"
            )
        rows = self._execute(
            prefix
            + "SELECT u.unit_id,LEAST(1.0,GREATEST(-1.0,1-u.distance)) AS cosine,"
            + self._basis_date("u")
            + " AS basis_date FROM (SELECT u.*,u.embedding <=> %(vector)s::vector AS distance "
            "FROM search_units u WHERE u.tenant_id=current_setting('oam.tenant_id',true) "
            "AND u.user_id=current_setting('oam.user_id',true) "
            "AND u.kind='fact' AND u.embedding IS NOT NULL AND "
            + time_predicate
            + " ORDER BY (u.embedding <=> %(vector)s::vector)+0,u.unit_id OFFSET 0) u "
            "WHERE 1-u.distance>=%(floor)s AND (SELECT true FROM filtered f WHERE "
            "(f.tenant_id,f.user_id,f.unit_id)=(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
            "ORDER BY u.distance,u.unit_id LIMIT %(limit)s",
            {
                "vector": json.dumps(tuple(vector)),
                "floor": self.config.search.temporal_semantic_floor,
                "limit": limit,
            },
        )
        return tuple(TemporalNode(row["unit_id"], row["cosine"], row["basis_date"]) for row in rows)

    def _temporal_edges(self, source_ids, vector, *, limit=None, edge_ids=None, per_source=False):
        prefix = self._eligible(temporal=True)
        source_match = "=source_seed.unit_id" if per_source else "=ANY(%(sources)s)"
        adjacent = []
        for source, target in (("left_id", "right_id"), ("right_id", "left_id")):
            adjacent.append(
                f"SELECT t.tenant_id,t.user_id,t.{source} AS source_id,t.{target} AS target_id,"
                "t.weight,'temporal' AS relation,t.left_id,t.right_id,"
                "t.left_annotation_id,t.right_annotation_id,"
                "jsonb_build_array('temporal',t.time_basis,t.left_id,t.right_id,"
                "t.left_annotation_id,t.right_annotation_id)::text AS edge_id "
                f"FROM temporal_links t WHERE t.{source}{source_match} "
                "AND t.time_basis=%(basis)s"
            )
        if self.causal_graph_enabled:
            adjacent.append(
                "SELECT l.tenant_id,l.user_id,l.source_id,l.target_id,l.weight,'caused_by',"
                "NULL::text,NULL::text,NULL::text,NULL::text,"
                "jsonb_build_array('caused_by',l.source_id,l.target_id)::text FROM unit_links l "
                f"WHERE l.kind='caused_by' AND l.source_id{source_match}"
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
            witnesses = "(" + " AND ".join(conditions) + ")"
            if self.causal_graph_enabled:
                witnesses = "(edge.relation='caused_by' OR " + witnesses + ")"

        def eligible_edges(edges):
            return (
                ("SELECT edge.source_id," if per_source else "SELECT ")
                + "target.unit_id,LEAST(1.0,GREATEST(-1.0,"
                "1-(target.embedding <=> %(vector)s::vector))) AS cosine,"
                + self._basis_date("target")
                + " AS basis_date,edge.weight,edge.relation,edge.edge_id "
                f"FROM {edges} JOIN filtered source ON "
                "(source.tenant_id,source.user_id,source.unit_id)="
                "(edge.tenant_id,edge.user_id,edge.source_id) "
                "JOIN filtered target ON "
                "(target.tenant_id,target.user_id,target.unit_id)="
                "(edge.tenant_id,edge.user_id,edge.target_id) "
                "WHERE source.kind='fact' AND target.kind='fact' "
                "AND 1-(target.embedding <=> %(vector)s::vector)>=%(floor)s AND "
                + witnesses
                + (" AND edge.edge_id=ANY(%(edges)s)" if edge_ids is not None else "")
            )

        parameters = {
            "sources": list(source_ids),
            "vector": json.dumps(tuple(vector)),
            "basis": self.request.time_basis.value,
            "floor": self.config.search.temporal_semantic_floor,
        }
        if edge_ids is not None:
            parameters["edges"] = list(edge_ids)
        if per_source:
            # A union's top-k is contained in its eligible branches' top-k.
            # Keep every support/time/cosine predicate before each branch cap.
            branches = [
                "("
                + eligible_edges(
                    f"({edges}) edge(tenant_id,user_id,source_id,target_id,weight,relation,"
                    "left_id,right_id,left_annotation_id,right_annotation_id,edge_id)"
                )
                + " ORDER BY edge.weight DESC,target.unit_id,edge.edge_id LIMIT %(limit)s)"
                for edges in adjacent
            ]
            query = (
                "SELECT * FROM (SELECT candidates.*,row_number() OVER (PARTITION BY source_id "
                "ORDER BY weight DESC,unit_id,edge_id) AS neighbor_rank FROM ("
                + prefix
                + " SELECT limited.* FROM (SELECT DISTINCT unnest(%(sources)s::text[]) "
                "AS unit_id) source_seed CROSS JOIN LATERAL ("
                + " UNION ALL ".join(branches)
                + ") limited"
                + ") candidates) ranked WHERE neighbor_rank<=%(limit)s "
                "ORDER BY source_id,weight DESC,unit_id,edge_id"
            )
            parameters["limit"] = limit
        else:
            query = (
                prefix
                + ", adjacent AS ("
                + " UNION ALL ".join(adjacent)
                + ") "
                + eligible_edges("adjacent edge")
                + " ORDER BY edge.weight DESC,target.unit_id,edge.edge_id"
            )
        if limit is not None and not per_source:
            query += " LIMIT %(limit)s"
            parameters["limit"] = limit
        return self._execute(query, parameters)

    def temporal_neighbors_batch(
        self, source_ids: Sequence[str], vector: Sequence[float], limit: int
    ) -> dict[str, tuple[TemporalEdge, ...]]:
        if not source_ids:
            return {}
        grouped: dict[str, list[TemporalEdge]] = {source_id: [] for source_id in source_ids}
        for row in self._temporal_edges(source_ids, vector, limit=limit, per_source=True):
            grouped[row["source_id"]].append(
                TemporalEdge(
                    TemporalNode(row["unit_id"], row["cosine"], row["basis_date"]),
                    row["weight"],
                    row["relation"],
                    row["edge_id"],
                )
            )
        return {source_id: tuple(edges) for source_id, edges in grouped.items()}

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
        fact_only = all(
            path.get("relation") in {"caused_by", "entity"}
            for group in paths.values()
            for path in group
        )
        rows = self._execute(
            (_fact_only_eligible() if fact_only else self._eligible(candidate_facts=True))
            + "SELECT unit_id FROM filtered WHERE unit_id=ANY(%(ids)s)",
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
        entity_paths = self._entity_path_witnesses(paths)
        return {
            node_id: [
                entity_paths[id(path)] if path.get("relation") == "entity" else path
                for path in group
                if set(path["nodes"]) <= eligible
                and (path["arm"] != "temporal" or set(path["nodes"]) <= temporal_eligible)
                and set(path.get("edge_ids", ())) <= valid_edges
                and (path.get("relation") != "caused_by" or self.causal_graph_enabled)
                and (path.get("relation") != "entity" or id(path) in entity_paths)
            ]
            for node_id, group in paths.items()
        }

    def _entity_path_witnesses(self, paths: dict[str, list[dict]]) -> dict[int, dict]:
        entity_paths = [
            path for group in paths.values() for path in group if path.get("relation") == "entity"
        ]
        if not entity_paths:
            return {}
        parameters = {
            "nodes": sorted({node for path in entity_paths for node in path["nodes"]}),
            "entities": sorted({path["entity"] for path in entity_paths}),
            "names": sorted({name for path in entity_paths for name in path.get("name_ids", ())}),
        }
        members = self._execute(
            "SELECT unit_id,entity_id,name_id FROM ("
            + ELIGIBLE_ENTITY_MEMBERSHIPS
            + ") membership WHERE unit_id=ANY(%(nodes)s) AND entity_id=ANY(%(entities)s)",
            parameters,
        )
        membership_keys = {(row["unit_id"], row["entity_id"], row["name_id"]) for row in members}
        supports = self._execute(
            "SELECT DISTINCT ON(entity_id,name_id) * FROM ("
            + ELIGIBLE_ENTITY_NAME_SUPPORT
            + ") binding WHERE entity_id=ANY(%(entities)s) AND name_id=ANY(%(names)s) "
            "ORDER BY entity_id,name_id,support_id",
            parameters,
        )
        by_name = {(row["entity_id"], row["name_id"]): row for row in supports}
        current = {}
        for path in entity_paths:
            names = path.get("name_ids", ())
            if len(names) != len(path["nodes"]) or not all(
                (node, path["entity"], name) in membership_keys
                for node, name in zip(path["nodes"], names, strict=True)
            ):
                continue
            bindings = [by_name.get((path["entity"], name)) for name in dict.fromkeys(names)]
            if any(binding is None for binding in bindings):
                continue
            witnesses = []
            for binding in bindings:
                if binding["support_kind"] == "alias":
                    witnesses.append(
                        {
                            "support_id": binding["support_id"],
                            "name_id": binding["name_id"],
                            "entity_id": binding["entity_id"],
                            "quote": binding["quote"],
                            "citation": {
                                "source_id": binding["source_id"],
                                "source_version": binding["source_version"],
                                "span": {**binding["locator"], "quote": binding["quote"]},
                            },
                        }
                    )
            current[id(path)] = {**path, "alias_witnesses": witnesses}
        return current
