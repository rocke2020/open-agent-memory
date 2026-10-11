"""Alias assertions require exact evidence without weakening independent facts."""

from pathlib import Path

import pytest

from oam.config import load_config
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import validate_fact_output
from oam.models.runtime import Usage


def validate_aliases(source, aliases, *, quote=None, previous_context="", config=None, entities=()):
    config = config or load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    return validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": "Nova Research opened a laboratory.",
                    "quote": source if quote is None else quote,
                    "entities": list(entities),
                    "entity_aliases": aliases,
                }
            ],
        },
        Usage(coverage="unavailable"),
        config,
        {None: TextChunk(source, 0, len(source), previous_context, 1)},
        set(),
    )


def test_explicit_alias_preserves_fact_text_and_exact_witness():
    result = validate_aliases(
        "Nova Research, also called NR, opened a laboratory.",
        [
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            }
        ],
        entities=("Nova Research",),
    )

    assert len(result.facts) == 1
    assert result.facts[0].text == "Nova Research opened a laboratory."
    assert result.facts[0].entities == ("Nova Research",)
    assert result.facts[0].entity_aliases[0].model_dump() == {
        "canonical_name": "Nova Research",
        "alias": "NR",
        "quote": "Nova Research, also called NR",
    }
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "assertion",
    [
        {
            "canonical_name": "Nova Research",
            "alias": "NR",
            "quote": "Nova Research, also called NR",
            "entity_id": "invented",
        },
        {"canonical_name": "Nova Research", "quote": "Nova Research, also called NR"},
        {"canonical_name": "Nova Research", "alias": 1, "quote": "Nova Research, also called NR"},
        {"canonical_name": " ", "alias": "NR", "quote": "Nova Research, also called NR"},
        {"canonical_name": "Nova Research", "alias": "NR", "quote": "Invented quote."},
        {"canonical_name": "Invented", "alias": "NR", "quote": "Nova Research, also called NR"},
        {
            "canonical_name": "Nova Research",
            "alias": "NR",
            "quote": "Nova Research, also called NR, opened a hospital.",
        },
        "Nova Research = NR",
        None,
    ],
)
def test_invalid_alias_assertion_is_diagnosed_without_dropping_fact(assertion):
    result = validate_aliases("Nova Research, also called NR, opened a laboratory.", [assertion])

    assert len(result.facts) == 1
    assert result.facts[0].text == "Nova Research opened a laboratory."
    assert result.facts[0].entity_aliases == ()
    assert [(item.field, item.code) for item in result.diagnostics] == [
        ("entity_aliases", "invalid_alias")
    ]
    assert result.outcome == "partial"


def test_absent_and_rejected_aliases_are_omitted_from_serialized_fact():
    result = validate_aliases("Nova Research opened a laboratory.", [])

    assert len(result.facts) == 1
    assert "entity_aliases" not in result.model_dump(mode="json")["facts"][0]


def test_alias_quote_must_be_unique_inside_fact_support():
    result = validate_aliases(
        "Nova Research (NR). Nova Research (NR) opened a laboratory.",
        [{"canonical_name": "Nova Research", "alias": "NR", "quote": "Nova Research (NR)"}],
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics[0].code == "invalid_alias"


def test_context_only_alias_cannot_back_new_fact():
    result = validate_aliases(
        "Nova Research opened a laboratory.",
        [
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            }
        ],
        previous_context="Nova Research, also called NR",
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics[0].code == "invalid_alias"


@pytest.mark.parametrize(
    ("source", "canonical", "alias", "alias_quote"),
    [
        (
            "Nova Research called NORWAY opened a lab.",
            "Nova Research",
            "NOR",
            "Nova Research called NOR",
        ),
        ("Nova Research uses NR-2 in the lab.", "Nova Research", "NR", "Nova Research uses NR"),
        (
            "Floor 101 is also called Level A.",
            "Floor 10",
            "Level A",
            "Floor 101 is also called Level A",
        ),
        ("上海交通大学简称SJTU。", "上海", "SJTU", "上海交通大学简称SJTU"),
        ("张三丰又叫Z。", "张三", "Z", "张三丰又叫Z"),
    ],
)
def test_names_cannot_be_arbitrary_substrings_or_clipped_quote_edges(
    source, canonical, alias, alias_quote
):
    result = validate_aliases(
        source, [{"canonical_name": canonical, "alias": alias, "quote": alias_quote}]
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics[0].code == "invalid_alias"


@pytest.mark.parametrize(
    ("source", "canonical", "alias"),
    [
        ("星云研究院又称NR，已成立。", "星云研究院", "NR"),
        ("北京大学的另一个名字是北大。", "北京大学", "北大"),
        ("张三丰（Z）搬家了。", "张三丰", "Z"),
        ("Nova Research的中文名是星云研究院。", "Nova Research", "星云研究院"),
    ],
)
def test_whole_chinese_mentions_need_no_surrounding_whitespace(source, canonical, alias):
    result = validate_aliases(
        source, [{"canonical_name": canonical, "alias": alias, "quote": source}]
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases[0].canonical_name == canonical
    assert result.facts[0].entity_aliases[0].alias == alias
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("source", "canonical", "alias"),
    [
        ("Nova Research is written nova research elsewhere.", "Nova Research", "nova research"),
        (
            "Ｎｏｖａ Research is written Nova  Research elsewhere.",
            "Ｎｏｖａ Research",
            "Nova  Research",
        ),
    ],
)
def test_normalization_equivalent_pair_adds_no_alias(source, canonical, alias):
    result = validate_aliases(
        source, [{"canonical_name": canonical, "alias": alias, "quote": source}]
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("source", "canonical", "alias", "max_bytes"),
    [
        ("Nova Research also called NR.", "Nova Research", "NR", 12),
        ("ＮＲ also called Nova.", "Nova", "ＮＲ", 5),
        ("İİ also called N.", "İİ", "N", 5),
    ],
)
def test_display_and_normalized_name_utf8_limits_preserve_independent_fact(
    source, canonical, alias, max_bytes
):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_name_max_bytes": max_bytes})}
    )
    result = validate_aliases(
        source, [{"canonical_name": canonical, "alias": alias, "quote": source}], config=config
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics[0].code == "invalid_alias"


def validate_entity_references(source, references, *, offered_ids=None, config=None, entities=()):
    config = config or load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    return validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": source,
                    "quote": source,
                    "entities": list(entities),
                    "entity_references": references,
                }
            ],
        },
        Usage(coverage="unavailable"),
        config,
        {None: TextChunk(source, 0, len(source), "", 1)},
        {"earlier-nova-fact"} if offered_ids is None else offered_ids,
    )


def test_offered_fact_reference_keeps_source_name_and_continuity_quote():
    source = "I joined NR, the research organization I mentioned earlier."
    result = validate_entity_references(
        source,
        [{"name": "NR", "reference_id": "earlier-nova-fact", "quote": source}],
        entities=("NR",),
    )

    assert len(result.facts) == 1
    assert result.facts[0].text == source
    assert result.facts[0].entities == ("NR",)
    assert result.facts[0].entity_references[0].model_dump() == {
        "name": "NR",
        "reference_id": "earlier-nova-fact",
        "quote": source,
    }
    assert result.facts[0].entity_aliases == ()
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "reference",
    [
        {"name": "NR", "reference_id": "unoffered", "quote": "I joined NR again."},
        {"name": "NR", "reference_id": "earlier-nova-fact", "quote": "I joined NR before."},
        {"name": "Invented", "reference_id": "earlier-nova-fact", "quote": "I joined NR again."},
        {"name": " ", "reference_id": "earlier-nova-fact", "quote": "I joined NR again."},
        {
            "name": "NR",
            "reference_id": "earlier-nova-fact",
            "quote": "I joined NR again.",
            "entity_id": "invented",
        },
        {"name": "NR", "reference_id": 1, "quote": "I joined NR again."},
        {"name": "NR", "quote": "I joined NR again."},
        None,
    ],
)
def test_invalid_entity_reference_preserves_fact_with_diagnostic(reference):
    result = validate_entity_references("I joined NR again.", [reference])

    assert len(result.facts) == 1
    assert result.facts[0].entity_references == ()
    assert "entity_references" not in result.facts[0].model_dump(mode="json")
    assert [(item.field, item.code) for item in result.diagnostics] == [
        ("entity_references", "invalid_entity_reference")
    ]
    assert result.outcome == "partial"


@pytest.mark.parametrize(
    ("source", "name", "quote"),
    [
        ("NR-2 is the project I mentioned.", "NR", "NR"),
        ("NORWAY is the country I mentioned.", "NOR", "NOR"),
        ("张三丰是之前提到的同事。", "张三", "张三丰是之前提到的同事。"),
        ("NR is here. NR is there.", "NR", "NR"),
    ],
)
def test_reference_quote_cannot_repeat_or_clip_a_full_source_name(source, name, quote):
    result = validate_entity_references(
        source, [{"name": name, "reference_id": "earlier-nova-fact", "quote": quote}]
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_references == ()
    assert result.diagnostics[0].code == "invalid_entity_reference"


def test_reference_name_uses_configured_utf8_byte_limit():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_name_max_bytes": 5})}
    )
    result = validate_entity_references(
        "ＮＲ is the company I mentioned.",
        [{"name": "ＮＲ", "reference_id": "earlier-nova-fact", "quote": "ＮＲ"}],
        config=config,
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_references == ()
    assert result.diagnostics[0].code == "invalid_entity_reference"


def test_new_alex_and_generic_related_hint_do_not_imply_entity_continuity():
    source = "Another person named Alex, my new swimming teacher, moved here."
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    result = validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": source,
                    "quote": source,
                    "entities": ["Alex"],
                    "related_existing_ids": ["coworker-alex"],
                }
            ],
        },
        Usage(coverage="unavailable"),
        config,
        {None: TextChunk(source, 0, len(source), "", 1)},
        {"coworker-alex"},
    )

    assert len(result.facts) == 1
    assert result.facts[0].related_existing_ids == ("coworker-alex",)
    assert result.facts[0].entity_references == ()
    assert "entity_references" not in result.facts[0].model_dump(mode="json")
    assert result.diagnostics == ()


def test_entity_reference_cannot_use_context_only_source_support():
    source = "I joined NR."
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    result = validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": source,
                    "quote": source,
                    "entities": ["NR"],
                    "entity_references": [
                        {
                            "name": "NR",
                            "reference_id": "earlier-nova-fact",
                            "quote": "NR is the organization I mentioned earlier.",
                        }
                    ],
                }
            ],
        },
        Usage(coverage="unavailable"),
        config,
        {None: TextChunk(source, 0, len(source), "NR is the organization I mentioned earlier.", 1)},
        {"earlier-nova-fact"},
    )

    assert len(result.facts) == 1
    assert result.facts[0].entity_references == ()
    assert result.diagnostics[0].code == "invalid_entity_reference"
