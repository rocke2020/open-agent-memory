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


def test_existing_source_work_remains_valid_after_namespace_changes():
    connection = GuardConnection()
    rows = list(connection.rows)
    rows[0] = ("active", 2, "new-namespace")
    connection.rows = iter(rows)
    guarded_store("index_source").guard(connection)


def test_consolidation_rejects_changed_related_source_policy():
    with pytest.raises(StaleIndexWork):
        guarded_store("consolidate").guard(GuardConnection())


@pytest.mark.parametrize("delay_hours", [2, 0.5])
def test_consolidation_schedule_uses_configured_hours(tmp_path, delay_hours):
    from datetime import UTC, datetime

    from oam.config import load_config

    scheduled = []

    class Connection(GuardConnection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT COALESCE(min(created_at)"):
                return SimpleNamespace(fetchone=lambda: (datetime(2026, 10, 18, tzinfo=UTC),))
            if query.startswith("INSERT INTO work_units"):
                scheduled.append((query, parameters))
            return super().execute(query, parameters)

    path = tmp_path / "oam.yaml"
    path.write_text(f"index:\n  insight_consolidation_delay_hours: {delay_hours}\n")
    store = guarded_store("index_source")
    store.config = load_config(path).config
    store.source["accepted_seq"] = 1
    store.claim.attempt_id = "attempt"
    store.summarize_usage = lambda connection: None
    store.readiness = lambda connection, **state: None

    store.finish(schedule_consolidation=True, connection=Connection())

    assert len(scheduled) == 1
    query, parameters = scheduled[0]
    assert "clock_timestamp()+(%s*interval '1 hour')" in query
    assert parameters[6] == delay_hours


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


def test_completed_checkpoint_is_reused_when_recipe_changes():
    from contextlib import contextmanager
    from threading import Lock

    class Connection:
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT output FROM index_stage_results"):
                value = ({"value": "stored"},) if parameters[5] == "extract:passage" else None
                return SimpleNamespace(fetchone=lambda: value)
            return SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def transaction():
        yield Connection()

    store = guarded_store("index_source")
    store._model_stage_lock = Lock()
    store._unsettled_model_stages = set()
    store.config = SimpleNamespace(lease=SimpleNamespace(seconds=30))
    store._record_model_stage_state = lambda *args, **kwargs: None
    store.transaction = transaction
    store.guard = lambda connection: None
    assert store.checkpoint(
        "extract:passage", {"text": "original"}, lambda: pytest.fail("repeated model work")
    ) == {"value": "stored"}


def test_legacy_recipe_qualified_checkpoint_remains_reusable():
    import sqlite3
    from contextlib import contextmanager
    from threading import Lock

    from oam.indexing.store import input_digest

    with sqlite3.connect(":memory:") as db:
        db.create_function("starts_with", 2, lambda value, prefix: value.startswith(prefix))
        db.execute(
            "CREATE TABLE index_stage_results(tenant_id,user_id,source_id,source_version,"
            "generation_id,stage_key,input_digest,output)"
        )
        db.execute(
            "INSERT INTO index_stage_results VALUES(?,?,?,?,?,?,?,?)",
            (
                "tenant",
                "user",
                "source",
                1,
                "generation",
                "extract:passage:recipe=old",
                input_digest({"text": "original"}),
                "stored",
            ),
        )

        class Connection:
            def execute(self, sql, parameters=()):
                return db.execute(sql.replace("%s", "?"), parameters)

        @contextmanager
        def transaction():
            yield Connection()

        store = guarded_store("index_source")
        store._model_stage_lock = Lock()
        store._unsettled_model_stages = set()
        store.transaction = transaction
        store.guard = lambda connection: None
        assert (
            store.checkpoint(
                "extract:passage", {"text": "original"}, lambda: pytest.fail("repeated model work")
            )
            == "stored"
        )


def test_resume_skips_every_passage_in_a_completed_extraction_group():
    from contextlib import contextmanager

    from oam.indexing.store import stable_id

    memory_id = stable_id("memory", "passage-a", 0, "First fact.")

    class Connection:
        def execute(self, query, parameters=()):
            if "FROM passages p" in query:
                rows = [
                    ("passage-a", {"message_index": 0, "passage_index": 0}, "A", memory_id),
                    ("passage-b", {"message_index": 1, "passage_index": 0}, "B", None),
                ]
                return SimpleNamespace(fetchall=lambda: rows)
            if "FROM index_stage_results" in query:
                output = {
                    "facts": [
                        {"passage_id": "passage-a", "output_index": 0, "text": "First fact."}
                    ],
                    "diagnostics": [],
                    "_passage_ids": ["passage-a", "passage-b"],
                }
                return SimpleNamespace(fetchall=lambda: [("extract:group", output)])
            if "jsonb_array_elements_text" in query:
                return SimpleNamespace(fetchall=lambda: [])
            return SimpleNamespace(fetchone=lambda: ([],))

    @contextmanager
    def transaction():
        yield Connection()

    store = guarded_store("index_source")
    store.transaction = transaction
    store.guard = lambda connection: None
    _, saved, _ = store.resume_source()
    assert saved["passage-a"] == {memory_id}
    assert saved["passage-b"] == set()


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
    store.config = SimpleNamespace(
        lease=SimpleNamespace(seconds=30, heartbeat_seconds=20),
        model=SimpleNamespace(attempt_timeout_seconds=60),
    )
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
