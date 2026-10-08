"""Source support uses owning identity and exact quotes without character coordinates."""

import pytest
from pydantic import ValidationError

from oam.config import load_config
from oam.contracts.evidence import EvidenceAppendRequest
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import extraction_envelope, validate_fact_output
from oam.models.runtime import Usage
from oam.paths import default_config_path


def test_caller_time_quote_needs_no_character_coordinates():
    request = EvidenceAppendRequest(
        source_id="chat",
        messages=[
            {
                "message_id": "m",
                "role": "user",
                "content": "Trip in 2025.",
                "event_time": "2024-09-01T00:00:00Z",
                "temporal_annotations": [
                    {
                        "role": "event",
                        "start": "2025-01-01T00:00:00Z",
                        "end": "2026-01-01T00:00:00Z",
                        "quote": "2025",
                    }
                ],
            }
        ],
    )
    assert request.messages[0].temporal_annotations[0].quote == "2025"
    with pytest.raises(ValidationError, match="quote"):
        EvidenceAppendRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "messages": [
                    {**request.messages[0].model_dump(mode="json"), "content": "2025 and 2025"}
                ],
            }
        )


def test_extraction_envelope_and_serialized_support_have_no_character_coordinates():
    config = load_config(default_config_path()).config
    chunk = TextChunk("Trip in 2025.", 100, 113, "", 4)
    envelope = extraction_envelope(chunk=chunk, config=config, message_role="user")
    assert envelope["center"] == {"text": chunk.text}
    result = validate_fact_output(
        {"outcome": "facts", "facts": [{"text": "Trip in 2025.", "quote": chunk.text}]},
        Usage(input_tokens=1, output_tokens=1, total_tokens=2, coverage="complete"),
        config,
        {None: chunk},
        set(),
    )
    assert result.outcome == "facts"
    assert {"start", "end", "span_start", "span_end"}.isdisjoint(
        result.model_dump(mode="json")["facts"][0]
    )
