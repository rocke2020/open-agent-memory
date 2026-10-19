"""Incremental synthesis validates dispositions, targets and complete input bounds."""

import json
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
        "expected_revision": 1,
        "text": "Approval is required; the approval rule changed.",
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
    assert result.actions[0].support_ids == ("new",)
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
        [update(action="CREATE", target_id=None, expected_revision=None)],
        [update(action="NO_CHANGE", text=None, retrieval_text=None, subtype=None, support_ids=[])],
    ],
)
def test_incremental_rejects_lost_duplicate_stale_or_unsupported_actions(actions):
    with pytest.raises(extraction.ExtractionError):
        synthesize(Reply(actions))


def test_no_change_requires_existing_coverage_and_no_insight_has_no_claim():
    facts, target = inputs()
    target["supports"].append({"id": "new", "revision": 1})
    model = Reply(
        [update(action="NO_CHANGE", text=None, retrieval_text=None, subtype=None, support_ids=[])]
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
    result = synthesize(
        Reply(
            [
                update(
                    action="NO_INSIGHT",
                    target_id=None,
                    expected_revision=None,
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


@pytest.mark.parametrize(
    "actions",
    [
        [update(action="NO_INSIGHT", target_id=None, expected_revision=None)],
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
            update(
                action="CREATE", target_id=None, expected_revision=None, support_ids=["new", "old"]
            ),
            update(
                action="NO_INSIGHT",
                target_id=None,
                expected_revision=None,
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
