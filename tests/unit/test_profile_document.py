import json
from typing import Any

import pytest

from oam.profiles.document import build_profile_document, render_stored_profile_fields

INPUTS = [
    {
        "kind": "fact",
        "id": "fact_1",
        "text": "The user prefers concise explanations.",
        "source_passages": [
            {
                "source_id": "chat_1",
                "source_version": 1,
                "passage_id": "passage_1",
                "event_time": "2026-09-01T09:00:00Z",
            }
        ],
    }
]
EXACT = [
    {
        "section": "main",
        "key": "date_of_birth",
        "value": "1990-05-10",
        "qualifier": "",
        "supports": [{"kind": "profile_resource", "resource_id": "basic", "resource_version": 2}],
    }
]


def test_default_profile_has_one_content_document_and_eligible_references() -> None:
    result = build_profile_document(
        json.dumps(
            {"content": "The user prefers concise explanations [1].", "support_ids": ["fact_1"]}
        ),
        INPUTS,
        EXACT,
        instruction=None,
        max_content_bytes=4096,
    )

    assert result["content"].startswith("## Main content\n\n")
    assert "The user prefers concise explanations [1]." in result["content"]
    assert "Date of birth: 1990-05-10" in result["content"]
    assert result["content"].count("## References") == 1
    assert "Conversation on 2026-09-01" in result["content"]
    assert "Provided profile details" in result["content"]
    assert result["support_ids"] == ["fact_1", "profile_resource:basic:2"]


def test_custom_json_remains_one_text_artifact_with_separate_support_metadata() -> None:
    content = '{"summary":"The user prefers concise explanations."}'
    result = build_profile_document(
        json.dumps({"content": content, "support_ids": ["fact_1"]}),
        INPUTS,
        [],
        instruction="Return JSON with summary",
        max_content_bytes=4096,
    )

    assert result["content"] == content
    assert result["support_ids"] == ["fact_1"]


def test_profile_rejects_support_the_model_did_not_receive() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        build_profile_document(
            json.dumps({"content": "A claim", "support_ids": ["withdrawn_fact"]}),
            INPUTS,
            [],
            instruction=None,
            max_content_bytes=4096,
        )


def test_generated_empty_body_cannot_replace_a_profile() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_profile_document(
            json.dumps({"content": " ", "support_ids": []}),
            INPUTS,
            [],
            instruction=None,
            max_content_bytes=4096,
        )


def test_custom_document_can_cite_an_exact_profile_resource() -> None:
    result = build_profile_document(
        json.dumps(
            {
                "content": '{"date_of_birth":"1990-05-10"}',
                "support_ids": ["profile_resource:basic:2"],
            }
        ),
        [],
        EXACT,
        instruction="Return one JSON object",
        max_content_bytes=4096,
    )

    assert json.loads(result["content"])["date_of_birth"] == "1990-05-10"
    assert result["references"][0]["resource_id"] == "basic"


def test_insight_reference_keeps_all_supplied_source_passages() -> None:
    insight = {
        **INPUTS[0],
        "kind": "insight",
        "id": "insight_1",
        "revision": 2,
        "source_passages": [
            INPUTS[0]["source_passages"][0],
            {
                "source_id": "chat_2",
                "source_version": 1,
                "passage_id": "passage_2",
                "event_time": "2026-09-02T09:00:00Z",
            },
        ],
    }
    result = build_profile_document(
        json.dumps(
            {
                "content": "The user consistently prefers concise replies [1].",
                "support_ids": ["insight_1"],
            }
        ),
        [insight],
        [],
        instruction=None,
        max_content_bytes=4096,
    )

    assert "Conversation on 2026-09-01" in result["content"]
    assert "Conversation on 2026-09-02" in result["content"]
    assert [item["source_id"] for item in result["references"][0]["passages"]] == [
        "chat_1",
        "chat_2",
    ]


def test_default_profile_rejects_a_reference_number_without_a_matching_support() -> None:
    with pytest.raises(ValueError, match="reference marker"):
        build_profile_document(
            json.dumps(
                {"content": "The user likes concise replies [2].", "support_ids": ["fact_1"]}
            ),
            INPUTS,
            [],
            instruction=None,
            max_content_bytes=4096,
        )


def test_default_profile_deduplicates_supports_without_changing_citation_sources() -> None:
    second = {
        **INPUTS[0],
        "id": "fact_2",
        "source_passages": [
            {
                "source_id": "chat_2",
                "source_version": 1,
                "passage_id": "passage_2",
                "event_time": None,
            }
        ],
    }
    result = build_profile_document(
        json.dumps(
            {
                "content": "First claim [1]. Second claim [2]. First source again [3].",
                "support_ids": ["fact_1", "fact_2", "fact_1"],
            }
        ),
        [INPUTS[0], second],
        EXACT,
        instruction=None,
        max_content_bytes=4096,
    )

    assert "First claim [1]. Second claim [2]. First source again [1]." in result["content"]
    assert result["support_ids"] == ["fact_1", "fact_2", "profile_resource:basic:2"]
    assert [reference.get("source_id") for reference in result["references"][:2]] == [
        "chat_1",
        "chat_2",
    ]
    assert "Date of birth: 1990-05-10 [3]" in result["content"]

    with pytest.raises(ValueError, match="reference marker"):
        build_profile_document(
            json.dumps(
                {
                    "content": "First [1], second [2], invalid [3].",
                    "support_ids": ["fact_1", "fact_2"],
                }
            ),
            [INPUTS[0], second],
            EXACT,
            instruction=None,
            max_content_bytes=4096,
        )
    with pytest.raises(ValueError, match="reference marker"):
        build_profile_document(
            json.dumps({"content": "Bad claim [4].", "support_ids": ["fact_1", "fact_1"]}),
            INPUTS,
            [],
            instruction=None,
            max_content_bytes=4096,
        )
    with pytest.raises(ValueError, match="unsupported input"):
        build_profile_document(
            json.dumps({"content": "Bad claim [1].", "support_ids": ["unknown", "unknown"]}),
            INPUTS,
            [],
            instruction=None,
            max_content_bytes=4096,
        )
    with pytest.raises(ValueError, match="repeated a support"):
        build_profile_document(
            json.dumps({"content": '{"references":[1,2]}', "support_ids": ["fact_1", "fact_1"]}),
            INPUTS,
            [],
            instruction="Return JSON.",
            max_content_bytes=4096,
        )


def test_stored_fields_preserve_conflicts_and_all_resource_references() -> None:
    supports = [
        {"kind": "profile_resource", "resource_id": "owner", "resource_version": 2},
        {"kind": "profile_resource", "resource_id": "imported", "resource_version": 1},
    ]
    sections: dict[str, list[dict[str, Any]]] = {
        "main": [
            {
                "key": "role",
                "value": "Stored role",
                "qualifier": "at the time of the conversation",
                "state": "conflict",
                "supports": supports,
            }
        ],
        "experiences": [
            {
                "key": "episode",
                "value": {
                    "situation": "A task",
                    "action": "A change",
                    "observed_outcome": "Done",
                    "conditions": ["One project"],
                },
                "qualifier": "",
                "state": "active",
                "supports": supports,
            }
        ],
    }
    eligible = {"profile_resource:owner:2", "profile_resource:imported:1"}

    document = render_stored_profile_fields(sections, [], eligible, max_content_bytes=4096)

    assert document["content"].count("Stored role") == 1
    assert "at the time of the conversation" in document["content"]
    assert "conflicting sources" in document["content"]
    assert '"observed_outcome": "Done"' in document["content"]
    assert '"conditions": ["One project"]' in document["content"]
    assert document["support_ids"] == ["profile_resource:owner:2", "profile_resource:imported:1"]
    assert [reference["resource_id"] for reference in document["references"]] == [
        "owner",
        "imported",
    ]
    assert document["content"].count("[1] [2]") == 2

    with pytest.raises(ValueError, match="no longer eligible"):
        render_stored_profile_fields(
            sections, [], {"profile_resource:owner:2"}, max_content_bytes=4096
        )
    with pytest.raises(ValueError, match="size limit"):
        render_stored_profile_fields(sections, [], eligible, max_content_bytes=200)


def test_insight_reference_preserves_canonical_identity_separate_from_search_projection():
    insight = {
        **INPUTS[0],
        "kind": "insight",
        "id": "projection_revision_2",
        "insight_id": "stable_insight",
        "revision": 2,
    }
    result = build_profile_document(
        json.dumps({"content": "A supported claim [1].", "support_ids": ["projection_revision_2"]}),
        [insight],
        [],
        instruction=None,
        max_content_bytes=4096,
    )
    assert result["references"][0]["support_id"] == "projection_revision_2"
    assert result["references"][0]["insight_id"] == "stable_insight"
    assert result["references"][0]["revision"] == 2
