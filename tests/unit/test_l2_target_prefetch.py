"""Bounded target reads preserve ordered admission and drain before dispatch."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.indexing import consolidation
from oam.indexing.extraction import InsightInput
from oam.indexing.store import IndexStore


def test_opportunistic_read_without_pool_immediately_uses_primary():
    store = object.__new__(IndexStore)
    store.pool = None
    with store.transaction(nonblocking_read=True, deadline=time.monotonic() + 1) as connection:
        assert connection is None


@pytest.mark.parametrize("failed", [False, True])
def test_target_prefetch_overlaps_preserves_order_and_drains(monkeypatch, failed):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"insight_generation_concurrency": 4})}
    )
    lock, slots, barrier = threading.Lock(), threading.BoundedSemaphore(3), threading.Barrier(3)
    active = peak = 0
    calls = []
    primary = object()
    deadline = time.monotonic() + 5

    @contextmanager
    def transaction(*, nonblocking_read, deadline):
        nonlocal active, peak
        assert nonblocking_read
        if not slots.acquire(blocking=False):
            yield None
            return
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            yield object()
        finally:
            with lock:
                active -= 1
            slots.release()

    store = SimpleNamespace(
        config=config, transaction=transaction, claim=SimpleNamespace(operation_id="operation")
    )
    groups = [{"unit_id": str(i), "memory_id": str(i)} for i in range(8)]

    def pending(store, connection, identifiers, examined):
        return next(([row] for row in groups if row["unit_id"] not in examined), [])

    def lookup(store, connection, group, *, deadline=None):
        identifier = group[0]["unit_id"]
        with lock:
            calls.append(identifier)
        assert deadline is not None and deadline > time.monotonic()
        if identifier == "0":
            assert connection is primary
        if connection is not primary and int(identifier) in {1, 2, 3}:
            barrier.wait(timeout=2)
            time.sleep(0.02)
        if failed and identifier == "0":
            raise ValueError("target read failed")
        return []

    def freeze(store, connection, group, **kwargs):
        identifier = group[0]["unit_id"]
        fact = InsightInput(id=identifier, text="Supported fact", source_refs=("source:1",))
        return (fact,), (identifier,), (), ()

    monkeypatch.setattr(consolidation, "_pending_group", pending)
    monkeypatch.setattr(consolidation, "_set_preparation_timeout", lambda *args: None)
    monkeypatch.setattr(consolidation, "_lookup_targets", lookup)
    monkeypatch.setattr(consolidation, "_freeze_group", freeze)
    if failed:
        with pytest.raises(ValueError, match="target read failed"):
            consolidation._parallel_groups(store, primary, [], set(), deadline=deadline)
    else:
        result = consolidation._parallel_groups(store, primary, [], set(), deadline=deadline)
        assert [group[1] for group in result] == [("0",), ("1",), ("2",), ("3",)]
        assert sorted(calls) == ["0", "1", "2", "3"]
    assert active == 0 and peak == 3


@pytest.mark.parametrize("failed", [False, True])
def test_source_seed_reads_order_fallback_and_failure_drain(monkeypatch, failed):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"insight_generation_concurrency": 4})}
    )
    slots, lock, barrier = threading.BoundedSemaphore(2), threading.Lock(), threading.Barrier(2)
    no_slot, primary = threading.Event(), object()
    active = peak = 0
    primary_ids, calls = [], []

    @contextmanager
    def transaction(**kwargs):
        nonlocal active, peak
        if not slots.acquire(blocking=False):
            no_slot.set()
            yield None
            return
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            yield object()
        finally:
            with lock:
                active -= 1
            slots.release()

    store = SimpleNamespace(
        config=config,
        pool=object(),
        transaction=transaction,
        claim=SimpleNamespace(operation_id="source-fixture"),
    )
    seeds = [{"unit_id": str(i)} for i in range(8)]

    def lookup(store, connection, rows, **kwargs):
        identifier = rows[0]["unit_id"]
        with lock:
            calls.append(identifier)
            if connection is primary:
                primary_ids.append(identifier)
        if connection is not primary and identifier in {"1", "2"}:
            barrier.wait(timeout=2)
        assert no_slot.wait(timeout=2)
        if failed and identifier == "0":
            raise ValueError("source read failed")
        return [{"id": identifier}]

    monkeypatch.setattr(consolidation, "_lookup_targets", lookup)
    if failed:
        with pytest.raises(ValueError, match="source read failed"):
            consolidation._source_seed_targets(store, primary, seeds, time.monotonic() + 5, set())
    else:
        ranked = consolidation._source_seed_targets(
            store, primary, seeds, time.monotonic() + 5, set()
        )
        assert [rows[0]["id"] for rows in ranked] == [str(i) for i in range(8)]
        assert sorted(calls) == [str(i) for i in range(8)]
        assert len(primary_ids) >= 2
    assert active == 0 and peak == 2
