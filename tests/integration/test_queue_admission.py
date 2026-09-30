"""Shared user capacity over real durable operations in isolated PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_postgres_repositories import command

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedUser
from oam.config import load_config
from oam.db.migrations import apply_migrations
from oam.db.queue_admission import PENDING_ROOTS_SQL, QueueAdmissionFull
from oam.db.sources import IdempotencyConflict, SourceRepository
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore
from oam.operations.repository import OperationIdempotencyConflict, OperationRepository
from oam.profiles.service import ProfileService
from oam.workers.claims import claim_one

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


@pytest.fixture
def queue_database():
    base_dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not base_dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    database_name = "oam_queue_" + uuid4().hex
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = make_conninfo(base_dsn, dbname=database_name)
    apply_migrations(dsn, ROOT / "migrations")
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('t','writer','writer','active')"
        )
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES ('t','u','synthetic','test','persistent'),"
            "('t','other','synthetic','test','persistent')"
        )
    principal = AuthenticatedPrincipal("key", "t", "writer", "writer", None)
    authority = AuthorizedUser.from_database_grant(
        principal,
        user_id="u",
        capabilities={"memory:append", "memory:read", "resource:write", "source:delete"},
    )
    return dsn, authority


def accept(dsn, authority, identifier, *, cap=2, **changes):
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET ROLE oam_api")
        return SourceRepository().accept(
            connection,
            authority,
            command(
                source_id=identifier,
                evidence_id=identifier,
                operation_id=identifier,
                work_unit_id=identifier,
                idempotency_key=identifier,
                **changes,
            ),
            max_pending_per_user=cap,
        )


def claim(dsn, worker, *, cap=2):
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_queue")
        return claim_one(
            connection.cursor(),
            worker_id=worker,
            attempt_id=uuid4().hex,
            lease_seconds=90,
            user_concurrency=cap,
        )


def pending(dsn):
    with psycopg.connect(dsn) as connection:
        return connection.execute(PENDING_ROOTS_SQL, ("t", "u")).fetchone()[0]


def finish_operation(dsn, operation_id, *, status="succeeded"):
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET status=%s WHERE operation_id=%s", (status, operation_id)
        )
        connection.execute(
            "UPDATE operations SET status=%s WHERE operation_id=%s", (status, operation_id)
        )


def test_profile_dependency_can_settle_after_failed_insight_work(queue_database):
    dsn, authority = queue_database
    accept(dsn, authority, "upstream")
    accept(dsn, authority, "ordinary", cap=3)
    finish_operation(dsn, "upstream", status="failed_terminal")
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET dependency_ids=ARRAY['upstream'] WHERE work_unit_id='ordinary'"
        )
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
            "schema_version,status,plan_sealed) "
            "VALUES ('t','u','profile','writer','refresh_profile',1,'accepted',true)"
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,dependency_ids,attempts_remaining) "
            "VALUES ('t','u','profile','profile','refresh_profile',"
            "decode(repeat('00',32),'hex'),'generation','queued',ARRAY['upstream'],1)"
        )
    claimed = claim(dsn, "profile-worker")
    assert claimed is not None and claimed.work_unit_id == "profile"
    assert claim(dsn, "ordinary-worker") is None


def test_replicas_cannot_accept_more_than_the_user_queue_cap(queue_database):
    dsn, authority = queue_database
    barrier = Barrier(4)

    def contender(index):
        barrier.wait(timeout=5)
        try:
            return accept(dsn, authority, str(index))
        except QueueAdmissionFull:
            return None

    with ThreadPoolExecutor(4) as workers:
        receipts = list(workers.map(contender, range(4)))
    accepted = [receipt for receipt in receipts if receipt]
    assert len(accepted) == 2
    assert pending(dsn) == 2
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM source_versions").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 2
    assert accept(dsn, authority, accepted[0].operation_id).reused
    with pytest.raises(IdempotencyConflict):
        accept(dsn, authority, accepted[0].operation_id, request_hash=b"x" * 32)


def test_delayed_consolidation_keeps_one_root_slot_until_all_work_ends(queue_database):
    dsn, authority = queue_database
    config = load_config(ROOT / "configs/oam.yaml").config
    generation = index_generation(config, {})
    accept(dsn, authority, "root", generation_id=generation)
    claimed = claim(dsn, "phase1")
    store = IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"), config, claimed, "phase1", generation
    )
    store.finish(schedule_consolidation=True)
    with psycopg.connect(dsn) as connection:
        child = connection.execute(
            "SELECT stage_state->>'consolidation_operation_id' FROM operations "
            "WHERE operation_id='root'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT available_at>clock_timestamp() FROM work_units WHERE operation_id=%s", (child,)
        ).fetchone()[0]
    assert pending(dsn) == 1
    accept(dsn, authority, "other-root")
    with pytest.raises(QueueAdmissionFull):
        accept(dsn, authority, "blocked")
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET status='retry_wait' WHERE operation_id=%s", (child,)
        )
    assert pending(dsn) == 2
    finish_operation(dsn, child, status="failed_terminal")
    assert pending(dsn) == 1
    assert not accept(dsn, authority, "blocked").reused


def test_explicit_retry_consumes_fresh_slot_but_exact_repeat_reuses_it(queue_database):
    dsn, authority = queue_database
    accept(dsn, authority, "failed")
    finish_operation(dsn, "failed", status="failed_terminal")
    repository = OperationRepository()

    def retry(identifier, digest=b"r" * 32):
        with psycopg.connect(dsn) as connection:
            connection.execute("SET LOCAL ROLE oam_api")
            return repository.retry(
                connection,
                authority,
                original_operation_id="failed",
                new_operation_id=identifier,
                idempotency_key=identifier,
                request_hash=digest,
                attempts_remaining=3,
                max_pending_per_user=1,
            )

    assert retry("retry").operation_id == "retry"
    assert pending(dsn) == 1
    assert retry("retry").operation_id == "retry"
    with pytest.raises(OperationIdempotencyConflict):
        retry("retry", b"x" * 32)
    with pytest.raises(QueueAdmissionFull):
        retry("second-retry")


def test_profile_refresh_reuses_work_and_obeys_queue_cap(queue_database):
    dsn, authority = queue_database
    config = load_config(ROOT / "configs/oam.yaml").config
    config = config.model_copy(
        update={"queue": config.queue.model_copy(update={"max_pending_per_user": 1})}
    )

    class Pool:
        @contextmanager
        def connection(self):
            with psycopg.connect(dsn) as connection:
                connection.execute("SET LOCAL ROLE oam_api")
                yield connection

    service = ProfileService(config, Pool())
    accept(dsn, authority, "root", cap=1)
    with pytest.raises(QueueAdmissionFull):
        service.refresh(authority)
    finish_operation(dsn, "root")
    receipt = service.refresh(authority)
    assert pending(dsn) == 1
    assert service.refresh(authority) == receipt
    current = service.get(authority, 1000)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,sections,content,"
            "input_identity,policy_epoch,generation_id,visible_from) "
            "VALUES ('t','u','user',1,'{}',%s,%s,%s,%s,1)",
            (
                "## Main content\n\nNo supported details yet.\n\n"
                "## References\n\nNo references yet.",
                bytes.fromhex(current["input_identity"]),
                current["policy_epoch"],
                index_generation(config, {}),
            ),
        )
    unchanged = service.refresh(authority)
    assert unchanged["status"] == "unchanged"
    assert unchanged["generation_calls"] == 0
    assert pending(dsn) == 1


def test_deletion_cleanup_remains_available_at_full_intake_cap(queue_database):
    dsn, authority = queue_database
    accept(dsn, authority, "root", cap=1)
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_api")
        receipt = SourceRepository().delete(
            connection,
            authority,
            source_id="root",
            resource_only=True,
            operation_id="cleanup",
            work_unit_id="cleanup",
            attempts_remaining=3,
        )
    assert receipt.cleanup_status == "accepted"
    assert pending(dsn) == 0
    accept(dsn, authority, "replacement", cap=1)


@pytest.mark.parametrize("release", ["succeeded", "cancelled", "expired"])
def test_live_claim_cap_is_shared_and_other_users_can_progress(queue_database, release):
    dsn, authority = queue_database
    for index in range(3):
        accept(dsn, authority, f"u-{index}", cap=4)
    other = AuthorizedUser(authority.credential_id, "t", "writer", "other", authority.capabilities)
    accept(dsn, other, "other", cap=4)
    barrier = Barrier(4)

    def contender(index):
        barrier.wait(timeout=5)
        return claim(dsn, str(index))

    with ThreadPoolExecutor(4) as workers:
        initial = list(workers.map(contender, range(4)))
    claimed = [item for item in initial if item]
    # A concurrent user-lock loser can retry through the normal poll boundary.
    while more := claim(dsn, "next-poll"):
        claimed.append(more)
    assert sorted(item.user_id for item in claimed) == ["other", "u", "u"]
    assert claim(dsn, "full") is None
    first = next(item for item in claimed if item.user_id == "u")
    if release == "expired":
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE work_unit_id=%s",
                (first.work_unit_id,),
            )
    elif release == "cancelled":
        with psycopg.connect(dsn) as connection:
            connection.execute("SET LOCAL ROLE oam_api")
            OperationRepository().cancel(connection, authority, first.operation_id)
    else:
        finish_operation(dsn, first.operation_id)
    replacement = claim(dsn, "expired-owner-replacement")
    assert replacement is not None
    assert replacement.user_id == "u"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM work_units WHERE user_id='u' AND status='running' "
                "AND lease_expires_at>clock_timestamp()"
            ).fetchone()[0]
            == 2
        )
