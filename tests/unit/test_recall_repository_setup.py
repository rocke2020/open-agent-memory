"""Repository setup performs only reads needed by the requested recall stage."""

import time

from test_query_time_recall import CONFIG

from oam.auth.credentials import AuthorizedUser
from oam.retrieval.repository import RecallRepository


class RecordingConnection:
    def __init__(self):
        self.statements = []
        self.parameters = []

    def cursor(self, **kwargs):
        return self

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
