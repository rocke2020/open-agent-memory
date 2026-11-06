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


def test_l2_generation_capacity_is_validated_without_changing_content_identity(tmp_path):
    from oam.indexing.identity import index_generation

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    override = tmp_path / "parallel.yaml"
    override.write_text("index:\n  insight_generation_concurrency: 4\n")
    parallel = load_config(override).config
    assert parallel.index.insight_generation_concurrency == 4
    environment = {
        "VLM_MODEL": "synthetic",
        "VLM_BASE_URL": "https://unused.test/v1",
        "VLM_API_KEY": "synthetic",
        "OAM_EMBEDDING_MODEL": "synthetic",
        "OAM_EMBEDDING_URL": "https://unused.test/v1",
        "OAM_EMBEDDING_KEY": "synthetic",
    }
    assert index_generation(config, environment) == index_generation(parallel, environment)


def test_l2_capacity_keeps_captured_legacy_generation_and_checkpoint(monkeypatch):
    monkeypatch.setattr("oam.indexing.identity.INDEX_RECIPE", "oam-source-fact-insight-v38")
    from oam.config import ModelsConfig, TokenizerConfig
    from oam.indexing.consolidation import _group_inputs
    from oam.indexing.identity import index_generation
    from oam.indexing.store import input_digest

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "models": ModelsConfig(),
            "tokenizer": TokenizerConfig(type="oam-unicode-quarter-v1"),
        }
    )
    facts = (
        InsightInput(id="a", text="The user uses glaze G41.", source_refs=("source:1",)),
        InsightInput(id="b", text="The user fires at 1240 C.", source_refs=("source:1",)),
    )
    for cap in (1, 4):
        selected = config.model_copy(
            update={
                "index": config.index.model_copy(update={"insight_generation_concurrency": cap})
            }
        )
        assert index_generation(selected, {}) == (
            "gen_bcbbe1a13f79116323228293c323d0ec162fe99987a3a54822029f0cb3563c8b"
        )
        inputs = _group_inputs(
            SimpleNamespace(config=selected, generation_id="legacy"), facts, ("a", "b"), (), []
        )
        assert input_digest(inputs).hex() == (
            "1ee6e49dc7e3abed2e8a68c3a8bd691c02e5469c0bd88c13b4a010ff8f297959"
        )


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


def test_parallel_selection_defers_shared_targets_and_facts_and_refreshes(monkeypatch):
    from oam.indexing import consolidation

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"insight_generation_concurrency": 2})}
    )
    store = SimpleNamespace(
        config=config,
        transaction=lambda **kwargs: nullcontext(None),
        claim=SimpleNamespace(operation_id="operation"),
    )
    groups = [{"unit_id": str(i), "memory_id": str(i)} for i in range(4)]
    frozen, looked_up = [], []

    def pending(store, connection, identifiers, examined):
        return next(([group] for group in groups if group["unit_id"] not in examined), [])

    def lookup(store, connection, group, **kwargs):
        identifier = group[0]["unit_id"]
        looked_up.append(identifier)
        return ({"id": "same-target"},) if identifier in {"0", "1"} else ()

    def freeze(store, connection, group, **kwargs):
        identifier = group[0]["unit_id"]
        frozen.append(identifier)
        # Group 1 shares a target; group 2 shares evidence before any CREATE.
        facts = (
            InsightInput(
                id="shared" if identifier in {"0", "2"} else identifier,
                text="Grounded fact",
                source_refs=("source:1",),
            ),
        )
        targets = (
            ({"id": "same-target", "text": "Supported state."},) if identifier in {"0", "1"} else ()
        )
        return facts, (identifier,), targets, targets

    monkeypatch.setattr(consolidation, "_pending_group", pending)
    monkeypatch.setattr(consolidation, "_set_preparation_timeout", lambda *args: None)
    monkeypatch.setattr(consolidation, "_lookup_targets", lookup)
    monkeypatch.setattr(consolidation, "_freeze_group", freeze)
    selected = consolidation._parallel_groups(store, None, ["0", "1", "2", "3"], set())
    assert [group[1] for group in selected] == [("0",), ("3",)]
    selected = consolidation._parallel_groups(store, None, ["0", "1", "2", "3"], {"0", "3"})
    assert [group[1] for group in selected] == [("1",), ("2",)]
    assert looked_up.count("1") == 2 and looked_up.count("2") == 2
    assert frozen.count("1") == 1 and frozen.count("2") == 2


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


@pytest.mark.parametrize("oversized", ["suffix", "first", "neither"])
def test_l2_freeze_admits_a_fitting_candidate_prefix_and_keeps_first_target(monkeypatch, oversized):
    from oam.indexing import consolidation

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    seed = {"unit_id": "new", "memory_id": "new"}
    fact = InsightInput(id="new", text="New supported state.", source_refs=("source:1",))
    targets = [
        {"id": key, "text": "Complete supported knowledge.", "vector": [1.0], "supports": []}
        for key in ("first", "suffix")
    ]
    if oversized != "neither":
        targets[0 if oversized == "first" else 1]["text"] = "Complete history. " * 10000
    lookups = []
    monkeypatch.setattr(consolidation, "_lookup_targets", lambda *args, **kwargs: targets)

    def facts_for_targets(store, connection, seeds, admitted, *, seed_only=False):
        if not seed_only:
            lookups.append([target["id"] for target in admitted])
        return [fact]

    monkeypatch.setattr(consolidation, "_fact_context", facts_for_targets)
    monkeypatch.setattr(consolidation, "_fact_input", lambda row: row)
    store = SimpleNamespace(config=config, claim=SimpleNamespace(operation_id="test-operation"))
    if oversized == "first":
        with pytest.raises(ValueError, match="one seed"):
            consolidation._freeze_group(store, None, [seed])
        return
    facts, seeds, public, private = consolidation._freeze_group(store, None, [seed])
    assert facts == (fact,)
    assert seeds == ("new",)
    assert private == (targets[:1] if oversized == "suffix" else targets)
    assert public == tuple(
        {key: value for key, value in x.items() if key != "vector"} for x in private
    )
    assert lookups[-1] == (["first"] if oversized == "suffix" else ["first", "suffix"])
    assert len(lookups) == 1


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

        def l2_seed_policy(self, connection):
            return "related"

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
