from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def test_rank_fusion_counts_each_arm_once_and_breaks_ties_by_id() -> None:
    from oam.retrieval.algorithms import reciprocal_rank_fusion

    result = reciprocal_rank_fusion((("b", "b", "a"), ("a", "b")), rrf_k=1)

    assert [item.node_id for item in result] == ["a", "b"]
    assert [item.score for item in result] == pytest.approx([5 / 6, 5 / 6])


def test_mid_fusion_keeps_a_lexical_only_candidate_ahead_of_graph_only_results() -> None:
    from oam.config import load_config
    from oam.retrieval.algorithms import reciprocal_rank_fusion

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    target = "user_preference"
    lexical = (target, *(f"lexical_{index}" for index in range(47)))
    dense = tuple(f"dense_{index}" for index in range(48))
    graph = tuple(f"graph_{index}" for index in range(32))
    weights = config.search.mid_arm_weights

    ranked = reciprocal_rank_fusion(
        (lexical, dense, graph),
        rrf_k=config.search.rrf_k,
        weights=(weights.lexical, weights.dense, weights.graph),
    )

    ids = [item.node_id for item in ranked]
    assert ids.index(target) < ids.index("graph_0")
    assert target in {item.node_id for item in ranked[: config.search.mid.max_results]}


def test_ordinary_causal_expansion_follows_two_outgoing_edges_only() -> None:
    from oam.retrieval.algorithms import GraphEdge, ordinary_graph_activation

    result = ordinary_graph_activation(
        ("move",),
        entity_matches={},
        semantic_edges=(),
        causal_edges=(
            GraphEdge("move", "rent", 0.8),
            GraphEdge("rent", "job", 0.5),
            GraphEdge("job", "other", 1.0),
            GraphEdge("effect", "move", 1.0),
            GraphEdge("rent", "move", 1.0),
        ),
    )

    assert [item.node_id for item in result] == ["rent", "job"]
    assert [item.score for item in result] == pytest.approx([0.8, 0.4])


def test_graph_activation_uses_distinct_entities_and_maximum_paths() -> None:
    from oam.retrieval.algorithms import GraphEdge, ordinary_graph_activation

    result = ordinary_graph_activation(
        ("seed", "other_seed"),
        entity_matches={"target": ("home", "home", "work")},
        semantic_edges=(
            GraphEdge("seed", "target", 0.3),
            GraphEdge("target", "other_seed", 0.8),
            GraphEdge("target", "beyond", 1.0),
        ),
        causal_edges=(
            GraphEdge("seed", "target", 0.3),
            GraphEdge("other_seed", "via", 0.8),
            GraphEdge("via", "target", 0.75),
        ),
    )

    assert [item.node_id for item in result] == ["target", "via"]
    assert result[0].score == pytest.approx(2.2)
    assert result[1].score == pytest.approx(0.8)


@pytest.mark.parametrize(("count", "expected"), [(1, 0.5), (2, 0.8), (3, 0.9)])
def test_graph_entity_score_uses_saturating_square(count: int, expected: float) -> None:
    from oam.retrieval.algorithms import ordinary_graph_activation

    result = ordinary_graph_activation(
        ("seed",),
        entity_matches={"target": tuple(f"entity_{index}" for index in range(count))},
        semantic_edges=(),
        causal_edges=(),
    )

    assert result[0].score == pytest.approx(expected)


@pytest.mark.parametrize("weight", [0.0, -0.1, 1.01, float("nan"), float("inf")])
def test_stored_graph_weights_reject_invalid_activation(weight: float) -> None:
    from oam.retrieval.algorithms import GraphEdge

    with pytest.raises(ValueError, match="weight"):
        GraphEdge("seed", "target", weight)


def _day(offset: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)


@pytest.mark.parametrize(
    ("start", "end", "end_inclusive", "expected"),
    [
        (0, 0, True, 0),
        (10, 10, True, None),
        (-2, 0, False, None),
        (-2, 0, True, 0),
        (-2, 4, False, 2),
        (8, 20, False, 9),
    ],
)
def test_strict_window_handles_instants_and_intersection_midpoints(
    start: int, end: int, end_inclusive: bool, expected: int | None
) -> None:
    from oam.retrieval.algorithms import TimeRange, matching_basis_date

    result = matching_basis_date(
        (TimeRange(_day(start), _day(end), end_inclusive=end_inclusive),),
        TimeRange(_day(0), _day(10)),
    )

    assert result == (None if expected is None else _day(expected))


def test_basis_date_uses_earliest_matching_annotation_intersection() -> None:
    from oam.retrieval.algorithms import TimeRange, matching_basis_date

    result = matching_basis_date(
        (
            TimeRange(_day(8), _day(9)),
            TimeRange(_day(-10), _day(-1)),
            TimeRange(_day(-1), _day(3)),
        ),
        TimeRange(_day(0), _day(10)),
    )

    assert result == _day(1.5)


def test_basis_date_measures_actual_time_across_a_daylight_saving_transition() -> None:
    from oam.retrieval.algorithms import TimeRange, matching_basis_date

    local = ZoneInfo("America/New_York")
    window = TimeRange(datetime(2026, 3, 8, tzinfo=local), datetime(2026, 3, 9, tzinfo=local))

    result = matching_basis_date((window,), window)

    assert result == datetime(2026, 3, 8, 16, 30, tzinfo=UTC)


def test_seed_selection_covers_time_buckets_without_duplicate_seed_votes() -> None:
    from oam.retrieval.algorithms import TemporalNode, TimeRange, select_temporal_seeds

    result = select_temporal_seeds(
        (
            TemporalNode("early_b", 0.98, _day(2)),
            TemporalNode("early_a", 0.99, _day(1)),
            TemporalNode("middle", 0.90, _day(6)),
            TemporalNode("late", 0.8, _day(9)),
            TemporalNode("early_a", 0.99, _day(1)),
            TemporalNode("outside", 1.0, _day(10)),
        ),
        window=TimeRange(_day(0), _day(10)),
        seed_limit=3,
        bucket_count=2,
    )

    assert [node.node_id for node in result] == ["early_a", "middle", "early_b"]


def _chain(*, date: float = 5):
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode

    nodes = [TemporalNode(str(index), 0.6, _day(date)) for index in range(8)]
    edges = {
        str(index): (TemporalEdge(nodes[index + 1], 1.0, "temporal", f"edge{index}"),)
        for index in range(7)
    }
    return nodes, edges


def _spread(seeds, neighbors, *, node_limit=20, neighbor_limit=5, deadline=lambda: False):
    from oam.retrieval.algorithms import TimeRange, temporal_spread

    return temporal_spread(
        seeds,
        lambda sources, limit: {source: neighbors(source, limit) for source in sources},
        window=TimeRange(_day(0), _day(10)),
        node_limit=node_limit,
        neighbor_limit=neighbor_limit,
        deadline_reached=deadline,
    )


def test_temporal_date_refresh_reaches_six_hops_without_seed_arm_vote() -> None:
    nodes, edges = _chain()
    edges["7"] = edges["0"]

    result = _spread((nodes[0], nodes[0]), lambda source, limit: edges.get(source, ())[:limit])

    assert [hit.node_id for hit in result.discoveries] == ["1", "2", "3", "4", "5", "6", "7"]
    sixth = result.discoveries[5]
    assert sixth.depth == 6
    assert sixth.path == ("0", "1", "2", "3", "4", "5", "6")
    assert sixth.path_edge_ids == ("edge0", "edge1", "edge2", "edge3", "edge4", "edge5")
    assert sixth.score == 1.0
    assert result.stop_reason == "frontier_exhausted"


def test_temporal_decay_admits_last_node_but_does_not_expand_it() -> None:
    nodes, edges = _chain(date=0)

    result = _spread((nodes[0],), lambda source, limit: edges.get(source, ())[:limit])

    assert [hit.node_id for hit in result.discoveries] == ["1", "2", "3", "4", "5"]
    assert [hit.score for hit in result.discoveries] == pytest.approx(
        [0.7, 0.49, 0.343, 0.2401, 0.16807]
    )


@pytest.mark.parametrize(
    ("weight", "expected_ids"), [(0.28571428571428575, ["1"]), (0.3, ["1", "2"])]
)
def test_exact_continuation_threshold_is_not_an_admission_threshold(weight, expected_ids) -> None:
    from oam.retrieval.algorithms import TemporalEdge

    nodes, edges = _chain(date=0)
    edges["0"] = (TemporalEdge(nodes[1], weight, "temporal"),)

    result = _spread((nodes[0],), lambda source, limit: edges.get(source, ())[:limit])

    assert [hit.node_id for hit in result.discoveries] == expected_ids
    assert result.discoveries[0].score == pytest.approx(0.2 if len(expected_ids) == 1 else 0.21)


def test_temporal_hidden_bridge_cannot_reach_visible_descendant() -> None:
    nodes, edges = _chain()

    def eligible_neighbors(source, limit):
        return tuple(edge for edge in edges.get(source, ()) if edge.target.node_id != "2")[:limit]

    result = _spread((nodes[0],), eligible_neighbors)

    assert [hit.node_id for hit in result.discoveries] == ["1"]


def test_temporal_level_chooses_best_path_then_stable_parent() -> None:
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode

    seed = TemporalNode("seed", 1.0, _day(5))
    a, b, target = (TemporalNode(name, 0.8, _day(5)) for name in ("a", "b", "target"))
    edges = {
        "seed": (TemporalEdge(b, 1.0, "temporal", "sb"), TemporalEdge(a, 1.0, "temporal", "sa")),
        "a": (TemporalEdge(target, 0.5, "temporal", "at"),),
        "b": (TemporalEdge(target, 1.0, "caused_by", "bt"),),
    }

    result = _spread((seed,), lambda source, limit: edges.get(source, ())[:limit])

    hit = next(hit for hit in result.discoveries if hit.node_id == "target")
    assert hit.path == ("seed", "b", "target")
    assert hit.path_edge_ids == ("sb", "bt")
    assert hit.propagated_score == pytest.approx(0.77)
    edges["a"] = (TemporalEdge(target, 1.0, "caused_by", "at"),)
    tied = _spread((seed,), lambda source, limit: tuple(reversed(edges.get(source, ())))[:limit])
    assert next(hit for hit in tied.discoveries if hit.node_id == "target").path == (
        "seed",
        "a",
        "target",
    )


def test_temporal_keeps_earliest_discovery_when_later_path_is_stronger() -> None:
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode

    seed = TemporalNode("seed", 1.0, _day(5))
    via = TemporalNode("via", 1.0, _day(5))
    target = TemporalNode("target", 1.0, _day(0))
    edges = {
        "seed": (
            TemporalEdge(target, 0.1, "temporal", "st"),
            TemporalEdge(via, 1, "temporal", "sv"),
        ),
        "via": (TemporalEdge(target, 1.0, "caused_by", "vt"),),
    }

    result = _spread((seed,), lambda source, limit: edges.get(source, ())[:limit])

    hit = next(hit for hit in result.discoveries if hit.node_id == "target")
    assert hit.depth == 1
    assert hit.path == ("seed", "target")
    assert hit.score == pytest.approx(0.07)


def test_temporal_discards_an_unfinished_level_on_deadline() -> None:
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode

    nodes = {name: TemporalNode(name, 0.8, _day(5)) for name in ("seed", "a", "b", "c", "d")}
    edges = {
        "seed": (TemporalEdge(nodes["a"], 1, "temporal"), TemporalEdge(nodes["b"], 1, "temporal")),
        "a": (TemporalEdge(nodes["c"], 1, "temporal"),),
        "b": (TemporalEdge(nodes["d"], 1, "temporal"),),
    }
    expired = False

    def neighbors(source, limit):
        nonlocal expired
        if source == "b":
            expired = True
        return edges.get(source, ())[:limit]

    result = _spread((nodes["seed"],), neighbors, deadline=lambda: expired)

    assert [hit.node_id for hit in result.discoveries] == ["a", "b"]
    assert result.stop_reason == "deadline"


def test_temporal_applies_neighbor_and_total_node_caps_before_further_work() -> None:
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode

    seed, a, b, c = (TemporalNode(name, 0.9, _day(5)) for name in ("seed", "a", "b", "c"))
    edges = (
        TemporalEdge(a, 1, "temporal"),
        TemporalEdge(b, 1, "temporal"),
        TemporalEdge(c, 1, "temporal"),
    )

    result = _spread((seed,), lambda source, limit: edges, neighbor_limit=2, node_limit=2)

    assert [hit.node_id for hit in result.discoveries] == ["a"]
    assert result.stop_reason == "node_budget"


def test_temporal_frontier_batches_preserve_exact_paths_and_discard_expired_level():
    from oam.retrieval.algorithms import TemporalEdge, TemporalNode, TimeRange, temporal_spread

    seed, a, b, target = (TemporalNode(name, 0.8, _day(5)) for name in ("s", "a", "b", "t"))
    edges = {
        "s": (TemporalEdge(b, 1.0, "temporal", "sb"), TemporalEdge(a, 1.0, "temporal", "sa")),
        "a": (TemporalEdge(target, 0.9, "temporal", "at"),),
        "b": (TemporalEdge(target, 0.8, "caused_by", "bt"),),
    }
    expected = _spread((seed,), lambda source, limit: edges.get(source, ())[:limit])
    calls = []
    expired = False

    def batch(sources, limit):
        nonlocal expired
        calls.append(sources)
        if sources == ("a", "b") and expire:
            expired = True
        return {source: edges.get(source, ())[:limit] for source in sources}

    def deadline():
        return expired

    for expire in (False, True):
        calls.clear()
        expired = False
        result = temporal_spread(
            (seed,),
            batch,
            window=TimeRange(_day(0), _day(10)),
            node_limit=20,
            neighbor_limit=5,
            deadline_reached=deadline,
        )
        assert calls[:2] == [("s",), ("a", "b")]
        if expire:
            assert result.stop_reason == "deadline"
            assert [hit.node_id for hit in result.discoveries] == ["a", "b"]
        else:
            assert result == expected
            assert result.discoveries[-1].path_edge_ids == ("sa", "at")
            assert calls == [("s",), ("a", "b"), ("t",)]
