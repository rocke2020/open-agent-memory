"""Profile preparation uses bounded existing retries before generation."""

import sqlite3
from types import SimpleNamespace

import pytest

from oam.auth.credentials import AuthorizedUser
from oam.operations.repository import RetryRequiresReconciliation
from oam.profiles import preparation

AUTHORITY = AuthorizedUser("key", "tenant", "owner", "user", frozenset({"memory:append"}))
CONFIG = SimpleNamespace(
    retry=SimpleNamespace(max_attempts=3), queue=SimpleNamespace(max_pending_per_user=10)
)


def test_queued_semantic_work_is_selected_after_initial_indexing_finishes():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE sources(tenant_id,user_id,source_id,active_version,status,input_role);"
            "CREATE TABLE operations(tenant_id,user_id,operation_id,source_id,source_version,"
            "status,retry_of_operation_id,stage_state,kind,created_at);"
            "CREATE TABLE work_units(tenant_id,user_id,operation_id,work_unit_id,status,stage,"
            "generation_id);"
            "CREATE TABLE l2_batch_members(tenant_id,user_id,source_id,source_version,batch_id,"
            "generation_id,status);"
            "CREATE TABLE users(tenant_id,user_id,index_generation_id);"
            "INSERT INTO users VALUES('tenant','user','generation');"
            "INSERT INTO sources VALUES('tenant','user','source',1,'active','interaction');"
            "INSERT INTO operations VALUES('tenant','user','index','source',1,'running',NULL,"
            "'{}','index_interaction',0);"
            "INSERT INTO work_units VALUES('tenant','user','index','initial',"
            "'succeeded','index_source',NULL);"
            "INSERT INTO work_units VALUES('tenant','user','index','semantic',"
            "'queued','index_semantic_links',NULL);"
        )
        query = (
            preparation._INPUT_WORK_SQL.replace(
                "COALESCE(o.stage_state,'{}'::jsonb) ?|\n"
                "            ARRAY['insight_repairs','removal_operation_id']",
                "(json_type(o.stage_state,'$.insight_repairs') IS NOT NULL OR "
                "json_type(o.stage_state,'$.removal_operation_id') IS NOT NULL)",
            )
            .replace("source_id=ANY(%s)", "source_id IN (%s)")
            .replace("%s", "?")
        )
        rows = connection.execute(query, ("tenant", "user", "source")).fetchall()
        assert [row[3] for row in rows] == ["semantic"]


def work(source, operation, status, *, root=True, has_retry=False, can_retry_generic=True):
    operation_status = "running" if status in {"queued", "running", "retry_wait"} else status
    return (
        source,
        operation,
        operation_status,
        "work_" + operation,
        status,
        None if root else "parent",
        has_retry,
        can_retry_generic,
    )


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.advanced = []

    def execute(self, query, params):
        self.queries.append((query, params))
        if query.startswith("UPDATE work_units"):
            self.advanced.extend(params[2])
            return self
        assert params[:2] == ("tenant", "user")
        self.selected = [row for row in self.rows if row[0] in params[2]]
        return self

    def fetchall(self):
        return self.selected


def test_failed_input_is_retried_and_joined_with_active_work(monkeypatch):
    connection = Connection(
        [work("new", "index", "running"), work("old", "failed", "failed_terminal")]
    )
    retries = []

    def retry(_self, actual_connection, authority, **kwargs):
        assert actual_connection is connection and authority is AUTHORITY
        retries.append(kwargs)
        connection.rows[1] = work("old", "failed", "failed_terminal", has_retry=True)
        connection.rows.append(work("old", "retry", "queued", root=False))

    monkeypatch.setattr(preparation.OperationRepository, "retry", retry)
    dependencies = preparation.prepare_profile_dependencies(
        connection, AUTHORITY, CONFIG, ("new", "old")
    )
    assert dependencies == ["work_index", "work_retry"]
    assert retries[0]["original_operation_id"] == "failed"
    assert retries[0]["attempts_remaining"] == 3
    assert retries[0]["max_pending_per_user"] == 10
    assert connection.advanced == dependencies
    assert (
        preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("new", "old"))
        == dependencies
    )
    assert len(retries) == 1


def test_failed_retry_does_not_start_another_retry_chain(monkeypatch):
    connection = Connection(
        [
            work("source", "original", "failed_terminal", has_retry=True),
            work("source", "retry", "failed_terminal", root=False),
        ]
    )
    monkeypatch.setattr(
        preparation.OperationRepository,
        "retry",
        lambda *_args, **_kwargs: pytest.fail("an automatic retry must not replenish its budget"),
    )
    with pytest.raises(preparation.InputsPreparationUnavailable):
        preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("source",))
    assert connection.advanced == []


def test_unknown_external_outcome_stays_blocked(monkeypatch):
    connection = Connection([work("source", "failed", "failed_terminal")])

    def retry(*_args, **_kwargs):
        raise RetryRequiresReconciliation("failed")

    monkeypatch.setattr(preparation.OperationRepository, "retry", retry)
    with pytest.raises(preparation.InputsPreparationUnavailable) as error:
        preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("source",))
    assert isinstance(error.value.__cause__, RetryRequiresReconciliation)
    assert connection.advanced == []


def test_no_preparation_needed_does_not_query_or_retry():
    connection = Connection([])
    assert preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ()) == []
    assert connection.queries == []


def test_unavailable_source_cannot_be_silently_omitted():
    connection = Connection([work("one", "index", "running")])
    with pytest.raises(preparation.InputsPreparationUnavailable):
        preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("one", "missing"))
    assert connection.advanced == []


def test_failed_governance_repair_is_not_retried_as_ordinary_consolidation(monkeypatch):
    connection = Connection([work("source", "repair", "failed_terminal", can_retry_generic=False)])
    monkeypatch.setattr(
        preparation.OperationRepository,
        "retry",
        lambda *_args, **_kwargs: pytest.fail("repair ownership must survive recovery"),
    )
    with pytest.raises(preparation.InputsPreparationUnavailable):
        preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("source",))
    assert connection.advanced == []


def test_active_governance_repair_remains_a_profile_dependency(monkeypatch):
    connection = Connection([work("source", "repair", "queued", can_retry_generic=False)])
    monkeypatch.setattr(
        preparation.OperationRepository,
        "retry",
        lambda *_args, **_kwargs: pytest.fail("active repair work must be reused"),
    )
    assert preparation.prepare_profile_dependencies(connection, AUTHORITY, CONFIG, ("source",)) == [
        "work_repair"
    ]
    assert connection.advanced == ["work_repair"]
