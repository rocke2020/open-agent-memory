from oam.retrieval.repository import lexicalize


def test_lexical_representation_retains_mixed_language_boundaries():
    assert lexicalize("APPLE苹果 tree_2026") == "apple 苹 果 tree 2026"
