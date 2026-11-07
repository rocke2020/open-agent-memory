"""Legacy estimates remain separate from native live-context counting."""

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


def test_live_context_uses_native_whole_string_count_with_literal_specials():
    from oam.retrieval.context import CONTEXT_TOKENIZER, context_token_count

    assert CONTEXT_TOKENIZER == "o200k_base"
    assert context_token_count("hello world") == 2
    assert context_token_count("甲乙丙丁") == 5
    assert context_token_count("<|endoftext|>") == 7
    assert context_token_count("a\n\nb") == 3
