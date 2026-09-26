"""One non-generative recall pipeline with a single operation deadline."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

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
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.repository import RecallRepository
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens


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
        if mode != "fast" and self.config.models.roles.reranker:
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
        index_state = read("user_state")
        if index_state["index_generation_id"] != self.generation_id:
            raise RecallUnavailable("user generation changed during recall")
        if index_state["parse_partial"] and not request.allow_partial:
            raise RecallUnavailable("PDF parse became incomplete during recall")
        hydrated = read("hydrate", final_ids)
        budget = request.max_context_tokens or self.config.context.default_tokens
        items, blocks = [], []
        used_units = 0
        for row in hydrated:
            item = {key: value for key, value in row.items() if key != "locator"}
            item["paths"] = paths.get(item["unit_id"], [])
            citations = json.dumps(item["citations"], ensure_ascii=False, separators=(",", ":"))
            time_evidence = json.dumps(
                item["time_evidence"], ensure_ascii=False, separators=(",", ":")
            )
            block = (
                f"[{item['unit_id']}] {item['kind']} {item['origin']}\n{item['text']}\n"
                f"Citations: {citations}\nTime: {time_evidence}"
            )
            next_units = used_units + (2 if blocks else 0) + estimate_units(block)
            if estimated_tokens(next_units) > budget:
                continue
            items.append(item)
            blocks.append(block)
            used_units = next_units
            if len(items) == tier.max_results:
                break
        _remaining(deadline)
        context = "\n\n".join(blocks)
        return {
            "items": items,
            "context": context,
            "context_tokens": estimated_tokens(used_units),
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


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("recall deadline exhausted")
    return remaining
