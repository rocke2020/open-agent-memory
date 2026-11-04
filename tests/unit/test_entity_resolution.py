"""Identity decisions preserve source names and locate namesakes precisely."""

from oam.config import load_config
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import validate_fact_output
from oam.models.runtime import Usage
from oam.paths import default_config_path


def extract(source, mentions):
    return validate_fact_output(
        {
            "outcome": "facts",
            "facts": [{"text": source, "quote": source, "entity_mentions": mentions}],
        },
        Usage(coverage="unavailable"),
        load_config(default_config_path()).config,
        {None: TextChunk(source, 0, len(source), "", 20)},
        set(),
    )


def test_two_namesake_mentions_keep_original_spelling_and_distinct_spans():
    source = "Alex introduced another Alex."
    result = extract(
        source,
        [
            {"name": "Alex", "quote": source, "object": "first", "decision": "new"},
            {
                "name": "Alex",
                "quote": source,
                "object": "second",
                "decision": "different",
                "different_from": ["first"],
                "occurrence": 2,
            },
        ],
    )
    assert len(result.facts) == 1
    assert [mention.span for mention in result.facts[0].entity_mentions] == [(0, 4), (24, 28)]
    assert [mention.name for mention in result.facts[0].entity_mentions] == ["Alex", "Alex"]
    assert result.facts[0].text == source


def test_invalid_identity_mention_preserves_the_independent_fact():
    source = "Alex introduced another Alex."
    result = extract(
        source,
        [
            {
                "name": "Alex",
                "quote": source,
                "object": "second",
                "decision": "different",
                "occurrence": 3,
            }
        ],
    )
    assert len(result.facts) == 1
    assert result.facts[0].entity_mentions == ()
    assert any(item.code == "invalid_entity_mention" for item in result.diagnostics)


def test_fresh_output_missing_identity_decisions_cannot_use_legacy_publication():
    source = "The daily-digest job delivered breakfast news."
    result = validate_fact_output(
        {
            "outcome": "facts",
            "facts": [{"text": source, "quote": source, "entities": ["daily-digest"]}],
        },
        Usage(coverage="unavailable"),
        load_config(default_config_path()).config,
        {None: TextChunk(source, 0, len(source), "", 20)},
        set(),
        require_entity_mentions=True,
    )
    assert len(result.facts) == 1
    assert result.facts[0].entity_mentions == ()
    assert result.diagnostics[0].code == "missing_entity_decision"
