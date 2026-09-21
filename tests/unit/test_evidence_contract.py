from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from oam.config import load_config
from oam.contracts.evidence import EvidenceAppendRequest, MessageRole


def test_learning_eligibility_is_explicit_and_defaults_closed() -> None:
    request = EvidenceAppendRequest(
        source_id="conversation-1",
        messages=[
            {
                "message_id": "m1",
                "role": "user",
                "content": "Prefer Python examples.",
                "event_time": "2026-09-21T00:00:00Z",
                "learning_eligible": True,
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "I used the report to answer.",
                "event_time": "2026-09-21T00:00:01Z",
            },
        ],
    )

    assert request.messages[0].role is MessageRole.USER
    assert request.messages[0].learning_eligible is True
    assert request.messages[1].learning_eligible is False
    assert request.messages[0].event_time == datetime(2026, 9, 21, tzinfo=UTC)


def test_public_body_cannot_select_tenant_or_scope() -> None:
    with pytest.raises(ValidationError):
        EvidenceAppendRequest(
            source_id="conversation-1",
            tenant_id="tenant-1",
            scope_id="scope-1",
            messages=[
                {
                    "message_id": "m1",
                    "role": "user",
                    "content": "Hello",
                    "event_time": "2026-09-21T00:00:00Z",
                }
            ],
        )


def test_interaction_payload_rejects_more_than_128_kibibytes() -> None:
    config = load_config(Path(__file__).parents[2] / "configs" / "oam.yaml").config
    request = EvidenceAppendRequest(
        source_id="conversation-1",
        messages=[
            {
                "message_id": "m1",
                "role": "user",
                "content": "x" * (128 * 1024 + 1),
                "event_time": "2026-09-21T00:00:00Z",
            }
        ],
    )
    with pytest.raises(ValueError, match="131072"):
        request.validate_limits(config)


def test_interaction_limits_follow_the_selected_config(tmp_path: Path) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text("ingest:\n  max_bytes: 6\n  max_messages: 1\n")
    config = load_config(path).config
    message = {
        "message_id": "m1",
        "role": "user",
        "content": "你好",
        "event_time": "2026-09-21T00:00:00Z",
    }
    EvidenceAppendRequest(source_id="s1", messages=[message]).validate_limits(config)

    with pytest.raises(ValueError, match="6 bytes"):
        EvidenceAppendRequest(
            source_id="s1", messages=[{**message, "content": "你好!"}]
        ).validate_limits(config)
    with pytest.raises(ValueError, match="1 messages"):
        EvidenceAppendRequest(source_id="s1", messages=[message, message]).validate_limits(config)


def test_selected_config_can_raise_interaction_limits(tmp_path: Path) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text("ingest:\n  max_bytes: 262144\n  max_messages: 129\n")
    config = load_config(path).config
    message = {
        "message_id": "m1",
        "role": "user",
        "content": "x" * 1024,
        "event_time": "2026-09-21T00:00:00Z",
    }

    EvidenceAppendRequest(source_id="s1", messages=[message] * 129).validate_limits(config)
