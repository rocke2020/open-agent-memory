"""One non-generative recall pipeline with a single operation deadline."""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.contracts.search import SearchRequest, TimeBasis
from oam.quotes import unique_quote
from oam.retrieval.algorithms import (
    GraphEdge,
    TimeRange,
    ordinary_graph_activation,
    reciprocal_rank_fusion,
    select_temporal_seeds,
    temporal_spread,
)
from oam.retrieval.context import (
    CONTEXT_TOKENIZER,
    ORIGINAL_KINDS,
    assemble_context,
    bundle_items,
    context_token_count,
    render_context,
    source_diverse_seed_order,
    source_window_ids,
)
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.repository import RecallRepository
from oam.tokens import estimate_units, estimated_tokens

SUPPORT_COMPANION_LIMIT = 16
RELATED_SOURCE_WINDOW_OFFSETS = (0, 1, -1, 2, -2, 3)


class RecallUnavailable(RuntimeError):
    """The configured current index cannot satisfy this recall operation."""


class RecallService:
    def __init__(self, config: OAMConfig, pool, models, *, generation_id: str):
        self.config = config
        self.pool = pool
        self.models = models
        self.generation_id = generation_id

    @contextmanager
    def _repository(self, authority, request, deadline):
        with (
            self.pool.connection(timeout=_remaining(deadline)) as connection,
            connection.transaction(),
        ):
            yield RecallRepository(
                connection,
                authority,
                self.config,
                deadline=deadline,
                request=request,
                read_committed=(
                    request.recall_level.value == "fast"
                    and not request.allow_partial
                    and request.time_window is None
                ),
            )

    def search(
        self,
        authority: AuthorizedUser,
        request: SearchRequest,
        *,
        deadline_at: datetime | None = None,
    ) -> dict:
        started = time.monotonic()
        try:
            with ExitStack() as sql_phase:
                result = self._search(
                    authority, request, deadline_at=deadline_at, sql_phase=sql_phase
                )
        except psycopg.errors.QueryCanceled as error:
            raise TimeoutError("recall SQL deadline exceeded") from error
        diagnostics = result["diagnostics"]
        budget = diagnostics["context_budget"]
        logging.getLogger(__name__).info(
            "oam.recall trace_id=%s mode=%s graph=%s arms=%s outcome=%s "
            "admission=relevance_order query_prefix=%s include_chunks=%s "
            "facts=%d originals=%d context_tokens=%d/%d "
            "omissions=%d oversized_single_fact=%s duration_ms=%.1f",
            result["trace_id"],
            diagnostics["recall_level"],
            diagnostics["graph"],
            ",".join(diagnostics["executed_arms"]),
            "partial" if diagnostics["partial"] else "ok",
            bool(self.config.models.embedding and self.config.models.embedding.query_prefix),
            budget["include_chunks"],
            budget["learned_count"],
            budget["original_count"],
            result["context_tokens"],
            budget["limit"],
            len(budget["omitted_ids"]),
            budget["oversized_single_fact"],
            (time.monotonic() - started) * 1000,
        )
        return result

    def _search(self, authority, request, *, deadline_at, sql_phase: ExitStack) -> dict:
        request.validate_limits(self.config)
        if estimated_tokens(estimate_units(request.query)) > self.config.search.max_query_tokens:
            raise ValueError("query exceeds configured token limit")
        started = time.monotonic()
        trace_id = f"trace_{uuid4().hex}"
        mode = request.recall_level.value
        tier = getattr(self.config.search, mode)
        graph = tier.graph
        if mode == "fast" and request.fast_graph is not None:
            graph = "light" if request.fast_graph else "off"
        deadline = started + getattr(self.config.recall, f"{mode}_deadline_seconds")
        if deadline_at is not None:
            if deadline_at.utcoffset() is None:
                raise ValueError("caller deadline must include an explicit offset")
            deadline = min(deadline, started + (deadline_at - datetime.now(UTC)).total_seconds())
        executed, degraded, timing = [], [], {}
        usage, paths, arms = {}, {}, {}
        temporal_stop = None
        time_started = time.monotonic()
        if request.time_window is not None:
            time_status = "explicit"
            time_expression = None
        else:
            parsed = infer_query_time(
                request.query,
                request.reference_time or datetime.now(UTC),
                request.reference_timezone or "UTC",
            )
            time_status = parsed.status
            time_expression = parsed.expression
            if parsed.window is not None:
                request = request.model_copy(
                    update={"time_basis": TimeBasis.EVENT, "time_window": parsed.window}
                )
        timing["query_time"] = (time.monotonic() - time_started) * 1000
        _remaining(deadline)
        shared_repository = None
        sql_phase_ready = False

        def read(method, *args, **kwargs):
            nonlocal shared_repository
            if (
                sql_phase_ready
                and mode == "fast"
                and not request.allow_partial
                and request.time_window is None
            ):
                if shared_repository is None:
                    shared_repository = sql_phase.enter_context(
                        self._repository(authority, request, deadline)
                    )
                return getattr(shared_repository, method)(*args, **kwargs)
            with self._repository(authority, request, deadline) as repository:
                return getattr(repository, method)(*args, **kwargs)

        def stage(name, call):
            began = time.monotonic()
            try:
                value = call()
            except (PermissionError, TimeoutError, psycopg.errors.QueryCanceled):
                raise
            except (RuntimeError, ValueError, httpx.HTTPError, psycopg.Error) as exc:
                if name == "graph":
                    logging.getLogger(__name__).warning(
                        "oam.recall.graph trace_id=%s mode=%s graph=%s outcome=%s reason=%s",
                        trace_id,
                        mode,
                        graph,
                        "partial" if request.allow_partial else "failed",
                        type(exc).__name__,
                    )
                if not request.allow_partial:
                    raise RecallUnavailable(f"{name} stage failed") from exc
                degraded.append(name)
                return None
            finally:
                timing[name] = (time.monotonic() - began) * 1000
            executed.append(name)
            return value

        def l2_read(method, *args):
            began = time.monotonic()
            try:
                value = read(method, *args)
                if "l2_source_expansion" not in executed:
                    executed.append("l2_source_expansion")
                return value
            except (PermissionError, TimeoutError, psycopg.errors.QueryCanceled):
                raise
            except (RuntimeError, ValueError, httpx.HTTPError, psycopg.Error) as error:
                raise RecallUnavailable("l2_source expansion failed") from error
            finally:
                timing["l2_source_expansion"] = (
                    timing.get("l2_source_expansion", 0) + (time.monotonic() - began) * 1000
                )

        initial_index_state = read("user_state")
        if initial_index_state["index_generation_id"] != self.generation_id:
            raise RecallUnavailable("query generation differs from active user generation")
        if initial_index_state["parse_partial"] and not request.allow_partial:
            raise RecallUnavailable("PDF parse is incomplete; set allow_partial to read it")

        def text_arms():
            return (
                stage("fts", lambda: read("lexical", request.query, tier.channel_candidates)) or [],
                stage("anchors", lambda: read("anchors", request.query, tier.channel_candidates))
                or [],
            )

        # One SQL task overlaps one embedding request; each owns separate results.
        binding = self.config.models.embedding
        embedding_query = (binding.query_prefix if binding is not None else "") + request.query
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="oam-recall-text") as executor:
            text_future = executor.submit(text_arms)
            embedding = stage(
                "embedding",
                lambda: self.models.embed((embedding_query,), timeout_seconds=_remaining(deadline)),
            )
            lexical, anchors = text_future.result(timeout=_remaining(deadline))
        sql_phase_ready = True
        arms["lexical"] = tuple(
            dict.fromkeys(
                (*[row["unit_id"] for row in anchors], *[row["unit_id"] for row in lexical])
            )
        )[: tier.channel_candidates]
        vector = embedding.vectors[0] if embedding is not None else None
        dense_pool = []
        if embedding is not None:
            usage["embedding"] = embedding.usage.model_dump(mode="json")
            pool_limit = (
                tier.channel_candidates
                if graph == "off"
                else max(tier.channel_candidates, self.config.search.graph_seed_max)
            )
            dense_pool = stage("dense", lambda: read("dense", vector, pool_limit)) or []
            arms["dense"] = tuple(row["unit_id"] for row in dense_pool[: tier.channel_candidates])
        if vector is not None:
            seeds = tuple(
                row["unit_id"]
                for row in dense_pool
                if row["kind"] == "fact"
                and row["score"] >= self.config.search.graph_seed_min_similarity
            )[: self.config.search.graph_seed_max]
            if graph != "off" and (seeds or mode == "normal"):
                expansion = stage(
                    "graph",
                    lambda: (
                        read("fast_graph", seeds, vector)
                        if mode == "fast" and graph == "light"
                        else read("ordinary", seeds, vector, include_semantic=graph == "full")
                    ),
                )
                if expansion is not None:
                    ranks, evidence = expansion
                    arms["graph"] = ranks
                    for node_id, node_paths in evidence.items():
                        paths.setdefault(node_id, []).extend(node_paths)
            if request.time_window:

                def temporal():
                    window = TimeRange(request.time_window.start, request.time_window.end)
                    node_limit = (
                        min(self.config.search.temporal_nodes, tier.max_results)
                        if mode == "fast"
                        else self.config.search.temporal_nodes
                    )
                    with self._repository(authority, request, deadline) as repository:
                        direct_ids = tuple(
                            item.node_id
                            for item in reciprocal_rank_fusion(
                                (arms["dense"], arms["lexical"]),
                                rrf_k=self.config.search.rrf_k,
                            )
                        )
                        matched = repository.temporal_matches(direct_ids)
                        temporal_nodes = {
                            node.node_id: node for node in repository.temporal_nodes(seeds, vector)
                        }
                        for node in repository.temporal_candidates(vector, tier.channel_candidates):
                            temporal_nodes.setdefault(node.node_id, node)
                        selected = select_temporal_seeds(
                            tuple(temporal_nodes.values()),
                            window=window,
                            seed_limit=min(self.config.search.temporal_seed_max, node_limit),
                            bucket_count=self.config.search.temporal_coverage_buckets,
                        )
                        recovered_seeds = tuple(
                            node.node_id for node in selected if node.node_id not in matched
                        )
                        expansion = temporal_spread(
                            selected,
                            lambda nodes, limit: repository.temporal_neighbors_batch(
                                nodes, vector, limit
                            ),
                            window=window,
                            node_limit=node_limit,
                            neighbor_limit=self.config.search.temporal_neighbors,
                            deadline_reached=lambda: time.monotonic() >= deadline,
                        )
                        return tuple(dict.fromkeys((*matched, *recovered_seeds))), expansion

                temporal_result = stage("temporal", temporal)
                if temporal_result is not None:
                    matched, expansion = temporal_result
                    temporal_stop = expansion.stop_reason
                    if expansion.stop_reason == "deadline":
                        if not request.allow_partial:
                            raise RecallUnavailable("temporal stage ended at deadline")
                        degraded.append("temporal_deadline")
                    arms["temporal"] = tuple(
                        dict.fromkeys((*matched, *(hit.node_id for hit in expansion.discoveries)))
                    )
                    for node_id in matched:
                        paths.setdefault(node_id, []).append(
                            {"arm": "temporal", "relation": "time_match", "nodes": [node_id]}
                        )
                    for hit in expansion.discoveries:
                        paths.setdefault(hit.node_id, []).append(
                            {
                                "arm": "temporal",
                                "nodes": list(hit.path),
                                "edge_ids": list(hit.path_edge_ids),
                                "score": hit.score,
                            }
                        )

        def validate_paths():
            nonlocal paths
            if not paths:
                return
            current = read("valid_paths", paths, vector)
            changed = {
                path["arm"]
                for node_id, group in paths.items()
                for path in group
                if not any(
                    {key: value for key, value in path.items() if key != "alias_witnesses"}
                    == {key: value for key, value in checked.items() if key != "alias_witnesses"}
                    for checked in current[node_id]
                )
            }
            if changed:
                if not request.allow_partial:
                    raise RecallUnavailable("graph support changed during recall")
                for name in changed:
                    arms[name] = tuple(
                        node_id
                        for node_id in arms.get(name, ())
                        if any(path["arm"] == name for path in current.get(node_id, ()))
                    )
                    degraded.append(f"{name}_revalidation")
            paths = current
            if "graph" in changed:
                rescore_graph_paths()

        def rescore_graph_paths():
            groups = {
                node: [path for path in paths.get(node, ()) if path["arm"] == "graph"]
                for node in arms.get("graph", ())
            }
            activation = ordinary_graph_activation(
                seeds,
                entity_matches={
                    node: {path["entity"] for path in group if path["relation"] == "entity"}
                    for node, group in groups.items()
                },
                semantic_edges=tuple(
                    GraphEdge(path["nodes"][0], node, path["score"])
                    for node, group in groups.items()
                    for path in group
                    if path["relation"] == "semantic"
                ),
                causal_edges=(),
            )
            scores = {candidate.node_id: candidate.score for candidate in activation}
            for node, group in groups.items():
                scores[node] = scores.get(node, 0) + max(
                    (path["score"] for path in group if path["relation"] == "caused_by"),
                    default=0,
                )
            arms["graph"] = tuple(sorted(scores, key=lambda node: (-scores[node], node)))

        def fused_ids():
            weights = (
                tuple(getattr(self.config.search.normal_arm_weights, name) for name in arms)
                if mode == "normal"
                else None
            )
            return tuple(
                item.node_id
                for item in reciprocal_rank_fusion(
                    tuple(arms.values()), rrf_k=self.config.search.rrf_k, weights=weights
                )[: tier.fused_candidates]
            )

        rerank_scores = None

        def final_order():
            identifiers = fused_ids()
            if rerank_scores is not None:
                identifiers = tuple(
                    sorted(
                        (node for node in identifiers if node in rerank_scores),
                        key=lambda node: (-rerank_scores[node], node),
                    )
                )
            return identifiers

        if mode != "fast" and self.config.models.reranker is not None:
            validate_paths()
            rows = read("hydrate", fused_ids())[: tier.rerank_candidates]
            if rows:
                scored = stage(
                    "rerank",
                    lambda: self.models.rerank(
                        request.query,
                        tuple(row["text"] for row in rows),
                        timeout_seconds=_remaining(deadline),
                    ),
                )
                if scored is not None:
                    rerank_scores = dict(
                        zip((row["unit_id"] for row in rows), scored.scores, strict=True)
                    )
                    usage["rerank"] = scored.usage.model_dump(mode="json")
        validate_paths()
        final_ids = final_order()
        related = (
            stage("related_user_fact", lambda: read("related_user_fact", final_ids))
            if vector is not None and final_ids and len(final_ids) < tier.fused_candidates
            else None
        )
        related_tail_id = None
        if related is not None and related["unit_id"] not in final_ids:
            related_tail_id = related["unit_id"]
            final_ids = (*final_ids, related_tail_id)
        index_state = read("user_state")
        if index_state["index_generation_id"] != self.generation_id:
            raise RecallUnavailable("user generation changed during recall")
        if index_state["parse_partial"] and not request.allow_partial:
            raise RecallUnavailable("PDF parse became incomplete during recall")
        hydrated = read("hydrate", final_ids)
        hydrated_passages = {row["unit_id"]: row for row in hydrated if row["kind"] == "passage"}
        context_limit = request.context_limit(self.config)
        chunks = request.include.chunks
        candidates = []
        for row in hydrated:
            item = {key: value for key, value in row.items() if key != "locator"}
            item["paths"] = paths.get(item["unit_id"], [])
            candidates.append(item)
        seed_limit = min(tier.source_seed_results, tier.max_results)
        ranked_candidates = [item for item in candidates if item["unit_id"] != related_tail_id]
        discovery_seed_items = source_diverse_seed_order(ranked_candidates)[:seed_limit]
        seed_items = [
            item for item in ranked_candidates if chunks or item["kind"] not in ORIGINAL_KINDS
        ][:seed_limit]
        source_seed_items = seed_items
        if related_tail_id is not None:
            related_item = next(
                (item for item in candidates if item["unit_id"] == related_tail_id), None
            )
            if related_item is not None:
                source_seed_items = [*source_seed_items, related_item][:seed_limit]
        source_ids = tuple(
            dict.fromkeys(
                citation["source_id"]
                for item in candidates
                if item["kind"] == "fact"
                for citation in item["citations"]
                if isinstance(citation.get("source_id"), str)
            )
        )
        primary_ids = {item["unit_id"] for item in seed_items}
        direct_ids = set(arms.get("dense", ())) | set(arms.get("lexical", ()))
        primary_insights = [
            item
            for item in candidates
            if item["unit_id"] in primary_ids & direct_ids and item["kind"] == "insight"
        ]
        l2_diagnostics, l2_supports, l2_fact_items = [], {}, {}
        if primary_insights:
            selected = l2_read(
                "insight_support_units",
                tuple(item["unit_id"] for item in primary_insights),
                vector,
                SUPPORT_COMPANION_LIMIT,
            )
            support_rows = {
                row["unit_id"]: row
                for row in l2_read(
                    "hydrate", tuple(dict.fromkeys(row["unit_id"] for row in selected))
                )
            }
            for insight in primary_insights:
                identity = insight["unit_id"]
                expected = {
                    support["memory_id"]: support["support_revision"]
                    for support in insight["supports"]
                }
                links = [row for row in selected if row["insight_unit_id"] == identity]
                facts = []
                valid = (
                    bool(expected)
                    and len(expected) <= SUPPORT_COMPANION_LIMIT
                    and {link["memory_id"] for link in links} == set(expected)
                )
                for link in links:
                    row = support_rows.get(link["unit_id"])
                    if (
                        row is None
                        or row["kind"] != "fact"
                        or row.get("memory_id") != link["memory_id"]
                        or expected.get(link["memory_id"]) != link["support_revision"]
                        or not row["citations"]
                    ):
                        valid = False
                        continue
                    facts.append(
                        l2_fact_items.setdefault(
                            row["unit_id"],
                            {
                                key: value
                                for key, value in {
                                    **row,
                                    "paths": list(paths.get(row["unit_id"], [])),
                                }.items()
                                if key != "locator"
                            },
                        )
                    )
                l2_diagnostics.append(
                    {
                        "unit_id": identity,
                        "insight_id": insight["insight_id"],
                        "insight_revision": insight["insight_revision"],
                        "support_count": len(expected),
                        "selected_support_ids": [row["memory_id"] for row in facts],
                        "support_selection_limit": SUPPORT_COMPANION_LIMIT,
                        "unselected_support_count": max(0, len(expected) - len(links)),
                        "selection": (
                            "query_vector_ranked_bounded_supports"
                            if vector is not None
                            else "stable_support_order_without_query_vector"
                        ),
                        "direct_chunk_ids": [],
                        "status": "pending" if valid else "ineligible",
                        "reason": (
                            None
                            if valid
                            else "support_selection_limit"
                            if len(expected) > SUPPORT_COMPANION_LIMIT
                            else "missing_or_ineligible_support"
                        ),
                    }
                )
                if valid:
                    l2_supports[identity] = facts
            source_ids = tuple(
                dict.fromkeys(
                    (
                        *source_ids,
                        *(
                            citation["source_id"]
                            for facts in l2_supports.values()
                            for fact in facts
                            for citation in fact["citations"]
                            if isinstance(citation.get("source_id"), str)
                        ),
                    )
                )
            )
        source_passages = []
        by_citation = {}
        original_detail_ids = []
        expanded_detail_ids = []
        l2_detail_ids = []
        if source_ids:
            locators = (l2_read if primary_insights else read)(
                "source_passage_locators", source_ids
            )
            original_detail_ids = list(
                dict.fromkeys(
                    [
                        *source_window_ids(seed_items, locators),
                        *source_window_ids(candidates, locators, policy="l2_direct"),
                    ]
                )
            )
            by_citation = {
                (row["source_id"], row["source_version"], row["passage_id"]): row
                for row in locators
            }
            for diagnostic in l2_diagnostics:
                identity = diagnostic["unit_id"]
                if identity not in l2_supports:
                    continue
                facts = l2_supports[identity]
                direct_ids = source_window_ids(facts, locators, policy="l2_direct")
                citations = [citation for fact in facts for citation in fact["citations"]]
                if not direct_ids or any(
                    (
                        citation.get("source_id"),
                        citation.get("source_version"),
                        citation.get("passage_id"),
                    )
                    not in by_citation
                    for citation in citations
                ):
                    diagnostic.update(
                        status="ineligible", reason="missing_or_ineligible_direct_chunk"
                    )
                    continue
                diagnostic["direct_chunk_ids"] = direct_ids
                l2_detail_ids.extend(direct_ids)
            detail_ids = list(dict.fromkeys((*original_detail_ids, *l2_detail_ids)))
            related_seed = next(
                (
                    item
                    for item in source_seed_items
                    if related and item["unit_id"] == related["unit_id"]
                ),
                None,
            )
            if related_seed is not None:
                source_rows = sorted(
                    (
                        row
                        for row in locators
                        if row["source_id"] == related["source_id"]
                        and row["source_version"] == related["source_version"]
                    ),
                    key=lambda row: (
                        row["locator"].get("message_index", float("inf")),
                        row["locator"].get("passage_index", 0),
                        row["unit_id"],
                    ),
                )
                source_positions = {
                    row["passage_id"]: index for index, row in enumerate(source_rows)
                }
                direct = (
                    citation.get("passage_id")
                    for citation in related_seed["citations"]
                    if citation.get("source_id") == related["source_id"]
                    and citation.get("source_version") == related["source_version"]
                )
                for passage_id in direct:
                    center = source_positions.get(passage_id)
                    if center is None:
                        continue
                    for offset in RELATED_SOURCE_WINDOW_OFFSETS:
                        index = center + offset
                        if 0 <= index < len(source_rows):
                            unit_id = source_rows[index]["unit_id"]
                            if unit_id not in expanded_detail_ids:
                                expanded_detail_ids.append(unit_id)
                    if len(expanded_detail_ids) >= len(RELATED_SOURCE_WINDOW_OFFSETS):
                        break
                expanded_detail_ids = expanded_detail_ids[: len(RELATED_SOURCE_WINDOW_OFFSETS)]
                detail_ids = list(dict.fromkeys((*expanded_detail_ids, *detail_ids)))
            if detail_ids:
                missing_ids = [node for node in detail_ids if node not in hydrated_passages]
                if missing_ids:
                    hydrated_passages.update(
                        (row["unit_id"], row)
                        for row in (l2_read if primary_insights else read)("hydrate", missing_ids)
                    )
                for node in detail_ids:
                    row = hydrated_passages.get(node)
                    if row is None:
                        continue
                    item = {key: value for key, value in row.items() if key != "locator"}
                    item["paths"] = paths.get(item["unit_id"], [])
                    source_passages.append(item)
                for diagnostic in l2_diagnostics:
                    if diagnostic["status"] != "pending":
                        continue
                    if any(
                        node not in hydrated_passages for node in diagnostic["direct_chunk_ids"]
                    ):
                        diagnostic.update(
                            status="ineligible", reason="missing_or_ineligible_direct_chunk"
                        )
                if expanded_detail_ids:
                    by_id = {item["unit_id"]: item for item in source_passages}
                    expanded = [
                        by_id[unit_id] for unit_id in expanded_detail_ids if unit_id in by_id
                    ]
                    selected = set(original_detail_ids) | set(l2_detail_ids)
                    source_passages = [
                        *(
                            by_id[unit_id]
                            for unit_id in dict.fromkeys((*original_detail_ids, *l2_detail_ids))
                            if unit_id in by_id
                        ),
                        *(item for item in expanded if item["unit_id"] not in selected),
                    ]
        if vector is not None:
            source_by_id = {item["unit_id"]: item for item in source_passages}
            companion_order = [
                *(
                    source_by_id[unit_id]
                    for unit_id in original_detail_ids
                    if unit_id in source_by_id
                ),
                *(item for item in source_passages if item["unit_id"] not in l2_detail_ids),
            ]
            passage_ids = tuple(
                dict.fromkeys(
                    item["unit_id"]
                    for item in (*seed_items, *companion_order)
                    if item["kind"] == "passage"
                    and item["citations"]
                    and item["citations"][0].get("role") == "user"
                )
            )[:SUPPORT_COMPANION_LIMIT]
            if passage_ids:
                existing = {item["unit_id"] for item in candidates}
                supported = (
                    stage(
                        "supported_facts",
                        lambda: read(
                            "supported_fact_ids",
                            passage_ids,
                            vector,
                            len(passage_ids),
                            tuple(existing),
                        ),
                    )
                    or []
                )
                fresh = [row for row in supported if row["unit_id"] not in existing]
                if fresh:
                    fact_ids = tuple(dict.fromkeys(row["unit_id"] for row in fresh))
                    companions = {}
                    for row in read("hydrate", fact_ids):
                        item = {key: value for key, value in row.items() if key != "locator"}
                        item["paths"] = paths.get(item["unit_id"], [])
                        companions[item["unit_id"]] = item
                    candidates.extend(companions.values())
        if vector is not None:
            passage_source_keys = tuple(
                dict.fromkeys(
                    (citation["source_id"], citation["source_version"])
                    for item in discovery_seed_items
                    if item["kind"] == "passage" and item["origin"] == "memory"
                    for citation in item["citations"]
                    if isinstance(citation.get("source_id"), str)
                    and type(citation.get("source_version")) is int
                )
            )
            if passage_source_keys:
                originals = (
                    stage(
                        "source_user_originals",
                        lambda: read(
                            "source_user_passage_ids",
                            passage_source_keys,
                            vector,
                            seed_limit,
                            tuple(hydrated_passages),
                        ),
                    )
                    or []
                )
                if originals:
                    original_ids = tuple(dict.fromkeys(row["unit_id"] for row in originals))
                    recovered = []
                    for row in read("hydrate", original_ids):
                        item = {key: value for key, value in row.items() if key != "locator"}
                        item["paths"] = paths.get(item["unit_id"], [])
                        recovered.append(item)
                    candidates = [*candidates, *recovered]
        original_detail_set = set(original_detail_ids)
        evidence_groups = []
        passages_by_id = {item["unit_id"]: item for item in source_passages}
        direct_locators = {row["unit_id"]: row for row in locators} if source_ids else {}
        for node in dict.fromkeys(l2_detail_ids):
            if node in passages_by_id:
                item = passages_by_id[node]
                item["citations"] = [
                    {**citation, "passage_id": direct_locators[node]["passage_id"]}
                    for citation in item["citations"]
                ]
        for insight, diagnostic in zip(primary_insights, l2_diagnostics, strict=True):
            if diagnostic["status"] != "pending":
                continue
            facts = l2_supports[insight["unit_id"]]
            for fact in facts:
                fact["paths"].append(
                    {
                        "arm": "l2_source",
                        "insight_id": insight["insight_id"],
                        "insight_revision": insight["insight_revision"],
                        "memory_id": fact["memory_id"],
                    }
                )
                for citation in fact["citations"]:
                    locator = by_citation[
                        (citation["source_id"], citation["source_version"], citation["passage_id"])
                    ]
                    passage = passages_by_id[locator["unit_id"]]
                    edge = {
                        "arm": "l2_source",
                        "insight_id": insight["insight_id"],
                        "insight_revision": insight["insight_revision"],
                        "memory_id": fact["memory_id"],
                        "support_revision": 1,
                        "passage_id": citation["passage_id"],
                        "source_id": citation["source_id"],
                        "source_version": citation["source_version"],
                    }
                    if edge not in passage["paths"]:
                        passage["paths"].append(edge)
            evidence_groups.append(
                [
                    insight,
                    *facts,
                    *(passages_by_id[node] for node in diagnostic["direct_chunk_ids"]),
                ]
            )
        invalid_insights = {
            entry["unit_id"] for entry in l2_diagnostics if entry["status"] == "ineligible"
        }
        candidates = [
            l2_fact_items.get(item["unit_id"], passages_by_id.get(item["unit_id"], item))
            for item in candidates
            if item["unit_id"] not in invalid_insights
        ]
        seed_items = [
            l2_fact_items.get(item["unit_id"], passages_by_id.get(item["unit_id"], item))
            for item in seed_items
            if item["unit_id"] not in invalid_insights
        ]
        candidates.extend(
            item
            for item in source_passages
            if item["unit_id"] not in original_detail_set and item["unit_id"] not in l2_detail_ids
        )
        source_passages = [
            item for item in source_passages if item["unit_id"] in original_detail_set
        ]
        # Companion facts can be discovered after the initial bounded source window.
        late_sources = tuple(
            dict.fromkeys(
                citation["source_id"]
                for fact in candidates
                if fact["kind"] == "fact"
                for citation in fact["citations"]
                if isinstance(citation.get("source_id"), str)
                and citation["source_id"] not in source_ids
            )
        )
        if late_sources:
            for row in read("source_passage_locators", late_sources):
                by_citation[(row["source_id"], row["source_version"], row["passage_id"])] = row
        required_ids = tuple(
            dict.fromkeys(
                locator["unit_id"]
                for fact in candidates
                if fact["kind"] == "fact"
                for citation in fact["citations"]
                if (
                    locator := by_citation.get(
                        (
                            citation.get("source_id"),
                            citation.get("source_version"),
                            citation.get("passage_id"),
                        )
                    )
                )
            )
        )
        missing_owners = [
            identity for identity in required_ids if identity not in hydrated_passages
        ]
        if missing_owners:
            hydrated_passages.update(
                (row["unit_id"], row) for row in read("hydrate", missing_owners)
            )
        for identity in required_ids:
            if identity in hydrated_passages and identity not in passages_by_id:
                passage = hydrated_passages[identity]
                passages_by_id[identity] = {
                    **{key: value for key, value in passage.items() if key != "locator"},
                    "paths": paths.get(identity, []),
                }
        source_omissions = []
        for fact in candidates:
            if fact["kind"] != "fact":
                continue
            required = []
            for citation in fact["citations"]:
                locator = by_citation.get(
                    (
                        citation.get("source_id"),
                        citation.get("source_version"),
                        citation.get("passage_id"),
                    )
                )
                passage = passages_by_id.get(locator["unit_id"]) if locator else None
                span = citation.get("span", {})
                if (
                    passage is None
                    or not unique_quote(passage["text"], span.get("quote"))
                    or any(
                        span.get(key) != locator["locator"].get(key)
                        for key in (
                            "source_id",
                            "source_version",
                            "message_id",
                            "message_index",
                            "role",
                            "passage_index",
                        )
                        if key in span
                    )
                ):
                    break
                required.append(passage)
            else:
                if required:
                    evidence_groups.append([fact, *required])
                    continue
            source_omissions.append(fact["unit_id"])
        grouped_roots = {group[0]["unit_id"] for group in evidence_groups}
        support_limit_omissions = {
            item["unit_id"]
            for item in candidates
            if item["kind"] == "insight" and item["unit_id"] not in grouped_roots
        }
        unavailable_roots = set(source_omissions) | support_limit_omissions
        packing_candidates = [
            item for item in candidates if item["unit_id"] not in unavailable_roots
        ]
        packing_started = time.monotonic()
        items, budget_omissions, admitted_roots, oversized_single_fact = assemble_context(
            packing_candidates,
            source_passages,
            context_max_tokens=context_limit,
            include_chunks=chunks,
            max_results=tier.max_results,
            evidence_groups=evidence_groups,
        )
        timing["context_packing"] = (time.monotonic() - packing_started) * 1000
        selected_insights = [
            item for item in items if item["kind"] == "insight" and item["unit_id"] in l2_supports
        ]
        fresh_insights = (
            {
                row["unit_id"]: row
                for row in l2_read("hydrate", tuple(item["unit_id"] for item in selected_insights))
            }
            if selected_insights
            else {}
        )
        selected_ids = {item["unit_id"] for item in items}
        for diagnostic in l2_diagnostics:
            if diagnostic["status"] != "pending":
                continue
            identity = diagnostic["unit_id"]
            if identity not in selected_ids:
                diagnostic.update(
                    status="budget_omitted", reason="complete_group_exceeds_hard_limit"
                )
                continue
            old = next(item for item in primary_insights if item["unit_id"] == identity)
            fresh = fresh_insights.get(identity)
            if (
                fresh is None
                or fresh["insight_revision"] != old["insight_revision"]
                or fresh["supports"] != old["supports"]
            ):
                diagnostic.update(status="ineligible", reason="insight_changed_before_return")
            else:
                diagnostic.update(status="included")
        # Only admitted roots own output; hydration-pool membership is not admission.
        invalid = {entry["unit_id"] for entry in l2_diagnostics if entry["status"] == "ineligible"}
        admitted_roots = [group for group in admitted_roots if group[0]["unit_id"] not in invalid]
        retained_identities = {
            group[0]["insight_id"] for group in admitted_roots if group[0]["kind"] == "insight"
        }
        for item in bundle_items(admitted_roots):
            item["paths"] = [
                path
                for path in item["paths"]
                if path.get("arm") != "l2_source" or path["insight_id"] in retained_identities
            ]
        items = bundle_items(admitted_roots)
        context = render_context(items)
        while (
            context_token_count(context) > context_limit
            and admitted_roots
            and not oversized_single_fact
        ):
            removed = admitted_roots.pop()
            budget_omissions.append(removed[0]["unit_id"])
            items = bundle_items(admitted_roots)
            context = render_context(items)
        context_tokens = context_token_count(context)
        selected_ids = {item["unit_id"] for item in items}
        for diagnostic in l2_diagnostics:
            if diagnostic["status"] == "included" and diagnostic["unit_id"] not in selected_ids:
                diagnostic.update(
                    status="budget_omitted", reason="complete_group_exceeds_hard_limit"
                )
        context_budget = {
            "limit": context_limit,
            "used": context_tokens,
            "oversized_single_fact": oversized_single_fact,
            "overrun_tokens": max(0, context_tokens - context_limit),
            "include_chunks": chunks,
            "learned_count": sum(item["kind"] not in ORIGINAL_KINDS for item in items),
            "original_count": sum(item["kind"] in ORIGINAL_KINDS for item in items),
            "omitted_ids": list(
                dict.fromkeys(
                    [
                        *budget_omissions,
                        *source_omissions,
                        *sorted(support_limit_omissions),
                    ]
                )
            ),
            "omission_reasons": {
                **dict.fromkeys(budget_omissions, "context_or_result_limit"),
                **dict.fromkeys(source_omissions, "missing_or_invalid_source_support"),
                **dict.fromkeys(sorted(support_limit_omissions), "support_expansion_limit"),
                **{
                    entry["unit_id"]: entry["reason"] for entry in l2_diagnostics if entry["reason"]
                },
            },
            "source_omitted_ids": source_omissions,
        }
        _remaining(deadline)
        result = {
            "items": items,
            "context": context,
            "context_tokens": context_tokens,
            "tokenizer": CONTEXT_TOKENIZER,
            "diagnostics": {
                "recall_level": mode,
                "graph": graph,
                "generation_id": self.generation_id,
                "executed_arms": list(arms),
                "executed_stages": [
                    name
                    for name in (
                        "fts",
                        "anchors",
                        "embedding",
                        "dense",
                        "related_user_fact",
                        "graph",
                        "temporal",
                        "rerank",
                        "source_user_originals",
                        "l2_source_expansion",
                    )
                    if name in executed
                ],
                "omitted_stages": [
                    name
                    for name in ("graph", "temporal", "rerank")
                    if name not in executed and name not in degraded
                ],
                "degraded": sorted(set(degraded)),
                "readiness": index_state["readiness"],
                "partial": index_state["readiness"] != "ready" or bool(degraded),
                "generative_calls": 0,
                "context_budget": context_budget,
                "l2_source_expansion": l2_diagnostics,
                "temporal_stop": temporal_stop,
                "query_time": {
                    "status": time_status,
                    "expression": time_expression,
                    "basis": request.time_basis.value if request.time_basis else None,
                    "window": request.time_window.model_dump(mode="json")
                    if request.time_window
                    else None,
                },
                "usage": usage,
                "timing_ms": {**timing, "total": (time.monotonic() - started) * 1000},
            },
        }
        weights = (
            tuple(getattr(self.config.search.normal_arm_weights, name) for name in arms)
            if mode == "normal"
            else (1.0,) * len(arms)
        )
        arm_positions = {
            name: {node_id: rank for rank, node_id in enumerate(dict.fromkeys(ids), 1)}
            for name, ids in arms.items()
        }
        fused = reciprocal_rank_fusion(
            tuple(arms.values()), rrf_k=self.config.search.rrf_k, weights=weights
        )[: tier.fused_candidates]
        fusion = [
            {
                "unit_id": candidate.node_id,
                "score": candidate.score,
                "contributions": [
                    {
                        "arm": name,
                        "rank": arm_positions[name][candidate.node_id],
                        "weight": weight,
                        "score": weight
                        / (self.config.search.rrf_k + arm_positions[name][candidate.node_id]),
                    }
                    for name, weight in zip(arms, weights, strict=True)
                    if candidate.node_id in arm_positions[name]
                ],
            }
            for candidate in fused
        ]
        decisions = {
            "query_hash": hashlib.sha256(request.query.encode()).hexdigest(),
            "options": request.model_dump(mode="json", exclude={"query"}),
            "generation_id": self.generation_id,
            "arms": {name: list(ids) for name, ids in arms.items()},
            "fusion": fusion,
            "rerank_scores": rerank_scores,
            "related_user_fact": related,
            "result_ids": [item["unit_id"] for item in items],
            "l2_source_expansion": l2_diagnostics,
            "packing": {
                "context_budget": context_budget,
                "selected_ids": [item["unit_id"] for item in items],
                "budget_omissions": budget_omissions,
                "hydration_omissions": [
                    node_id
                    for node_id in final_ids
                    if node_id not in {row["unit_id"] for row in hydrated}
                ],
            },
            "diagnostics": result["diagnostics"],
        }
        _remaining(deadline)
        if not read(
            "record_trace", trace_id, initial_index_state["policy_epoch"], decisions, result
        ):
            raise RecallUnavailable("source policy changed before trace publication")
        result["trace_id"] = trace_id
        return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("recall deadline exhausted")
    return remaining
