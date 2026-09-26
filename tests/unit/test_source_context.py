"""Source evidence compaction through the recall service's actual packing path."""

import json
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
def test_grouped_context_preserves_facts_and_shows_source_once(evidence_rows, mode):
    result = SourceService(evidence_rows).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )

    assert [row["unit_id"] for row in result["items"]] == [
        "fact",
        "other",
        "original",
        "second_event",
    ]
    assert evidence_rows[0]["text"] in result["context"]
    assert "Source: [original]" in result["context"]
    assert result["context"].index("[fact]") < result["context"].index("[original] passage")
    assert result["context"].index("[original] passage") < result["context"].index("[other]")
    assert result["context"].count(evidence_rows[2]["text"]) == 1
    assert "June 10" in result["context"]
    assert result["items"][0]["citations"] == evidence_rows[0]["citations"]
    assert result["items"][2]["citations"] == evidence_rows[2]["citations"]
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
    assert "Source: [original]" not in result["context"]


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
    assert "Source: [original]" in result["context"]
    assert result["context"].count(evidence_rows[2]["text"]) == 1


@pytest.mark.parametrize(
    ("cap", "expected"), [(2, ["fact", "other"]), (3, ["fact", "other", "original"])]
)
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
    assert ("Source: [original]" in result["context"]) == (cap == 3)


@pytest.mark.parametrize("correct_offset", [True, False])
def test_coverage_checks_exact_unicode_slice_at_nonzero_offset(evidence_rows, correct_offset):
    passage = evidence_rows[2]
    passage["text"] = "开始。检修完成。"
    passage["citations"][0].update(start=100, end=108)
    evidence_rows[0]["citations"][0]["span"].update(
        start=103 if correct_offset else 100, end=108 if correct_offset else 105, quote="检修完成。"
    )
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="检修"))
    assert "fact" in {row["unit_id"] for row in result["items"]}
    assert ("Source: [original]" in result["context"]) is correct_offset


def test_one_uncovered_support_prevents_grouping(evidence_rows):
    other = deepcopy(evidence_rows[0]["citations"][0])
    other["span"]["source_version"] = 2
    evidence_rows[0]["citations"].append(other)
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))
    assert "fact" in {row["unit_id"] for row in result["items"]}
    assert "Source: [original]" not in result["context"]


def test_resolved_subject_and_date_survive_an_ambiguous_source(evidence_rows):
    source = "She missed it yesterday."
    evidence_rows[2]["text"] = source
    evidence_rows[2]["citations"][0]["end"] = len(source)
    fact = evidence_rows[0]
    fact["text"] = "Maya missed the workshop on June 3."
    fact["citations"][0]["span"].update(quote=source, end=len(source))
    result = SourceService(evidence_rows).search(
        object(), SearchRequest(query="Who missed the workshop?")
    )

    assert fact["text"] in result["context"]
    assert source in result["context"]
    assert result["items"][0]["text"] == fact["text"]


def test_multiple_facts_share_one_source_without_losing_metadata(evidence_rows):
    second_fact = deepcopy(evidence_rows[0])
    second_fact.update(unit_id="shift", text="The user was covering an extra shift.")
    second_fact["time_evidence"] = [{"role": "event", "range": "2025-06-03"}]
    evidence_rows.insert(2, second_fact)
    before = deepcopy(evidence_rows)
    path = {"arm": "graph", "relation": "causal", "nodes": ["fact", "shift"]}
    result = SourceService(evidence_rows, graph_paths={"shift": [path]}).search(
        object(), SearchRequest(query="What happened at the workshop?")
    )

    assert evidence_rows == before
    assert {row["unit_id"] for row in result["items"]} == {row["unit_id"] for row in before}
    assert result["context"].count("Source: [original]") == 2
    assert result["context"].count(before[3]["text"]) == 1
    shift = next(row for row in result["items"] if row["unit_id"] == "shift")
    assert shift["paths"] == [path]
    assert shift["time_evidence"] == second_fact["time_evidence"]


def test_recorded_replay_reassembles_the_same_grouped_context(evidence_rows):
    from oam.debug.traces import TraceRepository

    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))

    class RecordedTrace(TraceRepository):
        def get(self, connection, authority, trace_id):
            return {"status": "recorded", "result": json.loads(json.dumps(result, sort_keys=True))}

    replayed = RecordedTrace().replay(None, None, "trace", "reassemble_recorded")
    assert replayed["status"] == "exact"
    assert replayed["result"] == result
