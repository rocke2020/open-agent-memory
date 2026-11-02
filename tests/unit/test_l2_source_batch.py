"""Source-only batching keeps complete targets while amortizing preparation."""

import time
from pathlib import Path
from types import SimpleNamespace

from oam.config import InsightSourceBatchConfig, load_config
from oam.indexing import consolidation
from oam.indexing.extraction import InsightInput


def test_source_batch_uses_large_union_without_widening_individual_lookups(monkeypatch):
    base = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = base.model_copy(
        update={
            "index": base.index.model_copy(
                update={
                    "insight_source_batch": InsightSourceBatchConfig(
                        seed_max=32, input_max_tokens=32768, target_max=64
                    )
                }
            )
        }
    )
    seeds = [{"unit_id": f"f{i}", "memory_id": f"f{i}"} for i in range(16)]
    facts = {
        row["memory_id"]: InsightInput(
            id=row["memory_id"], text=f"Project {i} requires two reviewers.", source_refs=("s:1",)
        )
        for i, row in enumerate(seeds)
    }
    targets = [
        [
            {"id": f"t{i}-{j}", "text": f"Project {i} condition {j}.", "supports": []}
            for j in range(2)
        ]
        for i in range(16)
    ]
    store = SimpleNamespace(config=config, claim=SimpleNamespace(operation_id="batch"))

    def lookup(selected_store, connection, selected_seeds, deadline, blocked_targets):
        assert selected_store.config.index.insight_candidate_max == 8
        return targets

    monkeypatch.setattr(consolidation, "_source_seed_targets", lookup)
    monkeypatch.setattr(consolidation, "_set_preparation_timeout", lambda *args: None)
    monkeypatch.setattr(
        consolidation,
        "_fact_context",
        lambda store, conn, group, targets, **kw: [facts[row["memory_id"]] for row in group],
    )
    monkeypatch.setattr(consolidation, "_fact_input", lambda row: row)

    frozen = consolidation._freeze_source_batch(
        store,
        None,
        seeds,
        deadline=time.monotonic() + 30,
        config=config.for_insight_source_batch(),
    )

    assert frozen[1] == tuple(f"f{i}" for i in range(16))
    assert len(frozen[2]) == 32
    assert len(frozen[0]) == 16
    assert config.extraction.max_input_tokens == 8192


def test_group_checkpoint_binds_effective_source_limits_without_changing_legacy_inputs():
    base = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    configured = base.model_copy(
        update={
            "index": base.index.model_copy(
                update={
                    "insight_source_batch": InsightSourceBatchConfig(
                        seed_max=32, input_max_tokens=32768, target_max=64
                    )
                }
            )
        }
    )
    store = SimpleNamespace(config=configured, generation_id="same")
    inputs = consolidation._group_inputs(
        store, (), (), (), [], config=configured.for_insight_source_batch()
    )
    assert inputs["config"]["index"]["insight_seed_max"] == 32
    assert inputs["config"]["index"]["insight_candidate_max"] == 64
    assert inputs["config"]["extraction"]["max_input_tokens"] == 32768
    assert "insight_source_batch" not in inputs["config"]["index"]
