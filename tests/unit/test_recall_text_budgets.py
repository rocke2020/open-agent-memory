"""Independent learned-text and original-text budgets through real recall assembly."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
import tiktoken
from test_source_context import SourceRepository, SourceService
from test_source_context import evidence_rows as evidence_rows

from oam.contracts.search import SearchRequest
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens


def _service(rows):
    class RankedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": row["unit_id"]} for row in rows][:limit]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, self.graph_paths)

    return RankedService(rows)


def test_online_recall_uses_compiled_estimates_without_bpe(evidence_rows, monkeypatch):
    from oam.retrieval import context

    if encoding := getattr(context, "_context_encoding", None):
        encoding.cache_clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("real-time recall must not invoke BPE")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    original = deepcopy(evidence_rows[2])
    original["text"] = "周一工作坊。🎯"
    result = _service([evidence_rows[0], original]).search(
        object(),
        SearchRequest(query="workshop", max_tokens=128, include={"chunks": {"max_tokens": 12}}),
    )

    assert result["tokenizer"] == ESTIMATOR
    assert result["context_tokens"] == estimated_tokens(estimate_units(result["context"]))
    selected = next(item for item in result["items"] if item["kind"] == "passage")
    assert selected["text"] == original["text"]
    assert not selected.get("truncated")


def test_learned_budget_accumulates_units_before_rounding(evidence_rows):
    facts = []
    for letter in "abcd":
        item = deepcopy(evidence_rows[0])
        item.update(unit_id=letter, text=letter)
        facts.append(item)
    result = _service(facts).search(object(), SearchRequest(query="workshop", max_tokens=1))

    assert {item["unit_id"] for item in result["items"]} == set("abcd")
    assert result["diagnostics"]["text_budgets"]["facts"]["used"] == 1


def test_fact_budget_counts_text_without_trimming_citations(evidence_rows):
    fact = deepcopy(evidence_rows[0])
    fact["citations"][0]["span"]["quote"] = "source words " * 200
    result = _service([fact]).search(object(), SearchRequest(query="workshop", max_tokens=16))

    assert [item["unit_id"] for item in result["items"]] == ["fact"]
    assert result["items"][0]["citations"] == fact["citations"]
    assert "source words " * 200 in result["context"]
    assert result["context_tokens"] > 16
    assert result["tokenizer"] == ESTIMATOR


def test_zero_fact_budget_still_returns_ranked_originals(evidence_rows):
    result = _service([evidence_rows[0], evidence_rows[2]]).search(
        object(),
        SearchRequest(query="workshop", max_tokens=0, include={"chunks": {"max_tokens": 128}}),
    )

    assert [item["unit_id"] for item in result["items"]] == ["original"]
    assert evidence_rows[2]["text"] in result["context"]
    assert result["diagnostics"]["generative_calls"] == 0


def test_ordinary_originals_require_explicit_inclusion(evidence_rows):
    result = _service([evidence_rows[2]]).search(
        object(), SearchRequest(query="workshop", max_tokens=0)
    )

    assert result["items"] == []
    assert result["context"] == ""


@pytest.mark.parametrize(
    "text", ["Monday workshop. " * 200, "周一讨论工作坊。🎯" * 200], ids=["english", "mixed-script"]
)
@pytest.mark.parametrize("kind", ["passage", "resource_passage"])
def test_crossing_original_is_returned_whole_then_later_chunks_are_omitted(
    evidence_rows, text, kind
):
    first = deepcopy(evidence_rows[2])
    first.update(unit_id="first", text="First.", kind=kind)
    original = deepcopy(evidence_rows[2])
    original.update(text=text, kind=kind)
    later = deepcopy(evidence_rows[2])
    later.update(unit_id="later", text="Fits", kind=kind)
    result = _service([first, original, later]).search(
        object(),
        SearchRequest(query="workshop", max_tokens=0, include={"chunks": {"max_tokens": 12}}),
    )

    stored = {item["unit_id"]: item for item in [first, original]}
    assert [item["unit_id"] for item in result["items"]] == ["first", "original"]
    for item in result["items"]:
        assert item["text"] == stored[item["unit_id"]]["text"]
        assert item["citations"] == stored[item["unit_id"]]["citations"]
    assert result["diagnostics"]["text_budgets"]["chunks"]["truncated_ids"] == []
    assert "native_truncated" not in result["context"]


@pytest.mark.parametrize("budget, expected", [(0, []), (1, ["original"])])
def test_zero_or_exact_original_target_does_not_admit_another_chunk(
    evidence_rows, budget, expected
):
    original = deepcopy(evidence_rows[2])
    original["text"] = "Fits"
    later = deepcopy(original)
    later.update(unit_id="later", text="Next")

    result = _service([original, later]).search(
        object(),
        SearchRequest(query="workshop", max_tokens=0, include={"chunks": {"max_tokens": budget}}),
    )

    assert [item["unit_id"] for item in result["items"]] == expected


def test_positive_original_target_returns_oversized_first_chunk_whole(evidence_rows):
    original = deepcopy(evidence_rows[2])
    original["text"] = "Complete stored original."

    result = _service([original]).search(
        object(),
        SearchRequest(query="workshop", max_tokens=0, include={"chunks": {"max_tokens": 1}}),
    )

    assert result["items"][0]["text"] == "Complete stored original."
    assert not result["items"][0].get("truncated")


def test_positive_fact_budget_returns_first_fact_when_none_fit(evidence_rows):
    result = _service([evidence_rows[0]]).search(
        object(), SearchRequest(query="workshop", max_tokens=1)
    )

    assert [item["unit_id"] for item in result["items"]] == ["fact"]
    assert result["items"][0]["text"] == evidence_rows[0]["text"]


def test_caller_can_request_more_than_the_removed_aggregate_ceiling(evidence_rows):
    request = SearchRequest(
        query="workshop", max_tokens=20000, include={"chunks": {"max_tokens": 20000}}
    )
    result = _service([evidence_rows[0], evidence_rows[2]]).search(object(), request)

    assert {item["unit_id"] for item in result["items"]} == {"fact", "original"}
