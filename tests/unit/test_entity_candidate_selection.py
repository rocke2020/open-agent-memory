"""Name and dense candidates share one bounded extraction reference selection."""

import pytest


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        ({"text": "I joined NR.", "context": "Old context only."}, "I joined NR."),
        (
            {"center": {"text": "我加入了NR。"}, "previous_context_only": "旧的上下文"},
            "我加入了NR。",
        ),
        (
            {
                "passages": [
                    {"center": {"text": "I joined NR."}},
                    {"center": {"text": "后来我离开了。"}},
                ],
                "previous_context_only": "unowned",
            },
            "I joined NR.\n后来我离开了。",
        ),
    ],
)
def test_name_candidate_query_uses_owned_centers_in_original_order(inputs, expected):
    from oam.indexing.entity_candidates import source_query_text

    assert source_query_text(inputs) == expected


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (1, ["alias-only"]),
        (2, ["alias-only", "dense-first"]),
        (4, ["alias-only", "dense-first", "other-name", "dense-second"]),
    ],
)
def test_alias_candidates_survive_a_full_dense_pool_without_exceeding_reference_cap(
    limit, expected
):
    from oam.indexing.entity_candidates import merge_reference_ids

    names = [
        {"names": [{"memory_id": "alias-only"}]},
        {"names": [{"memory_id": "other-name"}]},
    ]
    assert (
        merge_reference_ids(names, ["dense-first", "dense-second", "dense-third"], limit)
        == expected
    )


def test_same_fact_proposed_by_multiple_entities_uses_one_reference_slot():
    from oam.indexing.entity_candidates import merge_reference_ids

    names = [{"names": [{"memory_id": "shared"}]}] * 2
    assert merge_reference_ids(names, ["shared", "other"], 3) == ["shared", "other"]


def test_disabled_reference_budget_selects_nothing():
    from oam.indexing.entity_candidates import merge_reference_ids

    assert merge_reference_ids([{"names": [{"memory_id": "old"}]}], ["dense"], 0) == []
