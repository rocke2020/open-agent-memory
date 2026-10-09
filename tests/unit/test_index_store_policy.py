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


def test_completed_model_response_is_saved_before_policy_retry():
    from contextlib import contextmanager
    from threading import Lock

    from oam.indexing.store import StaleConsolidationPolicy

    queries = []

    class Connection:
        def execute(self, query, params=()):
            queries.append(query)
            return SimpleNamespace(fetchone=lambda: None)

    connection = Connection()

    @contextmanager
    def transaction():
        yield connection

    store = guarded_store("consolidate")
    store.config = SimpleNamespace(lease=SimpleNamespace(seconds=30))
    store.claim.attempt_id = "attempt"
    store._model_stage_lock = Lock()
    store._unsettled_model_stages = set()
    store.transaction = transaction
    completed = False

    def guard(connection, *, allow_policy_change=False):
        if completed and not allow_policy_change:
            raise StaleConsolidationPolicy("changed")

    store.guard = guard

    def produce():
        nonlocal completed
        completed = True
        return {"text": "known result", "usage": {}}

    with pytest.raises(StaleConsolidationPolicy):
        store.checkpoint("repair:ins:3:1:synthesis", ["fact"], produce)
    assert any(q.startswith("INSERT INTO index_stage_results") for q in queries)
    assert store._unsettled_model_stages == set()
