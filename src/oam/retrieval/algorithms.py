from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

TEMPORAL_PROPAGATION_DECAY = 0.7
CAUSAL_RELATION_FACTOR = 1.1
TEMPORAL_CONTINUATION_THRESHOLD = 0.2


@dataclass(frozen=True)
class RankedCandidate:
    node_id: str
    score: float


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    weight: float

    def __post_init__(self) -> None:
        _validate_weight(self.weight)


def _validate_weight(weight: float) -> None:
    if not math.isfinite(weight) or not 0 < weight <= 1:
        raise ValueError("edge weight must be finite and in (0, 1]")


def reciprocal_rank_fusion(
    arms: Sequence[Sequence[str]], *, rrf_k: int, weights: Sequence[float] | None = None
) -> tuple[RankedCandidate, ...]:
    scores: dict[str, float] = {}
    selected_weights = weights if weights is not None else (1.0,) * len(arms)
    for arm, weight in zip(arms, selected_weights, strict=True):
        for rank, node_id in enumerate(dict.fromkeys(arm), start=1):
            scores[node_id] = scores.get(node_id, 0.0) + weight / (rrf_k + rank)
    return tuple(
        RankedCandidate(node_id, score)
        for node_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    )


def ordinary_graph_activation(
    seed_ids: Sequence[str],
    *,
    entity_matches: Mapping[str, Iterable[str]],
    semantic_edges: Sequence[GraphEdge],
    causal_edges: Sequence[GraphEdge],
    entity_score_scale: float,
) -> tuple[RankedCandidate, ...]:
    """Score already bounded eligible joins from the original seed set."""
    seeds = set(seed_ids)
    entity = {
        node_id: math.tanh(len(set(entities)) * entity_score_scale)
        for node_id, entities in entity_matches.items()
    }
    semantic: dict[str, float] = {}
    for edge in semantic_edges:
        for source, target in (
            (edge.source_id, edge.target_id),
            (edge.target_id, edge.source_id),
        ):
            if source in seeds:
                semantic[target] = max(semantic.get(target, 0.0), edge.weight)
    outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in causal_edges:
        outgoing[edge.source_id].append(edge)
    causal: dict[str, float] = {}
    for seed in seeds:
        for first in outgoing[seed]:
            if first.target_id == seed:
                continue
            causal[first.target_id] = max(causal.get(first.target_id, 0.0), first.weight)
            for second in outgoing[first.target_id]:
                if second.target_id in {seed, first.target_id}:
                    continue
                causal[second.target_id] = max(
                    causal.get(second.target_id, 0.0), first.weight * second.weight
                )
    candidates = (
        RankedCandidate(
            node_id,
            entity.get(node_id, 0.0) + semantic.get(node_id, 0.0) + causal.get(node_id, 0.0),
        )
        for node_id in (entity.keys() | semantic.keys() | causal.keys()) - seeds
    )
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.node_id)))


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime
    start_inclusive: bool = True
    end_inclusive: bool = False

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("time bounds must include an explicit offset")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.start > self.end or (
            self.start == self.end and not (self.start_inclusive and self.end_inclusive)
        ):
            raise ValueError("time range must be nonempty")

    def contains(self, instant: datetime) -> bool:
        return (instant > self.start or (instant == self.start and self.start_inclusive)) and (
            instant < self.end or (instant == self.end and self.end_inclusive)
        )


def _validate_window(window: TimeRange) -> None:
    if not window.start_inclusive or window.end_inclusive or window.start >= window.end:
        raise ValueError("query window must be a nonempty half-open interval")


def matching_basis_date(annotations: Sequence[TimeRange], window: TimeRange) -> datetime | None:
    """Use the midpoint of the earliest annotation intersection with the window."""
    _validate_window(window)
    intersections: list[tuple[datetime, datetime]] = []
    for annotation in annotations:
        start = max(annotation.start, window.start)
        end = min(annotation.end, window.end)
        if start < end or (start == end and annotation.contains(start) and window.contains(start)):
            intersections.append((start, end))
    if not intersections:
        return None
    start, end = min(intersections)
    return start + (end - start) / 2


@dataclass(frozen=True)
class TemporalNode:
    node_id: str
    cosine: float
    basis_date: datetime

    def __post_init__(self) -> None:
        if not math.isfinite(self.cosine) or not -1 <= self.cosine <= 1:
            raise ValueError("query cosine must be finite and in [-1, 1]")
        if self.basis_date.utcoffset() is None:
            raise ValueError("basis date must include an explicit offset")
        object.__setattr__(self, "basis_date", self.basis_date.astimezone(UTC))


def select_temporal_seeds(
    nodes: Sequence[TemporalNode],
    *,
    window: TimeRange,
    seed_limit: int,
    bucket_count: int,
) -> tuple[TemporalNode, ...]:
    _validate_window(window)
    if seed_limit < 1 or bucket_count < 1:
        raise ValueError("seed and bucket limits must be positive")
    buckets: dict[int, list[TemporalNode]] = defaultdict(list)
    seen: set[str] = set()
    for node in sorted(nodes, key=lambda item: (-item.cosine, item.node_id)):
        if node.node_id in seen or not window.contains(node.basis_date):
            continue
        seen.add(node.node_id)
        bucket = int((node.basis_date - window.start) / (window.end - window.start) * bucket_count)
        buckets[bucket].append(node)
    ordered = sorted(buckets.values(), key=lambda group: (-group[0].cosine, group[0].node_id))
    result: list[TemporalNode] = []
    rank = 0
    while len(result) < seed_limit:
        added = False
        for bucket in ordered:
            if rank < len(bucket):
                result.append(bucket[rank])
                added = True
                if len(result) == seed_limit:
                    return tuple(result)
        if not added:
            break
        rank += 1
    return tuple(result)


@dataclass(frozen=True)
class TemporalEdge:
    target: TemporalNode
    weight: float
    relation: Literal["temporal", "caused_by"]
    edge_id: str = ""

    def __post_init__(self) -> None:
        _validate_weight(self.weight)
        if self.relation not in {"temporal", "caused_by"}:
            raise ValueError("unsupported temporal expansion relation")


@dataclass(frozen=True)
class TemporalHit:
    node: TemporalNode
    score: float
    propagated_score: float
    depth: int
    seed_id: str
    parent_id: str
    path: tuple[str, ...]
    path_edge_ids: tuple[str, ...]

    @property
    def node_id(self) -> str:
        return self.node.node_id


@dataclass(frozen=True)
class TemporalExpansion:
    discoveries: tuple[TemporalHit, ...]
    stop_reason: Literal["frontier_exhausted", "node_budget", "deadline"]


def _temporal_rank(hit: TemporalHit) -> tuple[float, int, float, str]:
    return (-hit.score, hit.depth, -hit.node.cosine, hit.node_id)


def _path_rank(hit: TemporalHit) -> tuple[float, float, str, str, tuple[str, ...]]:
    return (-hit.score, -hit.propagated_score, hit.seed_id, hit.parent_id, hit.path_edge_ids)


def temporal_spread(
    seeds: Sequence[TemporalNode],
    neighbors: Callable[[str, int], Sequence[TemporalEdge]],
    *,
    window: TimeRange,
    node_limit: int,
    neighbor_limit: int,
    deadline_reached: Callable[[], bool],
) -> TemporalExpansion:
    """Expand eligible, prefiltered neighbor rows; preserve exact selected paths.

    The callback applies authorization, generation, time witnesses and the query
    cosine floor before its cap. Node budgets include seeds. Expiration discards
    every proposal from the unfinished breadth-first level.
    """
    _validate_window(window)
    if node_limit < 1 or neighbor_limit < 1:
        raise ValueError("node and neighbor limits must be positive")
    seed_nodes: dict[str, TemporalNode] = {}
    for node in sorted(seeds, key=lambda item: (-item.cosine, item.node_id)):
        if window.contains(node.basis_date):
            seed_nodes.setdefault(node.node_id, node)
    if len(seed_nodes) > node_limit:
        raise ValueError("seed count exceeds the node limit")
    visited = set(seed_nodes)
    frontier = [
        TemporalHit(node, 1.0, 1.0, 0, node.node_id, node.node_id, (node.node_id,), ())
        for node in seed_nodes.values()
    ]
    discoveries: list[TemporalHit] = []
    midpoint = window.start + (window.end - window.start) / 2
    half_width = (window.end - window.start) / 2

    def finish(reason: Literal["frontier_exhausted", "node_budget", "deadline"]):
        return TemporalExpansion(tuple(sorted(discoveries, key=_temporal_rank)), reason)

    while frontier:
        if deadline_reached():
            return finish("deadline")
        if len(visited) == node_limit:
            return finish("node_budget")
        proposals: dict[str, TemporalHit] = {}
        for parent in sorted(frontier, key=lambda hit: hit.node_id):
            if deadline_reached():
                return finish("deadline")
            edges = neighbors(parent.node_id, neighbor_limit)
            for edge in edges[:neighbor_limit]:
                if deadline_reached():
                    return finish("deadline")
                target = edge.target
                if target.node_id in visited or not window.contains(target.basis_date):
                    continue
                date_score = 1 - min(abs(target.basis_date - midpoint) / half_width, 1.0)
                factor = CAUSAL_RELATION_FACTOR if edge.relation == "caused_by" else 1.0
                propagated = parent.score * edge.weight * TEMPORAL_PROPAGATION_DECAY * factor
                proposal = TemporalHit(
                    target,
                    max(date_score, propagated),
                    propagated,
                    parent.depth + 1,
                    parent.seed_id,
                    parent.node_id,
                    (*parent.path, target.node_id),
                    (*parent.path_edge_ids, edge.edge_id),
                )
                previous = proposals.get(target.node_id)
                if previous is None or _path_rank(proposal) < _path_rank(previous):
                    proposals[target.node_id] = proposal
        admitted = sorted(proposals.values(), key=_temporal_rank)[: node_limit - len(visited)]
        if deadline_reached():
            return finish("deadline")
        discoveries.extend(admitted)
        visited.update(hit.node_id for hit in admitted)
        if len(visited) == node_limit:
            return finish("node_budget")
        frontier = [hit for hit in admitted if hit.score > TEMPORAL_CONTINUATION_THRESHOLD]
    return finish("frontier_exhausted")
