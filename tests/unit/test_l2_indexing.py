from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from oam.config import load_config
from oam.contracts.evidence import TemporalAnnotation
from oam.indexing.consolidation import _pack_facts
from oam.indexing.extraction import InsightInput
from oam.indexing.store import IndexStore


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
