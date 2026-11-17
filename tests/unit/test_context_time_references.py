"""Repeated temporal metadata shares a context-local, fully budgeted legend."""

from copy import deepcopy

from oam.retrieval.context import (
    _ContextRenderer,
    assemble_context,
    context_token_count,
    render_context,
)


def temporal_passages(count=8):
    return [
        {
            "unit_id": f"passage_{index:032x}",
            "kind": "passage",
            "origin": "memory",
            "text": f"Record {index}: the appointment remains tentative until confirmed.",
            "citations": [
                {
                    "source_id": "src_" + "a" * 32,
                    "source_version": 2,
                    "role": "user" if index % 2 else "assistant",
                    "message_id": str(index),
                }
            ],
            "time_evidence": [
                {"role": "mentioned", "at": "2026-08-04T04:30:00.123+00:00"},
                {"role": "event", "range": "[2026-08-05,2026-08-07)", "detail": "tentative"},
            ],
            "paths": [],
        }
        for index in range(count)
    ]


def test_exact_repeated_metadata_has_one_complete_time_legend_without_mutating_items():
    rows = temporal_passages()
    before = deepcopy(rows)
    result = render_context(rows)

    assert result.count("mentioned=2026-08-04T04:30:00.123Z") == 1
    assert result.count("event range=[2026-08-05,2026-08-07) detail=tentative") == 1
    assert result.count("Time: T1") == len(rows)
    assert "Time T1:" in result
    assert all(row["text"] in result for row in rows)
    assert "assistant message:0" in result and "user message:1" in result
    assert rows == before
    assert render_context(rows) == result


def test_body_lines_cannot_be_replaced_as_temporal_metadata():
    rows = temporal_passages()
    literal = "Time: mentioned=2026-08-04T04:30:00.123Z"
    rows[0]["text"] += "\n" + literal

    result = render_context(rows)

    assert rows[0]["text"] in result
    assert result.count(literal) == 1
    assert result.count("Time: T1") == len(rows)


def test_temporal_role_range_precision_and_absence_do_not_share_metadata():
    rows = temporal_passages()
    rows[0]["time_evidence"][0]["role"] = "created"
    rows[1]["time_evidence"][1]["range"] = "[2026-08-05,2026-08-08)"
    rows[2]["time_evidence"][0]["at"] = "2026-08-04T04:30:00.124+00:00"
    rows[3]["time_evidence"] = []

    result = render_context(rows)

    assert "created=2026-08-04T04:30:00.123Z" in result
    assert "event range=[2026-08-05,2026-08-08)" in result
    assert "mentioned=2026-08-04T04:30:00.124Z" in result
    assert result.count("Time: T1") == 4
    absent_block = result.split("[4] passage memory", 1)[1].split("\n\n", 1)[0]
    assert "Time:" not in absent_block


def test_legend_and_references_are_included_in_complete_context_budget():
    rows = temporal_passages(24)
    budget = context_token_count(render_context(rows)) - 1
    selected, _, _, _ = assemble_context(
        rows, [], context_max_tokens=budget, include_chunks=True, max_results=len(rows)
    )

    assert len(selected) < len(rows)
    assert "Time T1:" in render_context(selected)
    assert context_token_count(render_context(selected)) <= budget


def test_short_repeated_times_keep_the_exact_nonbeneficial_baseline():
    rows = temporal_passages(2)
    for row in rows:
        row["time_evidence"] = [{"role": "x", "at": "y"}]

    result = render_context(rows)

    assert result.count("Time: x=y") == 2
    assert "Time T1:" not in result


def test_cached_blocks_follow_context_local_time_alias_renumbering():
    rows = temporal_passages()
    earlier = deepcopy(rows)
    for row in earlier:
        row["unit_id"] += "_earlier"
        row["time_evidence"][0]["at"] = "2026-07-04T04:30:00.123+00:00"
    renderer = _ContextRenderer()

    first = renderer.render(rows)[0]
    second = renderer.render([*earlier, *rows])[0]

    assert "Time: T1" in first
    assert "Time T2: mentioned=2026-08-04T04:30:00.123Z" in second
    assert second == render_context([*earlier, *rows])
    assert renderer.render(rows)[0] == first


def test_unknown_temporal_fields_keep_complete_json_in_shared_legend():
    rows = temporal_passages()
    for row in rows:
        row["time_evidence"] = [{"unrecognized_role": "historical", "nested": [1, {"end": 7}]}]

    result = render_context(rows)

    assert result.count('{"nested":[1,{"end":7}],"unrecognized_role":"historical"}') == 1
    assert result.count("Time: T1") == len(rows)
