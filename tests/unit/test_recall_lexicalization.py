from oam.retrieval.fts import IndexStreams, index_streams, query_terms


def test_lexical_representation_keeps_chinese_words_and_mixed_boundaries():
    assert index_streams("APPLE苹果 tree_2026") == IndexStreams("apple tree", "苹果 2026")
    assert index_streams("No 2026苹果") == IndexStreams("no", "no 2026 苹果")
    assert index_streams("Café苹果") == IndexStreams("café", "苹果")


def test_query_uses_chinese_content_and_keeps_standalone_modifiers_out_of_or_candidates():
    terms = query_terms("我不喜欢苹果, no peanuts before 2026")
    assert terms.candidates == (
        ("simple", "喜欢"),
        ("simple", "苹果"),
        ("english", "peanuts"),
        ("simple", "2026"),
    )
    assert terms.primary == (
        ("simple", "喜欢"),
        ("simple", "苹果"),
        ("english", "peanuts"),
        ("simple", "2026"),
    )
    assert terms.modifiers == (("simple", "不"), ("simple", "no"), ("simple", "before"))
    assert query_terms("的 不").candidates == ()
