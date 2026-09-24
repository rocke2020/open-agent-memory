from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oam.contracts.evidence import EvidenceAppendRequest
from oam.contracts.resources import EvidenceResourceCreate


def message(**changes):
    return {
        "message_id": "m",
        "role": "user",
        "content": "  Trip in 2025.  ",
        "event_time": "2024-09-01T00:00:00Z",
        **changes,
    }


def annotation(**changes):
    return {
        "role": "date_reference",
        "start": "2025-01-01T00:00:00Z",
        "end": "2026-01-01T00:00:00Z",
        "instant": False,
        "granularity": "year",
        "meaning": "possible_period",
        "origin": "caller",
        "reference_timezone": "UTC",
        "quote": "2025",
        "span_start": 10,
        "span_end": 14,
        **changes,
    }


def test_caller_annotations_preserve_exact_source_offsets_and_time_roles():
    request = EvidenceAppendRequest(
        source_id="chat",
        source_version=2,
        expected_active_version=1,
        source_created_at="2024-08-01T00:00:00Z",
        reference_timezone="UTC",
        messages=[message(temporal_annotations=[annotation()])],
    )
    assert request.messages[0].content == "  Trip in 2025.  "
    supplied = request.messages[0].temporal_annotations[0]
    assert supplied.role == "date_reference"
    assert supplied.origin == "caller"
    assert supplied.bounds == "[)"
    assert supplied.start == datetime(2025, 1, 1, tzinfo=UTC)
    assert request.source_created_at == datetime(2024, 8, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": "2024"},
        {"span_start": 0},
        {"span_end": 200},
        {"origin": "generative_extraction"},
        {"reference_timezone": "not/a-zone"},
        {"start": "2025-01-01T00:00:00"},
    ],
)
def test_caller_annotation_rejects_unverifiable_or_ambiguous_input(changes):
    with pytest.raises(ValidationError) as failure:
        EvidenceAppendRequest(
            source_id="chat", messages=[message(temporal_annotations=[annotation(**changes)])]
        )
    assert all(error["type"] != "extra_forbidden" for error in failure.value.errors())


@pytest.mark.parametrize(
    "values",
    [
        {"source_version": 2},
        {"source_version": 3, "expected_active_version": 1},
        {"source_version": 1, "expected_active_version": 1},
    ],
)
def test_stored_source_envelope_versions_require_the_exact_preceding_version(values):
    with pytest.raises(ValidationError, match="version") as failure:
        EvidenceAppendRequest(source_id="chat", messages=[message()], **values)
    assert all(error["type"] != "extra_forbidden" for error in failure.value.errors())


def test_resource_annotations_validate_against_the_named_parsed_block():
    resource = EvidenceResourceCreate(
        resource_id="report",
        media_type="text/html",
        filename="report.html",
        byte_length=24,
        sha256="a" * 64,
        source_version=1,
        source_updated_at="2026-01-01T00:00:00Z",
        temporal_annotations=[
            {**annotation(span_start=0, span_end=4), "locator": "html:block:0001"}
        ],
    )
    resource.validate_source_spans({"html:block:0001": "2025 report"})
    with pytest.raises(ValueError, match="quote"):
        resource.validate_source_spans({"html:block:0001": "2024 report"})
    with pytest.raises(ValueError, match="locator"):
        resource.validate_source_spans({"html:block:0002": "2025 report"})
