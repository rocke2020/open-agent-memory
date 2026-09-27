"""Exercise foundation repositories only in an explicitly isolated PostgreSQL database."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedUser
from oam.db.migrations import apply_migrations
from oam.db.sources import AcceptSource, SourceRepository, SourceVersionConflict
from oam.workers.claims import claim_one

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    apply_migrations(dsn, Path(__file__).parents[2] / "migrations")
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(force_rollback=True),
    ):
        tenant = f"test-{uuid4().hex}"
        connection.execute(
            "INSERT INTO tenants (tenant_id, state) VALUES (%s, 'active')", (tenant,)
        )
        connection.execute(
            "INSERT INTO principals (tenant_id, principal_id, role, state) "
            "VALUES (%s, 'writer', 'writer', 'active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO users (tenant_id, user_id, subject, namespace, lifetime) "
            "VALUES (%s, 'scope', 'synthetic', 'test', 'persistent')",
            (tenant,),
        )
        principal = AuthenticatedPrincipal("key", tenant, "writer", "writer", None)
        authority = AuthorizedUser.from_database_grant(
            principal,
            user_id="scope",
            capabilities={"memory:append", "memory:read", "resource:write"},
        )
        yield connection, authority


def command(**changes):
    return replace(
        AcceptSource(
            source_id="source",
            source_version=1,
            input_role="evidence_resource",
            resource_type="report",
            media_type="text/html",
            original_bytes=b"synthetic report",
            evidence_id="evidence",
            content_text=None,
            learning_eligible=False,
            operation_id="operation",
            operation_kind="retain_resource",
            schema_version=1,
            idempotency_key="accept",
            request_hash=b"r" * 32,
            work_unit_id="work",
            stage="parse_resource",
            generation_id="generation",
            attempts_remaining=3,
        ),
        **changes,
    )


def test_queue_role_claims_work_without_permission_to_modify_operations(database):
    connection, authority = database
    connection.execute("SET LOCAL ROLE oam_api")
    receipt = SourceRepository().accept(connection, authority, command())
    assert receipt.accepted_seq == 1
    connection.execute("SET LOCAL ROLE oam_queue")
    claimed = claim_one(
        connection.cursor(),
        worker_id="worker",
        attempt_id="attempt",
        lease_seconds=90,
        user_concurrency=2,
    )
    assert claimed is not None
    assert (claimed.tenant_id, claimed.work_unit_id, claimed.fence) == (
        authority.tenant_id,
        "work",
        1,
    )


def test_uncertain_attempt_cannot_be_reclaimed_until_reconciled(database):
    connection, authority = database
    SourceRepository().accept(connection, authority, command())
    connection.execute("SET LOCAL ROLE oam_queue")
    claim_one(
        connection.cursor(),
        worker_id="worker",
        attempt_id="attempt",
        lease_seconds=90,
        user_concurrency=2,
    )
    connection.execute("RESET ROLE")
    connection.execute(
        "UPDATE work_units SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE tenant_id = %s",
        (authority.tenant_id,),
    )
    connection.execute(
        "UPDATE attempts SET disposition = 'unknown_external_outcome' WHERE tenant_id = %s",
        (authority.tenant_id,),
    )
    connection.execute("SET LOCAL ROLE oam_queue")
    assert (
        claim_one(
            connection.cursor(),
            worker_id="replacement",
            attempt_id="second",
            lease_seconds=90,
            user_concurrency=2,
        )
        is None
    )


@pytest.mark.parametrize("source_state", ["deleting", "deleted"])
def test_accept_cannot_reactivate_a_deleted_source(database, source_state):
    connection, authority = database
    repository = SourceRepository()
    repository.accept(connection, authority, command())
    connection.execute(
        "UPDATE sources SET status = %s WHERE tenant_id = %s", (source_state, authority.tenant_id)
    )
    with pytest.raises(SourceVersionConflict):
        repository.accept(
            connection,
            authority,
            command(
                source_version=2,
                evidence_id="new-evidence",
                operation_id="new-operation",
                idempotency_key="new-accept",
                request_hash=b"s" * 32,
                work_unit_id="new-work",
            ),
        )


def test_original_read_requires_a_read_grant(database):
    connection, authority = database
    repository = SourceRepository()
    repository.accept(connection, authority, command())
    with pytest.raises(PermissionError):
        repository.read_original(
            connection,
            replace(authority, capabilities=frozenset({"memory:append"})),
            source_id="source",
            source_version=1,
        )


def test_overlapping_identical_accepts_reuse_one_receipt(database):
    connection, _ = database
    dsn = connection.info.dsn
    tenant = f"race-{uuid4().hex}"
    principal = AuthenticatedPrincipal("key", tenant, "writer", "writer", None)
    authority = AuthorizedUser.from_database_grant(
        principal, user_id="scope", capabilities={"memory:append", "resource:write"}
    )
    with psycopg.connect(dsn) as setup:
        setup.execute("INSERT INTO tenants (tenant_id, state) VALUES (%s, 'active')", (tenant,))
        setup.execute(
            "INSERT INTO principals (tenant_id, principal_id, role, state) "
            "VALUES (%s, 'writer', 'writer', 'active')",
            (tenant,),
        )
        setup.execute(
            "INSERT INTO users (tenant_id, user_id, subject, namespace, lifetime) "
            "VALUES (%s, 'scope', 'synthetic', 'test', 'persistent')",
            (tenant,),
        )

    def accept():
        with psycopg.connect(dsn, application_name=tenant, autocommit=True) as writer:
            writer.execute("SET ROLE oam_api")
            return SourceRepository().accept(writer, authority, command())

    try:
        with psycopg.connect(dsn, autocommit=True) as locker, ThreadPoolExecutor(2) as workers:
            with locker.transaction():
                locker.execute("SELECT 1 FROM users WHERE tenant_id = %s FOR UPDATE", (tenant,))
                pending = [workers.submit(accept) for _ in range(2)]
                deadline = time.monotonic() + 5
                while True:
                    locker.execute("SELECT pg_stat_clear_snapshot()")
                    blocked = locker.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name = %s AND wait_event_type = 'Lock'",
                        (tenant,),
                    ).fetchone()[0]
                    if blocked == 2:
                        break
                    assert time.monotonic() < deadline, "both accept calls must overlap"
                    threading.Event().wait(0.01)
            receipts = [future.result(timeout=5) for future in pending]
        assert {receipt.operation_id for receipt in receipts} == {"operation"}
        assert sorted(receipt.reused for receipt in receipts) == [False, True]
        with psycopg.connect(dsn) as check:
            assert (
                check.execute(
                    "SELECT accepted_seq FROM users WHERE tenant_id = %s", (tenant,)
                ).fetchone()[0]
                == 1
            )
    finally:
        with psycopg.connect(dsn) as cleanup:
            cleanup.execute(
                "UPDATE operations SET status = 'cancelled' WHERE tenant_id = %s", (tenant,)
            )
            cleanup.execute(
                "UPDATE work_units SET status = 'cancelled' WHERE tenant_id = %s", (tenant,)
            )
