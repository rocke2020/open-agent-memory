"""Grouped calls preserve individual passage support and bounded source coverage."""

import json
from datetime import UTC, datetime

import pytest
from test_indexing_extraction import extraction_models

from oam.indexing.chunker import TextChunk


def passage(identifier, text, index, role="user", *, estimated_tokens=4):
    from oam.indexing.grouping import ExtractionPassage

    return ExtractionPassage(
        identifier,
        TextChunk(text, 0, len(text), "", estimated_tokens),
        role,
        datetime(2026, 9, index + 1, tzinfo=UTC),
        index,
    )


def test_one_group_call_validates_each_fact_against_its_own_message():
    from oam.indexing.grouping import extract_group

    inputs = [
        passage("p-user", "I prefer tea.", 0),
        passage("p-assistant", "Try green tea.", 1, "assistant"),
    ]
    reply = {
        "outcome": "facts",
        "facts": [
            {"passage_id": "p0", "text": "The user prefers tea.", "quote": "I prefer tea."},
            {
                "passage_id": "p1",
                "text": "Green tea was recommended.",
                "quote": "Try green tea.",
            },
            {"passage_id": "p0", "text": "Wrong owner.", "quote": "Try green tea."},
            {"passage_id": "unknown", "text": "Unknown owner.", "quote": "I prefer tea."},
        ],
    }
    requests = []
    with extraction_models(reply, requests) as (models, config):
        result = extract_group(models, passages=tuple(inputs), config=config, timeout_seconds=1)
    assert len(requests) == 1
    assert [(f.passage_id, f.quote) for f in result.facts] == [
        ("p-user", "I prefer tea."),
        ("p-assistant", "Try green tea."),
    ]
    envelope = json.loads(requests[0]["messages"][1]["content"])
    assert [p["passage_id"] for p in envelope["passages"]] == ["p0", "p1"]
    assert [p["message_role"] for p in envelope["passages"]] == ["user", "assistant"]
    assert [p["mentioned_at"] for p in envelope["passages"]] == [
        "2026-09-01T00:00:00+00:00",
        "2026-09-02T00:00:00+00:00",
    ]


def test_grouping_respects_eligibility_gaps_and_never_discards_a_passage():
    from oam.indexing.grouping import plan_extraction_groups

    with extraction_models({"outcome": "no_facts", "facts": []}) as (_, config):
        inputs = (passage("a", "Tea.", 0), passage("b", "Coffee.", 1), passage("c", "Walking.", 3))
        groups = plan_extraction_groups(inputs, config)
    assert [[p.passage_id for p in group] for group in groups] == [["a", "b"], ["c"]]


def test_output_budget_splits_groups_before_dispatch():
    from oam.indexing.grouping import plan_extraction_groups

    with extraction_models({"outcome": "no_facts", "facts": []}) as (_, config):
        config = config.model_copy(
            update={
                "extraction": config.extraction.model_copy(
                    update={"group_max_estimated_output_tokens": 128}
                )
            }
        )
        inputs = (passage("a", "Tea.", 0), passage("b", "Coffee.", 1))
        groups = plan_extraction_groups(inputs, config)
    assert [[p.passage_id for p in group] for group in groups] == [["a"], ["b"]]


@pytest.mark.parametrize(
    ("second_tokens", "expected_groups"),
    [(640, [["a", "b"]]), (641, [["a"], ["b"]])],
)
def test_group_output_estimate_admits_1280_source_tokens_and_splits_above_them(
    second_tokens, expected_groups
):
    from oam.indexing.grouping import plan_extraction_groups

    with extraction_models({"outcome": "no_facts", "facts": []}) as (_, config):
        inputs = (
            passage("a", "a" * 2560, 0, estimated_tokens=640),
            passage("b", "b" * (second_tokens * 4), 1, estimated_tokens=second_tokens),
        )
        groups = plan_extraction_groups(inputs, config)
    assert [[p.passage_id for p in group] for group in groups] == expected_groups


def test_cross_passage_relations_skip_rejected_targets_without_renumbering():
    from oam.indexing.grouping import extract_group

    reply = {
        "outcome": "facts",
        "facts": [
            {"passage_id": "p0", "text": "Invalid.", "quote": "Missing."},
            {"passage_id": "p0", "text": "Tea.", "quote": "Tea."},
            {"passage_id": "p1", "text": "Coffee.", "quote": "Coffee.", "caused_by": [0, 1]},
        ],
    }
    with extraction_models(reply) as (models, config):
        result = extract_group(
            models,
            passages=(passage("a", "Tea.", 0), passage("b", "Coffee.", 1)),
            config=config,
            timeout_seconds=1,
        )
    assert [(f.output_index, f.passage_id) for f in result.facts] == [(1, "a"), (2, "b")]
    assert result.facts[1].caused_by == (1,)


@pytest.mark.parametrize("owner", [None, 0, "foreign", "canonical-a"])
def test_group_rejects_unoffered_ownership_without_guessing(owner):
    from oam.indexing.grouping import extract_group

    reply = {
        "outcome": "facts",
        "facts": [
            {"passage_id": owner, "text": "Unowned tea.", "quote": "Tea."},
            {"passage_id": "p1", "text": "Owned tea.", "quote": "Tea."},
        ],
    }
    with extraction_models(reply) as (models, config):
        result = extract_group(
            models,
            passages=(passage("canonical-a", "Tea.", 0), passage("canonical-b", "Tea.", 1)),
            config=config,
            timeout_seconds=1,
        )
    assert [(f.output_index, f.passage_id) for f in result.facts] == [(1, "canonical-b")]
    assert [(d.output_index, d.code) for d in result.diagnostics] == [(0, "invalid_passage")]
