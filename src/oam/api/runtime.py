"""Authenticated database composition for the public operation routes."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg_pool import ConnectionPool

from oam.auth.administration import CredentialAdministration
from oam.auth.credentials import PositiveCredentialCache, digest_secret
from oam.auth.repository import AuthRepository, ScopeDenied
from oam.config import OAMConfig
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
from oam.contracts.operations import ReconcileAttemptRequest
from oam.operations.repository import OperationNotFound, OperationRecord, OperationRepository


class AuthenticationFailed(PermissionError):
    """The supplied opaque bearer credential is not currently valid."""


class AuthorizationFailed(PermissionError):
    """The caller cannot use the requested operation capability in this scope."""


class APIRuntime(Protocol):
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
    _auth: AuthRepository = field(init=False, repr=False)
    _operations: OperationRepository = field(init=False, repr=False)
    _credential_cache: PositiveCredentialCache = field(init=False, repr=False)
    _administration: CredentialAdministration = field(init=False, repr=False)

    def __post_init__(self) -> None:
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

    def _authorize(self, connection, bearer_secret: str, scope_id: str, capability: str):
        digest = digest_secret(bearer_secret)
        principal = self._credential_cache.get(digest)
        if principal is None:
            lookup = self._credential_cache.begin_lookup(digest)
            principal = self._auth.lookup_credential(connection, digest)
            if principal is None:
                raise AuthenticationFailed
            self._credential_cache.store(lookup, principal)
        try:
            return self._auth.authorize_scope(
                connection,
                principal,
                scope_id=scope_id,
                capability=capability,
            )
        except ScopeDenied as exc:
            raise AuthorizationFailed from exc
