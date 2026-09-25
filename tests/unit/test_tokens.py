"""The public context budget uses one versioned Unicode estimator."""

from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens


def test_unicode_quarter_estimator_counts_disjoint_scripts_and_rounds_once():
    assert ESTIMATOR == "oam-unicode-quarter-v1"
    assert estimate_units("") == 0
    assert estimated_tokens(estimate_units("abcd")) == 1
    assert estimated_tokens(estimate_units("甲乙丙丁")) == 6
    assert estimated_tokens(estimate_units("😀")) == 2
    assert estimated_tokens(estimate_units("a甲😀")) == 4
    assert estimated_tokens(estimate_units("[id]\nCitations: []\nTime: []")) == 7


def test_rounded_context_count_includes_separator_only_once():
    blocks = ["a", "b"]
    units = estimate_units(blocks[0]) + 2 + estimate_units(blocks[1])
    assert units == estimate_units("\n\n".join(blocks)) == 4
    assert estimated_tokens(units) == 1
