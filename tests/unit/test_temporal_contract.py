"""Time evidence uses source-supported bounds instead of duplicated temporal flags."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from oam.config import load_config
from oam.contracts.evidence import TemporalAnnotation
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import validate_fact_output
from oam.indexing.store import IndexStore
from oam.models.runtime import Usage
from oam.paths import default_config_path


@pytest.mark.parametrize(
    "start,end,quote,bounds",
    [
        ("2025-03-01T10:30:00Z", "2025-03-01T10:30:00Z", "scheduled at 10:30", "[]"),
        ("2025-03-01T10:15:30Z", "2025-03-01T10:15:30Z", "exactly at 10:15:30", "[]"),
        ("2025-03-01T10:15:00Z", "2025-03-01T10:16:00Z", "during the 10:15 minute", "[)"),
        ("2025-03-01T10:00:00Z", "2025-03-01T11:00:00Z", "sometime in the 10 AM hour", "[)"),
        ("2025-03-01T00:00:00Z", "2025-03-02T00:00:00Z", "on March 1", "[)"),
        ("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "probably in 2025", "[)"),
    ],
)
def test_four_field_annotation_publishes_nonempty_canonical_range(start, end, quote, bounds):
    annotation = TemporalAnnotation(role="event", start=start, end=end, quote=quote)
    statements = []
    connection = SimpleNamespace(execute=lambda sql, values: statements.append((sql, values)))
    store = SimpleNamespace(key=("tenant", "user"))
    IndexStore.insert_annotation(
        store, connection, "fact", annotation, origin="caller", ordinal=0, reference_timezone="UTC"
    )
    values = statements[0][1]
    assert values[-2] == bounds
    assert values[-1].obj == {
        "role": "event",
        "start": start,
        "end": end,
        "quote": quote,
        "origin": "caller",
        "reference_timezone": "UTC",
    }
    assert set(annotation.model_dump()) == {"role", "start", "end", "quote"}


@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2025-01-01T00:00:00"},
        {"end": "2024-01-01T00:00:00Z"},
        {"start": 1735689600},
        {"role": "mentioned"},
    ],
)
def test_invalid_temporal_bounds_or_role_fail_admission(changes):
    with pytest.raises(ValidationError):
        TemporalAnnotation.model_validate(
            {
                "role": "event",
                "start": "2025-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
                "quote": "in 2025",
                **changes,
            }
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("instant", False),
        ("granularity", "year"),
        ("meaning", "possible_period"),
        ("bounds", "[)"),
        ("origin", "caller"),
        ("reference_timezone", "UTC"),
    ],
)
def test_retired_annotation_fields_are_rejected_explicitly(key, value):
    with pytest.raises(ValidationError) as failure:
        TemporalAnnotation.model_validate(
            {
                "role": "event",
                "start": "2025-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
                "quote": "in 2025",
                key: value,
            }
        )
    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == (key,)
        for error in failure.value.errors()
    )


def test_aware_datetime_point_preserves_event_reference_role():
    when = datetime(2025, 1, 1, tzinfo=UTC)
    annotation = TemporalAnnotation(
        role="date_reference", start=when, end=when, quote="exactly at 2025-01-01T00:00:00Z"
    )
    assert annotation.role == "date_reference"
    assert annotation.start == annotation.end == when


@pytest.mark.parametrize(
    "text,start,end,role",
    [
        (
            "A pulse happened exactly at 2025-03-01T10:15:30Z.",
            "2025-03-01T10:15:30Z",
            "2025-03-01T10:15:30Z",
            "event",
        ),
        (
            "An alert happened during the 10:15 minute on March 1, 2025.",
            "2025-03-01T10:15:00Z",
            "2025-03-01T10:16:00Z",
            "event",
        ),
        (
            "A failure happened sometime in the 10 AM hour on March 1, 2025.",
            "2025-03-01T10:00:00Z",
            "2025-03-01T11:00:00Z",
            "event",
        ),
        (
            "A visit happened on March 1, 2025.",
            "2025-03-01T00:00:00Z",
            "2025-03-02T00:00:00Z",
            "event",
        ),
        (
            "The user probably moved in 2025.",
            "2025-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "event",
        ),
        (
            "The user discussed a possible trip in 2025.",
            "2025-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "date_reference",
        ),
    ],
)
def test_generated_time_keeps_precision_uncertainty_and_role_in_supported_text(
    text, start, end, role
):
    result = validate_fact_output(
        {
            "outcome": "facts",
            "facts": [
                {
                    "text": text,
                    "quote": text,
                    "annotations": [{"role": role, "start": start, "end": end, "quote": text}],
                }
            ],
        },
        Usage(),
        load_config(default_config_path()).config,
        {None: TextChunk(text, 0, len(text), "", 20)},
        set(),
    )
    assert result.outcome == "facts" and result.diagnostics == ()
    fact = result.facts[0]
    assert fact.text == fact.quote == text
    assert fact.annotations[0].model_dump(mode="json") == {
        "role": role,
        "start": start,
        "end": end,
        "quote": text,
    }
