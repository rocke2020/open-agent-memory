from typing import Any

import pytest

from oam.profiles.delta import apply_profile_edits, parse_profile_blocks, render_profile_blocks

BASE = (
    "## Main content\n\n- The user likes sailing [1].\n"
    "- The user prefers English [2].\n\n## References\n\n"
    "1. Conversation on 2026-10-01\n2. Conversation on 2026-10-02"
)


def test_blank_lines_between_bullets_survive_incremental_edits() -> None:
    body = (
        "- The user likes sailing [1].\n\n- The user prefers English [2]."
        "\n \n- The user studies memory [3]."
    )
    content = f"## Main content\n\n{body}\n\n## References\n\n1. A\n2. B\n3. C"
    blocks = parse_profile_blocks(content, ["sailing", "language", "memory"])
    assert render_profile_blocks(blocks) == (body, ["sailing", "language", "memory"])
    changed = apply_profile_edits(
        blocks,
        [
            {
                "action": "replace",
                "block_id": blocks[1].block_id,
                "text": "- The user prefers Chinese [1].",
                "support_ids": ["new"],
            }
        ],
        {"new"},
    )
    text, ids = render_profile_blocks(changed)
    assert text == body.replace("English [2]", "Chinese [2]")
    assert ids == ["sailing", "new", "memory"]
    deleted = apply_profile_edits(
        blocks,
        [{"action": "delete", "block_id": blocks[1].block_id, "support_ids": ["new"]}],
        {"new"},
    )
    assert render_profile_blocks(deleted) == (
        "- The user likes sailing [1].\n \n- The user studies memory [2].",
        ["sailing", "memory"],
    )


def test_cleanup_empty_baseline_accepts_no_change_and_new_claim() -> None:
    from oam.profiles.delta import build_incremental_document

    snapshot: dict[str, Any] = {
        "content": (
            "## Main content\n\nNo supported details yet.\n\n## References\n\nNo references yet."
        ),
        "support_ids": [],
        "references": [],
    }
    assert parse_profile_blocks(snapshot["content"], []) == []
    assert (
        build_incremental_document(
            '{"no_change":true,"edits":[]}', snapshot, [], max_content_bytes=10000
        )
        == snapshot
    )
    item = {
        "id": "new",
        "kind": "fact",
        "source_passages": [{"source_id": "new", "source_version": 1, "passage_id": "p"}],
    }
    result = build_incremental_document(
        '{"no_change":false,"edits":[{"action":"add",'
        '"text":"- The user prefers English [1].","support_ids":["new"]}]}',
        snapshot,
        [item],
        max_content_bytes=10000,
    )
    assert "English [1]" in result["content"]
    assert result["support_ids"] == ["new"]


def test_incremental_edit_preserves_untouched_claim_and_citations() -> None:
    blocks = parse_profile_blocks(BASE, ["old_sailing", "old_language"])
    result = apply_profile_edits(
        blocks,
        [
            {
                "action": "add",
                "text": "- The user is building a boat [1].",
                "support_ids": ["new_boat"],
            }
        ],
        {"old_sailing", "old_language", "new_boat"},
    )
    body, ids = render_profile_blocks(result)
    assert "The user likes sailing [1]." in body
    assert "The user prefers English [2]." in body
    assert "The user is building a boat [3]." in body
    assert ids == ["old_sailing", "old_language", "new_boat"]


def test_incremental_rejects_unknown_targets_and_uncited_edits() -> None:
    blocks = parse_profile_blocks(BASE, ["old_sailing", "old_language"])
    with pytest.raises(ValueError):
        apply_profile_edits(blocks, [{"action": "delete", "block_id": "missing"}], {"old_sailing"})
    with pytest.raises(ValueError):
        apply_profile_edits(
            blocks, [{"action": "add", "text": "- A claim.", "support_ids": []}], set()
        )


def test_unstructured_baseline_cannot_be_incrementally_edited() -> None:
    with pytest.raises(ValueError):
        parse_profile_blocks("## Main content\n\nUnsupported prose.\n\n## References\n\nNone", [])


def test_incremental_manifest_only_uses_new_changed_or_pending_inputs() -> None:
    from oam.profiles.delta import incremental_candidates

    old = {
        "units": {"a": "hash-a", "b": "hash-b"},
        "examined": ["a"],
        "sources": {"s": 1},
        "exact": "x",
        "recipe": "r",
    }
    current: dict[str, Any] = {
        "units": {"a": "hash-a", "b": "hash-b", "c": "hash-c"},
        "sources": {"s": 1, "new": 1},
        "exact": "x",
        "recipe": "r",
    }
    assert incremental_candidates(old, current) == {"c"}
    current["units"]["a"] = "changed-support"
    assert incremental_candidates(old, current) == {"a", "c"}
    old["pending"] = ["b"]
    assert incremental_candidates(old, current) == {"a", "b", "c"}


def test_incremental_manifest_handles_removed_sources_but_rejects_changed_recipe() -> None:
    from oam.profiles.delta import incremental_candidates

    old = {
        "units": {"a": "v1"},
        "examined": ["a"],
        "sources": {"s": 1},
        "exact": "x",
        "recipe": "r",
    }
    current: dict[str, Any] = {
        "units": {"a": "v1"},
        "sources": {"s": 2},
        "exact": "x",
        "recipe": "r",
    }
    assert incremental_candidates(old, current) == set()
    current["sources"]["s"] = 1
    current["recipe"] = "changed"
    assert incremental_candidates(old, current) is None


def test_retractions_remove_only_actually_cited_affected_blocks_and_rehydrate() -> None:
    import hashlib

    from oam.profiles.delta import prune_profile_baseline

    def key(value):
        return hashlib.sha256(value.encode()).hexdigest()

    snapshot = {
        "content": BASE,
        "support_ids": ["old_sailing", "old_language"],
        "input_manifest": {"units": {key("old_sailing"): "old", key("old_language"): "same"}},
    }
    fresh = {
        "id": "old_language",
        "kind": "fact",
        "source_passages": [{"source_id": "current", "source_version": 2, "passage_id": "now"}],
    }
    result, count = prune_profile_baseline(
        snapshot, {"units": {key("old_language"): "same"}}, [fresh], max_content_bytes=10000
    )
    assert count == 1
    assert "sailing" not in result["content"]
    assert "The user prefers English [1]." in result["content"]
    assert result["support_ids"] == ["old_language"]
    assert result["references"][0]["source_id"] == "current"


def test_all_retracted_support_produces_empty_supported_baseline() -> None:
    from oam.profiles.delta import prune_profile_baseline

    snapshot = {
        "content": BASE,
        "support_ids": ["old_sailing", "old_language"],
        "input_manifest": {"units": {}},
    }
    result, count = prune_profile_baseline(snapshot, {"units": {}}, [], max_content_bytes=10000)
    assert count == 2
    assert result["support_ids"] == []
    assert parse_profile_blocks(result["content"], []) == []


def test_delta_response_requires_explicit_no_change() -> None:
    from oam.profiles.delta import parse_profile_edits

    assert parse_profile_edits('{"no_change":true,"edits":[]}') == []
    with pytest.raises(ValueError):
        parse_profile_edits('{"no_change":false,"edits":[]}')
    with pytest.raises(ValueError):
        parse_profile_edits('{"no_change":true,"edits":[{}]}')


def test_delta_document_preserves_old_references_and_adds_new_evidence() -> None:
    from oam.profiles.delta import build_incremental_document

    snapshot = {
        "content": BASE,
        "support_ids": ["old_sailing", "old_language"],
        "references": [
            {
                "support_id": identifier,
                "kind": "fact",
                "passages": [
                    {"source_id": identifier, "source_version": 1, "passage_id": identifier}
                ],
            }
            for identifier in ("old_sailing", "old_language")
        ],
    }
    new = {
        "id": "new",
        "kind": "fact",
        "source_passages": [
            {"source_id": "new-source", "source_version": 1, "passage_id": "new-passage"}
        ],
    }
    result = build_incremental_document(
        '{"no_change":false,"edits":[{"action":"add","text":"- The user builds boats [1].",'
        '"support_ids":["new"]}]}',
        snapshot,
        [new],
        max_content_bytes=10000,
    )
    assert result["support_ids"] == ["old_sailing", "old_language", "new"]
    assert result["references"][0]["passages"][0]["source_id"] == "old_sailing"
    assert "boats [3]" in result["content"]
    with pytest.raises(ValueError, match="unavailable"):
        build_incremental_document(
            '{"no_change":false,"edits":[{"action":"add","text":"- Invented [1].",'
            '"support_ids":["old_sailing"]}]}',
            snapshot,
            [new],
            max_content_bytes=10000,
        )


def test_incremental_prompt_keeps_profile_policies_without_full_response_contract() -> None:
    from oam.profiles.delta import incremental_system
    from oam.profiles.prompt import PROFILE_SYSTEM

    system = incremental_system([])
    selection = PROFILE_SYSTEM.split("SELECT AND SUMMARIZE\n", 1)[1].split(
        "\nGROUNDING AND CUSTOM FORMAT", 1
    )[0]
    assert selection in system
    assert "never they/them/their" in system
    assert '"support_ids":["<selected supplied input or exact-field id>"]' not in system


def test_replacement_profile_prompt_keeps_rules_without_required_section_headings() -> None:
    from oam.profiles.delta import _profile_content_policy

    policy = _profile_content_policy("Use only stated facts and preserve dates.")
    assert "Use only stated facts and preserve dates." in policy
    assert "preserve the incremental JSON schema" in policy
