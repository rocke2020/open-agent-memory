"""Authenticated database composition for the public operation routes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import psycopg
from psycopg.errors import (
    InsufficientPrivilege,
    InvalidAuthorizationSpecification,
    UndefinedTable,
    UniqueViolation,
)
from psycopg_pool import ConnectionPool

from oam.auth.administration import CredentialAdministration
from oam.auth.credentials import (
    AuthenticatedPrincipal,
    PositiveCredentialCache,
    digest_secret,
    issue_credential,
)
from oam.auth.modes import AssertedIdentity, RequestActor
from oam.auth.repository import AuthRepository, UserDenied
from oam.capacity import PostgresCapacity
from oam.config import ConfigurationError, OAMConfig
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
from oam.contracts.debug import ReplayRequest
from oam.contracts.evidence import EvidenceAppendRequest
from oam.contracts.operations import ReconcileAttemptRequest
from oam.contracts.profile import ProfileResourceRequest
from oam.contracts.resources import EvidenceResourceCreate, ResourceMediaType
from oam.contracts.retain import (
    RetainFileDocument,
    RetainInteraction,
    RetainTextDocument,
    RetainURLDocument,
)
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.db.context import bind_user
from oam.db.inspection import SourceInspector
from oam.db.migrations import load_migrations
from oam.db.readiness import DATABASE_READINESS_TIMEOUT_SECONDS
from oam.db.sources import AcceptanceReceipt, AcceptSource, SourceNotFound, SourceRepository
from oam.debug.traces import TraceRepository
from oam.exports.repository import ExportRepository
from oam.indexing.identity import index_generation
from oam.models.runtime import RuntimeModels
from oam.operations.repository import OperationNotFound, OperationRecord, OperationRepository
from oam.paths import default_migration_directory
from oam.profiles.resources import map_profile_resource
from oam.resources.url_fetch import fetch_resource_url, safe_source_url


class AuthenticationFailed(PermissionError):
    """The supplied request credential is not currently valid."""


class AuthorizationFailed(PermissionError):
    """The caller cannot use the requested operation capability for this user."""


class APIRuntime(Protocol):
    def authenticate(self, actor: RequestActor) -> None: ...

    def preflight_revision(
        self,
        actor: RequestActor,
        user_id: str,
        resource_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> None: ...

    def list_resources(self, actor: RequestActor, user_id: str, *, after: str | None) -> dict: ...

    def get_resource(self, actor: RequestActor, user_id: str, resource_id: str) -> dict: ...

    def get_resource_original(
        self, actor: RequestActor, user_id: str, resource_id: str
    ) -> tuple[bytes, str]: ...

    def list_memories(self, actor: RequestActor, user_id: str, *, after: str | None) -> dict: ...

    def get_evidence(self, actor: RequestActor, user_id: str, source_id: str) -> dict: ...

    def get_trace(self, actor: RequestActor, user_id: str, trace_id: str) -> dict: ...

    def replay(self, actor: RequestActor, user_id: str, body: ReplayRequest) -> dict: ...

    def diff_traces(self, actor: RequestActor, user_id: str, before: str, after: str) -> dict: ...

    def create_export(self, actor: RequestActor, user_id: str) -> dict: ...

    def get_export(self, actor: RequestActor, user_id: str, export_id: str) -> dict: ...

    def get_export_part(
        self, actor: RequestActor, user_id: str, export_id: str, index: int
    ) -> bytes: ...

    def get_export_original(
        self, actor: RequestActor, user_id: str, export_id: str, index: int
    ) -> tuple[bytes, str]: ...

    def revise_resource(
        self,
        actor: RequestActor,
        user_id: str,
        resource_id: str,
        body: RetainTextDocument | RetainURLDocument | RetainFileDocument,
        idempotency_key: str,
        expected_version: int,
        **upload,
    ) -> dict: ...

    def delete_source(
        self, actor: RequestActor, user_id: str, source_id: str, *, resource_only: bool
    ) -> dict: ...

    def retain_profile_resource(
        self,
        actor: RequestActor,
        user_id: str,
        body: ProfileResourceRequest,
        idempotency_key: str,
        *,
        expected_version: int | None,
    ) -> dict: ...

    def get_profile(self, actor: RequestActor, user_id: str, max_tokens: int) -> dict: ...

    def refresh_profile(
        self, actor: RequestActor, user_id: str, instruction: str | None, reset_instruction: bool
    ) -> dict: ...

    def search_many(
        self, actor: RequestActor, user_id: str, request: SearchManyRequest
    ) -> dict: ...

    def decompose_and_search(
        self, actor: RequestActor, user_id: str, request: SearchRequest
    ) -> dict: ...

    def list_credentials(
        self, actor: str, *, limit: int, after: str | None
    ) -> CredentialListResponse: ...

    def issue_credential(
        self, actor: str, request: CredentialIssueRequest
    ) -> CredentialIssuedResponse: ...

    def revoke_credential(self, actor: str, credential_id: str) -> CredentialRevokedResponse: ...

    def get(self, actor: RequestActor, user_id: str, operation_id: str) -> OperationRecord: ...

    def cancel(self, actor: RequestActor, user_id: str, operation_id: str) -> OperationRecord: ...

    def retry(
        self,
        actor: RequestActor,
        user_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> OperationRecord: ...

    def reconcile(
        self,
        actor: RequestActor,
        user_id: str,
        operation_id: str,
        request: ReconcileAttemptRequest,
    ) -> OperationRecord: ...


@dataclass(slots=True)
class DatabaseRuntime:
    """Own the API connection pool and compose auth with user operation repositories."""

    config: OAMConfig
    pool: ConnectionPool
    models: object | None = None
    url_fetcher: Callable[..., bytes] = fetch_resource_url
    capacity: PostgresCapacity | None = None
    _http: httpx.Client = field(init=False, repr=False)
    _auth: AuthRepository = field(init=False, repr=False)
    _operations: OperationRepository = field(init=False, repr=False)
    _credential_cache: PositiveCredentialCache = field(init=False, repr=False)
    _administration: CredentialAdministration = field(init=False, repr=False)
    _required_migrations: frozenset[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._required_migrations = frozenset(
            item.version for item in load_migrations(default_migration_directory())
        )
        self._http = httpx.Client(trust_env=False)
        if self.models is None:
            self.models = RuntimeModels(
                self.config, os.environ, self._http, admission=self.capacity
            )
        self._auth = AuthRepository()
        self._operations = OperationRepository()
        self._administration = CredentialAdministration()
        self._credential_cache = PositiveCredentialCache(
            ttl_seconds=self.config.auth.cache_ttl_seconds,
            max_entries=self.config.auth.cache_max_entries,
        )

    @classmethod
    def from_dsn(cls, config: OAMConfig, dsn: str) -> DatabaseRuntime:
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=config.database.api_pool_max,
            open=False,
            name="oam-api",
        )
        return cls(config=config, pool=pool, capacity=PostgresCapacity(config, dsn, pool=pool))

    def open(self) -> None:
        self.pool.open(wait=True)
        if self.capacity is not None:
            self.capacity.configure()

    def close(self) -> None:
        self.pool.close()
        self._http.close()
        if self.capacity is not None:
            self.capacity.close()

    def readiness(self) -> dict:
        """Probe the current connection and required schema using runtime authority."""
        try:
            with (
                self.pool.connection(timeout=DATABASE_READINESS_TIMEOUT_SECONDS) as connection,
                connection.transaction(),
            ):
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout',%s,true)",
                    (str(int(DATABASE_READINESS_TIMEOUT_SECONDS * 1000)),),
                )
                rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        except (UndefinedTable, InsufficientPrivilege):
            return {
                "database": "connected",
                "schema": "unavailable",
                "reason": "schema_unavailable",
            }
        except (psycopg.Error, TimeoutError):
            return {
                "database": "unavailable",
                "schema": "unchecked",
                "reason": "database_unavailable",
            }
        missing = sorted(self._required_migrations - {row[0] for row in rows})
        if missing:
            return {
                "database": "connected",
                "schema": "outdated",
                "missing_migrations": missing,
                "reason": "schema_outdated",
            }
        return {"database": "connected", "schema": "current"}

    def choose_auto_auth_mode(self) -> str:
        with self.pool.connection() as connection:
            row = connection.execute("SELECT oam_choose_auto_auth_mode()").fetchone()
        if row is None or row[0] not in ("api_key", "dev"):
            raise ConfigurationError(
                "automatic authentication mode is unavailable for this database"
            )
        return row[0]

    def has_active_root(self) -> bool:
        with self.pool.connection() as connection:
            return connection.execute("SELECT oam_has_active_root()").fetchone()[0] is True

    def has_configured_root(self, secret: str) -> bool:
        with self.pool.connection() as connection:
            principal = self._auth.lookup_credential(connection, digest_secret(secret))
        return principal is not None and principal.role == "administrator"

    def authenticate(self, actor: RequestActor) -> None:
        """Reject invalid credentials before reading a retained original."""
        with self.pool.connection() as connection:
            self._authenticate(connection, actor)

    def preflight_revision(
        self,
        actor: RequestActor,
        user_id: str,
        resource_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> None:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "resource:write")
            SourceRepository().require_active_resource_version(
                connection, authority, resource_id, expected_version, idempotency_key
            )

    def search(self, actor: RequestActor, user_id: str, request: SearchRequest) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
        return self._search_authorized(authority, request, deadline_at=request.deadline_at)

    def _search_authorized(self, authority, request, *, deadline_at=None) -> dict:
        from oam.profiles.assembly import search_with_profile

        return search_with_profile(
            self.config,
            self.pool,
            self._recall_service(),
            authority,
            request,
            deadline_at=deadline_at,
        )

    def get_profile(self, actor: RequestActor, user_id: str, max_tokens: int) -> dict:
        from oam.profiles.service import ProfileService

        if not self.config.profile.enabled:
            raise NotImplementedError("Profile feature is disabled")
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
        return ProfileService(self.config, self.pool).get(authority, max_tokens)

    def refresh_profile(
        self, actor: RequestActor, user_id: str, instruction: str | None, reset_instruction: bool
    ) -> dict:
        from oam.profiles.service import ProfileService

        if not self.config.profile.enabled:
            raise NotImplementedError("Profile feature is disabled")
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:append")
            self._authorize(connection, actor, user_id, "memory:read")
            authority = replace(authority, capabilities=frozenset({"memory:append", "memory:read"}))
        return ProfileService(self.config, self.pool).refresh(
            authority, instruction, reset_instruction=reset_instruction
        )

    def list_resources(self, actor: RequestActor, user_id: str, *, after: str | None) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            return SourceInspector().list_resources(connection, authority, after=after)

    def get_resource(self, actor: RequestActor, user_id: str, resource_id: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            result = SourceInspector().get_resource(connection, authority, resource_id)
        if result is None:
            raise SourceNotFound(resource_id)
        return result

    def get_resource_original(
        self, actor: RequestActor, user_id: str, resource_id: str
    ) -> tuple[bytes, str]:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            result = SourceInspector().get_original(connection, authority, resource_id)
        if result is None:
            raise SourceNotFound(resource_id)
        return result

    def list_memories(self, actor: RequestActor, user_id: str, *, after: str | None) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            return SourceInspector().list_memories(connection, authority, after=after)

    def get_evidence(self, actor: RequestActor, user_id: str, source_id: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            result = SourceInspector().get_interaction(connection, authority, source_id)
        if result is None:
            raise SourceNotFound(source_id)
        return result

    def get_trace(self, actor: RequestActor, user_id: str, trace_id: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "debug:read")
            return TraceRepository(
                content_retention_days=self.config.trace.content_retention_days
            ).get(connection, authority, trace_id)

    def replay(self, actor: RequestActor, user_id: str, body: ReplayRequest) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "debug:read")
            return TraceRepository(
                content_retention_days=self.config.trace.content_retention_days
            ).replay(connection, authority, body.trace_id, body.mode)

    def diff_traces(self, actor: RequestActor, user_id: str, before: str, after: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "debug:read")
            return TraceRepository(
                content_retention_days=self.config.trace.content_retention_days
            ).diff(connection, authority, before, after)

    def create_export(self, actor: RequestActor, user_id: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "export:read")
            return ExportRepository(self.config).create(
                connection,
                authority,
                export_id=f"export_{secrets.token_hex(16)}",
                retention_hours=self.config.export.retention_hours,
            )

    def get_export(self, actor: RequestActor, user_id: str, export_id: str) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "export:read")
            return ExportRepository(self.config).get_manifest(connection, authority, export_id)

    def get_export_part(
        self, actor: RequestActor, user_id: str, export_id: str, index: int
    ) -> bytes:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "export:read")
            return ExportRepository(self.config).get_part(connection, authority, export_id, index)

    def get_export_original(
        self, actor: RequestActor, user_id: str, export_id: str, index: int
    ) -> tuple[bytes, str]:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "export:read")
            return ExportRepository(self.config).get_original(
                connection, authority, export_id, index
            )

    def search_many(self, actor: RequestActor, user_id: str, request: SearchManyRequest) -> dict:
        from oam.api.search import search_many

        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
        return search_many(
            self.config,
            request,
            lambda child, deadline: self._search_authorized(authority, child, deadline_at=deadline),
        )

    def decompose_and_search(
        self, actor: RequestActor, user_id: str, request: SearchRequest
    ) -> dict:
        from oam.api.search import decompose_and_search

        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
        return decompose_and_search(
            self.config,
            request,
            lambda child, deadline: self._search_authorized(authority, child, deadline_at=deadline),
            self.models,
        )

    def _recall_service(self):
        from oam.retrieval.service import RecallService

        return RecallService(
            self.config,
            self.pool,
            self.models,
            generation_id=index_generation(self.config, os.environ),
        )

    def create_user(self, actor: str, user_id: str) -> dict:
        """Create a personal collection and return its first user key after commit."""
        issued = issue_credential()
        credential_id = f"key_{secrets.token_hex(12)}"
        try:
            with self.pool.connection() as connection, connection.transaction():
                connection.execute(
                    "SELECT oam_create_user_with_key(%s,%s,%s,%s)",
                    (digest_secret(actor), user_id, credential_id, issued.digest),
                )
        except InvalidAuthorizationSpecification as exc:
            raise AuthenticationFailed from exc
        except InsufficientPrivilege as exc:
            raise AuthorizationFailed from exc
        except UniqueViolation as exc:
            raise ValueError("user_id already exists") from exc
        return {"user_id": user_id, "credential_id": credential_id, "secret": issued.secret}

    def retain(
        self,
        actor: RequestActor,
        body,
        idempotency_key: str,
        *,
        content: bytes | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        expected_active_version: int | None = None,
    ) -> dict:
        """Freeze the caller payload, then let admission assign immutable source revisions."""
        caller = body.model_dump(mode="json")
        if expected_active_version is not None:
            caller = {**caller, "expected_active_version": expected_active_version}
        identifier = secrets.token_hex(16)
        metadata_fields = {
            name: caller.get(name)
            for name in ("source_created_at", "source_updated_at", "reference_timezone")
        }
        url_request_bytes = None
        if isinstance(body, RetainURLDocument):
            source_url = safe_source_url(body.url)
            url_request_bytes = json.dumps(
                caller, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            request_hash = hashlib.sha256(url_request_bytes).digest()
            with self.pool.connection() as connection:
                authority = self._authorize(connection, actor, body.user_id, "resource:write")
                prior = SourceRepository().find_idempotent(
                    connection,
                    authority,
                    operation_kind="index_resource",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    max_pending_per_user=self.config.queue.max_pending_per_user,
                )
                if prior is not None:
                    return self._retain_response(connection, authority, prior)
            content = self.url_fetcher(
                body.url,
                body.media_type,
                max_bytes=self.config.resource.max_bytes,
                timeout_seconds=self.config.resource.url_fetch_timeout_seconds,
            )
            metadata_fields["source_url"] = source_url
        if isinstance(body, RetainInteraction):
            body.validate_limits(self.config)
            source_id = f"src_{identifier}"
            envelope = EvidenceAppendRequest(
                source_id=source_id,
                messages=[
                    {**message.model_dump(mode="json"), "message_id": str(index)}
                    for index, message in enumerate(body.messages)
                ],
                **metadata_fields,
            )
            original = envelope.model_dump_json().encode()
            input_role, resource_type = "interaction", None
            stored_media_type, metadata, update_mode = "application/json", None, "replace"
            operation_kind = "index_interaction"
            request_bytes = json.dumps(
                caller, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            max_bytes = None
        else:
            source_id = body.document_id
            if isinstance(body, RetainTextDocument):
                body.validate_limits(self.config)
                original, stored_media_type = body.text.encode(), ResourceMediaType.TEXT.value
                stored_filename = body.document_id
            elif isinstance(body, RetainURLDocument):
                original, stored_media_type = content, body.media_type
                stored_filename = body.document_id
            else:
                if not isinstance(body, RetainFileDocument) or content is None:
                    raise ValueError("document original is required")
                if media_type not in {
                    ResourceMediaType.HTML.value,
                    ResourceMediaType.PDF.value,
                    ResourceMediaType.PNG.value,
                    ResourceMediaType.JPEG.value,
                }:
                    raise ValueError("unsupported document media type")
                if not content or len(content) > self.config.resource.max_bytes:
                    raise ValueError("document exceeds byte limit or is empty")
                original, stored_media_type = content, media_type
                stored_filename = filename or body.document_id
            metadata = EvidenceResourceCreate(
                resource_id=source_id,
                media_type=stored_media_type,
                filename=stored_filename,
                byte_length=len(original),
                sha256=hashlib.sha256(original).hexdigest(),
                temporal_annotations=body.temporal_annotations,
                **metadata_fields,
            ).model_dump_json()
            input_role, resource_type = "evidence_resource", "evidence"
            update_mode, operation_kind = body.update_mode, "index_resource"
            request_bytes = url_request_bytes or (
                json.dumps(
                    {
                        "request": caller,
                        "media_type": stored_media_type,
                        "filename": stored_filename,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                + b"\\0"
                + original
            )
            max_bytes = self.config.resource.max_bytes
        with self.pool.connection() as connection:
            capability = (
                "memory:append" if isinstance(body, RetainInteraction) else "resource:write"
            )
            authority = self._authorize(connection, actor, body.user_id, capability)
            receipt = SourceRepository().accept(
                connection,
                authority,
                AcceptSource(
                    source_id=source_id,
                    source_version=None,
                    input_role=input_role,
                    resource_type=resource_type,
                    media_type=stored_media_type,
                    original_bytes=original,
                    evidence_id=f"envelope_{identifier}",
                    content_text=metadata,
                    learning_eligible=False,
                    operation_id=f"op_{identifier}",
                    operation_kind=operation_kind,
                    schema_version=1,
                    idempotency_key=idempotency_key,
                    request_hash=hashlib.sha256(request_bytes).digest(),
                    work_unit_id=f"work_{identifier}",
                    stage="index_source",
                    generation_id=index_generation(self.config, os.environ),
                    attempts_remaining=self.config.retry.max_attempts,
                    expected_active_version=expected_active_version,
                    update_mode=update_mode,
                    max_bytes=max_bytes,
                ),
                max_pending_per_user=self.config.queue.max_pending_per_user,
            )
            return self._retain_response(connection, authority, receipt)

    def revise_resource(
        self,
        actor: RequestActor,
        user_id: str,
        resource_id: str,
        body: RetainTextDocument | RetainURLDocument | RetainFileDocument,
        idempotency_key: str,
        expected_version: int,
        **upload,
    ) -> dict:
        if body.user_id != user_id or body.document_id != resource_id:
            raise PermissionError("resource identity does not match the authorized path")
        if body.update_mode != "replace":
            raise ValueError("Resource revision requires replace")
        return self.retain(
            actor, body, idempotency_key, expected_active_version=expected_version, **upload
        )

    def delete_source(
        self, actor: RequestActor, user_id: str, source_id: str, *, resource_only: bool
    ) -> dict:
        from oam.db.sources import SourceRepository

        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "source:delete")
            receipt = SourceRepository().delete(
                connection,
                authority,
                source_id=source_id,
                resource_only=resource_only,
                operation_id=f"op_{secrets.token_hex(16)}",
                work_unit_id=f"work_{secrets.token_hex(16)}",
                attempts_remaining=self.config.retry.max_attempts,
            )
        return {
            "source_id": receipt.source_id,
            "status": "excluded",
            "excluded_versions": list(receipt.excluded_versions),
            "policy_epoch": receipt.policy_epoch,
            "cleanup_operation_id": receipt.cleanup_operation_id,
            "cleanup_status": receipt.cleanup_status,
        }

    def _retain_response(self, connection, authority, receipt: AcceptanceReceipt) -> dict:
        with connection.transaction():
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                "SELECT source_id,source_version FROM operations "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (authority.tenant_id, authority.user_id, receipt.operation_id),
            )
            source = cursor.fetchone()
        if source is None:
            raise RuntimeError("accepted operation is unavailable")
        return {
            "operation_id": receipt.operation_id,
            "source_id": source[0],
            "revision": source[1],
            "accepted_seq": receipt.accepted_seq,
            "reused": receipt.reused,
            "readiness": {"accepted": True},
        }

    def retain_profile_resource(
        self,
        actor: RequestActor,
        user_id: str,
        body: ProfileResourceRequest,
        idempotency_key: str,
        *,
        expected_version: int | None,
    ) -> dict:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "resource:write")
            resource = body.model_dump(mode="json", exclude_none=True)
            # Exact fields are a deterministic read view of the active original.
            # Validate all mappings before atomic source-version activation.
            map_profile_resource(resource, resource_version=1)
            original = json.dumps(
                resource, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if len(original) > self.config.resource.max_bytes:
                raise ValueError("Profile Resource exceeds byte limit")
            request_hash = hashlib.sha256(
                json.dumps(
                    {"resource": resource, "expected_version": expected_version},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).digest()
            identifier = secrets.token_hex(16)
            receipt = SourceRepository().accept(
                connection,
                authority,
                AcceptSource(
                    source_id=body.resource_id,
                    source_version=None,
                    input_role="profile_resource",
                    resource_type="profile",
                    media_type="application/json",
                    original_bytes=original,
                    evidence_id=f"envelope_{identifier}",
                    content_text=None,
                    learning_eligible=False,
                    operation_id=f"op_{identifier}",
                    operation_kind="index_profile_resource",
                    schema_version=1,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    work_unit_id=f"work_{identifier}",
                    stage="index_source",
                    generation_id=index_generation(self.config, os.environ),
                    attempts_remaining=self.config.retry.max_attempts,
                    expected_active_version=expected_version,
                    max_bytes=self.config.resource.max_bytes,
                ),
                max_pending_per_user=self.config.queue.max_pending_per_user,
            )
            return self._retain_response(connection, authority, receipt)

    def list_credentials(
        self, actor: str, *, limit: int, after: str | None
    ) -> CredentialListResponse:
        with self.pool.connection() as connection:
            return self._administration.list(
                connection, digest_secret(actor), limit=limit, after=after
            )

    def issue_credential(
        self, actor: str, request: CredentialIssueRequest
    ) -> CredentialIssuedResponse:
        with self.pool.connection() as connection:
            issued = self._administration.issue(connection, digest_secret(actor), request)
        return issued

    def revoke_credential(self, actor: str, credential_id: str) -> CredentialRevokedResponse:
        with self.pool.connection() as connection:
            digest = self._administration.revoke(connection, digest_secret(actor), credential_id)
        if digest is not None:
            self._credential_cache.invalidate(bytes(digest))
        return CredentialRevokedResponse(
            credential_id=credential_id,
            revocation_effective_by=datetime.now(UTC)
            + timedelta(seconds=self.config.auth.cache_ttl_seconds),
        )

    def get(self, actor: RequestActor, user_id: str, operation_id: str) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
            record = self._operations.get(connection, authority, operation_id)
        if record is None:
            raise OperationNotFound(operation_id)
        return record

    def cancel(self, actor: RequestActor, user_id: str, operation_id: str) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:append")
            return self._operations.cancel(connection, authority, operation_id)

    def retry(
        self,
        actor: RequestActor,
        user_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> OperationRecord:
        request_hash = hashlib.sha256(
            f"operation-retry\0{user_id}\0{operation_id}\0{idempotency_key}".encode()
        ).digest()
        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:append")
            return self._operations.retry(
                connection,
                authority,
                original_operation_id=operation_id,
                new_operation_id=f"op_{secrets.token_hex(12)}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                attempts_remaining=self.config.retry.max_attempts,
                max_pending_per_user=self.config.queue.max_pending_per_user,
            )

    def reconcile(
        self,
        actor: RequestActor,
        user_id: str,
        operation_id: str,
        request: ReconcileAttemptRequest,
    ) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(
                connection,
                actor,
                user_id,
                "operation:reconcile",
            )
            self._operations.reconcile_attempt(
                connection,
                authority,
                operation_id=operation_id,
                work_unit_id=request.work_unit_id,
                attempt_id=request.attempt_id,
                owner_id=request.owner_id,
                fence=request.fence,
                disposition=request.disposition,
                response_ref=request.response_ref,
                usage=request.usage,
            )
            read_authority = self._authorize(
                connection,
                actor,
                user_id,
                "memory:read",
            )
            record = self._operations.get(connection, read_authority, operation_id)
        if record is None:
            raise OperationNotFound(operation_id)
        return record

    def _authenticate(self, connection, actor: RequestActor):
        if isinstance(actor, AssertedIdentity):
            if actor.root_secret is not None:
                principal = self._auth.lookup_credential(
                    connection, digest_secret(actor.root_secret)
                )
                if principal is None:
                    raise AuthenticationFailed
                if principal.role != "administrator" or principal.tenant_id != actor.tenant_id:
                    raise AuthorizationFailed
            return actor
        digest = digest_secret(actor)
        principal = self._credential_cache.get(digest)
        if principal is None:
            lookup = self._credential_cache.begin_lookup(digest)
            principal = self._auth.lookup_credential(connection, digest)
            if principal is None:
                raise AuthenticationFailed
            self._credential_cache.store(lookup, principal)
        return principal

    def _authorize(self, connection, actor: RequestActor, user_id: str, capability: str):
        principal = self._authenticate(connection, actor)
        if isinstance(principal, AssertedIdentity):
            if principal.user_id != user_id:
                raise AuthorizationFailed
            try:
                registered = connection.execute(
                    "SELECT oam_register_asserted_user(%s,%s)",
                    (principal.tenant_id, principal.user_id),
                ).fetchone()[0]
            except UniqueViolation as exc:
                raise AuthorizationFailed from exc
            if not registered:
                raise AuthorizationFailed
            principal = AuthenticatedPrincipal(
                credential_id=f"{principal.mode}-asserted",
                tenant_id=principal.tenant_id,
                principal_id=f"user:{principal.user_id}",
                role="user",
                expires_at=None,
            )
        try:
            return self._auth.authorize_user(
                connection,
                principal,
                user_id=user_id,
                capability=capability,
            )
        except UserDenied as exc:
            raise AuthorizationFailed from exc
