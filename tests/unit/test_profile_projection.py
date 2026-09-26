import json

import pytest

from oam.profiles.projection import build_sections, select_sections


def test_generated_profile_requires_current_source_support() -> None:
    supported = {"fact_1": {"kind": "fact", "id": "fact_1"}}
    content = json.dumps(
        {
            "main": [{"key": "role", "value": "engineer", "support_ids": ["fact_1"]}],
            "preferences": [],
            "experiences": [],
        }
    )

    sections = build_sections(content, supported, (), max_fields=32, max_value_bytes=2048)

    assert sections["main"][0]["value"] == "engineer"
    assert sections["main"][0]["supports"] == [{"kind": "fact", "id": "fact_1"}]
    with pytest.raises(ValueError, match="unsupported"):
        build_sections(content, {}, (), max_fields=32, max_value_bytes=2048)


def test_exact_profile_resource_field_cannot_be_overwritten_by_generation() -> None:
    exact = (
        {
            "section": "preferences",
            "key": "example_language",
            "value": "Python",
            "supports": [{"kind": "profile_resource", "resource_id": "preferences"}],
        },
    )
    content = json.dumps(
        {
            "main": [],
            "preferences": [
                {"key": "example_language", "value": "Java", "support_ids": ["fact_1"]}
            ],
            "experiences": [],
        }
    )

    sections = build_sections(
        content,
        {"fact_1": {"kind": "fact", "id": "fact_1"}},
        exact,
        max_fields=32,
        max_value_bytes=2048,
    )

    assert [field["value"] for field in sections["preferences"]] == ["Python"]


def test_requested_sections_obey_shared_budget_and_current_support() -> None:
    sections = {
        "main": [
            {
                "field_id": "main_role",
                "key": "role",
                "value": "engineer",
                "supports": [{"kind": "fact", "id": "fact_1"}],
            }
        ],
        "preferences": [
            {
                "field_id": "pref_language",
                "key": "language",
                "value": "English",
                "supports": [{"kind": "fact", "id": "fact_2"}],
            }
        ],
        "experiences": [],
    }

    selected = select_sections(sections, ["main", "preferences"], {"fact_1"}, max_tokens=100)

    assert [field["field_id"] for field in selected["sections"]["main"]] == ["main_role"]
    assert selected["sections"]["preferences"] == []
    assert selected["freshness"] == "stale"
    assert select_sections(sections, ["main"], {"fact_1"}, max_tokens=0)["sections"] == {"main": []}


def test_exact_field_id_stays_stable_across_resource_revisions() -> None:
    def mapped(version: int):
        return (
            {
                "section": "main",
                "key": "date_of_birth",
                "value": "1990-05-10" if version == 1 else "1991-05-10",
                "supports": [
                    {
                        "kind": "profile_resource",
                        "resource_id": "basic",
                        "resource_version": version,
                    }
                ],
            },
        )

    first = build_sections(
        '{"main":[],"preferences":[],"experiences":[]}',
        {},
        mapped(1),
        max_fields=32,
        max_value_bytes=2048,
    )
    second = build_sections(
        '{"main":[],"preferences":[],"experiences":[]}',
        {},
        mapped(2),
        max_fields=32,
        max_value_bytes=2048,
    )

    assert first["main"][0]["field_id"] == second["main"][0]["field_id"]


def test_distinct_profile_resources_expose_conflicts_without_dropping_support() -> None:
    exact = (
        {
            "section": "main",
            "key": "location",
            "value": "Hangzhou",
            "supports": [
                {"kind": "profile_resource", "resource_id": "home", "resource_version": 1}
            ],
        },
        {
            "section": "main",
            "key": "location",
            "value": "Shanghai",
            "supports": [
                {"kind": "profile_resource", "resource_id": "office", "resource_version": 1}
            ],
        },
    )

    sections = build_sections(
        '{"main":[],"preferences":[],"experiences":[]}',
        {},
        exact,
        max_fields=32,
        max_value_bytes=2048,
    )

    assert {field["value"] for field in sections["main"]} == {"Hangzhou", "Shanghai"}
    assert {field["state"] for field in sections["main"]} == {"conflict"}


def test_identical_exact_values_combine_support_from_two_resources() -> None:
    exact = (
        {
            "section": "main",
            "key": "location",
            "value": "Hangzhou",
            "supports": [
                {"kind": "profile_resource", "resource_id": "home", "resource_version": 1}
            ],
        },
        {
            "section": "main",
            "key": "location",
            "value": "Hangzhou",
            "supports": [
                {"kind": "profile_resource", "resource_id": "office", "resource_version": 1}
            ],
        },
    )

    sections = build_sections(
        '{"main":[],"preferences":[],"experiences":[]}',
        {},
        exact,
        max_fields=32,
        max_value_bytes=2048,
    )

    assert len(sections["main"]) == 1
    assert {ref["resource_id"] for ref in sections["main"][0]["supports"]} == {"home", "office"}


def test_generated_conflicts_have_distinct_ids_and_conflict_state() -> None:
    content = json.dumps(
        {
            "main": [
                {"key": "location", "value": "Hangzhou", "support_ids": ["fact_1"]},
                {"key": "location", "value": "Shanghai", "support_ids": ["fact_2"]},
            ],
            "preferences": [],
            "experiences": [],
        }
    )
    supported = {
        "fact_1": {"kind": "fact", "id": "fact_1"},
        "fact_2": {"kind": "fact", "id": "fact_2"},
    }

    sections = build_sections(content, supported, (), max_fields=32, max_value_bytes=2048)

    assert {field["value"] for field in sections["main"]} == {"Hangzhou", "Shanghai"}
    assert len({field["field_id"] for field in sections["main"]}) == 2
    assert {field["state"] for field in sections["main"]} == {"conflict"}


def test_independent_experiences_are_not_scalar_conflicts() -> None:
    content = json.dumps(
        {
            "main": [],
            "preferences": [],
            "experiences": [
                {
                    "key": "episode",
                    "value": {
                        "situation": "garden",
                        "action": "planted pears",
                        "observed_outcome": "harvested pears",
                        "conditions": "spring",
                    },
                    "support_ids": ["fact_1"],
                },
                {
                    "key": "episode",
                    "value": {
                        "situation": "kitchen",
                        "action": "baked bread",
                        "observed_outcome": "shared bread",
                        "conditions": "winter",
                    },
                    "support_ids": ["fact_2"],
                },
            ],
        }
    )
    supported = {
        "fact_1": {"kind": "fact", "id": "fact_1"},
        "fact_2": {"kind": "fact", "id": "fact_2"},
    }

    sections = build_sections(content, supported, (), max_fields=32, max_value_bytes=2048)

    assert len(sections["experiences"]) == 2
    assert {field["state"] for field in sections["experiences"]} == {"active"}


@pytest.mark.parametrize("support_order", [["fact_1", "insight_1"], ["insight_1", "fact_1"]])
def test_merged_generated_claim_keeps_insight_attribution(support_order: list[str]) -> None:
    content = json.dumps(
        {
            "main": [
                {"key": "role", "value": "engineer", "support_ids": [support_id]}
                for support_id in support_order
            ],
            "preferences": [],
            "experiences": [],
        }
    )
    supported = {
        "fact_1": {"kind": "fact", "id": "fact_1"},
        "insight_1": {"kind": "insight", "id": "insight_1"},
    }

    sections = build_sections(content, supported, (), max_fields=32, max_value_bytes=2048)

    assert len(sections["main"]) == 1
    assert {ref["id"] for ref in sections["main"][0]["supports"]} == set(support_order)
    assert sections["main"][0]["assertion_kind"] == "inferred"
