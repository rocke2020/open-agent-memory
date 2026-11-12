"""Incremental synthesis validates dispositions, targets and complete input bounds."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oam.config import ModelsConfig, load_config
from oam.indexing import extraction
from oam.models.runtime import ModelResult, Usage

CONFIG = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config


def inputs():
    facts = tuple(
        extraction.InsightInput(id=name, text=text, source_refs=("source:1",))
        for name, text in (("new", "The approval rule changed."), ("old", "Approval is required."))
    )
    target = {
        "id": "policy",
        "revision": 1,
        "text": "Approval is required.",
        "title": "Deployment approval",
        "retrieval_text": "Deployment approval",
        "subtype": "projects",
        "supports": [{"id": "old", "revision": 1}, {"id": "prior", "revision": 1}],
    }
    return facts, target


class Reply:
    def __init__(self, actions):
        self.actions = actions
        self.requests = []

    def generate(self, role, **kwargs):
        self.requests.append((role, kwargs))
        return ModelResult(content=json.dumps({"actions": self.actions}), usage=Usage())


def update(**changes):
    return {
        "action": "UPDATE",
        "target_id": "policy",
        "text": "Approval is required; the approval rule changed.",
        "title": "Deployment approval",
        "retrieval_text": "Deployment approval",
        "subtype": "projects",
        "support_ids": ["new"],
        "seed_ids": ["new"],
        **changes,
    }


def synthesize(model, config=CONFIG):
    facts, target = inputs()
    return extraction.consolidate_insights(
        model,
        facts=facts,
        seeds=("new",),
        targets=(target,),
        config=config,
        timeout_seconds=10,
    )


@pytest.mark.parametrize("completion_bound", [8192, 64000])
def test_incremental_update_accepts_one_new_fact_and_uses_dedicated_completion_bound(
    completion_bound,
):
    model = Reply([update()])
    config = CONFIG.model_copy(
        update={
            "index": CONFIG.index.model_copy(update={"insight_max_output_tokens": completion_bound})
        }
    )
    result = synthesize(model, config)
    assert result.actions[0].target_id == "policy"
    assert result.actions[0].title == "Deployment approval"
    assert result.actions[0].support_ids == ("new",)
    assert result.actions[0].expected_revision == 1
    role, request = model.requests[0]
    assert role == "insights"
    assert request["max_output_tokens"] == completion_bound
    prompt = json.loads(request["user"])
    assert prompt["targets"][0]["text"] == "Approval is required."
    assert prompt["seed_ids"] == ["new"]


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [update(seed_ids=[])],
        [update(), update()],
        [update(target_id="unseen")],
        [update(expected_revision=2)],
        [update(support_ids=["unseen"])],
        [update(action="CREATE", target_id=None)],
        [update(action="NO_CHANGE", text=None, retrieval_text=None, subtype=None, support_ids=[])],
    ],
)
def test_incremental_rejects_lost_duplicate_stale_or_unsupported_actions(actions):
    with pytest.raises(extraction.ExtractionError):
        synthesize(Reply(actions))


@pytest.mark.parametrize("title", [None, "", " \t", "x" * 121])
def test_incremental_update_rejects_missing_title(title):
    action = update(title=title)
    if title is None:
        action.pop("title")
    with pytest.raises(extraction.ExtractionError):
        synthesize(Reply([action]))


def test_incremental_update_can_retitle_the_same_insight():
    result = synthesize(Reply([update(title="Production deployment approval")]))
    assert result.actions[0].title == "Production deployment approval"
    assert result.actions[0].target_id == "policy"


def test_keep_text_with_retained_support_normalizes_to_no_change():
    facts, target = inputs()
    target["supports"].append({"id": "new", "revision": 1})
    model = Reply(
        [
            {
                "action": "KEEP_TEXT",
                "target_id": "policy",
                "support_ids": ["new"],
                "seed_ids": ["new"],
            }
        ]
    )
    result = extraction.consolidate_insights(
        model,
        facts=facts,
        seeds=("new",),
        targets=(target,),
        config=CONFIG,
        timeout_seconds=10,
    )
    assert result.actions[0].action == "NO_CHANGE"
    assert result.actions[0].expected_revision == 1


def test_no_insight_accepts_absent_inapplicable_fields():
    result = synthesize(
        Reply(
            [
                update(
                    action="NO_INSIGHT",
                    title=None,
                    target_id=None,
                    text=None,
                    retrieval_text=None,
                    subtype=None,
                    support_ids=[],
                )
            ]
        )
    )
    assert result.actions[0].action == "NO_INSIGHT"


def test_complete_target_that_exceeds_input_budget_fails_before_dispatch():
    facts, target = inputs()
    target["text"] = "Complete historic policy. " * 5000
    model = Reply([update()])
    with pytest.raises(extraction.ExtractionInputTooLongError):
        extraction.consolidate_insights(
            model,
            facts=facts,
            seeds=("new",),
            targets=(target,),
            config=CONFIG,
            timeout_seconds=10,
        )
    assert model.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("insight_seed_max", 2),
        ("insight_candidate_max", 3),
        ("insight_max_output_tokens", 8192),
    ],
)
def test_producing_l2_bounds_bind_enabled_generation_and_not_disabled_generation(field, value):
    from oam.indexing.identity import index_generation

    config = CONFIG.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(update={"index": config.index.model_copy(update={field: value})})
    assert index_generation(config, {}) != index_generation(tuned, {})
    disabled = config.model_copy(
        update={"index": config.index.model_copy(update={"insights_enabled": False})}
    )
    disabled_tuned = tuned.model_copy(
        update={"index": tuned.index.model_copy(update={"insights_enabled": False})}
    )
    assert index_generation(disabled, {}) == index_generation(disabled_tuned, {})


def test_model_boundary_recipe_does_not_require_collection_rebuild(monkeypatch):
    from oam.indexing import identity

    config = CONFIG.model_copy(update={"models": ModelsConfig()})
    disabled = config.model_copy(
        update={"index": config.index.model_copy(update={"insights_enabled": False})}
    )
    enabled_before = identity.index_generation(config, {})
    disabled_before = identity.index_generation(disabled, {})
    monkeypatch.setattr(identity, "INSIGHT_MODEL_RECIPE", "different-model-boundary")
    assert identity.index_generation(config, {}) == enabled_before
    assert identity.index_generation(disabled, {}) == disabled_before


@pytest.mark.parametrize(
    "actions",
    [
        [update(action="NO_INSIGHT", target_id=None)],
        [update(support_ids=["new", "new"])],
        [update(seed_ids=["new", "new"])],
        [update(action="UPDATE", text=" ")],
    ],
)
def test_actions_reject_claims_in_no_insight_and_duplicate_support(actions):
    with pytest.raises(extraction.ExtractionError):
        synthesize(Reply(actions))


def test_create_and_no_insight_can_account_for_different_seeds_in_one_group():
    facts, _ = inputs()
    extra = extraction.InsightInput(
        id="unrelated", text="An isolated report.", source_refs=("other:1",)
    )
    model = Reply(
        [
            update(action="CREATE", target_id=None, support_ids=["new", "old"]),
            update(
                action="NO_INSIGHT",
                title=None,
                target_id=None,
                text=None,
                retrieval_text=None,
                subtype=None,
                support_ids=[],
                seed_ids=["unrelated"],
            ),
        ]
    )
    result = extraction.consolidate_insights(
        model,
        facts=(*facts, extra),
        seeds=("new", "unrelated"),
        targets=(),
        config=CONFIG,
        timeout_seconds=10,
    )
    assert [action.action for action in result.actions] == ["CREATE", "NO_INSIGHT"]


def test_model_projection_excludes_private_state_and_omits_absent_values():
    facts, target = inputs()
    target["vector"] = [0.25, 0.75]
    annotated = facts[0].model_copy(
        update={
            "mentioned_at": datetime(2026, 10, 22, tzinfo=UTC),
            "message_role": extraction.MessageRole.TOOL,
            "temporal_annotations": (
                extraction.TemporalAnnotation(
                    role="event",
                    start="2026-07-03T00:00:00Z",
                    end="2026-07-04T00:00:00Z",
                    quote="on July 3",
                ),
            ),
        }
    )
    model = Reply([update()])
    extraction.consolidate_insights(
        model,
        facts=(annotated, facts[1]),
        seeds=("new",),
        targets=(target,),
        config=CONFIG,
        timeout_seconds=10,
    )
    body = json.loads(model.requests[0][1]["user"])
    assert body == {
        "facts": [
            {
                "id": "new",
                "text": "The approval rule changed.",
                "message_role": "tool",
                "mentioned_at": "2026-10-22T00:00:00Z",
                "temporal_annotations": [
                    {
                        "role": "event",
                        "start": "2026-07-03T00:00:00Z",
                        "end": "2026-07-04T00:00:00Z",
                        "quote": "on July 3",
                    }
                ],
            },
            {"id": "old", "text": "Approval is required."},
        ],
        "seed_ids": ["new"],
        "targets": [
            {
                "id": "policy",
                "title": "Deployment approval",
                "text": "Approval is required.",
                "retrieval_text": "Deployment approval",
            }
        ],
    }
    assert annotated.model_dump(mode="json")["source_refs"] == ["source:1"]


@pytest.mark.parametrize("label", [None, "", "Approval is required.", "Deployment approval"])
def test_target_labels_survive_without_redundancy_classification(label):
    facts, target = inputs()
    target["retrieval_text"] = label
    model = Reply([update()])
    extraction.consolidate_insights(
        model, facts=facts, seeds=("new",), targets=(target,), config=CONFIG, timeout_seconds=10
    )
    supplied = json.loads(model.requests[0][1]["user"])["targets"][0]
    assert supplied == {
        "id": "policy",
        "title": "Deployment approval",
        "text": "Approval is required.",
        **({"retrieval_text": label} if label else {}),
    }


def test_keep_text_adds_support_and_binds_exact_captured_claim_and_revision():
    facts, target = inputs()
    target.update(revision=7, text="  Exact stored claim.  ", title="  Deployment approval  ")
    result = extraction.consolidate_insights(
        Reply(
            [
                {
                    "action": "KEEP_TEXT",
                    "target_id": "policy",
                    "support_ids": ["new"],
                    "seed_ids": ["new"],
                }
            ]
        ),
        facts=facts,
        seeds=("new",),
        targets=(target,),
        config=CONFIG,
        timeout_seconds=10,
    )
    action = result.actions[0]
    assert (action.action, action.expected_revision) == ("UPDATE", 7)
    assert action.title == "  Deployment approval  "
    assert (action.text, action.retrieval_text, action.subtype) == (
        "  Exact stored claim.  ",
        "Deployment approval",
        "projects",
    )
    assert action.support_ids == ("new",)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_id": "unknown"},
        {"support_ids": []},
        {"support_ids": ["old"]},
        {"support_ids": ["unknown"]},
        {"text": "Replacement"},
        {"expected_revision": 1},
    ],
)
def test_keep_text_rejects_unknown_or_unsupported_decisions(changes):
    decision = {
        "action": "KEEP_TEXT",
        "target_id": "policy",
        "support_ids": ["new"],
        "seed_ids": ["new"],
        **changes,
    }
    with pytest.raises(extraction.ExtractionError):
        synthesize(Reply([decision]))


def test_two_distinct_seed_decisions_cannot_address_one_target():
    facts, target = inputs()
    model = Reply(
        [
            {
                "action": "KEEP_TEXT",
                "target_id": "policy",
                "support_ids": [seed],
                "seed_ids": [seed],
            }
            for seed in ("new", "old")
        ]
    )
    with pytest.raises(extraction.ExtractionError, match="duplicate.*target"):
        extraction.consolidate_insights(
            model,
            facts=facts,
            seeds=("new", "old"),
            targets=(target,),
            config=CONFIG,
            timeout_seconds=10,
        )


def test_label_budget_matches_dispatch_and_ignores_private_source_metadata():
    from oam.indexing.consolidation import _pack_facts

    facts, target = inputs()
    facts = tuple(
        fact.model_copy(update={"source_refs": ("private provenance " * 10000,)}) for fact in facts
    )
    expected = {
        "facts": [
            {"id": "new", "text": "The approval rule changed."},
            {"id": "old", "text": "Approval is required."},
        ],
        "seed_ids": ["new"],
        "targets": [
            {
                "id": "policy",
                "title": "Deployment approval",
                "text": "Approval is required.",
                "retrieval_text": "Deployment approval",
            }
        ],
    }
    bound = extraction.model_input_tokens(extraction.INCREMENTAL_INSIGHT_SYSTEM, expected)
    config = CONFIG.model_copy(
        update={"extraction": CONFIG.extraction.model_copy(update={"max_input_tokens": bound})}
    )
    assert _pack_facts(facts, ("new",), (target,), config) == facts
    model = Reply([update()])

    def synthesize_facts():
        return extraction.consolidate_insights(
            model, facts=facts, seeds=("new",), targets=(target,), config=config, timeout_seconds=10
        )

    synthesize_facts()
    assert json.loads(model.requests[0][1]["user"]) == expected
    target["retrieval_text"] = "Label-only context " * 5000
    assert _pack_facts(facts, ("new",), (target,), config) is None
    with pytest.raises(extraction.ExtractionInputTooLongError):
        synthesize_facts()
    assert len(model.requests) == 1
