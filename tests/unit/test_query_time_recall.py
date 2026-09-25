"""Natural-language time in the public recall service."""

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.models.runtime import Usage
from oam.retrieval.algorithms import TemporalExpansion
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.service import RecallService, RecallUnavailable

CONFIG = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config


class DatedRepository:
    def __init__(self, request):
        self.request = request
        self.rows = {
            "old": {"year": 2024, "text": "Apple harvest was good."},
            "new": {"year": 2025, "text": "Apple harvest was good."},
        }

    def user_state(self):
        return {"index_generation_id": "generation", "readiness": "ready"}

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

    def valid_paths(self, paths, vector):
        return paths

    def temporal_nodes(self, unit_ids, vector):
        return []

    def temporal_neighbors(self, source_id, vector, limit):
        return []

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


class DatedService(RecallService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield DatedRepository(request)


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
        SearchRequest(query=query, recall_level="fast", reference_time="2026-09-23T12:00:00Z"),
    )

    assert [item["unit_id"] for item in result["items"]] == ["new", "old"]
    assert result["items"][0]["paths"][0]["arm"] == "temporal"
    assert result["diagnostics"]["generative_calls"] == 0


def test_explicit_window_overrides_date_in_query():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        query="apple harvest in 2025",
        recall_level="fast",
        time_basis="event",
        time_window={"start": "2024-01-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"},
    )

    result = service.search(object(), request)

    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["query_time"]["status"] == "explicit"


def test_ambiguous_query_retains_ordinary_recall_with_diagnostic():
    service = DatedService(CONFIG, None, EmbeddingOnly(), generation_id="generation")
    request = SearchRequest(
        query="Compare apple harvest in 2024 and 2025",
        recall_level="fast",
    )

    result = service.search(object(), request)

    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["query_time"]["status"] == "ambiguous_time_expression"
    assert "temporal" in result["diagnostics"]["omitted_stages"]


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
        service.search(object(), SearchRequest(query="apple harvest in 2025", recall_level="fast"))

    partial = service.search(
        object(),
        SearchRequest(query="apple harvest in 2025", recall_level="fast", allow_partial=True),
    )
    assert {item["unit_id"] for item in partial["items"]} == {"new", "old"}
    assert partial["diagnostics"]["temporal_stop"] == "deadline"
    assert "temporal_deadline" in partial["diagnostics"]["degraded"]


class ExpansionRepository(DatedRepository):
    def __init__(self, request, activity):
        super().__init__(request)
        self.activity = activity

    def dense(self, vector, limit):
        return [{"unit_id": "old", "kind": "fact", "score": 0.9}]

    def ordinary(self, seeds, vector):
        return self._expand("graph", "old")

    def hierarchy(self, seeds, vector):
        return self._expand("hierarchy", "new")

    def _expand(self, name, node):
        with self.activity["lock"]:
            self.activity["active"] += 1
            self.activity["peak"] = max(self.activity["peak"], self.activity["active"])
            self.activity["repositories"].add(id(self))
        try:
            time.sleep(0.04)
            if self.activity.get("fail") == name:
                raise RuntimeError("expansion failed")
            return (node,), {}
        finally:
            with self.activity["lock"]:
                self.activity["active"] -= 1


class ExpansionService(RecallService):
    def __init__(self, *, pool_size=2, fail=None):
        super().__init__(
            CONFIG,
            SimpleNamespace(max_size=pool_size),
            EmbeddingOnly(),
            generation_id="generation",
        )
        self.activity = {
            "lock": Lock(),
            "active": 0,
            "peak": 0,
            "repositories": set(),
            "fail": fail,
        }

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield ExpansionRepository(request, self.activity)


def test_mid_expansions_overlap_on_distinct_repositories_and_keep_arm_order():
    service = ExpansionService()

    result = service.search(object(), SearchRequest(query="apple", recall_level="mid"))

    assert service.activity["peak"] == 2
    assert len(service.activity["repositories"]) == 2
    assert service.activity["active"] == 0
    assert result["diagnostics"]["executed_arms"] == [
        "lexical",
        "dense",
        "graph",
        "hierarchy",
    ]
    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]


def test_mid_expansions_remain_serial_with_one_database_connection():
    service = ExpansionService(pool_size=1)

    result = service.search(object(), SearchRequest(query="apple", recall_level="mid"))

    assert service.activity["peak"] == 1
    assert service.activity["active"] == 0
    assert result["diagnostics"]["degraded"] == []


@pytest.mark.parametrize("allow_partial", [False, True])
@pytest.mark.parametrize("failed_arm", ["graph", "hierarchy"])
def test_mid_expansion_failure_drains_other_job_and_obeys_partial_policy(allow_partial, failed_arm):
    service = ExpansionService(fail=failed_arm)
    request = SearchRequest(query="apple", recall_level="mid", allow_partial=allow_partial)

    if allow_partial:
        result = service.search(object(), request)
        assert result["diagnostics"]["degraded"] == [failed_arm]
        assert (set(result["diagnostics"]["executed_arms"]) & {"graph", "hierarchy"}) == {
            "hierarchy" if failed_arm == "graph" else "graph"
        }
        assert result["diagnostics"]["timing_ms"][failed_arm] >= 30
    else:
        with pytest.raises(RecallUnavailable, match=f"{failed_arm} stage failed"):
            service.search(object(), request)
    assert service.activity["active"] == 0
