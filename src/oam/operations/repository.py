"""Database-backed durable operation repository."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.queue_admission import PENDING_ROOTS_SQL, QueueAdmissionFull, require_queue_capacity
from oam.operations.state import OperationStatus


class OperationNotFound(LookupError):
    """No operation is visible for the authorized user."""


class OperationNotCancellable(RuntimeError):
    """The operation is terminal or does not exist."""


class OperationNotRetryable(RuntimeError):
    """Only terminal failed/cancelled operations can create a linked retry."""


class RemovalRecoveryConflict(RuntimeError):
    """Removal maintenance cannot be recovered without violating its saved identity."""


class OperationIdempotencyConflict(RuntimeError):
    """An operation retry key was already used for a different request."""


class RetryRequiresPlanning(RuntimeError):
    """A dependency-bearing retry needs the later deterministic replanner."""


class RetryRequiresReconciliation(RuntimeError):
    """An ambiguous external attempt must be reconciled before replay."""


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
    stage_state: dict = field(default_factory=dict)


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> object: ...

    def cursor(self) -> Cursor: ...


_FIELDS = (
    "operation_id, kind, status, accepted_seq, plan_sealed, cancellation_revision, "
    "retry_of_operation_id, source_id, source_version, created_at, updated_at, stage_state"
)


class OperationRepository:
    def get(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        operation_id: str,
    ) -> OperationRecord | None:
        self._require(authority, "memory:read")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608 - fixed internal fields
                "WHERE tenant_id = %s AND user_id = %s AND operation_id = %s",
                (authority.tenant_id, authority.user_id, operation_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._record(row)

    def cancel(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        operation_id: str,
    ) -> OperationRecord:
        self._require(authority, "memory:append")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                f"UPDATE operations SET status = 'cancelled', "  # noqa: S608
                "cancellation_revision = cancellation_revision + 1, "
                "updated_at = clock_timestamp() "
                "WHERE tenant_id = %s AND user_id = %s AND operation_id = %s "
                "AND kind NOT IN ('remove_memory', 'remove_insight') "
                "AND status IN ('accepted', 'running') "
                f"RETURNING {_FIELDS}",
                (authority.tenant_id, authority.user_id, operation_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise OperationNotCancellable(operation_id)
            cursor.execute(
                "UPDATE work_units SET status = 'cancelled', fence = fence + 1, "
                "owner_id = NULL, lease_expires_at = NULL, updated_at = clock_timestamp() "
                "WHERE tenant_id = %s AND user_id = %s AND operation_id = %s "
                "AND status IN ('queued', 'running', 'retry_wait')",
                (authority.tenant_id, authority.user_id, operation_id),
            )
            return self._record(row)

    def retry(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        *,
        original_operation_id: str,
        new_operation_id: str,
        idempotency_key: str,
        request_hash: bytes,
        attempts_remaining: int,
        max_pending_per_user: int | None = None,
    ) -> OperationRecord:
        self._require(authority, "memory:append")
        if len(request_hash) != hashlib.sha256().digest_size:
            raise ValueError("request_hash must be a SHA-256 digest")
        if attempts_remaining <= 0:
            raise ValueError("attempts_remaining must be positive")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            if max_pending_per_user is not None:
                cursor.execute(
                    "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s "
                    "AND state='active' FOR UPDATE",
                    (authority.tenant_id, authority.user_id),
                )
                if cursor.fetchone() is None:
                    raise PermissionError("user is unavailable")
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608
                "WHERE tenant_id = %s AND user_id = %s AND operation_id = %s FOR UPDATE",
                (authority.tenant_id, authority.user_id, original_operation_id),
            )
            original_row = cursor.fetchone()
            if original_row is None or original_row[1] in {"remove_memory", "remove_insight"}:
                raise OperationNotRetryable(original_operation_id)
            if OperationStatus(original_row[2]) not in {
                OperationStatus.PARTIAL_FAILED,
                OperationStatus.FAILED_TERMINAL,
                OperationStatus.CANCELLED,
            }:
                raise OperationNotRetryable(original_operation_id)
            cursor.execute(
                f"SELECT {_FIELDS}, request_hash FROM operations "  # noqa: S608
                "WHERE tenant_id = %s AND user_id = %s AND principal_id = %s "
                "AND retry_of_operation_id = %s AND dedup_key = %s",
                (
                    authority.tenant_id,
                    authority.user_id,
                    authority.principal_id,
                    original_operation_id,
                    idempotency_key,
                ),
            )
            existing_retry = cursor.fetchone()
            if existing_retry is not None:
                if bytes(existing_retry[12]) != request_hash:
                    raise OperationIdempotencyConflict(idempotency_key)
                return self._record(existing_retry)
            if max_pending_per_user is not None and original_row[1] != "cleanup_source":
                require_queue_capacity(cursor, authority, max_pending_per_user)
            cursor.execute(
                "SELECT count(*) FROM work_units WHERE tenant_id = %s AND user_id = %s "
                "AND operation_id = %s AND status IN ('failed_terminal', 'cancelled') "
                "AND cardinality(dependency_ids) > 0",
                (authority.tenant_id, authority.user_id, original_operation_id),
            )
            dependencies = cursor.fetchone()
            if dependencies is None or int(dependencies[0]) > 0:
                raise RetryRequiresPlanning(original_operation_id)
            cursor.execute(
                "SELECT 1 FROM attempts a "
                "JOIN work_units w USING(tenant_id,user_id,work_unit_id) "
                "WHERE w.tenant_id=%s AND w.user_id=%s AND w.operation_id=%s "
                "AND w.status IN ('failed_terminal','cancelled') "
                "AND a.disposition='unknown_external_outcome' LIMIT 1",
                (authority.tenant_id, authority.user_id, original_operation_id),
            )
            if cursor.fetchone() is not None:
                raise RetryRequiresReconciliation(original_operation_id)
            cursor.execute(
                f"INSERT INTO operations "  # noqa: S608
                "(tenant_id, user_id, operation_id, principal_id, source_id, source_version, "
                "accepted_seq, kind, schema_version, status, dedup_key, request_hash, "
                "plan_sealed, retry_of_operation_id) "
                "SELECT tenant_id, user_id, %s, %s, source_id, source_version, accepted_seq, "
                "kind, schema_version, 'accepted', %s, %s, true, operation_id "
                "FROM operations WHERE tenant_id = %s AND user_id = %s AND operation_id = %s "
                f"RETURNING {_FIELDS}",
                (
                    new_operation_id,
                    authority.principal_id,
                    idempotency_key,
                    request_hash,
                    authority.tenant_id,
                    authority.user_id,
                    original_operation_id,
                ),
            )
            retry_row = cursor.fetchone()
            if retry_row is None:
                raise OperationNotFound(original_operation_id)
            cursor.execute(
                "INSERT INTO work_units "
                "(tenant_id, user_id, work_unit_id, operation_id, stage, chunk_group_id, "
                "input_digest, generation_id, dependency_ids, status, attempts_remaining) "
                "SELECT tenant_id, user_id, %s || ':' || work_unit_id, %s, stage, "
                "chunk_group_id, input_digest, generation_id, '{}', 'queued', %s "
                "FROM work_units WHERE tenant_id = %s AND user_id = %s AND operation_id = %s "
                "AND status IN ('failed_terminal', 'cancelled')",
                (
                    new_operation_id,
                    new_operation_id,
                    attempts_remaining,
                    authority.tenant_id,
                    authority.user_id,
                    original_operation_id,
                ),
            )
            return self._record(retry_row)

    def recover_removal(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        operation_id: str,
        *,
        attempts_remaining: int,
        max_pending_per_user: int,
    ) -> OperationRecord:
        """Rearm failed removal maintenance without replaying removal or extraction."""
        self._require(authority, "source:delete")
        if attempts_remaining <= 0:
            raise ValueError("attempts_remaining must be positive")
        key = (authority.tenant_id, authority.user_id)
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s "
                "AND state='active' FOR UPDATE",
                key,
            )
            user = cursor.fetchone()
            if user is None:
                raise OperationNotFound(operation_id)
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s FOR UPDATE",
                (*key, operation_id),
            )
            original = cursor.fetchone()
            if original is None:
                raise OperationNotFound(operation_id)
            if original[1] not in {"remove_memory", "remove_insight"}:
                raise RemovalRecoveryConflict(
                    "only original removal operations can recover maintenance"
                )
            linked = (original[11] or {}).get("consolidation_operation_ids", [])
            operation_ids = list(dict.fromkeys([operation_id, *linked]))
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=ANY(%s) "
                "ORDER BY operation_id FOR UPDATE",
                (*key, operation_ids),
            )
            records = {row[0]: self._record(row) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT work_unit_id,operation_id,stage,status,generation_id,dependency_ids "
                "FROM work_units WHERE tenant_id=%s AND user_id=%s AND operation_id=ANY(%s) "
                "ORDER BY work_unit_id FOR UPDATE",
                (*key, operation_ids),
            )
            work = cursor.fetchall()
            failed = [row for row in work if row[3] == "failed_terminal"]
            if not failed:
                if any(row[3] == "cancelled" for row in work):
                    raise RemovalRecoveryConflict("cancelled maintenance is not resumed")
                return self._record(original)
            cleanup_ids = [row[0] for row in work if row[1] == operation_id]
            for _unit_id, owner_id, stage, _, _unit_generation, dependencies in failed:
                record = records[owner_id]
                if record.cancellation_revision:
                    raise RemovalRecoveryConflict("cancelled maintenance is not resumed")
                if owner_id == operation_id:
                    if stage != "cleanup_" + original[1].removeprefix("remove_"):
                        raise RemovalRecoveryConflict("removal cleanup identity does not match")
                    continue
                repairs = record.stage_state.get("insight_repairs", [])
                if (
                    record.kind != "consolidate"
                    or stage != "consolidate"
                    or dependencies != cleanup_ids
                    or not repairs
                    or any(item.get("removal_operation_id") != operation_id for item in repairs)
                ):
                    raise RemovalRecoveryConflict("linked repair identity does not match")
                cursor.execute(
                    "SELECT 1 FROM sources s JOIN source_versions v "
                    "USING(tenant_id,user_id,source_id) WHERE s.tenant_id=%s AND s.user_id=%s "
                    "AND s.source_id=%s AND s.status='active' AND s.active_version=%s "
                    "AND v.source_version=%s AND v.status='active'",
                    (*key, record.source_id, record.source_version, record.source_version),
                )
                if cursor.fetchone() is None:
                    raise RemovalRecoveryConflict("repair source is no longer eligible")
            selected_ids = [row[0] for row in failed]
            cursor.execute(
                "SELECT 1 FROM attempts WHERE tenant_id=%s AND user_id=%s "
                "AND work_unit_id=ANY(%s) AND disposition='unknown_external_outcome' LIMIT 1",
                (*key, selected_ids),
            )
            if cursor.fetchone() is not None:
                raise RemovalRecoveryConflict("reconcile unknown external outcomes before recovery")
            cursor.execute(PENDING_ROOTS_SQL, key)
            before = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE work_units SET status='queued',attempts_remaining=%s,available_at=now(),"
                "fence=fence+1,owner_id=NULL,lease_expires_at=NULL,updated_at=now() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=ANY(%s) "
                "AND status='failed_terminal'",
                (attempts_remaining, *key, selected_ids),
            )
            for owner_id in dict.fromkeys(row[1] for row in failed):
                state = dict(records[owner_id].stage_state)
                state.pop("error", None)
                state.pop("error_detail", None)
                if owner_id == operation_id:
                    state["cleanup_status"] = "pending"
                else:
                    state.update(insights_ready=False, consolidation_status="pending")
                cursor.execute(
                    "UPDATE operations SET status='accepted',stage_state=%s,updated_at=now() "
                    "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                    (Jsonb(state), *key, owner_id),
                )
                if owner_id != operation_id and state.get("source_operation_id"):
                    cursor.execute(
                        "UPDATE operations SET stage_state=stage_state||%s::jsonb "
                        "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                        (
                            Jsonb({"insights_ready": False, "consolidation_status": "pending"}),
                            *key,
                            state["source_operation_id"],
                        ),
                    )
            cursor.execute(PENDING_ROOTS_SQL, key)
            after = cursor.fetchone()[0]
            if after > before and after > max_pending_per_user:
                raise QueueAdmissionFull("user queue capacity is full")
            cursor.execute(
                f"SELECT {_FIELDS} FROM operations "  # noqa: S608
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (*key, operation_id),
            )
            return self._record(cursor.fetchone())

    def reconcile_attempt(
        self,
        connection: Connection,
        authority: AuthorizedUser,
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
            bind_user(cursor, authority)
            cursor.execute(
                "UPDATE attempts AS attempt SET disposition = %s, response_ref = %s, "
                "usage = %s::jsonb, "
                "finished_at = clock_timestamp() "
                "FROM work_units AS work "
                "WHERE (work.tenant_id, work.user_id, work.work_unit_id) = "
                "(attempt.tenant_id, attempt.user_id, attempt.work_unit_id) "
                "AND work.operation_id = %s "
                "AND attempt.tenant_id = %s AND attempt.user_id = %s "
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
                    authority.user_id,
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
            stage_state=row[11],
        )

    @staticmethod
    def _require(authority: AuthorizedUser, capability: str) -> None:
        if not authority.allows(capability):
            raise PermissionError(f"user does not grant {capability}")
