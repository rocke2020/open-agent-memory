"""One non-generative recall pipeline with a single operation deadline."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.contracts.search import SearchRequest, TimeBasis
from oam.retrieval.algorithms import (
    TimeRange,
    reciprocal_rank_fusion,
    select_temporal_seeds,
    temporal_spread,
)
from oam.retrieval.context import (
    ContextUnitCosts,
    assemble_context,
    pack_ranked,
    render_context,
    source_diverse_seed_order,
    source_window_ids,
)
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.repository import RecallRepository
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens

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
                connection, authority, self.config, deadline=deadline, request=request
            )

    def search(
        self,
        authority: AuthorizedUser,
        request: SearchRequest,
        *,
        deadline_at: datetime | None = None,
    ) -> dict:
        request.validate_limits(self.config)
        if request.include.chunks is not None:
            raise NotImplementedError("separate recalled chunks are unavailable")
        if estimated_tokens(estimate_units(request.query)) > self.config.search.max_query_tokens:
            raise ValueError("query exceeds configured token limit")
        started = time.monotonic()
        mode = request.recall_level.value
        deadline = started + getattr(self.config.recall, f"{mode}_deadline_seconds")
        if deadline_at is not None:
            if deadline_at.utcoffset() is None:
                raise ValueError("caller deadline must include an explicit offset")
            deadline = min(deadline, started + (deadline_at - datetime.now(UTC)).total_seconds())
        tier = getattr(self.config.search, mode)
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

        def read(method, *args):
            with self._repository(authority, request, deadline) as repository:
                return getattr(repository, method)(*args)

        def stage(name, call, *, timed=False):
            began = time.monotonic()
            duration_ms = None
            try:
                value = call()
                if timed:
                    value, error, duration_ms = value
                    if error is not None:
                        raise error
            except (PermissionError, TimeoutError):
                raise
            except (RuntimeError, ValueError, httpx.HTTPError, psycopg.Error) as exc:
                if not request.allow_partial:
                    raise RecallUnavailable(f"{name} stage failed") from exc
                degraded.append(name)
                return None
            finally:
                timing[name] = (
                    duration_ms if duration_ms is not None else (time.monotonic() - began) * 1000
                )
            executed.append(name)
            return value

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
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="oam-recall-text") as executor:
            text_future = executor.submit(text_arms)
            embedding = stage(
                "embedding",
                lambda: self.models.embed((request.query,), timeout_seconds=_remaining(deadline)),
            )
            lexical, anchors = text_future.result(timeout=_remaining(deadline))
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
                if mode == "fast"
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
            if mode != "fast":

                def record_expansion(name, result):
                    if result is not None:
                        ranks, evidence = result
                        arms[name] = ranks
                        for node_id, node_paths in evidence.items():
                            paths.setdefault(node_id, []).extend(node_paths)

                expansions = (("graph", "ordinary"),)
                if mode == "deep":
                    expansions += (("hierarchy", "hierarchy"),)
                if len(expansions) > 1 and getattr(self.pool, "max_size", 1) >= 2:

                    def timed_expansion(method):
                        began = time.monotonic()
                        try:
                            result = read(method, seeds, vector)
                        except Exception as error:
                            return None, error, (time.monotonic() - began) * 1000
                        return result, None, (time.monotonic() - began) * 1000

                    with ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="oam-recall-expand"
                    ) as executor:
                        futures = {
                            name: executor.submit(timed_expansion, method)
                            for name, method in expansions
                        }
                        for name, _ in expansions:
                            outcome = stage(
                                name,
                                lambda name=name: futures[name].result(
                                    timeout=_remaining(deadline)
                                ),
                                timed=True,
                            )
                            if outcome is not None:
                                record_expansion(name, outcome)
                else:
                    for name, method in expansions:
                        record_expansion(
                            name, stage(name, lambda method=method: read(method, seeds, vector))
                        )
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
                        selected = select_temporal_seeds(
                            repository.temporal_nodes(seeds, vector),
                            window=window,
                            seed_limit=min(self.config.search.temporal_seed_max, node_limit),
                            bucket_count=self.config.search.temporal_coverage_buckets,
                        )
                        expansion = temporal_spread(
                            selected,
                            lambda node, limit: repository.temporal_neighbors(node, vector, limit),
                            window=window,
                            node_limit=node_limit,
                            neighbor_limit=self.config.search.temporal_neighbors,
                            deadline_reached=lambda: time.monotonic() >= deadline,
                        )
                        return matched, expansion

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
                if path not in current[node_id]
            }
            if changed:
                if not request.allow_partial:
                    raise RecallUnavailable("graph support changed during recall")
                for name in changed:
                    arms.pop(name, None)
                    degraded.append(f"{name}_revalidation")
                current = {
                    node_id: [path for path in group if path["arm"] not in changed]
                    for node_id, group in current.items()
                }
            paths = current

        def fused_ids():
            weights = (
                tuple(getattr(self.config.search.mid_arm_weights, name) for name in arms)
                if mode == "mid"
                else None
            )
            return tuple(
                item.node_id
                for item in reciprocal_rank_fusion(
                    tuple(arms.values()), rrf_k=self.config.search.rrf_k, weights=weights
                )[: tier.fused_candidates]
            )

        rerank_scores = None
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
        final_ids = fused_ids()
        if rerank_scores is not None:
            final_ids = tuple(
                sorted(
                    (node for node in final_ids if node in rerank_scores),
                    key=lambda node: (-rerank_scores[node], node),
                )
            )
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
        budget = request.max_context_tokens or self.config.context.default_tokens
        candidates = []
        for row in hydrated:
            item = {key: value for key, value in row.items() if key != "locator"}
            item["paths"] = paths.get(item["unit_id"], [])
            candidates.append(item)
        seed_budget = min(budget, self.config.context.source_seed_tokens)
        context_costs = ContextUnitCosts()
        seed_limit = min(tier.source_seed_results, tier.max_results)
        ranked_candidates = [item for item in candidates if item["unit_id"] != related_tail_id]
        seed_items = pack_ranked(
            source_diverse_seed_order(ranked_candidates),
            seed_budget,
            seed_limit,
            costs=context_costs,
        )
        source_seed_items = seed_items
        if related_tail_id is not None:
            related_item = next(
                (item for item in candidates if item["unit_id"] == related_tail_id), None
            )
            if related_item is not None:
                source_seed_items = pack_ranked(
                    [related_item], seed_budget, seed_limit, source_seed_items, costs=context_costs
                )
        source_ids = tuple(
            dict.fromkeys(
                citation["source_id"]
                for item in source_seed_items
                if item["kind"] == "fact"
                for citation in item["citations"]
                if isinstance(citation.get("source_id"), str)
            )
        )
        source_passages = []
        original_detail_ids = []
        expanded_detail_ids = []
        if source_ids:
            locators = read("source_passage_locators", source_ids)
            original_detail_ids = source_window_ids(seed_items, locators)
            detail_ids = original_detail_ids
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
                detail_ids = list(dict.fromkeys((*expanded_detail_ids, *original_detail_ids)))
            if detail_ids:
                for row in read("hydrate", detail_ids):
                    item = {key: value for key, value in row.items() if key != "locator"}
                    item["paths"] = paths.get(item["unit_id"], [])
                    source_passages.append(item)
                if expanded_detail_ids:
                    by_id = {item["unit_id"]: item for item in source_passages}
                    detail_cap = min(
                        self.config.context.default_tokens,
                        int(seed_budget * self.config.context.source_detail_budget_fraction),
                    )
                    expanded = pack_ranked(
                        [by_id[unit_id] for unit_id in expanded_detail_ids if unit_id in by_id],
                        detail_cap,
                        tier.max_results,
                        costs=context_costs,
                    )
                    selected = set(original_detail_ids)
                    source_passages = [
                        *(by_id[unit_id] for unit_id in original_detail_ids if unit_id in by_id),
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
                *source_passages,
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
        original_detail_set = set(original_detail_ids)
        candidates.extend(
            item for item in source_passages if item["unit_id"] not in original_detail_set
        )
        source_passages = [
            item for item in source_passages if item["unit_id"] in original_detail_set
        ]
        items, budget_omissions = assemble_context(
            candidates,
            seed_items,
            source_passages,
            budget=budget,
            seed_budget=seed_budget,
            detail_fraction=self.config.context.source_detail_budget_fraction,
            max_results=tier.max_results,
            costs=context_costs,
        )
        context = render_context(items)
        context_tokens = estimated_tokens(estimate_units(context))
        if context_tokens > budget:
            raise RecallUnavailable("rendered context exceeds requested token estimate")
        _remaining(deadline)
        if read("user_state")["policy_epoch"] != initial_index_state["policy_epoch"]:
            raise RecallUnavailable("source policy changed during recall")
        result = {
            "items": items,
            "context": context,
            "context_tokens": context_tokens,
            "tokenizer": ESTIMATOR,
            "diagnostics": {
                "recall_level": mode,
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
                        "hierarchy",
                        "temporal",
                        "rerank",
                    )
                    if name in executed
                ],
                "omitted_stages": [
                    name
                    for name in ("graph", "hierarchy", "temporal", "rerank")
                    if name not in executed and name not in degraded
                ],
                "degraded": sorted(set(degraded)),
                "readiness": index_state["readiness"],
                "partial": index_state["readiness"] != "ready" or bool(degraded),
                "generative_calls": 0,
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
            tuple(getattr(self.config.search.mid_arm_weights, name) for name in arms)
            if mode == "mid"
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
        trace_id = f"trace_{uuid4().hex}"
        decisions = {
            "query_hash": hashlib.sha256(request.query.encode()).hexdigest(),
            "options": request.model_dump(mode="json", exclude={"query"}),
            "generation_id": self.generation_id,
            "arms": {name: list(ids) for name, ids in arms.items()},
            "fusion": fusion,
            "rerank_scores": rerank_scores,
            "related_user_fact": related,
            "result_ids": [item["unit_id"] for item in items],
            "packing": {
                "token_limit": budget,
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
