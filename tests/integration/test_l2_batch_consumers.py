"""User batch consumers preserve each source's durable L2 obligation."""

from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import core_database as core_database
from test_core_http import process_source, use_created_user_key
from test_incremental_l2 import PolicyModels

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.queue_admission import PENDING_ROOTS_SQL, QueueAdmissionFull
from oam.operations.repository import (
    OperationNotRetryable,
    OperationRepository,
    RetryRequiresReconciliation,
)
from oam.profiles.inputs import PENDING_PROFILE_INPUTS_SQL
from oam.profiles.service import ProfileService
from oam.workers.l2_hold import set_l2_hold
from oam.workers.l2_index import queue_existing_l1

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture
async def batch_sources(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        for index, text in enumerate(
            [
                "Production Atlas owned by Lin rolls back at 5 percent failures.",
                "Production Atlas rollback requires manual approval.",
            ]
        ):
            response = await client.post(
                "/v1/retain",
                headers={"Idempotency-Key": str(index)},
                json={
                    "input_type": "interaction",
                    "user_id": "scope",
                    "messages": [
                        {
                            "role": "user",
                            "content": text,
                            "learning_eligible": True,
                            "event_time": "2026-11-18T00:00:00Z",
                        }
                    ],
                },
            )
            assert response.status_code == 202, response.text
            process_source(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=PolicyModels(),
                worker_id=f"source-{index}",
            )
    with psycopg.connect(dsn) as db:
        rows = db.execute(
            "SELECT source_operation_id,batch_id FROM l2_batch_members "
            "WHERE tenant_id=%s ORDER BY source_operation_id",
            (tenant,),
        ).fetchall()
        assert len(rows) == 2 and rows[0][1] == rows[1][1]
        principal = db.execute(
            "SELECT principal_id FROM operations WHERE operation_id=%s", (rows[0][0],)
        ).fetchone()[0]
    authority = AuthorizedUser(
        "test", tenant, principal, "scope", frozenset({"memory:read", "memory:append"})
    )
    return dsn, tenant, config, authority, rows[0][1], [row[0] for row in rows]


async def test_batch_capacity_counts_source_obligations_and_deduplicates_active_source(
    batch_sources,
):
    dsn, tenant, _, _, batch, sources = batch_sources
    with psycopg.connect(dsn) as db:
        assert db.execute(PENDING_ROOTS_SQL, (tenant, "scope")).fetchone()[0] == 2
        db.execute("UPDATE operations SET status='running' WHERE operation_id=%s", (sources[0],))
        assert db.execute(PENDING_ROOTS_SQL, (tenant, "scope")).fetchone()[0] == 2
        db.execute(
            "UPDATE l2_batch_members SET status='succeeded' WHERE source_operation_id=%s",
            (sources[1],),
        )
        assert db.execute(PENDING_ROOTS_SQL, (tenant, "scope")).fetchone()[0] == 1
        db.execute("UPDATE operations SET status='failed_terminal' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE work_units SET status='failed_terminal' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE operations SET status='succeeded' WHERE operation_id=%s", (sources[0],))
        assert db.execute(PENDING_ROOTS_SQL, (tenant, "scope")).fetchone()[0] == 0


async def test_batch_retry_reopens_only_failed_members_and_keeps_root_metadata(batch_sources):
    dsn, tenant, _, authority, batch, sources = batch_sources
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE operations SET status='partial_failed' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE work_units SET status='failed_terminal' WHERE operation_id=%s", (batch,))
        db.execute(
            "UPDATE l2_batch_members SET status=CASE WHEN source_operation_id=%s "
            "THEN 'succeeded' ELSE 'failed' END,error='failure'",
            (sources[0],),
        )
        before = db.execute(
            "SELECT operation_id,stage_state FROM operations WHERE operation_id=ANY(%s) "
            "ORDER BY operation_id",
            (sources,),
        ).fetchall()
        original_state = db.execute(
            "SELECT stage_state FROM operations WHERE operation_id=%s", (batch,)
        ).fetchone()[0]
        db.execute("SET LOCAL ROLE oam_api")
        # One reopened source obligation plus the explicit retry require two slots.
        with pytest.raises(QueueAdmissionFull):
            OperationRepository().retry(
                db,
                authority,
                original_operation_id=batch,
                new_operation_id="too-small",
                idempotency_key="too-small",
                request_hash=b"t" * 32,
                attempts_remaining=3,
                max_pending_per_user=1,
            )
        assert (
            db.execute("SELECT count(*) FROM operations WHERE operation_id='too-small'").fetchone()[
                0
            ]
            == 0
        )
        result = OperationRepository().retry(
            db,
            authority,
            original_operation_id=batch,
            new_operation_id="batch-retry",
            idempotency_key="batch-retry",
            request_hash=b"r" * 32,
            attempts_remaining=3,
            max_pending_per_user=5,
        )
        assert result.stage_state == original_state
        assert result.stage_state["l2_batch_id"] == batch
        assert db.execute(
            "SELECT source_operation_id,status,error FROM l2_batch_members "
            "ORDER BY source_operation_id"
        ).fetchall() == [(sources[0], "succeeded", "failure"), (sources[1], "pending", None)]
        after = db.execute(
            "SELECT operation_id,stage_state FROM operations WHERE operation_id=ANY(%s) "
            "ORDER BY operation_id",
            (sources,),
        ).fetchall()
        assert after[0] == before[0]
        assert after[1][1]["insights_ready"] is False
        assert after[1][1]["consolidation_status"] == "pending"
        assert db.execute(PENDING_ROOTS_SQL, (tenant, "scope")).fetchone()[0] == 2
        with pytest.raises(QueueAdmissionFull):
            OperationRepository().retry(
                db,
                authority,
                original_operation_id=batch,
                new_operation_id="second-retry",
                idempotency_key="second-retry",
                request_hash=b"s" * 32,
                attempts_remaining=3,
                max_pending_per_user=2,
            )
        with pytest.raises(OperationNotRetryable, match="unfinished"):
            OperationRepository().retry(
                db,
                authority,
                original_operation_id=batch,
                new_operation_id="duplicate",
                idempotency_key="duplicate",
                request_hash=b"d" * 32,
                attempts_remaining=3,
                max_pending_per_user=10,
            )
        reused = OperationRepository().retry(
            db,
            authority,
            original_operation_id=batch,
            new_operation_id="unused",
            idempotency_key="batch-retry",
            request_hash=b"r" * 32,
            attempts_remaining=3,
            max_pending_per_user=1,
        )
        assert reused.operation_id == "batch-retry"


async def test_profile_reports_failed_batch_members_and_pending_retry(batch_sources):
    dsn, tenant, _, _, batch, sources = batch_sources
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE operations SET status='partial_failed' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE work_units SET status='failed_terminal' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE l2_batch_members SET status='failed'")
        rows = db.execute(PENDING_PROFILE_INPUTS_SQL, (tenant, "scope", True, True)).fetchall()
        assert len(rows) == 2
        assert {row[2] for row in rows} == {"failed"}
        db.execute(
            "UPDATE l2_batch_members SET status='pending' WHERE source_operation_id=%s",
            (sources[0],),
        )
        rows = db.execute(PENDING_PROFILE_INPUTS_SQL, (tenant, "scope", True, True)).fetchall()
        assert sorted(row[2] for row in rows) == ["failed", "pending"]


@pytest.mark.parametrize("member_status", ["pending", "succeeded", "failed", "excluded"])
async def test_manual_backfill_skips_all_assigned_member_states(batch_sources, member_status):
    dsn, tenant, config, _, _, _ = batch_sources
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE l2_batch_members SET status=%s", (member_status,))
    receipt = queue_existing_l1(dsn, config, tenant, "scope", 1)
    assert receipt["scheduled_count"] == 0
    assert receipt["scheduled"] == []
    assert receipt["queue_full"] is False


async def test_hold_refuses_active_batch_and_does_not_set_hold(batch_sources):
    dsn, tenant, _, _, batch, _ = batch_sources
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE work_units SET status='running' WHERE operation_id=%s", (batch,))
    with pytest.raises(ValueError, match="running"):
        set_l2_hold(dsn, tenant, "scope", True)
    with psycopg.connect(dsn) as db:
        assert (
            db.execute("SELECT held FROM l2_claim_holds WHERE tenant_id=%s", (tenant,)).fetchone()
            is None
        )


async def test_profile_refresh_waits_for_batch_once_and_expedites_it(batch_sources):
    dsn, tenant, config, authority, batch, _ = batch_sources

    class Pool:
        @contextmanager
        def connection(self):
            with psycopg.connect(dsn) as db:
                db.execute("SET LOCAL ROLE oam_api")
                yield db

    receipt = ProfileService(config, Pool()).refresh(authority)
    assert receipt["status"] == "accepted"
    with psycopg.connect(dsn) as db:
        batch_work = db.execute(
            "SELECT work_unit_id FROM work_units WHERE operation_id=%s", (batch,)
        ).fetchone()[0]
        dependencies = db.execute(
            "SELECT dependency_ids FROM work_units WHERE operation_id=%s",
            (receipt["operation_id"],),
        ).fetchone()[0]
        assert dependencies == [batch_work]
        assert (
            db.execute(
                "SELECT available_at<=clock_timestamp() FROM work_units WHERE operation_id=%s",
                (batch,),
            ).fetchone()[0]
            is True
        )


async def test_root_retry_cannot_bypass_unknown_outcome_of_linked_batch_retry(batch_sources):
    dsn, _, _, authority, batch, _ = batch_sources
    repository = OperationRepository()
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE operations SET status='partial_failed' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE work_units SET status='failed_terminal' WHERE operation_id=%s", (batch,))
        db.execute("UPDATE l2_batch_members SET status='failed'")
        db.execute("SET LOCAL ROLE oam_api")
        repository.retry(
            db,
            authority,
            original_operation_id=batch,
            new_operation_id="ambiguous-retry",
            idempotency_key="ambiguous-retry",
            request_hash=b"a" * 32,
            attempts_remaining=1,
        )
    with psycopg.connect(dsn) as db:
        db.execute(
            "UPDATE operations SET status='partial_failed' WHERE operation_id='ambiguous-retry'"
        )
        db.execute(
            "UPDATE work_units SET status='failed_terminal' WHERE operation_id='ambiguous-retry'"
        )
        db.execute("UPDATE l2_batch_members SET status='failed'")
        db.execute(
            "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,ordinal,"
            "owner_id,fence,disposition) SELECT tenant_id,user_id,work_unit_id,"
            "'ambiguous-attempt',1,'lost-worker',1,'unknown_external_outcome' "
            "FROM work_units WHERE operation_id='ambiguous-retry'"
        )
        db.execute("SET LOCAL ROLE oam_api")
        with pytest.raises(RetryRequiresReconciliation):
            repository.retry(
                db,
                authority,
                original_operation_id=batch,
                new_operation_id="unsafe-root-retry",
                idempotency_key="unsafe-root-retry",
                request_hash=b"u" * 32,
                attempts_remaining=1,
            )
        db.execute("RESET ROLE")
        assert (
            db.execute(
                "SELECT count(*) FROM operations WHERE operation_id='unsafe-root-retry'"
            ).fetchone()[0]
            == 0
        )
        assert {row[0] for row in db.execute("SELECT status FROM l2_batch_members")} == {"failed"}
