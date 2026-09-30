import json

import pytest

from oam.profiles.document import build_profile_document

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
