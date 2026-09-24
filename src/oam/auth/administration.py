"""Credential administration using authoritative, digest-authenticated SQL functions."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from oam.auth.credentials import issue_credential
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialMetadata,
)


class CredentialAuthenticationFailed(PermissionError):
    """The actor's credential is not currently valid in PostgreSQL."""


class CredentialAdministrationDenied(PermissionError):
    """The current actor is not an administrator."""


class CredentialPrincipalUnavailable(LookupError):
    """The requested principal is not active in the actor's tenant."""


@contextmanager
def _administration_errors() -> Iterator[None]:
    try:
        yield
    except psycopg.errors.InvalidAuthorizationSpecification as exc:
        raise CredentialAuthenticationFailed from exc
    except psycopg.errors.InsufficientPrivilege as exc:
        raise CredentialAdministrationDenied from exc
    except psycopg.errors.NoDataFound as exc:
        raise CredentialPrincipalUnavailable from exc
    except psycopg.errors.InvalidParameterValue as exc:
        raise ValueError(exc.diag.message_primary) from exc


def _metadata(row: tuple) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=row[0],
        principal_id=row[1],
        state=row[2],
        expires_at=row[3],
        revoked_at=row[4],
        created_at=row[5],
    )


class CredentialAdministration:
    """Caller owns commit and may disclose secrets or invalidate caches only afterward."""

    def list(
        self,
        connection: psycopg.Connection,
        actor_secret_digest: bytes,
        *,
        limit: int,
        after: str | None = None,
    ) -> CredentialListResponse:
        if limit <= 0:
            raise ValueError("credential page limit must be positive")
        with _administration_errors(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT credential_id, principal_id, state, expires_at, revoked_at, created_at "
                "FROM public.oam_list_credentials(%s, %s, %s)",
                (actor_secret_digest, limit, after),
            )
            rows = cursor.fetchall()
        items = tuple(_metadata(row) for row in rows[:limit])
        return CredentialListResponse(
            items=items,
            next_after=items[-1].credential_id if len(rows) > limit else None,
        )

    def issue(
        self,
        connection: psycopg.Connection,
        actor_secret_digest: bytes,
        request: CredentialIssueRequest,
    ) -> CredentialIssuedResponse:
        issued = issue_credential()
        credential_id = f"key_{secrets.token_hex(12)}"
        principal_id = request.principal_id or f"user:{request.user_id}"
        with _administration_errors(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT credential_id, principal_id, state, expires_at, revoked_at, created_at "
                "FROM public.oam_issue_credential(%s, %s, %s, %s, %s)",
                (
                    actor_secret_digest,
                    principal_id,
                    credential_id,
                    issued.digest,
                    request.expires_at,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise CredentialPrincipalUnavailable
        return CredentialIssuedResponse(**_metadata(row).model_dump(), secret=issued.secret)

    def revoke(
        self,
        connection: psycopg.Connection,
        actor_secret_digest: bytes,
        credential_id: str,
    ) -> bytes | None:
        """Return the digest for local post-commit eviction; never expose it over HTTP."""
        with _administration_errors(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT public.oam_revoke_credential(%s, %s)",
                (actor_secret_digest, credential_id),
            )
            return cursor.fetchone()[0]
