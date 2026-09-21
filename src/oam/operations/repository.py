"""Database-backed durable operation repository."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from oam.auth.credentials import AuthorizedScope
from oam.db.context import bind_scope
from oam.operations.state import OperationStatus


class OperationNotFound(LookupError):
    """No operation is visible in the authorized scope."""


class OperationNotCancellable(RuntimeError):
    """The operation is terminal or does not exist."""


class OperationNotRetryable(RuntimeError):
    """Only terminal failed/cancelled operations can create a linked retry."""


class OperationIdempotencyConflict(RuntimeError):
    """An operation retry key was already used for a different request."""


class RetryRequiresPlanning(RuntimeError):
    """A dependency-bearing retry needs the later deterministic replanner."""


class AttemptOwnershipMismatch(RuntimeError):
    """Attempt reconciliation did not match its immutable owner and fence."""


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    kind: str
    status: OperationStatus
    accepted_seq: int | None
    plan_sealed: bool
    cancellation_revision: int
    retry_of_operation_id: str | None
    source_id: str | None
    source_version: int | None
    created_at: datetime
    updated_at: datetime


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> object: ...

    def cursor(self) -> Cursor: ...


_FIELDS = (
    "operation_id, kind, status, accepted_seq, plan_sealed, cancellation_revision, "
    "retry_of_operation_id, source_id, source_version, created_at, updated_at"
)


class OperationRepository:
    def get(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        operation_id: str,
    ) -> OperationRecord | None:
        self._require(authority, "memory:read")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608 - fixed internal fields
                "WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s",
                (authority.tenant_id, authority.scope_id, operation_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._record(row)

    def cancel(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        operation_id: str,
    ) -> OperationRecord:
        self._require(authority, "memory:append")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(
                f"UPDATE operations SET status = 'cancelled', "  # noqa: S608
                "cancellation_revision = cancellation_revision + 1, "
                "updated_at = clock_timestamp() "
                "WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s "
                "AND status IN ('accepted', 'running') "
                f"RETURNING {_FIELDS}",
                (authority.tenant_id, authority.scope_id, operation_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise OperationNotCancellable(operation_id)
            cursor.execute(
                "UPDATE work_units SET status = 'cancelled', fence = fence + 1, "
                "owner_id = NULL, lease_expires_at = NULL, updated_at = clock_timestamp() "
                "WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s "
                "AND status IN ('queued', 'running', 'retry_wait')",
                (authority.tenant_id, authority.scope_id, operation_id),
            )
            return self._record(row)

    def retry(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        *,
        original_operation_id: str,
        new_operation_id: str,
        idempotency_key: str,
        request_hash: bytes,
        attempts_remaining: int,
    ) -> OperationRecord:
        self._require(authority, "memory:append")
        if len(request_hash) != hashlib.sha256().digest_size:
            raise ValueError("request_hash must be a SHA-256 digest")
        if attempts_remaining <= 0:
            raise ValueError("attempts_remaining must be positive")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608
                "WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s FOR UPDATE",
                (authority.tenant_id, authority.scope_id, original_operation_id),
            )
            original_row = cursor.fetchone()
            if original_row is None or OperationStatus(original_row[2]) not in {
                OperationStatus.PARTIAL_FAILED,
                OperationStatus.FAILED_TERMINAL,
                OperationStatus.CANCELLED,
            }:
                raise OperationNotRetryable(original_operation_id)
            cursor.execute(
                f"SELECT {_FIELDS}, request_hash FROM operations "  # noqa: S608
                "WHERE tenant_id = %s AND scope_id = %s AND principal_id = %s "
                "AND retry_of_operation_id = %s AND dedup_key = %s",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    authority.principal_id,
                    original_operation_id,
                    idempotency_key,
                ),
            )
            existing_retry = cursor.fetchone()
            if existing_retry is not None:
                if bytes(existing_retry[11]) != request_hash:
                    raise OperationIdempotencyConflict(idempotency_key)
                return self._record(existing_retry)
            cursor.execute(
                "SELECT count(*) FROM work_units WHERE tenant_id = %s AND scope_id = %s "
                "AND operation_id = %s AND status IN ('failed_terminal', 'cancelled') "
                "AND cardinality(dependency_ids) > 0",
                (authority.tenant_id, authority.scope_id, original_operation_id),
            )
            dependencies = cursor.fetchone()
            if dependencies is None or int(dependencies[0]) > 0:
                raise RetryRequiresPlanning(original_operation_id)
            cursor.execute(
                f"INSERT INTO operations "  # noqa: S608
                "(tenant_id, scope_id, operation_id, principal_id, source_id, source_version, "
                "accepted_seq, kind, schema_version, status, dedup_key, request_hash, "
                "plan_sealed, retry_of_operation_id) "
                "SELECT tenant_id, scope_id, %s, %s, source_id, source_version, accepted_seq, "
                "kind, schema_version, 'accepted', %s, %s, true, operation_id "
                "FROM operations WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s "
                f"RETURNING {_FIELDS}",
                (
                    new_operation_id,
                    authority.principal_id,
                    idempotency_key,
                    request_hash,
                    authority.tenant_id,
                    authority.scope_id,
                    original_operation_id,
                ),
            )
            retry_row = cursor.fetchone()
            if retry_row is None:
                raise OperationNotFound(original_operation_id)
            cursor.execute(
                "INSERT INTO work_units "
                "(tenant_id, scope_id, work_unit_id, operation_id, stage, chunk_group_id, "
                "input_digest, generation_id, dependency_ids, status, attempts_remaining) "
                "SELECT tenant_id, scope_id, %s || ':' || work_unit_id, %s, stage, "
                "chunk_group_id, input_digest, generation_id, '{}', 'queued', %s "
                "FROM work_units WHERE tenant_id = %s AND scope_id = %s AND operation_id = %s "
                "AND status IN ('failed_terminal', 'cancelled')",
                (
                    new_operation_id,
                    new_operation_id,
                    attempts_remaining,
                    authority.tenant_id,
                    authority.scope_id,
                    original_operation_id,
                ),
            )
            return self._record(retry_row)

    def reconcile_attempt(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        *,
        operation_id: str,
        work_unit_id: str,
        attempt_id: str,
        owner_id: str,
        fence: int,
        disposition: str,
        response_ref: str,
        usage: dict[str, object],
    ) -> None:
        self._require(authority, "operation:reconcile")
        if disposition not in {"succeeded", "failed"}:
            raise ValueError("reconciliation disposition must be succeeded or failed")
        usage_json = json.dumps(usage, separators=(",", ":"), sort_keys=True)
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(
                "UPDATE attempts AS attempt SET disposition = %s, response_ref = %s, "
                "usage = %s::jsonb, "
                "finished_at = clock_timestamp() "
                "FROM work_units AS work "
                "WHERE (work.tenant_id, work.scope_id, work.work_unit_id) = "
                "(attempt.tenant_id, attempt.scope_id, attempt.work_unit_id) "
                "AND work.operation_id = %s "
                "AND attempt.tenant_id = %s AND attempt.scope_id = %s "
                "AND attempt.work_unit_id = %s AND attempt.attempt_id = %s "
                "AND attempt.owner_id = %s AND attempt.fence = %s "
                "AND attempt.disposition = 'unknown_external_outcome' "
                "RETURNING attempt.attempt_id",
                (
                    disposition,
                    response_ref,
                    usage_json,
                    operation_id,
                    authority.tenant_id,
                    authority.scope_id,
                    work_unit_id,
                    attempt_id,
                    owner_id,
                    fence,
                ),
            )
            if cursor.fetchone() is None:
                raise AttemptOwnershipMismatch(attempt_id)

    @staticmethod
    def _record(row: tuple[object, ...]) -> OperationRecord:
        return OperationRecord(
            operation_id=str(row[0]),
            kind=str(row[1]),
            status=OperationStatus(row[2]),
            accepted_seq=None if row[3] is None else int(row[3]),
            plan_sealed=bool(row[4]),
            cancellation_revision=int(row[5]),
            retry_of_operation_id=None if row[6] is None else str(row[6]),
            source_id=None if row[7] is None else str(row[7]),
            source_version=None if row[8] is None else int(row[8]),
            created_at=row[9],  # type: ignore[arg-type]
            updated_at=row[10],  # type: ignore[arg-type]
        )

    @staticmethod
    def _require(authority: AuthorizedScope, capability: str) -> None:
        if not authority.allows(capability):
            raise PermissionError(f"scope does not grant {capability}")
