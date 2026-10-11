"""Ordinary name support must cite the source-written name, independently of fact prose."""

import pytest

from oam.config import load_config
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import validate_fact_output
from oam.models.runtime import Usage
from oam.paths import default_config_path


@pytest.mark.parametrize("invalid", ["Maya", "NR", "x" * 1025])
def test_invalid_entity_name_keeps_coreferential_fact_prose_and_other_names(invalid):
    source = "I moved to London and bought an NR-2 sensor."
    config = load_config(default_config_path()).config
    result = validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": "The user moved to London and bought a sensor.",
                    "quote": source,
                    "entities": ["London", invalid],
                }
            ],
        },
        Usage(coverage="unavailable"),
        config,
        {None: TextChunk(source, 0, len(source), "Maya previously lived in Paris.", 20)},
        set(),
    )
    assert result.facts[0].text == "The user moved to London and bought a sensor."
    assert result.facts[0].quote == source
    assert result.facts[0].entities == ("London",)
    assert [(item.field, item.code) for item in result.diagnostics] == [
        ("entities", "invalid_entity_name")
    ]


def test_grounded_oversized_name_is_not_indexable_even_when_written_in_quote():
    source = "x" * 1025
    result = validate_fact_output(
        {"outcome": "facts", "facts": [{"text": source, "quote": source, "entities": [source]}]},
        Usage(coverage="unavailable"),
        load_config(default_config_path()).config,
        {None: TextChunk(source, 0, len(source), "", 300)},
        set(),
    )
    assert result.facts[0].entities == ()
    assert result.facts[0].text == source
    assert result.diagnostics[0].code == "invalid_entity_name"
