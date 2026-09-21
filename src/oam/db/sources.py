"""Atomic original-source acceptance repository."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from oam.auth.credentials import AuthorizedScope
from oam.db.context import bind_scope


class IdempotencyConflict(RuntimeError):
    """An idempotency key was already committed for a different request."""


class SourceVersionConflict(RuntimeError):
    """The requested immutable source version cannot become active."""


@dataclass(frozen=True, slots=True)
class AcceptSource:
    source_id: str
    source_version: int
    input_role: str
    resource_type: str | None
    media_type: str
    original_bytes: bytes
    evidence_id: str
    content_text: str | None
    learning_eligible: bool
    operation_id: str
    operation_kind: str
    schema_version: int
    idempotency_key: str
    request_hash: bytes
    work_unit_id: str
    stage: str
    generation_id: str
    attempts_remaining: int

    def __post_init__(self) -> None:
        if self.source_version <= 0:
            raise ValueError("source_version must be positive")
        if len(self.request_hash) != hashlib.sha256().digest_size:
            raise ValueError("request_hash must be a SHA-256 digest")
        if self.attempts_remaining <= 0:
            raise ValueError("attempts_remaining must be positive")


@dataclass(frozen=True, slots=True)
class AcceptanceReceipt:
    operation_id: str
    accepted_seq: int
    reused: bool


@dataclass(frozen=True, slots=True)
class StoredOriginal:
    content: bytes
    media_type: str
    sha256: bytes
    byte_length: int
    status: str
    received_at: datetime


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> object: ...

    def cursor(self) -> Cursor: ...


FIND_IDEMPOTENT_OPERATION_SQL = """
SELECT operation_id, request_hash, accepted_seq
FROM operations
WHERE tenant_id = %s
  AND scope_id = %s
  AND principal_id = %s
  AND kind = %s
  AND dedup_key = %s
""".strip()

LOCK_SCOPE_SQL = """
SELECT accepted_seq
FROM scopes
WHERE tenant_id = %s AND scope_id = %s AND state = 'active'
FOR UPDATE
""".strip()

READ_ORIGINAL_SQL = """
SELECT original_bytes, media_type, content_sha256, byte_length, status, received_at
FROM source_versions
WHERE tenant_id = %s
  AND scope_id = %s
  AND source_id = %s
  AND source_version = %s
""".strip()


class SourceRepository:
    def accept(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        command: AcceptSource,
    ) -> AcceptanceReceipt:
        if not authority.allows("memory:append"):
            raise PermissionError("scope does not grant memory:append")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(LOCK_SCOPE_SQL, (authority.tenant_id, authority.scope_id))
            scope_row = cursor.fetchone()
            if scope_row is None:
                raise PermissionError("scope is unavailable")
            cursor.execute(
                FIND_IDEMPOTENT_OPERATION_SQL,
                (
                    authority.tenant_id,
                    authority.scope_id,
                    authority.principal_id,
                    command.operation_kind,
                    command.idempotency_key,
                ),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if bytes(existing[1]) != command.request_hash:
                    raise IdempotencyConflict(
                        f"idempotency key {command.idempotency_key!r} has another payload"
                    )
                return AcceptanceReceipt(
                    operation_id=str(existing[0]),
                    accepted_seq=int(existing[2]),
                    reused=True,
                )

            accepted_seq = int(scope_row[0]) + 1
            cursor.execute(
                "UPDATE scopes SET accepted_seq = %s WHERE tenant_id = %s AND scope_id = %s",
                (accepted_seq, authority.tenant_id, authority.scope_id),
            )
            cursor.execute(
                "INSERT INTO sources "
                "(tenant_id, scope_id, source_id, input_role, resource_type, status) "
                "VALUES (%s, %s, %s, %s, %s, 'active') ON CONFLICT DO NOTHING",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    command.source_id,
                    command.input_role,
                    command.resource_type,
                ),
            )
            cursor.execute(
                "UPDATE source_versions SET status = 'superseded' "
                "WHERE tenant_id = %s AND scope_id = %s AND source_id = %s "
                "AND status = 'active'",
                (authority.tenant_id, authority.scope_id, command.source_id),
            )
            content_sha256 = hashlib.sha256(command.original_bytes).digest()
            cursor.execute(
                "INSERT INTO source_versions "
                "(tenant_id, scope_id, source_id, source_version, media_type, original_bytes, "
                "content_sha256, byte_length, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    command.source_id,
                    command.source_version,
                    command.media_type,
                    command.original_bytes,
                    content_sha256,
                    len(command.original_bytes),
                ),
            )
            cursor.execute(
                "UPDATE sources SET active_version = %s, status = 'active' "
                "WHERE tenant_id = %s AND scope_id = %s AND source_id = %s "
                "AND input_role = %s AND resource_type IS NOT DISTINCT FROM %s "
                "AND status = 'active' "
                "AND (active_version IS NULL OR active_version < %s) "
                "RETURNING source_id",
                (
                    command.source_version,
                    authority.tenant_id,
                    authority.scope_id,
                    command.source_id,
                    command.input_role,
                    command.resource_type,
                    command.source_version,
                ),
            )
            if cursor.fetchone() is None:
                raise SourceVersionConflict(command.source_id)
            cursor.execute(
                "INSERT INTO evidence "
                "(tenant_id, scope_id, evidence_id, source_id, source_version, accepted_seq, "
                "content_text, learning_eligible, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    command.evidence_id,
                    command.source_id,
                    command.source_version,
                    accepted_seq,
                    command.content_text,
                    command.learning_eligible,
                ),
            )
            cursor.execute(
                "INSERT INTO operations "
                "(tenant_id, scope_id, operation_id, principal_id, source_id, source_version, "
                "accepted_seq, kind, schema_version, status, dedup_key, request_hash, plan_sealed) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted', %s, %s, true)",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    command.operation_id,
                    authority.principal_id,
                    command.source_id,
                    command.source_version,
                    accepted_seq,
                    command.operation_kind,
                    command.schema_version,
                    command.idempotency_key,
                    command.request_hash,
                ),
            )
            cursor.execute(
                "INSERT INTO work_units "
                "(tenant_id, scope_id, work_unit_id, operation_id, stage, input_digest, "
                "generation_id, status, attempts_remaining) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)",
                (
                    authority.tenant_id,
                    authority.scope_id,
                    command.work_unit_id,
                    command.operation_id,
                    command.stage,
                    command.request_hash,
                    command.generation_id,
                    command.attempts_remaining,
                ),
            )
            return AcceptanceReceipt(
                operation_id=command.operation_id,
                accepted_seq=accepted_seq,
                reused=False,
            )

    def read_original(
        self,
        connection: Connection,
        authority: AuthorizedScope,
        *,
        source_id: str,
        source_version: int,
    ) -> StoredOriginal | None:
        if not authority.allows("memory:read"):
            raise PermissionError("scope does not grant memory:read")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_scope(cursor, authority)
            cursor.execute(
                READ_ORIGINAL_SQL,
                (authority.tenant_id, authority.scope_id, source_id, source_version),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return StoredOriginal(
                content=bytes(row[0]),
                media_type=str(row[1]),
                sha256=bytes(row[2]),
                byte_length=int(row[3]),
                status=str(row[4]),
                received_at=row[5],  # type: ignore[arg-type]
            )
