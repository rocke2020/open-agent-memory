from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

import pytest

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedUser
from oam.operations import repository, state


class Cursor:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.rows = iter(rows)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))

    def fetchone(self) -> tuple[object, ...] | None:
        return next(self.rows)

    def fetchall(self) -> list[tuple[object, ...]]:
        return [row for row in self.rows if row is not None]


class Connection:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.cursor_value = Cursor(rows)
        self.events: list[str] = []

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        finally:
            self.events.append("end")

    def cursor(self) -> Cursor:
        return self.cursor_value


def _authority(*capabilities: str) -> AuthorizedUser:
    principal = AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="writer-1",
        role="writer",
        expires_at=None,
    )
    return AuthorizedUser.from_database_grant(
        principal, user_id="scope-1", capabilities=set(capabilities)
    )


def _operation_row(
    *, status: str = "running", operation_id: str = "op-1", retry_of: str | None = None
) -> tuple[object, ...]:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    return (
        operation_id,
        "retain_resource",
        status,
        5,
        True,
        0,
        retry_of,
        "source-1",
        1,
        now,
        now,
        {},
    )


def test_get_operation_is_bound_to_authorized_tenant_and_scope() -> None:
    connection = Connection([_operation_row()])

    operation = repository.OperationRepository().get(connection, _authority("memory:read"), "op-1")

    assert operation is not None
    assert operation.operation_id == "op-1"
    query, parameters = connection.cursor_value.calls[1]
    assert "tenant_id = %s" in query and "user_id = %s" in query
    assert parameters == ("tenant-1", "scope-1", "op-1")


def test_cancel_fences_operation_and_unfinished_units_in_one_transaction() -> None:
    connection = Connection([_operation_row(status="cancelled")])

    operation = repository.OperationRepository().cancel(
        connection, _authority("memory:append"), "op-1"
    )

    assert operation.status is state.OperationStatus.CANCELLED
    assert connection.events == ["begin", "end"]
    assert any(
        "cancellation_revision = cancellation_revision + 1" in query
        for query, _ in connection.cursor_value.calls
    )
    assert any("fence = fence + 1" in query for query, _ in connection.cursor_value.calls)


def test_retry_creates_linked_operation_without_mutating_terminal_original() -> None:
    connection = Connection(
        [
            _operation_row(status="failed_terminal"),
            None,  # no existing retry for this caller key
            (0,),  # no dependency-bearing failed units
            None,  # no unknown external attempt
            _operation_row(status="accepted", operation_id="op-2", retry_of="op-1"),
        ]
    )

    retried = repository.OperationRepository().retry(
        connection,
        _authority("memory:append"),
        original_operation_id="op-1",
        new_operation_id="op-2",
        idempotency_key="retry-1",
        request_hash=b"r" * 32,
        attempts_remaining=3,
    )

    assert retried.retry_of_operation_id == "op-1"
    calls = connection.cursor_value.calls
    assert any(
        "retry_of_operation_id" in query and "INSERT INTO operations" in query for query, _ in calls
    )
    assert any(
        "INSERT INTO work_units" in query and "failed_terminal" in query for query, _ in calls
    )
    assert not any("UPDATE operations" in query for query, _ in calls)


def test_retry_fails_closed_for_dependency_graph_that_requires_replanning() -> None:
    connection = Connection([_operation_row(status="failed_terminal"), None, (2,)])

    with pytest.raises(repository.RetryRequiresPlanning):
        repository.OperationRepository().retry(
            connection,
            _authority("memory:append"),
            original_operation_id="op-1",
            new_operation_id="op-2",
            idempotency_key="retry-1",
            request_hash=b"r" * 32,
            attempts_remaining=3,
        )


def test_retry_fails_closed_for_unknown_external_outcome() -> None:
    connection = Connection([_operation_row(status="failed_terminal"), None, (0,), (1,)])

    with pytest.raises(repository.RetryRequiresReconciliation):
        repository.OperationRepository().retry(
            connection,
            _authority("memory:append"),
            original_operation_id="op-1",
            new_operation_id="op-2",
            idempotency_key="retry-1",
            request_hash=b"r" * 32,
            attempts_remaining=3,
        )


def test_retry_reuses_existing_linked_operation_for_same_idempotency_payload() -> None:
    existing = _operation_row(status="accepted", operation_id="op-2", retry_of="op-1")
    connection = Connection(
        [
            _operation_row(status="failed_terminal"),
            (*existing, b"r" * 32),
        ]
    )

    retried = repository.OperationRepository().retry(
        connection,
        _authority("memory:append"),
        original_operation_id="op-1",
        new_operation_id="ignored-op-3",
        idempotency_key="retry-1",
        request_hash=b"r" * 32,
        attempts_remaining=3,
    )

    assert retried.operation_id == "op-2"
    assert not any("INSERT INTO operations" in query for query, _ in connection.cursor_value.calls)


def test_reconcile_attempt_updates_only_exact_owner_and_fence() -> None:
    connection = Connection([("attempt-1",)])

    repository.OperationRepository().reconcile_attempt(
        connection,
        _authority("operation:reconcile"),
        operation_id="op-1",
        work_unit_id="work-1",
        attempt_id="attempt-1",
        owner_id="worker-1",
        fence=7,
        disposition="failed",
        response_ref="response-1",
        usage={"input_tokens": 12},
    )

    query, parameters = connection.cursor_value.calls[1]
    assert "owner_id = %s" in query
    assert "fence = %s" in query
    assert "operation_id = %s" in query
    assert "disposition = 'unknown_external_outcome'" in query
    assert parameters[0:6] == (
        "failed",
        "response-1",
        '{"input_tokens":12}',
        "op-1",
        "tenant-1",
        "scope-1",
    )


@pytest.mark.parametrize("current_namespace", ["stored-namespace", "new-namespace"])
def test_removal_recovery_uses_stored_namespace_after_recipe_change(current_namespace) -> None:
    original = list(_operation_row(status="partial_failed", operation_id="remove"))
    original[1] = "remove_memory"
    original[11] = {"consolidation_operation_ids": ["repair"]}
    repair = list(_operation_row(status="partial_failed", operation_id="repair"))
    repair[1] = "consolidate"
    repair[11] = {"insight_repairs": [{"removal_operation_id": "remove"}]}
    work = [
        ("cleanup", "remove", "cleanup_memory", "succeeded", "cleanup", []),
        (
            "repair-work",
            "repair",
            "consolidate",
            "failed_terminal",
            "stored-namespace",
            ["cleanup"],
        ),
    ]

    class RecoveryCursor:
        def __init__(self):
            self.one = iter(
                [(current_namespace,), tuple(original), (1,), None, (0,), (0,), tuple(original)]
            )
            self.many = iter([[tuple(original), tuple(repair)], work])

        def execute(self, query, parameters=()):
            pass

        def fetchone(self):
            return next(self.one)

        def fetchall(self):
            return next(self.many)

    connection = Connection([])
    connection.cursor_value = cast(Cursor, RecoveryCursor())
    result = repository.OperationRepository().recover_removal(
        connection,
        _authority("source:delete"),
        "remove",
        attempts_remaining=3,
        max_pending_per_user=2,
    )
    assert result.operation_id == "remove"
