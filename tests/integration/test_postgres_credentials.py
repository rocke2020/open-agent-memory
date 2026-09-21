"""Credential administration under real API-role database privileges."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from oam.auth.credentials import digest_secret
from oam.auth.repository import AuthRepository
from oam.db.migrations import apply_migrations

pytestmark = pytest.mark.integration


@pytest.fixture
def credentials_database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    apply_migrations(dsn, Path(__file__).parents[2] / "migrations")
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(force_rollback=True),
    ):
        tenant = f"credential-{uuid4().hex}"
        other_tenant = f"credential-{uuid4().hex}"
        secrets = {
            "admin": digest_secret(f"admin-{uuid4().hex}"),
            "writer": digest_secret(f"writer-{uuid4().hex}"),
            "member": digest_secret(f"member-{uuid4().hex}"),
            "foreign": digest_secret(f"foreign-{uuid4().hex}"),
        }
        for tenant_id in (tenant, other_tenant):
            connection.execute(
                "INSERT INTO tenants (tenant_id, state) VALUES (%s, 'active')", (tenant_id,)
            )
        for tenant_id, principal_id, role, state in (
            (tenant, "admin", "administrator", "active"),
            (tenant, "writer", "writer", "active"),
            (tenant, "member", "reader", "active"),
            (tenant, "disabled", "reader", "disabled"),
            (other_tenant, "foreign", "administrator", "active"),
        ):
            connection.execute(
                "INSERT INTO principals (tenant_id, principal_id, role, state) "
                "VALUES (%s, %s, %s, %s)",
                (tenant_id, principal_id, role, state),
            )
        for principal_id, digest in secrets.items():
            connection.execute(
                "INSERT INTO credentials "
                "(tenant_id, credential_id, principal_id, secret_digest, state) "
                "VALUES (%s, %s, %s, %s, 'active')",
                (
                    other_tenant if principal_id == "foreign" else tenant,
                    f"key-{principal_id}",
                    principal_id,
                    digest,
                ),
            )
        connection.execute("SET LOCAL ROLE oam_api")
        yield connection, tenant, other_tenant, secrets


def test_issue_keeps_sibling_key_and_persists_only_digest(credentials_database):
    from oam.auth.administration import CredentialAdministration
    from oam.contracts.credentials import CredentialIssueRequest

    connection, tenant, _, secrets = credentials_database
    expires_at = datetime.now(UTC) + timedelta(days=1)
    issued = CredentialAdministration().issue(
        connection,
        secrets["admin"],
        CredentialIssueRequest(principal_id="member", expires_at=expires_at),
    )
    assert issued.principal_id == "member"
    assert issued.secret.startswith("oam_")
    assert issued.state == "active"
    assert issued.expires_at == expires_at
    assert issued.revoked_at is None
    auth = AuthRepository()
    assert auth.lookup_credential(connection, secrets["member"]) is not None
    new_principal = auth.lookup_credential(connection, digest_secret(issued.secret))
    assert new_principal is not None
    assert (new_principal.tenant_id, new_principal.principal_id) == (tenant, "member")
    connection.execute("RESET ROLE")
    stored = connection.execute(
        "SELECT secret_digest FROM credentials WHERE tenant_id = %s AND credential_id = %s",
        (tenant, issued.credential_id),
    ).fetchone()
    assert stored == (digest_secret(issued.secret),)


def test_list_is_tenant_scoped_paginated_metadata(credentials_database):
    from oam.auth.administration import CredentialAdministration

    connection, _, _, secrets = credentials_database
    repository = CredentialAdministration()
    first = repository.list(connection, secrets["admin"], limit=2)
    assert [item.credential_id for item in first.items] == ["key-admin", "key-member"]
    assert first.next_after == "key-member"
    last = repository.list(connection, secrets["admin"], limit=2, after=first.next_after)
    assert [item.credential_id for item in last.items] == ["key-writer"]
    assert last.next_after is None
    serialized = first.model_dump_json()
    assert "secret" not in serialized
    assert "digest" not in serialized
    assert "key-foreign" not in serialized


def test_revoke_is_idempotent_non_enumerating_and_preserves_sibling(credentials_database):
    from oam.auth.administration import CredentialAdministration
    from oam.contracts.credentials import CredentialIssueRequest

    connection, _, _, secrets = credentials_database
    repository = CredentialAdministration()
    sibling = repository.issue(
        connection, secrets["admin"], CredentialIssueRequest(principal_id="member")
    )
    assert repository.revoke(connection, secrets["admin"], "missing") is None
    assert repository.revoke(connection, secrets["admin"], "key-foreign") is None
    assert repository.revoke(connection, secrets["admin"], "key-member") == secrets["member"]
    before = repository.list(connection, secrets["admin"], limit=50)
    assert repository.revoke(connection, secrets["admin"], "key-member") == secrets["member"]
    assert repository.list(connection, secrets["admin"], limit=50) == before
    auth = AuthRepository()
    assert auth.lookup_credential(connection, secrets["member"]) is None
    assert auth.lookup_credential(connection, digest_secret(sibling.secret)) is not None
    assert auth.lookup_credential(connection, secrets["foreign"]) is not None


@pytest.mark.parametrize("principal_id", ["missing", "foreign", "disabled"])
def test_issue_requires_an_existing_active_principal_in_actor_tenant(
    credentials_database, principal_id
):
    from oam.auth.administration import CredentialAdministration, CredentialPrincipalUnavailable
    from oam.contracts.credentials import CredentialIssueRequest

    connection, _, _, secrets = credentials_database
    with (
        pytest.raises(CredentialPrincipalUnavailable),
        connection.transaction(),
    ):
        CredentialAdministration().issue(
            connection, secrets["admin"], CredentialIssueRequest(principal_id=principal_id)
        )


@pytest.mark.parametrize("action", ["list", "issue", "revoke"])
@pytest.mark.parametrize("actor", ["writer", "unknown", "revoked", "expired", "disabled"])
def test_every_administration_action_rechecks_current_actor(credentials_database, action, actor):
    from oam.auth.administration import (
        CredentialAdministration,
        CredentialAdministrationDenied,
        CredentialAuthenticationFailed,
    )
    from oam.contracts.credentials import CredentialIssueRequest

    connection, tenant, _, secrets = credentials_database
    actor_digest = secrets["admin"]
    if actor in ("writer", "unknown"):
        actor_digest = secrets.get(actor, b"?" * 32)
    else:
        connection.execute("RESET ROLE")
        if actor == "revoked":
            connection.execute(
                "UPDATE credentials SET state = 'revoked', revoked_at = clock_timestamp() "
                "WHERE tenant_id = %s AND credential_id = 'key-admin'",
                (tenant,),
            )
        elif actor == "expired":
            connection.execute(
                "UPDATE credentials SET expires_at = clock_timestamp() - interval '1 second' "
                "WHERE tenant_id = %s AND credential_id = 'key-admin'",
                (tenant,),
            )
        else:
            connection.execute(
                "UPDATE principals SET state = 'disabled' "
                "WHERE tenant_id = %s AND principal_id = 'admin'",
                (tenant,),
            )
        connection.execute("SET LOCAL ROLE oam_api")
    repository = CredentialAdministration()
    expected = (
        CredentialAdministrationDenied if actor == "writer" else CredentialAuthenticationFailed
    )
    with pytest.raises(expected), connection.transaction():
        if action == "list":
            repository.list(connection, actor_digest, limit=50)
        elif action == "issue":
            repository.issue(
                connection, actor_digest, CredentialIssueRequest(principal_id="member")
            )
        else:
            repository.revoke(connection, actor_digest, "key-member")


def test_database_functions_reject_nonadministrators_and_caller_tenant_spoofing(
    credentials_database,
):
    connection, tenant, _, secrets = credentials_database
    connection.execute("SELECT set_config('oam.tenant_id', %s, true)", (tenant,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
        connection.execute("SELECT * FROM oam_list_credentials(%s, 50, NULL)", (secrets["writer"],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
        connection.execute("UPDATE credentials SET state = 'revoked'")
    connection.execute("SET LOCAL ROLE oam_queue")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
        connection.execute("SELECT * FROM oam_list_credentials(%s, 50, NULL)", (secrets["admin"],))
