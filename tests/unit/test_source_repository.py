from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedScope
from oam.db import sources


class RecordingCursor:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self._rows = iter(rows)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))

    def fetchone(self) -> tuple[object, ...] | None:
        return next(self._rows)


class RecordingConnection:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.cursor_value = RecordingCursor(rows)
        self.transaction_events: list[str] = []

    @contextmanager
    def transaction(self):
        self.transaction_events.append("begin")
        try:
            yield
        finally:
            self.transaction_events.append("end")

    def cursor(self) -> RecordingCursor:
        return self.cursor_value


def _authority() -> AuthorizedScope:
    principal = AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="writer-1",
        role="writer",
        expires_at=None,
    )
    return AuthorizedScope.from_database_grant(
        principal,
        scope_id="scope-1",
        capabilities={"memory:append", "memory:read"},
    )


def _command(**changes: object) -> sources.AcceptSource:
    values: dict[str, object] = {
        "source_id": "source-1",
        "source_version": 1,
        "input_role": "evidence_resource",
        "resource_type": "report",
        "media_type": "text/html",
        "original_bytes": b"private medical report",
        "evidence_id": "evidence-1",
        "content_text": None,
        "learning_eligible": False,
        "operation_id": "operation-1",
        "operation_kind": "retain_resource",
        "schema_version": 1,
        "idempotency_key": "idem-1",
        "request_hash": b"r" * 32,
        "work_unit_id": "work-1",
        "stage": "parse_resource",
        "generation_id": "generation-1",
        "attempts_remaining": 3,
    }
    values.update(changes)
    return sources.AcceptSource(**values)


def test_accept_original_commits_bytes_evidence_receipt_and_work_in_one_transaction() -> None:
    connection = RecordingConnection(
        [
            (4,),  # current accepted sequence under lock
            None,  # no prior idempotent operation
            ("source-1",),  # source activation succeeded
        ]
    )

    receipt = sources.SourceRepository().accept(connection, _authority(), _command())

    assert receipt == sources.AcceptanceReceipt(
        operation_id="operation-1", accepted_seq=5, reused=False
    )
    assert connection.transaction_events == ["begin", "end"]
    calls = connection.cursor_value.calls
    assert "set_config('oam.tenant_id'" in calls[0][0]
    assert "FOR UPDATE" in calls[1][0]
    source_version_call = next(call for call in calls if "INSERT INTO source_versions" in call[0])
    assert b"private medical report" in source_version_call[1]
    assert "private medical report" not in source_version_call[0]
    assert any("INSERT INTO evidence" in query for query, _ in calls)
    assert any("INSERT INTO operations" in query for query, _ in calls)
    assert any("INSERT INTO work_units" in query for query, _ in calls)


def test_same_idempotency_key_and_hash_reuses_original_receipt_without_writes() -> None:
    connection = RecordingConnection([(5,), ("operation-1", b"r" * 32, 5)])

    receipt = sources.SourceRepository().accept(connection, _authority(), _command())

    assert receipt.reused is True
    assert receipt.operation_id == "operation-1"
    assert len(connection.cursor_value.calls) == 3


def test_same_idempotency_key_with_different_payload_is_a_conflict() -> None:
    connection = RecordingConnection([(5,), ("operation-1", b"other" * 6 + b"xx", 5)])

    with pytest.raises(sources.IdempotencyConflict, match="idem-1"):
        sources.SourceRepository().accept(connection, _authority(), _command())


def test_read_original_is_explicitly_scoped_and_returns_immutable_bytes() -> None:
    received = datetime(2026, 9, 21, tzinfo=UTC)
    connection = RecordingConnection(
        [(b"stored bytes", "application/pdf", b"h" * 32, 12, "active", received)]
    )

    original = sources.SourceRepository().read_original(
        connection,
        _authority(),
        source_id="source-1",
        source_version=2,
    )

    assert original == sources.StoredOriginal(
        content=b"stored bytes",
        media_type="application/pdf",
        sha256=b"h" * 32,
        byte_length=12,
        status="active",
        received_at=received,
    )
    query, parameters = connection.cursor_value.calls[1]
    assert "tenant_id = %s" in query
    assert "scope_id = %s" in query
    assert parameters == ("tenant-1", "scope-1", "source-1", 2)
