"""Compact context keeps evidence and metadata ownership while bounding presentation cost."""

from copy import deepcopy

import pytest

from oam.retrieval.context import render_context, render_item
from oam.tokens import estimate_units


def supported_items():
    source = "src_" + "a" * 32
    passage_id = "passage_" + "b" * 32
    fact_id = "memory_" + "c" * 32
    text = f"Keep this literal identifier {fact_id}; the desk is in room 7."
    locator = {
        "source_id": source,
        "source_version": 2,
        "message_id": "turn-4",
        "message_index": 4,
        "role": "user",
        "passage_index": 0,
    }
    times = [{"role": "mentioned", "at": "2026-08-04T00:00:00+00:00"}]
    return [
        {
            "unit_id": fact_id,
            "kind": "fact",
            "origin": "memory",
            "text": "The user's desk is in room 7.",
            "citations": [
                {
                    "source_id": source,
                    "source_version": 2,
                    "passage_id": passage_id,
                    "span": {**locator, "quote": text},
                }
            ],
            "time_evidence": deepcopy(times),
            "paths": [],
        },
        {
            "unit_id": passage_id,
            "kind": "passage",
            "origin": "memory",
            "text": text,
            "citations": [locator],
            "time_evidence": deepcopy(times),
            "paths": [],
        },
    ]


def test_compact_refs_preserve_literal_evidence_and_structured_provenance():
    items = supported_items()
    before = deepcopy(items)
    context = render_context(items)

    assert "[2] passage memory" in context
    assert "[1] fact memory source=[2] time=source" in context
    assert "S1@2 user message:turn-4" in context
    assert "mentioned=2026-08-04T00:00:00Z" in context
    assert items[0]["unit_id"] in context  # The original contains this literal identifier.
    assert items[0]["text"] in context and items[1]["text"] in context
    assert items == before
    legacy = render_item(items[1]) + "\n\n" + render_item(items[0], items[1]["unit_id"])
    assert estimate_units(context) <= estimate_units(legacy)


@pytest.mark.parametrize(
    "fact_times",
    [
        [],
        [{"role": "mentioned", "at": "2026-08-05T00:00:00+00:00"}],
        [
            {"role": "mentioned", "at": "2026-08-04T00:00:00+00:00"},
            {"role": "event", "range": "[2026-08-01,2026-08-02)"},
        ],
    ],
)
def test_fact_does_not_inherit_partial_different_or_absent_times(fact_times):
    items = supported_items()
    items[0]["time_evidence"] = fact_times
    context = render_context(items)

    assert "time=source" not in context
    if fact_times:
        assert "Time:" in context[context.index("[1] fact") :]
    else:
        assert "Time:" not in context[context.index("[1] fact") :]
    if len(fact_times) > 1:
        assert "event range=[2026-08-01,2026-08-02)" in context


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("2026-08-04T04:30:00.123+00:00", "2026-08-04T04:30:00.123Z"),
        ("2026-08-04T04:30:00+08:00", "2026-08-04T04:30:00+08:00"),
        ("2026-08-04T04:30:00", "2026-08-04T04:30:00"),
        ("not-a-date+00:00", "not-a-date+00:00"),
    ],
)
def test_only_valid_utc_at_metadata_uses_equivalent_z_notation(value, rendered):
    items = supported_items()
    items[1]["time_evidence"] = [{"role": "mentioned", "at": value}]
    context = render_context(items)
    assert "mentioned=" + rendered in context
    assert items[1]["time_evidence"][0]["at"] == value


def test_alias_witness_and_unselected_source_keep_exact_quotes_and_times():
    items = supported_items()
    witness_quote = "room 7+00:00 is also named Blue Room."
    items[0]["paths"] = [
        {
            "alias_witnesses": [
                {
                    "support_id": "alias-support",
                    "quote": witness_quote,
                    "citation": {
                        "source_id": "another_source",
                        "source_version": 3,
                        "span": {"message_id": "7", "role": "user", "quote": witness_quote},
                    },
                }
            ]
        }
    ]
    context = render_context(items[:1])
    assert "source=[" not in context and "time=source" not in context
    assert repr(witness_quote) in context
    assert "another_source@3 user message:7" in context
    assert "mentioned=2026-08-04T00:00:00Z" in context


def test_tiny_identifiers_keep_legacy_when_compact_would_cost_more():
    item = {
        "unit_id": "x",
        "kind": "passage",
        "origin": "memory",
        "text": "A.",
        "citations": [{"source_id": "s", "source_version": 1, "role": "user", "message_id": "0"}],
        "time_evidence": [],
    }
    context = render_context([item])
    assert context == render_item(item)
    assert estimate_units(context) <= estimate_units(render_item(item))


def test_fact_does_not_inherit_an_extra_source_time():
    items = supported_items()
    items[1]["time_evidence"].append({"role": "created", "at": "2026-08-05T00:00:00+00:00"})
    context = render_context(items)
    fact_context = context[context.index("[1] fact") :]
    assert "time=source" not in fact_context
    assert "mentioned=2026-08-04T00:00:00Z" in fact_context
    assert "created=" not in fact_context
