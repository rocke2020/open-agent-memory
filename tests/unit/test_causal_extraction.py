"""The indexing switch selects instructions and controls structured causal output."""

import json
from datetime import UTC, datetime

import pytest
from test_indexing_extraction import extraction_models

from oam.config import load_config
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import extract_facts
from oam.indexing.grouping import ExtractionPassage, extract_group
from oam.paths import default_config_path


@pytest.mark.parametrize("causal", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("references", [False, True])
def test_index_switch_selects_prompt_and_preserves_updates(causal, grouped, references):
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "index": config.index.model_copy(update={"enable_causal_graph": causal}),
            "extraction": config.extraction.model_copy(
                update={"reference_max_facts": 2 if references else 0}
            ),
        }
    )
    source = "I worked at Acme. Acme closed, so I joined Beta."
    facts = [
        {"text": "The user worked at Acme.", "quote": "I worked at Acme."},
        {"text": "Acme closed.", "quote": "Acme closed"},
        {
            "text": "The user joined Beta because Acme closed.",
            "quote": "Acme closed, so I joined Beta.",
            "entities": ["Acme", "Beta"],
            "caused_by": [1],
            "updates": [0],
        },
    ]
    if grouped:
        facts = [{**fact, "passage_id": "p0"} for fact in facts]
    requests = []
    chunk = TextChunk(source, 0, len(source), "", 15)
    with extraction_models({"outcome": "facts", "facts": facts}, requests, config=config) as (
        models,
        config,
    ):
        if grouped:
            result = extract_group(
                models,
                passages=(ExtractionPassage("source", chunk, "user", datetime.now(UTC), 0),),
                config=config,
                timeout_seconds=1,
            )
        else:
            result = extract_facts(
                models, chunk=chunk, config=config, message_role="user", timeout_seconds=1
            )
    system = requests[0]["messages"][0]["content"]
    assert ("caused_by" in system) is causal
    assert "updates" in system and "entity_aliases" in system and "date_reference" in system
    assert result.facts[-1].caused_by == ((1,) if causal else ())
    assert result.facts[-1].updates == (0,)
    assert result.facts[-1].entities == ("Acme", "Beta")
    assert ("reference_facts" in json.loads(requests[0]["messages"][1]["content"])) is references


@pytest.mark.parametrize("causes", [[0], "malformed", {"wrong": 0}, None])
def test_disabled_causal_metadata_cannot_discard_a_supported_fact(causes):
    source = "I worked at Acme. I joined Beta in 2024."
    reply = {
        "outcome": "facts",
        "facts": [
            {"text": "The user worked at Acme.", "quote": "I worked at Acme."},
            {
                "text": "The user joined Beta in 2024.",
                "quote": "I joined Beta in 2024.",
                "caused_by": causes,
                "updates": [0],
                "annotations": [
                    {
                        "role": "event",
                        "start": "2024-01-01T00:00:00Z",
                        "end": "2025-01-01T00:00:00Z",
                        "quote": "2024",
                    }
                ],
            },
        ],
    }
    with extraction_models(reply) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(source, 0, len(source), "", 15),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert len(result.facts) == 2
    assert result.facts[1].caused_by == ()
    assert result.facts[1].updates == (0,)
    assert len(result.facts[1].annotations) == 1
