"""Atomic original-source acceptance repository."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.queue_admission import require_queue_capacity


class IdempotencyConflict(RuntimeError):
    """An idempotency key was already committed for a different request."""


class SourceVersionConflict(RuntimeError):
    """The requested immutable source version cannot become active."""


class SourceNotFound(LookupError):
    """The source is not visible with the requested role."""


@dataclass(frozen=True, slots=True)
class AcceptSource:
    source_id: str
    source_version: int | None
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
    expected_active_version: int | None = None
    update_mode: str = "replace"
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.source_version is not None and self.source_version <= 0:
            raise ValueError("source_version must be positive")
        if self.update_mode not in {"replace", "append"}:
            raise ValueError("unsupported source update mode")
        if self.expected_active_version is not None and self.expected_active_version <= 0:
            raise ValueError("expected_active_version must be positive when supplied")
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
class DeletionReceipt:
    source_id: str
    excluded_versions: tuple[int, ...]
    policy_epoch: int
    cleanup_operation_id: str
    cleanup_status: str


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
  AND user_id = %s
  AND principal_id = %s
  AND kind = %s
  AND dedup_key = %s
""".strip()

LOCK_USER_SQL = """
SELECT accepted_seq, index_generation_id
FROM users
WHERE tenant_id = %s AND user_id = %s AND state = 'active'
FOR UPDATE
""".strip()

READ_ORIGINAL_SQL = """
SELECT v.original_bytes, v.media_type, v.content_sha256, v.byte_length,
       v.status, v.received_at
FROM source_versions v
JOIN sources s USING (tenant_id,user_id,source_id)
WHERE v.tenant_id = %s
  AND v.user_id = %s
  AND v.source_id = %s
  AND v.source_version = %s
  AND s.status = 'active'
  AND s.active_version = v.source_version
  AND v.status = 'active'
""".strip()


class SourceRepository:
    @staticmethod
    def _withdraw_prior_operations(
        cursor: Cursor, key: tuple[str, str], source_id: str, reason: str
    ) -> None:
        cursor.execute(
            "UPDATE operations SET status=CASE WHEN status IN ('accepted','running') "
            "THEN 'cancelled' ELSE status END,cancellation_revision="
            "cancellation_revision+CASE WHEN status IN ('accepted','running') "
            "THEN 1 ELSE 0 END,stage_state=jsonb_build_object(%s::text,true),"
            "updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
            (reason, *key, source_id),
        )
        cursor.execute(
            "UPDATE work_units SET status='cancelled',fence=fence+1,owner_id=NULL,"
            "lease_expires_at=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s "
            "AND user_id=%s AND operation_id IN (SELECT operation_id FROM operations "
            "WHERE tenant_id=%s AND user_id=%s AND source_id=%s AND status='cancelled') "
            "AND status IN ('queued','running','retry_wait')",
            (*key, *key, source_id),
        )

    def require_active_resource_version(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        source_id: str,
        expected_version: int,
        idempotency_key: str,
        *,
        input_role: str = "evidence_resource",
    ) -> None:
        if not authority.allows("resource:write"):
            raise PermissionError("resource:write is required")
        with connection.transaction():
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                "SELECT source_id FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND principal_id=%s AND kind=%s AND dedup_key=%s",
                (
                    authority.tenant_id,
                    authority.user_id,
                    authority.principal_id,
                    "index_profile_resource"
                    if input_role == "profile_resource"
                    else "index_resource",
                    idempotency_key,
                ),
            )
            reused = cursor.fetchone()
            if reused is not None:
                if reused[0] != source_id:
                    raise IdempotencyConflict(idempotency_key)
                return
            cursor.execute(
                "SELECT active_version FROM sources WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s AND input_role=%s AND status='active'",
                (authority.tenant_id, authority.user_id, source_id, input_role),
            )
            row = cursor.fetchone()
            if row is None:
                raise SourceNotFound(source_id)
            if row[0] != expected_version:
                raise SourceVersionConflict(source_id)

    def delete(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        *,
        source_id: str,
        resource_only: bool,
        operation_id: str,
        work_unit_id: str,
        attempts_remaining: int,
        expected_input_role: str | None = None,
    ) -> DeletionReceipt:
        if not authority.allows("source:delete"):
            raise PermissionError("source:delete is required")
        if attempts_remaining <= 0:
            raise ValueError("cleanup attempts must be positive")
        key = (authority.tenant_id, authority.user_id)
        allowed_roles = (
            {"evidence_resource", "profile_resource"} if resource_only else {"interaction"}
        )
        with connection.transaction():
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                "SELECT accepted_seq,policy_epoch FROM users WHERE tenant_id=%s AND user_id=%s "
                "AND state='active' FOR UPDATE",
                key,
            )
            owner = cursor.fetchone()
            if owner is None:
                raise SourceNotFound(source_id)
            cursor.execute(
                "SELECT input_role,resource_type,active_version,status FROM sources "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s FOR UPDATE",
                (*key, source_id),
            )
            source = cursor.fetchone()
            if (
                source is None
                or source[0] not in allowed_roles
                or (expected_input_role is not None and source[0] != expected_input_role)
            ):
                raise SourceNotFound(source_id)
            cursor.execute(
                "SELECT array_agg(source_version ORDER BY source_version) FROM source_versions "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (*key, source_id),
            )
            versions = tuple(cursor.fetchone()[0] or ())
            if source[3] != "active":
                cursor.execute(
                    "SELECT operation_id,stage_state->>'policy_epoch',status FROM operations "
                    "WHERE tenant_id=%s AND user_id=%s AND source_id=%s "
                    "AND kind='cleanup_source' ORDER BY created_at DESC LIMIT 1",
                    (*key, source_id),
                )
                prior = cursor.fetchone()
                if prior is None:
                    raise RuntimeError("deleted source has no cleanup receipt")
                return DeletionReceipt(
                    source_id, versions, int(prior[1]), str(prior[0]), str(prior[2])
                )
            accepted_seq, policy_epoch = int(owner[0]) + 1, int(owner[1]) + 1
            cursor.execute(
                "UPDATE users SET accepted_seq=%s,policy_epoch=%s "
                "WHERE tenant_id=%s AND user_id=%s",
                (accepted_seq, policy_epoch, *key),
            )
            cursor.execute(
                "UPDATE sources SET status='deleting',active_version=NULL "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (*key, source_id),
            )
            cursor.execute(
                "UPDATE source_versions SET status='excluded' "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (*key, source_id),
            )
            cursor.execute(
                "UPDATE evidence SET status='excluded' "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (*key, source_id),
            )
            self._withdraw_prior_operations(cursor, key, source_id, "source_excluded")
            cursor.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
                "accepted_seq,kind,schema_version,status,plan_sealed,stage_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,'cleanup_source',1,'accepted',true,"
                "jsonb_build_object('policy_epoch',%s,'excluded_versions',%s::int[]))",
                (
                    *key,
                    operation_id,
                    authority.principal_id,
                    source_id,
                    accepted_seq,
                    policy_epoch,
                    list(versions),
                ),
            )
            cursor.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,status,attempts_remaining) "
                "VALUES (%s,%s,%s,%s,'cleanup_source',%s,'source-cleanup-v1','queued',%s)",
                (
                    *key,
                    work_unit_id,
                    operation_id,
                    hashlib.sha256(source_id.encode()).digest(),
                    attempts_remaining,
                ),
            )
            return DeletionReceipt(source_id, versions, policy_epoch, operation_id, "accepted")

    def find_idempotent(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        *,
        operation_kind: str,
        idempotency_key: str,
        request_hash: bytes,
        max_pending_per_user: int | None = None,
    ) -> AcceptanceReceipt | None:
        """Avoid refetching a URL whose exact caller request was already accepted."""
        capability = "resource:write" if operation_kind == "index_resource" else "memory:append"
        if not authority.allows(capability):
            raise PermissionError(f"user does not grant {capability}")
        with connection.transaction():
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                FIND_IDEMPOTENT_OPERATION_SQL,
                (
                    authority.tenant_id,
                    authority.user_id,
                    authority.principal_id,
                    operation_kind,
                    idempotency_key,
                ),
            )
            existing = cursor.fetchone()
            if existing is None and max_pending_per_user is not None:
                require_queue_capacity(cursor, authority, max_pending_per_user)
        if existing is None:
            return None
        if bytes(existing[1]) != request_hash:
            raise IdempotencyConflict(f"idempotency key {idempotency_key!r} has another payload")
        return AcceptanceReceipt(
            operation_id=str(existing[0]), accepted_seq=int(existing[2]), reused=True
        )

    def accept(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        command: AcceptSource,
        *,
        max_pending_per_user: int | None = None,
    ) -> AcceptanceReceipt:
        capability = (
            "resource:write"
            if command.input_role in {"evidence_resource", "profile_resource"}
            else "memory:append"
        )
        if not authority.allows(capability):
            raise PermissionError(f"user does not grant {capability}")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(LOCK_USER_SQL, (authority.tenant_id, authority.user_id))
            user_row = cursor.fetchone()
            if user_row is None:
                raise PermissionError("user is unavailable")
            cursor.execute(
                FIND_IDEMPOTENT_OPERATION_SQL,
                (
                    authority.tenant_id,
                    authority.user_id,
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

            if max_pending_per_user is not None:
                require_queue_capacity(cursor, authority, max_pending_per_user)

            if command.source_version is None:
                command = self._assign_revision(cursor, authority, command)

            accepted_seq = int(user_row[0]) + 1
            command = replace(command, generation_id=user_row[1] or command.generation_id)
            cursor.execute(
                "UPDATE users SET accepted_seq = %s, "
                "index_generation_id = COALESCE(index_generation_id, %s), "
                "policy_epoch = policy_epoch + %s "
                "WHERE tenant_id = %s AND user_id = %s",
                (
                    accepted_seq,
                    command.generation_id,
                    int(command.expected_active_version is not None),
                    authority.tenant_id,
                    authority.user_id,
                ),
            )
            cursor.execute(
                "INSERT INTO sources "
                "(tenant_id, user_id, source_id, input_role, resource_type, status) "
                "VALUES (%s, %s, %s, %s, %s, 'active') ON CONFLICT DO NOTHING",
                (
                    authority.tenant_id,
                    authority.user_id,
                    command.source_id,
                    command.input_role,
                    command.resource_type,
                ),
            )
            cursor.execute(
                "UPDATE sources SET active_version = %s "
                "WHERE tenant_id = %s AND user_id = %s AND source_id = %s "
                "AND input_role = %s AND resource_type IS NOT DISTINCT FROM %s "
                "AND status = 'active' "
                "AND active_version IS NOT DISTINCT FROM %s "
                "AND %s = COALESCE(active_version, 0) + 1 "
                "RETURNING source_id",
                (
                    command.source_version,
                    authority.tenant_id,
                    authority.user_id,
                    command.source_id,
                    command.input_role,
                    command.resource_type,
                    command.expected_active_version,
                    command.source_version,
                ),
            )
            if cursor.fetchone() is None:
                raise SourceVersionConflict(command.source_id)
            cursor.execute(
                "UPDATE source_versions SET status = 'superseded' "
                "WHERE tenant_id = %s AND user_id = %s AND source_id = %s "
                "AND status = 'active'",
                (authority.tenant_id, authority.user_id, command.source_id),
            )
            if command.expected_active_version is not None:
                self._withdraw_prior_operations(
                    cursor,
                    (authority.tenant_id, authority.user_id),
                    command.source_id,
                    "source_superseded",
                )
            content_sha256 = hashlib.sha256(command.original_bytes).digest()
            cursor.execute(
                "INSERT INTO source_versions "
                "(tenant_id, user_id, source_id, source_version, media_type, original_bytes, "
                "content_sha256, byte_length, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
                (
                    authority.tenant_id,
                    authority.user_id,
                    command.source_id,
                    command.source_version,
                    command.media_type,
                    command.original_bytes,
                    content_sha256,
                    len(command.original_bytes),
                ),
            )
            cursor.execute(
                "INSERT INTO evidence "
                "(tenant_id, user_id, evidence_id, source_id, source_version, accepted_seq, "
                "content_text, learning_eligible, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
                (
                    authority.tenant_id,
                    authority.user_id,
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
                "(tenant_id, user_id, operation_id, principal_id, source_id, source_version, "
                "accepted_seq, kind, schema_version, status, dedup_key, request_hash, plan_sealed) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted', %s, %s, true)",
                (
                    authority.tenant_id,
                    authority.user_id,
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
                "(tenant_id, user_id, work_unit_id, operation_id, stage, input_digest, "
                "generation_id, status, attempts_remaining) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)",
                (
                    authority.tenant_id,
                    authority.user_id,
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

    @staticmethod
    def _assign_revision(cursor: Cursor, authority: AuthorizedUser, command: AcceptSource):
        """Allocate under the existing user lock, after checking the logical retry key."""
        cursor.execute(
            "SELECT s.active_version,v.media_type,v.original_bytes FROM sources s "
            "LEFT JOIN source_versions v ON "
            "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
            "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
            "WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=%s",
            (authority.tenant_id, authority.user_id, command.source_id),
        )
        active = cursor.fetchone()
        previous = int(active[0]) if active and active[0] is not None else None
        if (
            command.expected_active_version is not None or command.input_role == "profile_resource"
        ) and previous != command.expected_active_version:
            raise SourceVersionConflict(command.source_id)
        content = command.original_bytes
        if command.update_mode == "append":
            if not active or active[1] != "text/plain" or command.media_type != "text/plain":
                raise ValueError("append requires an existing text document")
            content = bytes(active[2]) + content
        if command.max_bytes is not None and len(content) > command.max_bytes:
            raise ValueError("resulting document exceeds byte limit")
        metadata = command.content_text
        if command.input_role == "evidence_resource" and metadata is not None:
            body = json.loads(metadata)
            body.update(
                source_version=(previous or 0) + 1,
                expected_active_version=previous,
                byte_length=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            metadata = json.dumps(body, ensure_ascii=False)
        return replace(
            command,
            source_version=(previous or 0) + 1,
            expected_active_version=previous,
            original_bytes=content,
            content_text=metadata,
        )

    def read_original(
        self,
        connection: Connection,
        authority: AuthorizedUser,
        *,
        source_id: str,
        source_version: int,
    ) -> StoredOriginal | None:
        if not authority.allows("memory:read"):
            raise PermissionError("user does not grant memory:read")
        with connection.transaction():  # type: ignore[attr-defined]
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                READ_ORIGINAL_SQL,
                (authority.tenant_id, authority.user_id, source_id, source_version),
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
