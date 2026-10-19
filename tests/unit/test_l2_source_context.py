"""Primary Insight evidence reaches the caller's actual context atomically."""

from contextlib import contextmanager
from copy import deepcopy

import psycopg
import pytest
from test_query_time_recall import CONFIG, EmbeddingOnly
from test_source_context import SourceRepository

from oam.contracts.search import SearchRequest
from oam.retrieval.context import source_window_ids
from oam.retrieval.service import RecallService, RecallUnavailable

ORIGINAL = (
    "Batch duration improved from 40 to 32 ms.\n```python\n"
    "from joblib import Parallel, delayed\n"
    "results = Parallel(n_jobs=4)(delayed(run)(x) for x in batches)\n```"
)


def evidence():
    locator = {
        "source_id": "chat",
        "source_version": 1,
        "message_id": "code-message",
        "role": "user",
        "passage_index": 3,
    }
    citation = {
        "source_id": "chat",
        "source_version": 1,
        "passage_id": "code",
        "evidence_id": "message",
        "span": {**locator, "quote": "40 to 32 ms"},
    }
    common = {"origin": "memory", "time_evidence": [], "locator": {}}
    return [
        {
            **common,
            "unit_id": "insight",
            "insight_id": "insight",
            "insight_revision": 1,
            "kind": "insight",
            "text": "Batch optimization improved observed latency.",
            "citations": [citation],
            "supports": [
                {"memory_id": "fact", "support_revision": 1},
                {"memory_id": "fact2", "support_revision": 1},
            ],
        },
        {
            **common,
            "unit_id": "fact",
            "memory_id": "fact",
            "kind": "fact",
            "text": "Batch duration improved from 40 to 32 ms.",
            "citations": [citation],
        },
        {
            **common,
            "unit_id": "fact2",
            "memory_id": "fact2",
            "kind": "fact",
            "text": "The implementation was submitted for review.",
            "citations": [citation],
        },
        {
            **common,
            "unit_id": "code",
            "kind": "passage",
            "text": ORIGINAL,
            "citations": [locator],
            "time_evidence": [{"role": "mentioned", "at": "2026-09-03T12:00:00Z"}],
        },
        {
            **common,
            "unit_id": "neighbor",
            "kind": "passage",
            "text": "UNRELATED NEIGHBOR",
            "citations": [{**locator, "message_id": "next", "message_index": 5}],
        },
    ]


class InsightRepository(SourceRepository):
    def lexical(self, query, limit):
        return [{"unit_id": "insight"}]

    def insight_support_units(self, unit_ids, vector, limit):
        return [
            {
                "insight_unit_id": "insight",
                "unit_id": name,
                "memory_id": name,
                "support_revision": 1,
            }
            for name in ("fact", "fact2")
            if name in self.rows
        ][:limit]

    def record_trace(self, trace_id, policy_epoch, decisions, result):
        self.trace = decisions
        return trace_id


class InsightService(RecallService):
    repository_type = InsightRepository

    def __init__(self, rows=None, *, config=CONFIG):
        super().__init__(config, None, EmbeddingOnly(), generation_id="generation")
        self.rows = evidence() if rows is None else rows
        self.repository = None

    @contextmanager
    def _repository(self, authority, request, deadline):
        if self.repository is None:
            self.repository = self.repository_type(request, self.rows, {})
        yield self.repository


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_primary_insight_restores_complete_direct_code_without_independent_fact_hit(mode):
    service = InsightService()
    result = service.search(
        object(), SearchRequest(query="batch implementation", recall_level=mode)
    )
    assert ORIGINAL in result["context"]
    assert "joblib" not in evidence()[1]["text"]
    assert "UNRELATED NEIGHBOR" not in result["context"]
    assert {item["unit_id"] for item in result["items"]} == {"insight", "fact", "fact2", "code"}
    original = next(item for item in result["items"] if item["unit_id"] == "code")
    edges = [path for path in original["paths"] if path["arm"] == "l2_source"]
    assert {path["memory_id"] for path in edges} == {"fact", "fact2"}
    assert result["context"].count("from joblib import Parallel, delayed") == 1
    assert "message:code-message" in result["context"]
    assert "chunk:code" in result["context"]
    assert "2026-09-03T12:00:00Z" in result["context"]
    assert result["diagnostics"]["l2_source_expansion"][0]["status"] == "included"
    assert (
        service.repository.trace["l2_source_expansion"]
        == result["diagnostics"]["l2_source_expansion"]
    )


def test_direct_selection_does_not_require_message_index_or_expand_siblings():
    rows = evidence()
    locators = [
        {
            "unit_id": row["unit_id"],
            "passage_id": row["unit_id"],
            "source_id": "chat",
            "source_version": 1,
            "locator": row["citations"][0],
        }
        for row in rows
        if row["kind"] == "passage"
    ]
    assert source_window_ids([rows[1]], locators, policy="l2_direct") == ["code"]


@pytest.mark.parametrize("hard_limit", ["tokens", "results"])
def test_incomplete_group_is_omitted_without_insight_fill_reentry(hard_limit):
    config = CONFIG
    options = {"max_context_tokens": 160} if hard_limit == "tokens" else {}
    if hard_limit == "results":
        config = CONFIG.model_copy(
            update={
                "search": CONFIG.search.model_copy(
                    update={"fast": CONFIG.search.fast.model_copy(update={"max_results": 3})}
                )
            }
        )
    result = InsightService(config=config).search(
        object(), SearchRequest(query="batch implementation", recall_level="fast", **options)
    )
    assert "insight" not in {item["unit_id"] for item in result["items"]}
    assert result["diagnostics"]["l2_source_expansion"][0]["status"] == "budget_omitted"


@pytest.mark.parametrize("missing", ["fact2", "code"])
def test_missing_required_evidence_invalidates_group(missing):
    rows = [row for row in evidence() if row["unit_id"] != missing]
    result = InsightService(rows).search(object(), SearchRequest(query="batch implementation"))
    assert "insight" not in {item["unit_id"] for item in result["items"]}
    assert result["diagnostics"]["l2_source_expansion"][0]["status"] == "ineligible"


@pytest.mark.parametrize("method", ["insight_support_units", "source_passage_locators"])
def test_expansion_read_errors_fail_even_when_partial_allowed(monkeypatch, method):
    def unavailable(*args):
        raise psycopg.OperationalError("synthetic unavailable database")

    monkeypatch.setattr(InsightRepository, method, unavailable)
    with pytest.raises(RecallUnavailable, match="l2_source"):
        InsightService().search(
            object(), SearchRequest(query="batch implementation", allow_partial=True)
        )


def test_required_direct_chunk_can_exceed_soft_detail_fraction():
    config = CONFIG.model_copy(
        update={
            "context": CONFIG.context.model_copy(update={"source_detail_budget_fraction": 0.01})
        }
    )
    result = InsightService(config=config).search(
        object(), SearchRequest(query="batch implementation")
    )
    assert ORIGINAL in result["context"]
    assert result["diagnostics"]["l2_source_expansion"][0]["status"] == "included"


def test_shared_original_preserves_every_insight_and_fact_edge_once(monkeypatch):
    rows = evidence()
    second = deepcopy(rows[0])
    second.update(unit_id="second", insight_id="second")
    rows.insert(1, second)
    monkeypatch.setattr(
        InsightRepository, "lexical", lambda *args: [{"unit_id": "insight"}, {"unit_id": "second"}]
    )
    original_supports = InsightRepository.insight_support_units

    def both(self, unit_ids, vector, limit):
        selected = original_supports(self, unit_ids, vector, limit)
        return [*selected, *({**row, "insight_unit_id": "second"} for row in selected)]

    monkeypatch.setattr(InsightRepository, "insight_support_units", both)
    result = InsightService(rows).search(object(), SearchRequest(query="batch implementation"))
    assert {item["unit_id"] for item in result["items"]} == {
        "insight",
        "second",
        "fact",
        "fact2",
        "code",
    }
    for identifier in ("code", "fact", "fact2"):
        item = next(item for item in result["items"] if item["unit_id"] == identifier)
        assert {path["insight_id"] for path in item["paths"] if path["arm"] == "l2_source"} == {
            "insight",
            "second",
        }
    assert result["context"].count("from joblib import Parallel, delayed") == 1


def test_final_current_revision_recheck_withholds_changed_insight(monkeypatch):
    original = InsightRepository.hydrate
    calls = 0

    def changed(self, unit_ids):
        nonlocal calls
        rows = original(self, unit_ids)
        if "insight" in unit_ids:
            calls += 1
            if calls > 1:
                return [row for row in rows if row["unit_id"] != "insight"]
        return rows

    monkeypatch.setattr(InsightRepository, "hydrate", changed)
    result = InsightService().search(object(), SearchRequest(query="batch implementation"))
    assert result["items"] == []
    assert (
        result["diagnostics"]["l2_source_expansion"][0]["reason"] == "insight_changed_before_return"
    )


def test_candidate_outside_primary_selection_does_not_trigger_expansion(monkeypatch):
    rows = evidence()
    ordinary = {**deepcopy(rows[1]), "unit_id": "ordinary", "citations": []}
    rows.insert(0, ordinary)
    monkeypatch.setattr(
        InsightRepository,
        "lexical",
        lambda *args: [{"unit_id": "ordinary"}, {"unit_id": "insight"}],
    )

    def forbidden(*args):
        pytest.fail("secondary Insight must not trigger source expansion")

    monkeypatch.setattr(InsightRepository, "insight_support_units", forbidden)
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(
                update={"fast": CONFIG.search.fast.model_copy(update={"source_seed_results": 1})}
            )
        }
    )
    result = InsightService(rows, config=config).search(
        object(), SearchRequest(query="batch implementation", recall_level="fast")
    )
    assert result["diagnostics"]["l2_source_expansion"] == []
    assert ORIGINAL not in result["context"]


def test_insight_reached_only_by_secondary_expansion_does_not_expand(monkeypatch):
    rows = evidence()
    rows[1]["citations"] = []
    monkeypatch.setattr(InsightRepository, "lexical", lambda *args: [{"unit_id": "fact"}])
    monkeypatch.setattr(
        InsightRepository,
        "ordinary",
        lambda *args: (
            ("insight",),
            {
                "insight": [
                    {
                        "arm": "graph",
                        "relation": "semantic",
                        "nodes": ["fact", "insight"],
                        "score": 0.9,
                    }
                ]
            },
        ),
    )

    def forbidden(*args):
        pytest.fail("secondary expansion must not recursively expand Insight sources")

    monkeypatch.setattr(InsightRepository, "insight_support_units", forbidden)
    result = InsightService(rows).search(
        object(), SearchRequest(query="batch implementation", recall_level="normal")
    )
    assert result["diagnostics"]["l2_source_expansion"] == []
    assert ORIGINAL not in result["context"]


def test_independent_fact_window_and_insight_share_one_original_with_all_edges(monkeypatch):
    rows = evidence()
    for row in rows:
        for citation in row["citations"]:
            locator = citation.get("span", citation)
            if locator.get("message_id") == "code-message":
                locator["message_index"] = 2
    monkeypatch.setattr(
        InsightRepository, "lexical", lambda *args: [{"unit_id": "fact"}, {"unit_id": "insight"}]
    )
    result = InsightService(rows).search(object(), SearchRequest(query="batch implementation"))
    assert result["context"].count("from joblib import Parallel, delayed") == 1
    assert len([item for item in result["items"] if item["unit_id"] == "code"]) == 1
    for identifier in ("fact", "code"):
        item = next(item for item in result["items"] if item["unit_id"] == identifier)
        assert any(path["arm"] == "l2_source" for path in item["paths"])


def test_higher_ranked_complete_group_survives_lower_group_hard_limit(monkeypatch):
    rows = evidence()
    second = deepcopy(rows[0])
    second.update(unit_id="second", insight_id="second")
    rows.insert(1, second)
    monkeypatch.setattr(
        InsightRepository, "lexical", lambda *args: [{"unit_id": "insight"}, {"unit_id": "second"}]
    )
    original = InsightRepository.insight_support_units

    def both(self, unit_ids, vector, limit):
        selected = original(self, unit_ids, vector, limit)
        return [*selected, *({**row, "insight_unit_id": "second"} for row in selected)]

    monkeypatch.setattr(InsightRepository, "insight_support_units", both)
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(
                update={"fast": CONFIG.search.fast.model_copy(update={"max_results": 4})}
            )
        }
    )
    result = InsightService(rows, config=config).search(
        object(), SearchRequest(query="batch implementation", recall_level="fast")
    )
    assert {item["unit_id"] for item in result["items"]} == {"insight", "fact", "fact2", "code"}
    assert [entry["status"] for entry in result["diagnostics"]["l2_source_expansion"]] == [
        "included",
        "budget_omitted",
    ]
    for item in result["items"]:
        assert all(path.get("insight_id") != "second" for path in item["paths"])


def test_missing_hydrated_direct_chunk_invalidates_group_even_if_locator_was_found(monkeypatch):
    original = InsightRepository.hydrate

    def disappeared(self, unit_ids):
        return [row for row in original(self, unit_ids) if row["unit_id"] != "code"]

    monkeypatch.setattr(InsightRepository, "hydrate", disappeared)
    result = InsightService().search(object(), SearchRequest(query="batch implementation"))
    assert result["items"] == []
    assert (
        result["diagnostics"]["l2_source_expansion"][0]["reason"]
        == "missing_or_ineligible_direct_chunk"
    )
