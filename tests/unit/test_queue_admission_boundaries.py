"""Queue admission preserves reuse and rejects new durable work before writes."""

import pytest
from test_operation_repository import Connection, _operation_row
from test_source_repository import RecordingConnection, _authority, _command

from oam.db.sources import IdempotencyConflict, SourceRepository
from oam.operations.repository import OperationRepository


def test_full_queue_rejects_source_without_accepting_bytes_or_work():
    connection = RecordingConnection([(4,), None, (2,)])
    with pytest.raises(RuntimeError, match="queue capacity"):
        SourceRepository().accept(connection, _authority(), _command(), max_pending_per_user=2)
    assert not any(
        query.startswith(("INSERT", "UPDATE")) for query, _ in connection.cursor_value.calls
    )


def test_full_queue_still_reuses_exact_source_receipt():
    connection = RecordingConnection([(4,), ("original", b"r" * 32, 4)])
    receipt = SourceRepository().accept(
        connection, _authority(), _command(), max_pending_per_user=1
    )
    assert receipt.operation_id == "original"
    assert receipt.reused


def test_full_queue_preserves_source_idempotency_conflict():
    connection = RecordingConnection([(4,), ("original", b"x" * 32, 4)])
    with pytest.raises(IdempotencyConflict):
        SourceRepository().accept(connection, _authority(), _command(), max_pending_per_user=1)


def test_url_precheck_rejects_full_queue_before_fetch_can_start():
    connection = RecordingConnection([None, (2,)])
    with pytest.raises(RuntimeError, match="queue capacity"):
        SourceRepository().find_idempotent(
            connection,
            _authority(),
            operation_kind="index_resource",
            idempotency_key="url",
            request_hash=b"r" * 32,
            max_pending_per_user=2,
        )


def test_explicit_retry_requires_new_admission_at_full_queue():
    connection = Connection([(4,), _operation_row(status="failed_terminal"), None, (2,)])
    with pytest.raises(RuntimeError, match="queue capacity"):
        OperationRepository().retry(
            connection,
            _authority(),
            original_operation_id="op-1",
            new_operation_id="new-retry",
            idempotency_key="retry",
            request_hash=b"r" * 32,
            attempts_remaining=3,
            max_pending_per_user=2,
        )
    assert not any(
        query.startswith(("INSERT", "UPDATE")) for query, _ in connection.cursor_value.calls
    )


def test_exact_retry_reuses_admission_at_full_queue():
    existing = _operation_row(status="accepted", operation_id="op-retry", retry_of="op-1")
    connection = Connection(
        [(4,), _operation_row(status="failed_terminal"), (*existing, b"r" * 32)]
    )
    receipt = OperationRepository().retry(
        connection,
        _authority(),
        original_operation_id="op-1",
        new_operation_id="unused",
        idempotency_key="retry",
        request_hash=b"r" * 32,
        attempts_remaining=3,
        max_pending_per_user=1,
    )
    assert receipt.operation_id == "op-retry"
