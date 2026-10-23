import copy
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.contracts.evidence import TemporalAnnotation
from oam.indexing.consolidation import _pack_facts
from oam.indexing.extraction import InsightInput
from oam.indexing.store import IndexStore
from oam.models.runtime import ModelResult, Usage


def test_insight_input_preserves_evidence_dates_and_temporal_annotations():
    mentioned_at = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    annotation = TemporalAnnotation(
        role="event",
        start="2019-01-01T00:00:00+00:00",
        end="2020-01-01T00:00:00+00:00",
        quote="in 2019",
    )
    fact = InsightInput(
        id="fact-1",
        text="The user moved in 2019.",
        source_refs=("source-1:1",),
        mentioned_at=mentioned_at,
        temporal_annotations=(annotation,),
    )
    assert fact.mentioned_at == mentioned_at
    assert fact.temporal_annotations == (annotation,)
    assert fact.model_dump(mode="json")["mentioned_at"] == "2026-10-05T09:00:00Z"


class ProgressConnection:
    def __init__(self, state):
        self.state = state

    def execute(self, query, params):
        if query.startswith("SELECT stage_state FROM operations"):
            return SimpleNamespace(fetchone=lambda: (dict(self.state),))
        if query.startswith("UPDATE operations SET stage_state="):
            self.state.update(params[0].obj)
            return None
        raise AssertionError(query)


def test_l2_completion_retains_insight_outcome_after_no_synthesis_group():
    store = object.__new__(IndexStore)
    store.key = ("synthetic-tenant", "synthetic-user")
    store.claim = SimpleNamespace(operation_id="synthetic-operation")
    state = {"l2_examined_ids": ["completed"], "last_insight_outcome": "insight"}
    connection = ProgressConnection(state)

    store.mark_l2_group(connection, ["no-synthesis"], "no_insight")

    assert store.l2_progress(connection) == ({"completed", "no-synthesis"}, "insight")


def test_l2_pack_defers_seed_when_its_complete_admitted_target_cannot_fit():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    seed = InsightInput(id="new", text="New supported state.", source_refs=("source:1",))
    target = {
        "id": "existing",
        "revision": 1,
        "text": "complete knowledge " * 10000,
        "retrieval_text": "Existing claim",
        "subtype": "projects",
        "supports": [],
    }

    assert _pack_facts((seed,), ("new",), (target,), config) is None


def test_l2_pack_samples_old_fact_bodies_without_losing_presented_seeds():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    seed = InsightInput(id="new", text="New supported state.", source_refs=("source:1",))
    oversized = InsightInput(
        id="old-large", text="historical evidence " * 10000, source_refs=("old:1",)
    )
    old = InsightInput(id="old-small", text="Prior related state.", source_refs=("old:2",))
    target = {
        "id": "existing",
        "revision": 1,
        "text": "Complete existing claim.",
        "retrieval_text": "Existing claim",
        "subtype": "projects",
        "supports": [],
    }

    packed = _pack_facts((oversized, old, seed), ("new",), (target,), config)

    assert packed == (seed, old)


@pytest.mark.parametrize("changed", ["revision", "supports", "vector", "source_refs", "recipe"])
def test_private_changes_prevent_checkpoint_reuse_with_identical_model_input(monkeypatch, changed):
    from oam.indexing import consolidation
    from oam.indexing.store import input_digest

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    facts = (InsightInput(id="new", text="Approval still required.", source_refs=("source:1",)),)
    target = {
        "id": "policy",
        "revision": 1,
        "text": "Approval required.",
        "retrieval_text": "Approval",
        "subtype": "projects",
        "vector": [1.0, 0.0],
        "supports": [{"id": "old", "revision": 1}, {"id": "prior", "revision": 1}],
    }
    cache, requests = {}, []

    class Store:
        claim = SimpleNamespace(operation_id="operation")
        generation_id = "generation"
        done = False

        def transaction(self):
            return nullcontext(None)

        def guard(self, connection):
            pass

        def l2_progress(self, connection):
            return set(), None

        def readiness(self, connection, **values):
            pass

        def checkpoint(self, stage, inputs, produce):
            key = (stage, input_digest(inputs))
            if key not in cache:
                cache[key] = produce().model_dump(mode="json")
            return cache[key]

    class Models:
        def generate(self, role, **kwargs):
            requests.append(json.loads(kwargs["user"]))
            return ModelResult(
                content=json.dumps(
                    {
                        "actions": [
                            {
                                "action": "KEEP_TEXT",
                                "target_id": "policy",
                                "support_ids": ["new"],
                                "seed_ids": ["new"],
                            }
                        ]
                    }
                ),
                usage=Usage(),
            )

        def embed(self, *args, **kwargs):
            raise AssertionError("support-only updates must reuse the captured vector")

    store = Store()
    store.config = config
    monkeypatch.setattr(
        consolidation, "_pending_group", lambda *args: [] if store.done else ["seed"]
    )
    monkeypatch.setattr(
        consolidation, "_freeze_group", lambda *args: (facts, ("new",), (target,), (target,))
    )

    def publish(*args, **kwargs):
        store.done = True
        return []

    monkeypatch.setattr(consolidation, "_publish_group", publish)
    consolidation.consolidate_source(store, Models(), ["new"])
    target = copy.deepcopy(target)
    if changed == "recipe":
        monkeypatch.setattr(consolidation, "INSIGHT_MODEL_RECIPE", "changed-boundary")
    elif changed == "source_refs":
        facts = (facts[0].model_copy(update={"source_refs": ("source:2",)}),)
    else:
        target[changed] = {
            "revision": 2,
            "supports": [{"id": "old", "revision": 2}],
            "vector": [0.0, 1.0],
        }[changed]
    store.done = False
    consolidation.consolidate_source(store, Models(), ["new"])
    assert len(requests) == 2
    assert requests[0] == requests[1]
