"""A source fence must preserve independent indexing across other source changes."""

from types import SimpleNamespace

import pytest

from oam.indexing.store import IndexStore, StaleIndexWork


class GuardConnection:
    def __init__(self, source_status="active"):
        self.rows = iter(
            [
                ("active", 2, "generation"),
                (
                    "worker",
                    3,
                    "running",
                    True,
                    0,
                    "running",
                    source_status,
                    1,
                    "active",
                    "generation",
                    "source",
                    1,
                    "operation",
                    b"x" * 32,
                ),
            ]
        )

    def execute(self, query, parameters):
        return self

    def fetchone(self):
        return next(self.rows)


def guarded_store(stage):
    store = object.__new__(IndexStore)
    store.key = ("tenant", "user")
    store.worker_id = "worker"
    store.generation_id = "generation"
    store.claim = SimpleNamespace(
        work_unit_id="work", fence=3, stage=stage, operation_id="operation"
    )
    store.source = {
        "policy_epoch": 1,
        "source_version": 1,
        "source_id": "source",
        "input_digest": b"x" * 32,
    }
    return store


def test_independent_source_indexing_survives_other_source_policy_change():
    guarded_store("index_source").guard(GuardConnection())


def test_consolidation_rejects_changed_related_source_policy():
    with pytest.raises(StaleIndexWork):
        guarded_store("consolidate").guard(GuardConnection())


def test_index_store_pool_checkout_is_bounded_by_pool_timeout():
    from contextlib import contextmanager

    config = SimpleNamespace(
        model=SimpleNamespace(attempt_timeout_seconds=30),
        database=SimpleNamespace(indexing_concurrency=1, pool_acquire_timeout_seconds=5),
    )
    checkout_timeouts = []

    class Pool:
        @contextmanager
        def connection(self, *, timeout):
            checkout_timeouts.append(timeout)
            raise RuntimeError("stop after checkout")
            yield

    store = object.__new__(IndexStore)
    store.dsn = "content"
    store.config = config
    store.pool = Pool()
    with pytest.raises(RuntimeError, match="stop after checkout"), store.transaction():
        pass
    assert checkout_timeouts == [5]
