"""Repository setup performs only reads needed by the requested recall stage."""

import time
from contextlib import nullcontext

import pytest
from test_query_time_recall import CONFIG

from oam.auth.credentials import AuthorizedUser
from oam.retrieval.repository import RecallRepository


class RecordingConnection:
    def __init__(self):
        self.statements = []
        self.parameters = []

    def cursor(self, **kwargs):
        return self

    def pipeline(self):
        return nullcontext()

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        self.parameters.append(parameters)
        self.last_sql = sql
        return self

    def fetchone(self):
        return (False,)

    def fetchall(self):
        if "SELECT sc.index_generation_id" not in self.last_sql:
            return []
        return [
            {
                "index_generation_id": "generation",
                "policy_epoch": 1,
                "parse_partial": False,
                "readiness": "ready",
            }
        ]


def test_insight_lookup_only_runs_for_candidate_queries():
    connection = RecordingConnection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))
    repository = RecallRepository(connection, authority, CONFIG, deadline=time.monotonic() + 5)

    assert repository.user_state()["readiness"] == "ready"
    assert not any("SELECT EXISTS(SELECT 1 FROM insights" in sql for sql in connection.statements)

    repository.lexical("workshop", 5)
    repository.lexical("workshop", 5)
    assert sum("SELECT EXISTS(SELECT 1 FROM insights" in sql for sql in connection.statements) == 1


def test_repository_binds_scope_and_jit_in_one_transaction_local_statement():
    connection = RecordingConnection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))

    RecallRepository(connection, authority, CONFIG, deadline=time.monotonic() + 5)

    assert len(connection.statements) == 1
    assert all(
        setting in connection.statements[0]
        for setting in ("oam.tenant_id", "oam.user_id", "'jit','off',true")
    )
    assert connection.parameters[0] == ("tenant", "user")


def test_shared_phase_sets_read_committed_before_binding_scope_or_reading():
    connection = RecordingConnection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))

    repository = RecallRepository(
        connection, authority, CONFIG, deadline=time.monotonic() + 5, read_committed=True
    )
    assert repository.user_state()["readiness"] == "ready"
    assert connection.statements[0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert "oam.tenant_id" in connection.statements[1]
    assert "SELECT sc.index_generation_id" in connection.statements[-1]


@pytest.mark.parametrize("pipeline_supported", [False, True])
def test_dense_sets_ann_and_deadline_together_before_query(monkeypatch, pipeline_supported):
    from contextlib import contextmanager

    class Connection(RecordingConnection):
        active_pipeline = False

        @contextmanager
        def pipeline(self):
            self.active_pipeline = True
            try:
                yield
            finally:
                self.active_pipeline = False

        def execute(self, sql, parameters=()):
            if "hnsw.iterative_scan" in sql:
                assert "statement_timeout" in sql
                assert self.active_pipeline is pipeline_supported
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        "oam.retrieval.repository.Pipeline.is_supported", staticmethod(lambda: pipeline_supported)
    )
    connection = Connection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))
    repository = RecallRepository(connection, authority, CONFIG, deadline=time.monotonic() + 5)
    repository._has_insights = False
    connection.statements.clear()
    connection.parameters.clear()
    repository.dense((1.0,), 5)
    assert len(connection.statements) == 2
    assert connection.parameters[0][1:] == (
        CONFIG.search.ann_iterative_scan,
        str(CONFIG.search.ann_scan_max_tuples),
    )
    assert "SELECT u.unit_id,u.kind" in connection.statements[1]
