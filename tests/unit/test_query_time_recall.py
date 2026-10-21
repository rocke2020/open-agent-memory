"""Natural-language time in the public recall service."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.models.runtime import Usage
from oam.retrieval.algorithms import TemporalExpansion, TemporalNode
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.service import RecallService, RecallUnavailable
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens

CONFIG = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config


class DatedRepository:
    def __init__(self, request):
        self.request = request
        self.rows = {
            "old": {"year": 2024, "text": "Apple harvest was good."},
            "new": {"year": 2025, "text": "Apple harvest was good."},
        }

    def user_state(self):
        return {
            "index_generation_id": "generation",
            "readiness": "ready",
            "parse_partial": False,
            "policy_epoch": 0,
        }

    def temporal_matches(self, unit_ids):
        window = self.request.time_window
        return tuple(
            name
            for name in unit_ids
            if window.start <= datetime(self.rows[name]["year"], 9, 23, tzinfo=UTC) < window.end
        )

    def lexical(self, query, limit):
        return [{"unit_id": name} for name in tuple(self.rows)[:limit]]

    def anchors(self, query, limit):
        return []

    def dense(self, vector, limit):
        return [
            {"unit_id": name, "kind": "passage", "score": 0.8} for name in tuple(self.rows)[:limit]
        ]

    def related_user_fact(self, unit_ids):
        return None

    def temporal_nodes(self, unit_ids, vector):
        return []

    def temporal_candidates(self, vector, limit):
        return ()

    def ordinary(self, seeds, vector, *, include_semantic=True):
        return (), {}

    def valid_paths(self, paths, vector):
        return paths

    def temporal_neighbors_batch(self, source_ids, vector, limit):
        return {source_id: () for source_id in source_ids}

    def hydrate(self, unit_ids):
        return [
            {
                "unit_id": name,
                "kind": "passage",
                "origin": "memory",
                "text": self.rows[name]["text"],
                "citations": [],
                "time_evidence": [],
                "locator": {},
            }
            for name in unit_ids
        ]

    def record_trace(self, trace_id, policy_epoch, decisions, result):
        assert policy_epoch == 0
        assert decisions["query_hash"]
        assert decisions["fusion"][0]["contributions"]
        assert decisions["packing"]["selected_ids"] == [item["unit_id"] for item in result["items"]]
        return trace_id


class DatedService(RecallService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield DatedRepository(request)


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("existing_seed", [False, True])
def test_window_pool_preserves_disconnected_evidence_with_existing_time_matches(
    mode, existing_seed
):
    query = "equipment maintenance in 2024"

    class WindowRepository(DatedRepository):
        def __init__(self, request):
            super().__init__(request)
            self.rows["late"] = {"year": 2024, "text": "A replacement lens arrived in December."}

        def lexical(self, query, limit):
            return [{"unit_id": "old"}, {"unit_id": "new"}]

        def dense(self, vector, limit):
            return [{"unit_id": "old", "kind": "fact", "score": 0.8}]

        def temporal_nodes(self, unit_ids, vector):
            return (
                (TemporalNode("old", 0.8, datetime(2024, 9, 23, tzinfo=UTC)),)
                if existing_seed
                else ()
            )

        def temporal_candidates(self, vector, limit):
            return (TemporalNode("late", 0.7, datetime(2024, 12, 10, tzinfo=UTC)),)

        def hydrate(self, unit_ids):
            rows = super().hydrate(unit_ids)
            for row in rows:
                row["citations"] = [
                    {"source_id": row["unit_id"], "source_version": 1, "role": "user"}
                ]
            return rows

        def source_user_passage_ids(self, *args):
            return []

        def supported_fact_ids(self, *args):
            return []

    class WindowService(DatedService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield WindowRepository(request)

    class CompleteQueryEmbedding(EmbeddingOnly):
        def embed(self, texts, **kwargs):
            assert texts == (query,)
            return super().embed(texts, **kwargs)

    result = WindowService(
        CONFIG, None, CompleteQueryEmbedding(), generation_id="generation"
    ).search(object(), SearchRequest(include={"chunks": {}}, query=query, recall_level=mode))

    items = {item["unit_id"]: item for item in result["items"]}
    assert set(items) == {"old", "new", "late"}
    assert items["late"]["text"] == "A replacement lens arrived in December."
    assert items["late"]["citations"] == [
        {"source_id": "late", "source_version": 1, "role": "user"}
    ]
    assert items["late"]["paths"] == [
        {"arm": "temporal", "relation": "time_match", "nodes": ["late"]}
    ]
    assert result["diagnostics"]["generative_calls"] == 0


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("existing", ["none", "matched", "seed", "failed"])
def test_temporal_window_pool_is_independent_of_direct_matches_and_roots(mode, existing):
    calls = []

    class RecoveryRepository(DatedRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "new"}]

        def dense(self, vector, limit):
            return [{"unit_id": "new", "kind": "fact", "score": 0.8}]

        def temporal_matches(self, unit_ids):
            return ("old",) if existing == "matched" else ()

        def temporal_nodes(self, unit_ids, vector):
            if existing == "failed":
                raise RuntimeError("temporal qualification failed")
            return (
                (TemporalNode("old", 0.8, datetime(2024, 9, 23, tzinfo=UTC)),)
                if existing == "seed"
                else ()
            )

        def temporal_candidates(self, vector, limit):
            assert existing != "failed"
            assert self.request.query == "apple harvest in 2024"
            assert limit == CONFIG.search.fast.channel_candidates
            calls.append("recovery")
            return (TemporalNode("old", 0.8, datetime(2024, 9, 23, tzinfo=UTC)),)

    class RecoveryService(DatedService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RecoveryRepository(request)

    result = RecoveryService(CONFIG, None, EmbeddingOnly(), generation_id="generation").search(
        object(),
        SearchRequest(
            include={"chunks": {}},
            query="apple harvest in 2024",
            recall_level=mode,
            allow_partial=existing == "failed",
        ),
    )

    assert calls == ([] if existing == "failed" else ["recovery"])
    if existing in {"none", "matched"}:
        old = next(item for item in result["items"] if item["unit_id"] == "old")
        assert old["paths"][0]["arm"] == "temporal"
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["diagnostics"]["degraded"] == (["temporal"] if existing == "failed" else [])


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_window_pool_failure_does_not_publish_existing_temporal_votes(mode, allow_partial):
    class FailedPoolRepository(DatedRepository):
        def dense(self, vector, limit):
            return [{"unit_id": "old", "kind": "fact", "score": 0.8}]

        def temporal_nodes(self, unit_ids, vector):
            return (TemporalNode("old", 0.8, datetime(2024, 9, 23, tzinfo=UTC)),)

        def temporal_candidates(self, vector, limit):
            raise RuntimeError("window pool unavailable")

    class FailedPoolService(DatedService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield FailedPoolRepository(request)

    service = FailedPoolService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        include={"chunks": {}},
        query="apple harvest in 2024",
        recall_level=mode,
        allow_partial=allow_partial,
    )
    if not allow_partial:
        with pytest.raises(RecallUnavailable, match="temporal stage failed"):
            service.search(object(), request)
        return

    result = service.search(object(), request)
    assert {item["unit_id"] for item in result["items"]} == {"old", "new"}
    assert result["diagnostics"]["partial"] is True
    assert result["diagnostics"]["degraded"] == ["temporal"]
    assert "temporal" not in result["diagnostics"]["executed_arms"]
    assert all(not item["paths"] for item in result["items"])
    assert result["diagnostics"]["generative_calls"] == 0


def test_recall_rejects_policy_change_after_hydration():
    class ChangedRepository(DatedRepository):
        def user_state(self):
            state = super().user_state()
            state["policy_epoch"] = service.epoch
            return state

        def hydrate(self, unit_ids):
            result = super().hydrate(unit_ids)
            service.epoch = 1
            return result

        def record_trace(self, trace_id, policy_epoch, decisions, result):
            return policy_epoch == service.epoch

    class ChangedService(RecallService):
        epoch = 0

        @contextmanager
        def _repository(self, authority, request, deadline):
            yield ChangedRepository(request)

    service = ChangedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")

    with pytest.raises(RecallUnavailable, match="policy"):
        service.search(
            object(), SearchRequest(include={"chunks": {}}, query="apple", recall_level="fast")
        )


class EmbeddingOnly:
    def embed(self, texts, **kwargs):
        return SimpleNamespace(vectors=((0.1,),), usage=Usage(coverage="complete"))


@pytest.mark.parametrize(
    "query",
    [
        "apple harvest in 2025",
        "2025年苹果收成",
        "2025年のりんご収穫",
        "apple harvest last year",
        "去年苹果收成",
        "去年のりんご収穫",
        "apple harvest 1 year ago",
    ],
)
def test_fast_recall_ranks_date_match_without_excluding_ordinary_candidates(query):
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")

    result = service.search(
        object(),
        SearchRequest(
            include={"chunks": {}},
            query=query,
            recall_level="fast",
            reference_time="2026-09-23T12:00:00Z",
        ),
    )

    assert [item["unit_id"] for item in result["items"]] == ["new", "old"]
    assert result["items"][0]["paths"][0]["arm"] == "temporal"
    assert result["diagnostics"]["generative_calls"] == 0


def test_recall_returns_a_recorded_trace_identity():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")

    result = service.search(
        object(), SearchRequest(include={"chunks": {}}, query="apple", recall_level="fast")
    )

    assert result["trace_id"].startswith("trace_")


def test_explicit_window_overrides_date_in_query():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        include={"chunks": {}},
        query="apple harvest in 2025",
        recall_level="fast",
        time_basis="event",
        time_window={"start": "2024-01-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"},
    )

    result = service.search(object(), request)

    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["query_time"]["status"] == "explicit"


def test_normal_keeps_temporal_rank_and_out_of_window_direct_evidence():
    service = DatedService(
        CONFIG, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
    )
    request = SearchRequest(
        include={"chunks": {}},
        query="apple harvest in 2025",
        recall_level="normal",
        reference_time="2026-09-23T12:00:00Z",
    )

    result = service.search(object(), request)

    assert [item["unit_id"] for item in result["items"]] == ["new", "old"]
    assert result["diagnostics"]["executed_arms"] == [
        "lexical",
        "dense",
        "graph",
        "temporal",
    ]


def test_ambiguous_query_retains_ordinary_recall_with_diagnostic():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        include={"chunks": {}},
        query="Compare apple harvest in 2024 and 2025",
        recall_level="fast",
    )

    result = service.search(object(), request)

    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["query_time"]["status"] == "ambiguous_time_expression"
    assert "temporal" in result["diagnostics"]["omitted_stages"]


def test_context_reports_estimated_tokens_of_complete_rendered_blocks():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    result = service.search(
        object(),
        SearchRequest(include={"chunks": {}}, query="apple", recall_level="fast", max_tokens=64),
    )
    assert result["tokenizer"] == ESTIMATOR
    assert result["context_tokens"] == estimated_tokens(estimate_units(result["context"]))
    assert len(result["items"]) == 2


def test_query_budget_uses_unicode_estimate():
    config = CONFIG.model_copy(
        update={"search": CONFIG.search.model_copy(update={"max_query_tokens": 2})}
    )
    service = DatedService(config, None, EmbeddingOnly(), generation_id="generation")
    with pytest.raises(ValueError, match="query exceeds"):
        service.search(
            object(), SearchRequest(include={"chunks": {}}, query="甲乙", recall_level="fast")
        )


@pytest.mark.parametrize(
    "query,expected_start,expected_end",
    [
        ("March 1, 2025 harvest", "2025-03-01T00:00:00+00:00", "2025-03-02T00:00:00+00:00"),
        ("昨年の収穫", "2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        ("再来週の予定", "2026-10-05T00:00:00+00:00", "2026-10-12T00:00:00+00:00"),
        ("2025-09-23 14:30 harvest", "2025-09-23T14:30:00+00:00", "2025-09-23T14:31:00+00:00"),
        ("2025-09-23T14:30:45Z harvest", "2025-09-23T14:30:45+00:00", "2025-09-23T14:30:46+00:00"),
        ("2025年9月23日14時30分の収穫", "2025-09-23T14:30:00+00:00", "2025-09-23T14:31:00+00:00"),
    ],
)
def test_query_time_uses_exact_calendar_precision(query, expected_start, expected_end):
    parsed = infer_query_time(query, datetime(2026, 9, 23, tzinfo=UTC), "UTC")

    assert parsed.window.start.isoformat() == expected_start
    assert parsed.window.end.isoformat() == expected_end


@pytest.mark.parametrize(
    "expression,expected_start,expected_end",
    [
        ("last  week", "2026-09-14T00:00:00+00:00", "2026-09-21T00:00:00+00:00"),
        ("this\nmonth", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("next\tyear", "2027-01-01T00:00:00+00:00", "2028-01-01T00:00:00+00:00"),
    ],
)
def test_relative_dates_accept_whitespace_and_preserve_source_expression(
    expression, expected_start, expected_end
):
    parsed = infer_query_time(
        f"What happened {expression}?", datetime(2026, 9, 23, tzinfo=UTC), "UTC"
    )

    assert parsed.status == "inferred"
    assert parsed.window.start.isoformat() == expected_start
    assert parsed.window.end.isoformat() == expected_end
    assert parsed.expression == expression


def test_local_day_bounds_convert_separately_across_daylight_saving_change():
    parsed = infer_query_time(
        "2025-03-09 appointments",
        datetime(2025, 3, 10, tzinfo=UTC),
        "America/Los_Angeles",
    )

    assert parsed.window.start.isoformat() == "2025-03-09T08:00:00+00:00"
    assert parsed.window.end.isoformat() == "2025-03-10T07:00:00+00:00"


def test_person_name_without_date_signal_stays_unfiltered():
    parsed = infer_query_time(
        "May preferred the apple harvest", datetime(2026, 9, 23, tzinfo=UTC), "UTC"
    )

    assert parsed.status == "no_time_expression"
    assert parsed.window is None


@pytest.mark.parametrize(
    "query",
    ["harvest 2025-09-23T25:30", "2025年9月23日25時30分の収穫"],
)
def test_invalid_recognized_clock_time_does_not_broaden_to_whole_day(query):
    parsed = infer_query_time(query, datetime(2026, 9, 23, tzinfo=UTC), "UTC")
    assert parsed.window is None
    assert parsed.status == "invalid_time_expression"


def test_ambiguous_query_date_returns_diagnostic_without_a_window():
    parsed = infer_query_time("harvest in 2024 and 2025", datetime(2026, 9, 23, tzinfo=UTC), "UTC")
    assert parsed.window is None
    assert parsed.status == "ambiguous_time_expression"


def test_fast_temporal_deadline_cannot_report_complete_recall(monkeypatch):
    monkeypatch.setattr(
        "oam.retrieval.service.temporal_spread",
        lambda *args, **kwargs: TemporalExpansion((), "deadline"),
    )
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")

    with pytest.raises(RecallUnavailable, match="temporal"):
        service.search(
            object(),
            SearchRequest(
                include={"chunks": {}}, query="apple harvest in 2025", recall_level="fast"
            ),
        )

    partial = service.search(
        object(),
        SearchRequest(
            include={"chunks": {}},
            query="apple harvest in 2025",
            recall_level="fast",
            allow_partial=True,
        ),
    )
    assert {item["unit_id"] for item in partial["items"]} == {"new", "old"}
    assert partial["diagnostics"]["temporal_stop"] == "deadline"
    assert "temporal_deadline" in partial["diagnostics"]["degraded"]


@pytest.mark.parametrize("allow_partial", [False, True])
def test_normal_graph_failure_obeys_partial_policy(allow_partial):
    class FailedGraphRepository(DatedRepository):
        def ordinary(self, seeds, vector, *, include_semantic=True):
            raise RuntimeError("expansion failed")

    class FailedGraphService(RecallService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield FailedGraphRepository(request)

    service = FailedGraphService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        include={"chunks": {}}, query="apple", recall_level="normal", allow_partial=allow_partial
    )
    if allow_partial:
        result = service.search(object(), request)
        assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
        assert result["diagnostics"]["degraded"] == ["graph"]
        assert result["diagnostics"]["executed_arms"] == ["lexical", "dense"]
    else:
        with pytest.raises(RecallUnavailable, match="graph stage failed"):
            service.search(object(), request)


class DirectEvidenceRepository(DatedRepository):
    def lexical(self, query, limit):
        return []

    def dense(self, vector, limit):
        return [{"unit_id": "z_direct", "kind": "fact", "score": 0.9}]

    def ordinary(self, seeds, vector, *, include_semantic=True):
        return ("a_graph",), {}

    def hydrate(self, unit_ids):
        return [
            {
                "unit_id": unit_id,
                "kind": "fact",
                "origin": "memory",
                "text": unit_id,
                "citations": [],
                "time_evidence": [],
                "locator": {},
            }
            for unit_id in unit_ids
        ]


class DirectEvidenceService(RecallService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield DirectEvidenceRepository(request)


def test_normal_equal_arm_weights_use_stable_id_for_dense_graph_tie():
    equal_search = CONFIG.search.model_copy(
        update={
            "normal_arm_weights": CONFIG.search.normal_arm_weights.model_copy(update={"graph": 1.0})
        }
    )
    equal_config = CONFIG.model_copy(update={"search": equal_search})
    service = DirectEvidenceService(
        equal_config, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
    )

    result = service.search(
        object(), SearchRequest(include={"chunks": {}}, query="apple", recall_level="normal")
    )

    assert [item["unit_id"] for item in result["items"]] == ["a_graph", "z_direct"]
    assert result["diagnostics"]["executed_arms"] == ["lexical", "dense", "graph"]


class OverlappingGraphRepository(DirectEvidenceRepository):
    def lexical(self, query, limit):
        return [{"unit_id": "a_relevant", "score": 1.0}]

    def dense(self, vector, limit):
        return [
            {"unit_id": "z_seed", "kind": "fact", "score": 0.9},
            {"unit_id": "z_unrelated", "kind": "fact", "score": 0.2},
        ]

    def ordinary(self, seeds, vector, *, include_semantic=True):
        assert seeds == ("z_seed",)
        return ("z_unrelated",), {}


class OverlappingGraphService(RecallService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield OverlappingGraphRepository(request)


def test_graph_and_dense_each_vote_for_a_shared_candidate():
    service = OverlappingGraphService(
        CONFIG, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
    )

    result = service.search(
        object(), SearchRequest(include={"chunks": {}}, query="apple", recall_level="normal")
    )

    assert [item["unit_id"] for item in result["items"]][:2] == ["z_unrelated", "a_relevant"]
    expected_arms = ["lexical", "dense", "graph"]
    assert result["diagnostics"]["executed_arms"] == expected_arms


def test_normal_partial_embedding_failure_keeps_lexical_arm_weight():
    class FailedEmbedding:
        def embed(self, texts, **kwargs):
            raise RuntimeError("embedding unavailable")

    service = DatedService(CONFIG, None, FailedEmbedding(), generation_id="generation")

    result = service.search(
        object(),
        SearchRequest(
            include={"chunks": {}}, query="apple", recall_level="normal", allow_partial=True
        ),
    )

    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["executed_arms"] == ["lexical"]
    assert result["diagnostics"]["degraded"] == ["embedding"]


class ConversationRepository(DatedRepository):
    def lexical(self, query, limit):
        return [{"unit_id": "z_user"}, {"unit_id": "z_reply"}]

    def dense(self, vector, limit):
        return [
            {"unit_id": "a_noise", "kind": "fact", "score": 0.9},
            {"unit_id": "b_noise", "kind": "fact", "score": 0.8},
        ]

    def ordinary(self, seeds, vector, *, include_semantic=True):
        return ("a_noise",), {}

    def hydrate(self, unit_ids):
        locators = {
            "z_user": {
                "source_id": "conversation",
                "source_version": 1,
                "role": "user",
                "message_index": 0,
            },
            "z_reply": {
                "source_id": "conversation",
                "source_version": 1,
                "role": "assistant",
                "message_index": 1,
            },
        }
        return [
            {
                "unit_id": unit_id,
                "kind": "passage" if unit_id.startswith("z_") else "fact",
                "origin": "memory",
                "text": unit_id,
                "citations": [],
                "time_evidence": [],
                "locator": locators.get(unit_id, {}),
            }
            for unit_id in unit_ids
        ]


class ConversationService(RecallService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield ConversationRepository(request)


def test_normal_context_respects_fused_relevance_under_item_cap():
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(
                update={"normal": CONFIG.search.normal.model_copy(update={"max_results": 2})}
            )
        }
    )
    service = ConversationService(
        config, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
    )

    result = service.search(
        object(),
        SearchRequest(include={"chunks": {}}, query="recommend something", recall_level="normal"),
    )

    assert [item["unit_id"] for item in result["items"]] == [
        "a_noise",
        "b_noise",
        "z_user",
        "z_reply",
    ]
    assert result["diagnostics"]["executed_arms"] == ["lexical", "dense", "graph"]


@pytest.mark.parametrize(
    "query,start,end,expression",
    [
        (
            "Summarize the workshop from 2024-08-01 to 2024-10-22.",
            "2024-08-01T00:00:00+00:00",
            "2024-10-23T00:00:00+00:00",
            "2024-08-01 to 2024-10-22",
        ),
        (
            "Purchases between July 2024 and September 2024",
            "2024-07-01T00:00:00+00:00",
            "2024-10-01T00:00:00+00:00",
            "July 2024 and September 2024",
        ),
        (
            "Travel 2024-07-01–2024-07-29",
            "2024-07-01T00:00:00+00:00",
            "2024-07-30T00:00:00+00:00",
            "2024-07-01–2024-07-29",
        ),
        (
            "Employment from 2023 to 2025",
            "2023-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2023 to 2025",
        ),
        (
            "总结2024年8月1日到2024年10月22日的采购",
            "2024-08-01T00:00:00+00:00",
            "2024-10-23T00:00:00+00:00",
            "2024年8月1日到2024年10月22日",
        ),
        (
            "2024年7月1日から2024年7月29日までの旅行",
            "2024-07-01T00:00:00+00:00",
            "2024-07-30T00:00:00+00:00",
            "2024年7月1日から2024年7月29日",
        ),
        (
            "Harvest 2024-07 to 2024-07",
            "2024-07-01T00:00:00+00:00",
            "2024-08-01T00:00:00+00:00",
            "2024-07 to 2024-07",
        ),
    ],
)
def test_explicit_calendar_range_includes_final_calendar_period(query, start, end, expression):
    parsed = infer_query_time(query, datetime(2026, 10, 13, tzinfo=UTC), "UTC")

    assert parsed.status == "inferred"
    assert parsed.window.start.isoformat() == start
    assert parsed.window.end.isoformat() == end
    assert parsed.expression == expression


def test_calendar_range_computes_both_local_boundaries_across_dst():
    parsed = infer_query_time(
        "Trips from 2025-03-08 to 2025-03-09",
        datetime(2026, 10, 13, tzinfo=UTC),
        "America/Los_Angeles",
    )

    assert parsed.window.start.isoformat() == "2025-03-08T08:00:00+00:00"
    assert parsed.window.end.isoformat() == "2025-03-10T07:00:00+00:00"


@pytest.mark.parametrize(
    "query",
    [
        "Compare 2024-07-01 and 2024-07-29",
        "Compare 2024-07-01 to 2024-07-29",
        "2024-10-22 to October 2024",
        "Travel on 2024-07-01 or 2024-07-29",
        "From 2024-07-29 to 2024-07-01",
        "From 2024-08 to 2024-07",
        "From 2024-07-01 to 2024-07-29, compared with 2025-01-01",
        "Between 2024-07-01 and 2024-07-29 or 2024-08-01",
        "2024年7月1日から2024年7月29日の予定",
        "2024-07-01T10:00Z to 2024-07-01T11:00Z",
        "2024-07-01T10:00Z to 2024-07-01T10:00Z",
        "last month to this month",
    ],
)
def test_unrelated_or_unsupported_ranges_do_not_create_a_window(query):
    parsed = infer_query_time(query, datetime(2026, 10, 13, tzinfo=UTC), "UTC")

    assert parsed.window is None
    assert parsed.status == "ambiguous_time_expression"


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_calendar_range_recall_preserves_query_and_outside_evidence(mode):
    query = "Apple harvest from 2025-09-01 to 2025-09-30"

    class RecordingEmbedding(EmbeddingOnly):
        def embed(self, texts, **kwargs):
            assert texts == (query,)
            return super().embed(texts, **kwargs)

    service = DatedService(CONFIG, None, RecordingEmbedding(), generation_id="generation")
    result = service.search(
        object(), SearchRequest(include={"chunks": {}}, query=query, recall_level=mode)
    )

    assert result["diagnostics"]["query_time"]["window"] == {
        "start": "2025-09-01T00:00:00Z",
        "end": "2025-10-01T00:00:00Z",
    }
    assert result["diagnostics"]["query_time"]["basis"] == "event"
    assert "temporal" in result["diagnostics"]["executed_arms"]
    assert result["items"][0]["unit_id"] == "new"
    assert {item["unit_id"] for item in result["items"]} == {"new", "old"}
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["diagnostics"]["degraded"] == []
