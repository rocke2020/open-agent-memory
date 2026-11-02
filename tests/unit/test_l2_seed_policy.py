"""Batch selection stays pinned across retries without replaying old model work."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import InsightSourceBatchConfig, ModelsConfig, load_config
from oam.indexing.consolidation import _group_inputs
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore, input_digest


def test_source_seed_policy_keeps_existing_index_and_empty_group_identity(tmp_path):
    baseline = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    path = tmp_path / "source.yaml"
    path.write_text("index:\n  insight_seed_policy: source\n")
    source = load_config(path).config
    baseline = baseline.model_copy(update={"models": ModelsConfig()})
    source = source.model_copy(update={"models": ModelsConfig()})
    assert baseline.index.insight_seed_policy == "related"
    assert source.index.insight_seed_policy == "source"
    assert index_generation(baseline, {}) == index_generation(source, {})
    before = _group_inputs(SimpleNamespace(config=baseline, generation_id="same"), (), (), (), [])
    after = _group_inputs(SimpleNamespace(config=source, generation_id="same"), (), (), (), [])
    assert input_digest(before) == input_digest(after)


@pytest.mark.parametrize("legacy", ["examined", "cached", "fresh", "pinned"])
def test_source_policy_pins_before_dispatch_and_preserves_legacy_checkpoints(legacy):
    state = {"l2_examined_ids": ["done"]} if legacy == "examined" else {}
    if legacy == "pinned":
        state["l2_seed_policy"] = "source"
    cached = legacy == "cached"

    class Connection:
        def execute(self, query, params):
            if query.startswith("SELECT stage_state"):
                return SimpleNamespace(fetchone=lambda: (state,))
            if query.startswith("SELECT EXISTS"):
                assert params[:2] == ("tenant", "user")
                assert params[2:5] == ("source", 1, "generation")
                return SimpleNamespace(fetchone=lambda: (cached,))
            if query.startswith("UPDATE operations"):
                state.update(params[0].obj)
                return None
            raise AssertionError(query)

    store = object.__new__(IndexStore)
    store.key = ("tenant", "user")
    store.source = {"source_id": "source", "source_version": 1}
    store.generation_id = "generation"
    store.claim = SimpleNamespace(operation_id="operation")
    store.config = SimpleNamespace(index=SimpleNamespace(insight_seed_policy="source"))
    expected = "related" if legacy in {"examined", "cached"} else "source"
    assert store.l2_seed_policy(Connection()) == expected
    assert state["l2_seed_policy"] == expected
    store.config.index.insight_seed_policy = "related"
    assert store.l2_seed_policy(Connection()) == expected


class SourceBatchConnection:
    def __init__(self, state, cached=False):
        self.state = state
        self.cached = cached

    def execute(self, query, params):
        if query.startswith("SELECT stage_state"):
            assert query.endswith("FOR UPDATE")
            assert params == ("tenant", "user", "operation")
            return SimpleNamespace(fetchone=lambda: (self.state,))
        if query.startswith("SELECT EXISTS"):
            assert params == ("tenant", "user", "source", 1, "generation")
            assert "starts_with(stage_key,'insight')" in query
            return SimpleNamespace(fetchone=lambda: (self.cached,))
        if query.startswith("UPDATE operations"):
            assert params[1:] == ("tenant", "user", "operation")
            self.state.update(params[0].obj)
            return None
        raise AssertionError(query)


def _source_batch_store():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    bundle = InsightSourceBatchConfig(seed_max=32, input_max_tokens=32768, target_max=64)
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"insight_source_batch": bundle})}
    )
    store = object.__new__(IndexStore)
    store.key = ("tenant", "user")
    store.source = {"source_id": "source", "source_version": 1}
    store.generation_id = "generation"
    store.claim = SimpleNamespace(operation_id="operation")
    store.config = config
    return store


def test_fresh_source_pins_batch_limits_before_config_changes():
    store = _source_batch_store()
    state = {}
    connection = SourceBatchConnection(state)
    original = store.config
    effective = store.l2_source_config(connection)
    assert state["l2_source_batch"] == {
        "seed_max": 32,
        "input_max_tokens": 32768,
        "target_max": 64,
    }
    assert effective.index.insight_seed_max == 32
    assert effective.index.insight_candidate_max == 64
    assert effective.extraction.max_input_tokens == 32768
    assert store.config is original
    assert store.config.index.insight_candidate_max == 8
    changed = InsightSourceBatchConfig(seed_max=16, input_max_tokens=16384, target_max=32)
    store.config = original.model_copy(
        update={"index": original.index.model_copy(update={"insight_source_batch": changed})}
    )
    resumed = store.l2_source_config(connection)
    assert resumed.index.insight_seed_max == 32
    assert resumed.index.insight_candidate_max == 64
    assert resumed.extraction.max_input_tokens == 32768


@pytest.mark.parametrize("legacy", ["examined", "cached", "pinned_null"])
def test_existing_source_keeps_legacy_limits_when_batch_config_is_enabled(legacy):
    store = _source_batch_store()
    state = {"l2_seed_policy": "source"}
    if legacy == "examined":
        state["l2_examined_ids"] = ["done"]
    if legacy == "pinned_null":
        state["l2_source_batch"] = None
    connection = SourceBatchConnection(state, cached=legacy == "cached")
    effective = store.l2_source_config(connection)
    assert state["l2_source_batch"] is None
    assert state["l2_seed_policy"] == "source"
    assert effective.index.insight_source_batch is None
    assert effective.index.insight_seed_max == 8
    assert effective.index.insight_candidate_max == 8
    assert effective.extraction.max_input_tokens == 8192
    assert store.config.index.insight_source_batch is not None
    assert store.l2_source_config(connection).index.insight_seed_max == 8
