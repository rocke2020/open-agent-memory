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
from psycopg.errors import (
    InsufficientPrivilege,
    InvalidAuthorizationSpecification,
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
from oam.config import ConfigurationError, OAMConfig
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
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
from oam.db.sources import AcceptanceReceipt, AcceptSource, SourceRepository
from oam.indexing.identity import index_generation
from oam.models.runtime import RuntimeModels
from oam.operations.repository import OperationNotFound, OperationRecord, OperationRepository
from oam.profiles.resources import map_profile_resource
from oam.resources.url_fetch import fetch_resource_url, safe_source_url


class AuthenticationFailed(PermissionError):
    """The supplied request credential is not currently valid."""


class AuthorizationFailed(PermissionError):
    """The caller cannot use the requested operation capability for this user."""


class APIRuntime(Protocol):
    def authenticate(self, actor: RequestActor) -> None: ...

    def retain_profile_resource(
        self,
        actor: RequestActor,
        user_id: str,
        body: ProfileResourceRequest,
        idempotency_key: str,
        *,
        expected_version: int | None,
    ) -> dict: ...

    def get_profile(
        self, actor: RequestActor, user_id: str, sections: list[str], max_tokens: int
    ) -> dict: ...

    def refresh_profile(self, actor: RequestActor, user_id: str, sections: list[str]) -> dict: ...

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

    def get_profile(
        self, actor: RequestActor, user_id: str, sections: list[str], max_tokens: int
    ) -> dict:
        from oam.profiles.service import ProfileService

        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:read")
        return ProfileService(self.config, self.pool).get(authority, sections, max_tokens)

    def refresh_profile(self, actor: RequestActor, user_id: str, sections: list[str]) -> dict:
        from oam.profiles.service import ProfileService

        with self.pool.connection() as connection:
            authority = self._authorize(connection, actor, user_id, "memory:append")
            self._authorize(connection, actor, user_id, "memory:read")
            authority = replace(authority, capabilities=frozenset({"memory:append", "memory:read"}))
        return ProfileService(self.config, self.pool).refresh(authority, sections)

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
    ) -> dict:
        """Freeze the caller payload, then let admission assign immutable source revisions."""
        caller = body.model_dump(mode="json")
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
                authority = self._authorize(connection, actor, body.user_id, "memory:append")
                prior = SourceRepository().find_idempotent(
                    connection,
                    authority,
                    operation_kind="index_resource",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
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
            authority = self._authorize(connection, actor, body.user_id, "memory:append")
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
            return self._retain_response(connection, authority, receipt)

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
            authority = self._authorize(connection, actor, user_id, "memory:append")
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
