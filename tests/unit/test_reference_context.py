import json
from copy import deepcopy

import pytest

from oam.config import load_config
from oam.indexing.extraction import (
    REFERENCE_EXTRACTION_SYSTEM,
    model_input_tokens,
    pack_reference_facts,
)
from oam.paths import default_config_path
from oam.tokens import estimate_units, estimated_tokens


def test_reference_defaults_are_ten_facts_with_a_2048_token_target():
    config = load_config(default_config_path()).config
    assert config.extraction.reference_max_facts == 10
    assert config.extraction.reference_max_input_tokens == 2048


def test_reference_texts_scale_proportionally_and_keep_semantic_boundaries():
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(
                update={"reference_max_facts": 10, "reference_max_input_tokens": 2048}
            )
        }
    )
    long_text = ("L" * 398 + ". ") * 10
    short_text = ("S" * 158 + ". ") * 2 + "S" * 78 + ". "
    references = tuple(
        {"id": f"memory-{index}", "text": text}
        for index, text in enumerate([long_text] * 5 + [short_text] * 5)
    )
    original = deepcopy(references)
    envelope = {"center": {"text": "New evidence stays whole."}}
    packed = pack_reference_facts(references, envelope, config)
    assert len(packed) == 10
    assert [item["id"] for item in packed] == [item["id"] for item in references]
    lengths = [estimated_tokens(estimate_units(item["text"])) for item in packed]
    assert all(390 <= value <= 410 for value in lengths[:5])
    assert all(30 <= value <= 50 for value in lengths[5:])
    assert all(item["text"].endswith(".") and item["text_truncated"] for item in packed)
    assert all(
        old["text"].startswith(new["text"]) for old, new in zip(references, packed, strict=True)
    )
    assert estimated_tokens(estimate_units(json.dumps(packed, ensure_ascii=False))) > 2048
    assert references == original
    assert envelope["center"]["text"] == "New evidence stays whole."


@pytest.mark.parametrize(
    ("text", "ending"),
    [
        ("Value 3.14 and count 1,000 are recorded, " + "further detail " * 100 + ".", ","),
        ("工作计划需要保留完整分句，" + "后续细节" * 100 + "。", "，"),
    ],
)
def test_reference_previews_use_clause_boundaries_without_splitting_numbers(text, ending):
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(update={"reference_max_input_tokens": 20})
        }
    )
    packed = pack_reference_facts(({"id": "old", "text": text},), {}, config)
    assert len(packed) == 1 and packed[0]["text"].endswith(ending)
    assert text.startswith(packed[0]["text"]) and packed[0]["text_truncated"]
    if text.isascii():
        assert "3.14" in packed[0]["text"] and "1,000" in packed[0]["text"]


def test_indivisible_reference_stays_whole_when_the_full_prompt_fits():
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(update={"reference_max_input_tokens": 20})
        }
    )
    reference = {"id": "old", "text": "Unbroken" * 200}
    packed = pack_reference_facts((reference,), {}, config)
    assert packed == [reference]


@pytest.mark.parametrize(
    "first_unit",
    [
        "The endpoint is https://example.test/path?token=abc and uses TLS.",
        "Dr. Smith moved to Lisbon,",
        "J. R. Smith moved to Lisbon,",
        "The user joined the U.S. office,",
    ],
)
def test_reference_boundaries_preserve_urls_abbreviations_and_initials(first_unit):
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(update={"reference_max_input_tokens": 8})
        }
    )
    reference = {"id": "old", "text": first_unit + " Subsequent detail, " * 100}
    packed = pack_reference_facts((reference,), {}, config)
    assert len(packed) == 1 and packed[0]["text"] == first_unit


def test_complete_input_limit_keeps_new_evidence_and_drops_whole_reference_records():
    config = load_config(default_config_path()).config
    reference = {"id": "first", "text": "A previous claim. " * 20}
    envelope = {"center": {"text": "New eligible evidence. " * 100}}
    budget = model_input_tokens(
        REFERENCE_EXTRACTION_SYSTEM, {**envelope, "reference_facts": [reference]}
    )
    config = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"max_input_tokens": budget})}
    )
    references = (reference, {**reference, "id": "second"})
    packed = pack_reference_facts(references, envelope, config)
    assert packed == [reference]
    assert (
        model_input_tokens(REFERENCE_EXTRACTION_SYSTEM, {**envelope, "reference_facts": packed})
        <= budget
    )
    assert envelope["center"]["text"] == "New eligible evidence. " * 100
