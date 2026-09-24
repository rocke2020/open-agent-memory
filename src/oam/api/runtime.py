"""Authenticated database composition for the public operation routes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from oam.auth.administration import CredentialAdministration
from oam.auth.credentials import AuthorizedScope, PositiveCredentialCache, digest_secret
from oam.auth.repository import AuthRepository, ScopeDenied
from oam.config import OAMConfig
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
from oam.contracts.evidence import EvidenceAppendRequest
from oam.contracts.operations import ReconcileAttemptRequest
from oam.contracts.resources import EvidenceResourceCreate, ResourceMediaType
from oam.contracts.retain import RetainFileDocument, RetainInteraction, RetainTextDocument
from oam.contracts.search import SearchRequest
from oam.db.context import bind_scope
from oam.db.sources import AcceptSource, SourceRepository
from oam.indexing.identity import index_generation
from oam.models.runtime import RuntimeModels
from oam.operations.repository import OperationNotFound, OperationRecord, OperationRepository


class AuthenticationFailed(PermissionError):
    """The supplied opaque bearer credential is not currently valid."""


class AuthorizationFailed(PermissionError):
    """The caller cannot use the requested operation capability in this scope."""


class APIRuntime(Protocol):
    def authenticate(self, bearer_secret: str) -> None: ...

    def list_credentials(
        self, bearer_secret: str, *, limit: int, after: str | None
    ) -> CredentialListResponse: ...

    def issue_credential(
        self, bearer_secret: str, request: CredentialIssueRequest
    ) -> CredentialIssuedResponse: ...

    def revoke_credential(
        self, bearer_secret: str, credential_id: str
    ) -> CredentialRevokedResponse: ...

    def get(self, bearer_secret: str, scope_id: str, operation_id: str) -> OperationRecord: ...

    def cancel(self, bearer_secret: str, scope_id: str, operation_id: str) -> OperationRecord: ...

    def retry(
        self,
        bearer_secret: str,
        scope_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> OperationRecord: ...

    def reconcile(
        self,
        bearer_secret: str,
        scope_id: str,
        operation_id: str,
        request: ReconcileAttemptRequest,
    ) -> OperationRecord: ...


@dataclass(slots=True)
class DatabaseRuntime:
    """Own the API connection pool and compose auth with scoped operation repositories."""

    config: OAMConfig
    pool: ConnectionPool
    models: object | None = None
    _http: httpx.Client = field(init=False, repr=False)
    _auth: AuthRepository = field(init=False, repr=False)
    _operations: OperationRepository = field(init=False, repr=False)
    _credential_cache: PositiveCredentialCache = field(init=False, repr=False)
    _administration: CredentialAdministration = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(trust_env=False)
        if self.models is None:
            self.models = RuntimeModels(self.config, os.environ, self._http)
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
        return cls(config=config, pool=pool)

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()
        self._http.close()

    def authenticate(self, bearer_secret: str) -> None:
        """Reject invalid credentials before reading a retained original."""
        with self.pool.connection() as connection:
            self._authenticate(connection, bearer_secret)

    def search(self, bearer_secret: str, scope_id: str, request: SearchRequest) -> dict:
        from oam.retrieval.service import RecallService

        with self.pool.connection() as connection:
            authority = self._authorize(connection, bearer_secret, scope_id, "memory:read")
        return RecallService(
            self.config,
            self.pool,
            self.models,
            generation_id=index_generation(self.config, os.environ),
        ).search(authority, request, deadline_at=request.deadline_at)

    def create_user(self, bearer_secret: str, user_id: str) -> dict:
        """Create one explicitly authorized personal collection, with no third selector."""
        with self.pool.connection() as connection, connection.transaction():
            principal = self._auth.lookup_credential(connection, digest_secret(bearer_secret))
            if principal is None:
                raise AuthenticationFailed
            if principal.role != "administrator":
                raise AuthorizationFailed
            authority = AuthorizedScope.from_database_grant(
                principal, scope_id=user_id, capabilities={"memory:read", "memory:append"}
            )
            bind_scope(connection.cursor(), authority)
            try:
                connection.execute(
                    "INSERT INTO scopes(tenant_id,scope_id,subject,namespace,lifetime) "
                    "VALUES (%s,%s,%s,'personal','persistent')",
                    (principal.tenant_id, user_id, user_id),
                )
            except UniqueViolation as exc:
                raise ValueError("user_id already exists") from exc
            connection.execute(
                "INSERT INTO grants(tenant_id,scope_id,principal_id,capabilities) "
                "VALUES (%s,%s,%s,%s)",
                (
                    principal.tenant_id,
                    user_id,
                    principal.principal_id,
                    ["memory:read", "memory:append", "operation:reconcile"],
                ),
            )
        return {"user_id": user_id}

    def retain(
        self,
        bearer_secret: str,
        body,
        idempotency_key: str,
        *,
        content: bytes | None = None,
        media_type: str | None = None,
        filename: str | None = None,
    ) -> dict:
        """Freeze the caller payload, then let admission assign immutable source revisions."""
        caller = body.model_dump(mode="json")
        identifier = secrets.token_hex(16)
        metadata_fields = {
            name: caller.get(name)
            for name in ("source_created_at", "source_updated_at", "reference_timezone")
        }
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
            else:
                if not isinstance(body, RetainFileDocument) or content is None:
                    raise ValueError("document original is required")
                if media_type != ResourceMediaType.HTML.value:
                    raise NotImplementedError(
                        "only text and HTML documents are available in this core stage"
                    )
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
            request_bytes = (
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
            authority = self._authorize(connection, bearer_secret, body.user_id, "memory:append")
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
                    update_mode=update_mode,
                    max_bytes=max_bytes,
                ),
            )
            source = connection.execute(
                "SELECT source_id,source_version FROM operations "
                "WHERE tenant_id=%s AND scope_id=%s AND operation_id=%s",
                (authority.tenant_id, authority.scope_id, receipt.operation_id),
            ).fetchone()
        return {
            "operation_id": receipt.operation_id,
            "source_id": source[0],
            "revision": source[1],
            "accepted_seq": receipt.accepted_seq,
            "reused": receipt.reused,
            "readiness": {"accepted": True},
        }

    def list_credentials(
        self, bearer_secret: str, *, limit: int, after: str | None
    ) -> CredentialListResponse:
        with self.pool.connection() as connection:
            return self._administration.list(
                connection, digest_secret(bearer_secret), limit=limit, after=after
            )

    def issue_credential(
        self, bearer_secret: str, request: CredentialIssueRequest
    ) -> CredentialIssuedResponse:
        with self.pool.connection() as connection:
            issued = self._administration.issue(connection, digest_secret(bearer_secret), request)
        return issued

    def revoke_credential(
        self, bearer_secret: str, credential_id: str
    ) -> CredentialRevokedResponse:
        with self.pool.connection() as connection:
            digest = self._administration.revoke(
                connection, digest_secret(bearer_secret), credential_id
            )
        if digest is not None:
            self._credential_cache.invalidate(bytes(digest))
        return CredentialRevokedResponse(
            credential_id=credential_id,
            revocation_effective_by=datetime.now(UTC)
            + timedelta(seconds=self.config.auth.cache_ttl_seconds),
        )

    def get(self, bearer_secret: str, scope_id: str, operation_id: str) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, bearer_secret, scope_id, "memory:read")
            record = self._operations.get(connection, authority, operation_id)
        if record is None:
            raise OperationNotFound(operation_id)
        return record

    def cancel(self, bearer_secret: str, scope_id: str, operation_id: str) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(connection, bearer_secret, scope_id, "memory:append")
            return self._operations.cancel(connection, authority, operation_id)

    def retry(
        self,
        bearer_secret: str,
        scope_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> OperationRecord:
        request_hash = hashlib.sha256(
            f"operation-retry\0{scope_id}\0{operation_id}\0{idempotency_key}".encode()
        ).digest()
        with self.pool.connection() as connection:
            authority = self._authorize(connection, bearer_secret, scope_id, "memory:append")
            return self._operations.retry(
                connection,
                authority,
                original_operation_id=operation_id,
                new_operation_id=f"op_{secrets.token_hex(12)}",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                attempts_remaining=self.config.retry.max_attempts,
            )

    def reconcile(
        self,
        bearer_secret: str,
        scope_id: str,
        operation_id: str,
        request: ReconcileAttemptRequest,
    ) -> OperationRecord:
        with self.pool.connection() as connection:
            authority = self._authorize(
                connection,
                bearer_secret,
                scope_id,
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
                bearer_secret,
                scope_id,
                "memory:read",
            )
            record = self._operations.get(connection, read_authority, operation_id)
        if record is None:
            raise OperationNotFound(operation_id)
        return record

    def _authenticate(self, connection, bearer_secret: str):
        digest = digest_secret(bearer_secret)
        principal = self._credential_cache.get(digest)
        if principal is None:
            lookup = self._credential_cache.begin_lookup(digest)
            principal = self._auth.lookup_credential(connection, digest)
            if principal is None:
                raise AuthenticationFailed
            self._credential_cache.store(lookup, principal)
        return principal

    def _authorize(self, connection, bearer_secret: str, scope_id: str, capability: str):
        principal = self._authenticate(connection, bearer_secret)
        try:
            return self._auth.authorize_scope(
                connection,
                principal,
                scope_id=scope_id,
                capability=capability,
            )
        except ScopeDenied as exc:
            raise AuthorizationFailed from exc
