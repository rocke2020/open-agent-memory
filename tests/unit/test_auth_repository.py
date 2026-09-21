from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from oam.auth import repository
from oam.auth.credentials import AuthenticatedPrincipal, digest_secret


class Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.calls.append((query, parameters))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class Connection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.cursor_value = Cursor(row)

    @contextmanager
    def cursor(self):
        yield self.cursor_value


def test_digest_lookup_uses_only_allowlisted_pre_scope_function() -> None:
    expiry = datetime(2026, 10, 1, tzinfo=UTC)
    connection = Connection(("key-1", "tenant-1", "principal-1", "writer", expiry))
    digest = digest_secret("oam_secret")

    principal = repository.AuthRepository().lookup_credential(connection, digest)

    assert principal == AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        role="writer",
        expires_at=expiry,
    )
    assert connection.cursor_value.calls == [
        (
            "SELECT credential_id, tenant_id, principal_id, principal_role, expires_at "
            "FROM oam_lookup_credential(%s)",
            (digest,),
        )
    ]


def test_scope_grant_is_checked_with_credential_derived_tenant() -> None:
    principal = AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        role="writer",
        expires_at=None,
    )
    connection = Connection((True,))

    authority = repository.AuthRepository().authorize_scope(
        connection,
        principal,
        scope_id="scope-1",
        capability="memory:append",
    )

    assert authority.tenant_id == "tenant-1"
    assert authority.scope_id == "scope-1"
    assert authority.capabilities == frozenset({"memory:append"})
    assert connection.cursor_value.calls[0][1] == (
        "tenant-1",
        "principal-1",
        "scope-1",
        "memory:append",
    )


def test_denied_scope_grant_does_not_construct_authority() -> None:
    principal = AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        role="writer",
        expires_at=None,
    )

    with pytest.raises(repository.ScopeDenied):
        repository.AuthRepository().authorize_scope(
            Connection((False,)),
            principal,
            scope_id="scope-foreign",
            capability="memory:read",
        )
