"""Whole original text and metadata are admitted under the rendered context limit."""

from copy import deepcopy

import pytest
import tiktoken
from test_context_total_budgets import ranked_service
from test_source_context import evidence_rows as evidence_rows

from oam.contracts.search import SearchRequest
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens


def test_online_recall_uses_compiled_estimates_without_bpe(evidence_rows, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real-time recall must not invoke BPE")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    original = deepcopy(evidence_rows[2])
    original["text"] = "周一工作坊。🎯"
    result = ranked_service([original]).search(object(), SearchRequest(query="workshop"))
    assert result["tokenizer"] == ESTIMATOR
    assert result["context_tokens"] == estimated_tokens(estimate_units(result["context"]))
    assert result["items"][0]["text"] == original["text"]
    assert not result["items"][0].get("truncated")


@pytest.mark.parametrize("kind", ["passage", "resource_passage"])
@pytest.mark.parametrize("text", ["Monday workshop. " * 200, "周一讨论工作坊。🎯" * 200])
def test_crossing_original_is_skipped_whole_and_later_fitting_original_survives(
    evidence_rows, kind, text
):
    first = {**deepcopy(evidence_rows[2]), "unit_id": "first", "kind": kind, "text": "First."}
    crossing = {**deepcopy(first), "unit_id": "crossing", "text": text}
    later = {**deepcopy(first), "unit_id": "later", "text": "Later."}
    result = ranked_service([first, crossing, later], budget=128).search(
        object(), SearchRequest(query="workshop")
    )
    assert [row["unit_id"] for row in result["items"]] == ["first", "later"]
    assert result["items"][0]["citations"] == first["citations"]
    assert text not in result["context"]
    assert "crossing" in result["diagnostics"]["context_budget"]["omitted_ids"]
    assert result["context_tokens"] <= 128
    assert all(not row.get("truncated") for row in result["items"])


def test_genuine_quote_equal_to_short_chunk_survives_false(evidence_rows):
    fact, original = deepcopy(evidence_rows[0]), deepcopy(evidence_rows[2])
    result = ranked_service([fact, original]).search(
        object(), SearchRequest(query="workshop", include={"chunks": False})
    )
    assert [row["unit_id"] for row in result["items"]] == ["fact"]
    assert original["text"] in result["context"]
    assert result["items"][0]["citations"] == fact["citations"]
    assert result["diagnostics"]["context_budget"]["original_count"] == 0


def test_missing_source_invalidates_fact_even_when_chunks_are_false(evidence_rows):
    result = ranked_service([evidence_rows[0]]).search(
        object(), SearchRequest(query="workshop", include={"chunks": False})
    )
    assert result["items"] == []
    assert result["diagnostics"]["context_budget"]["source_omitted_ids"] == ["fact"]
