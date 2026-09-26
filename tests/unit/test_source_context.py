"""Source evidence compaction through the recall service's actual packing path."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.contracts.search import SearchRequest
from oam.retrieval.service import RecallService
from oam.tokens import estimate_units, estimated_tokens


@pytest.fixture
def evidence_rows():
    text = "On June 3, I missed the workshop while covering an extra shift."
    locator = {
        "source_id": "conversation",
        "source_version": 1,
        "message_id": "0",
        "message_index": 0,
        "role": "user",
        "start": 0,
        "end": len(text),
    }
    common = {"origin": "memory", "time_evidence": [], "locator": {}}
    return [
        {
            **common,
            "unit_id": "fact",
            "kind": "fact",
            "text": "The user missed the workshop on June 3.",
            "citations": [
                {
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": "original",
                    "evidence_id": "evidence",
                    "span": {**locator, "quote": text},
                }
            ],
        },
        {**common, "unit_id": "other", "kind": "fact", "text": "Another topic.", "citations": []},
        {
            **common,
            "unit_id": "original",
            "kind": "passage",
            "text": text,
            "citations": [locator],
        },
        {
            **common,
            "unit_id": "second_event",
            "kind": "passage",
            "text": "On June 10, I missed another workshop while working.",
            "citations": [{**locator, "message_id": "2", "message_index": 2}],
        },
    ]


class SourceRepository(DatedRepository):
    def __init__(self, request, rows, graph_paths):
        self.request = request
        self.rows = {row["unit_id"]: row for row in rows}
        self.graph_paths = graph_paths

    def dense(self, vector, limit):
        return []

    def ordinary(self, seeds, vector):
        return tuple(self.graph_paths), self.graph_paths

    def hierarchy(self, seeds, vector):
        return (), {}

    def hydrate(self, unit_ids):
        return [deepcopy(self.rows[unit_id]) for unit_id in unit_ids]


class SourceService(RecallService):
    def __init__(self, rows, *, config=CONFIG, graph_paths=None):
        super().__init__(config, None, EmbeddingOnly(), generation_id="generation")
        self.rows = rows
        self.graph_paths = graph_paths or {}

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield SourceRepository(request, self.rows, self.graph_paths)


@pytest.mark.parametrize("mode", ["fast", "mid", "deep"])
def test_covered_fact_uses_original_once_at_earliest_rank(evidence_rows, mode):
    result = SourceService(evidence_rows).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )

    assert [row["unit_id"] for row in result["items"]] == ["original", "other", "second_event"]
    assert result["context"].count(evidence_rows[2]["text"]) == 1
    assert "June 10" in result["context"]
    assert result["items"][0]["citations"] == evidence_rows[2]["citations"]
    assert result["context_tokens"] == estimated_tokens(estimate_units(result["context"]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_version", 2),
        ("message_id", "other"),
        ("message_index", 2),
        ("role", "assistant"),
        ("quote", "unsupported text"),
        ("start", 1),
    ],
)
def test_inconsistent_support_keeps_fact(evidence_rows, field, value):
    evidence_rows[0]["citations"][0]["span"][field] = value
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))
    assert {row["unit_id"] for row in result["items"]} == {
        "fact",
        "other",
        "original",
        "second_event",
    }


@pytest.mark.parametrize("unique_evidence", ["time", "path"])
def test_unique_time_and_graph_evidence_remain_on_the_fact(evidence_rows, unique_evidence):
    event = {"role": "event", "range": "2025-06-03"}
    evidence_rows[0]["time_evidence"] = [event] if unique_evidence == "time" else []
    path = {"arm": "graph", "nodes": ["seed", "fact"]}
    paths = {"fact": [path]} if unique_evidence == "path" else {}
    result = SourceService(evidence_rows, graph_paths=paths).search(
        object(), SearchRequest(query="workshops")
    )
    fact = next(row for row in result["items"] if row["unit_id"] == "fact")
    original = next(row for row in result["items"] if row["unit_id"] == "original")
    assert fact["paths"] == paths.get("fact", [])
    assert fact["time_evidence"] == evidence_rows[0]["time_evidence"]
    assert original["paths"] == []
    assert original["time_evidence"] == []


@pytest.mark.parametrize(("cap", "expected"), [(2, ["fact", "other"]), (3, ["original", "other"])])
def test_grouping_neither_fetches_nor_backfills_beyond_item_cap(evidence_rows, cap, expected):
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(
                update={"mid": CONFIG.search.mid.model_copy(update={"max_results": cap})}
            )
        }
    )
    result = SourceService(evidence_rows, config=config).search(
        object(), SearchRequest(query="workshops")
    )
    assert [row["unit_id"] for row in result["items"]] == expected


@pytest.mark.parametrize("correct_offset", [True, False])
def test_coverage_checks_exact_unicode_slice_at_nonzero_offset(evidence_rows, correct_offset):
    passage = evidence_rows[2]
    passage["text"] = "开始。检修完成。"
    passage["citations"][0].update(start=100, end=108)
    evidence_rows[0]["citations"][0]["span"].update(
        start=103 if correct_offset else 100, end=108 if correct_offset else 105, quote="检修完成。"
    )
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="检修"))
    assert ("fact" in {row["unit_id"] for row in result["items"]}) is not correct_offset


def test_one_uncovered_support_prevents_grouping(evidence_rows):
    other = deepcopy(evidence_rows[0]["citations"][0])
    other["span"]["source_version"] = 2
    evidence_rows[0]["citations"].append(other)
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))
    assert "fact" in {row["unit_id"] for row in result["items"]}
