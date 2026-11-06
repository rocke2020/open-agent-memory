"""Related batches retain per-seed targets while fitting the captured window."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.indexing import consolidation
from oam.indexing.extraction import InsightInput


@pytest.mark.parametrize("defer_second,target_cap", [(False, 2), (True, 2), (False, 1)])
def test_related_batch_keeps_seed_targets_without_repeating_lookup(
    monkeypatch, defer_second, target_cap
):
    base = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = base.model_copy(
        update={"index": base.index.model_copy(update={"insight_candidate_max": target_cap})}
    )
    seeds = [{"unit_id": key, "memory_id": key} for key in ("a", "b")]
    facts = {
        key: InsightInput(
            id=key,
            text="Complete supported change. " * (10000 if key == "b" and defer_second else 1),
            source_refs=("source:1",),
        )
        for key in ("a", "b")
    }
    targets = {
        key: {
            "id": key,
            "text": "Complete existing supported knowledge.",
            "retrieval_text": key,
            "revision": 1,
            "subtype": "projects",
            "supports": [],
            "vector": [1.0],
        }
        for key in ("a1", "a2", "b1")
    }
    lookups = []

    def lookup(store, connection, selected, **kwargs):
        ids = tuple(seed["memory_id"] for seed in selected)
        lookups.append(ids)
        return [targets["b1"]] if ids == ("b",) else [targets["a1"], targets["a2"]]

    monkeypatch.setattr(consolidation, "_lookup_targets", lookup)
    monkeypatch.setattr(
        consolidation,
        "_fact_context",
        lambda store, conn, selected, admitted, **kwargs: [
            facts[seed["memory_id"]] for seed in selected
        ],
    )
    monkeypatch.setattr(consolidation, "_fact_input", lambda row: row)
    store = SimpleNamespace(config=config, claim=SimpleNamespace(operation_id="related"))

    packed, presented, public, private = consolidation._freeze_group(store, None, seeds)

    assert presented == (("a",) if defer_second or target_cap == 1 else ("a", "b"))
    assert tuple(fact.id for fact in packed) == presented
    expected_targets = ["a1"] if target_cap == 1 else ["a1", "a2"] if defer_second else ["a1", "b1"]
    assert [target["id"] for target in public] == expected_targets
    assert [target["text"] for target in public] == [target["text"] for target in private]
    assert lookups == [("a",), ("b",)]
