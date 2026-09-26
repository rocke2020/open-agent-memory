"""Cleanup redacts only traces influenced by a withdrawn source."""

from oam.governance.cleanup import affected_trace_ids


def test_independent_recorded_trace_survives_source_cleanup():
    traces = [
        ("withdrawn", {"arms": {"dense": ["removed_unit"]}, "result_ids": []}),
        (
            "independent",
            {"arms": {"dense": ["other_unit"]}, "result_ids": ["other_unit"]},
        ),
    ]

    assert affected_trace_ids(traces, {"removed_unit"}) == ["withdrawn"]
