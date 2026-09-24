"""Database-backed credential and user authorization repositories."""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from typing import Protocol

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedUser


class UserDenied(PermissionError):
    """The authenticated principal lacks the requested user capability."""


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class Connection(Protocol):
    def cursor(self) -> AbstractContextManager[Cursor]: ...


class AuthRepository:
    def lookup_credential(
        self, connection: Connection, digest: bytes
    ) -> AuthenticatedPrincipal | None:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("credential digest must be a SHA-256 digest")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT credential_id, tenant_id, principal_id, principal_role, expires_at "
                "FROM oam_lookup_credential(%s)",
                (digest,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return AuthenticatedPrincipal(
            credential_id=str(row[0]),
            tenant_id=str(row[1]),
            principal_id=str(row[2]),
            role=str(row[3]),
            expires_at=row[4],  # type: ignore[arg-type]
        )

    def authorize_user(
        self,
        connection: Connection,
        principal: AuthenticatedPrincipal,
        *,
        user_id: str,
        capability: str,
    ) -> AuthorizedUser:
        if not user_id.strip() or not capability.strip():
            raise ValueError("user_id and capability cannot be empty")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT oam_authorize_user(%s, %s, %s, %s)",
                (principal.tenant_id, principal.principal_id, user_id, capability),
            )
            row = cursor.fetchone()
        if row is None or row[0] is not True:
            raise UserDenied(user_id)
        return AuthorizedUser.from_database_grant(
            principal,
            user_id=user_id,
            capabilities={capability},
        )
