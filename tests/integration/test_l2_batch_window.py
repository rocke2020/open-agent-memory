"""User windows retain every source obligation and synthesize new seeds jointly."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import core_database as core_database
from test_core_http import process_source, use_created_user_key
from test_incremental_l2 import PolicyModels

from oam.config import load_config
from oam.workers.index_source import process_one

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


async def test_ready_sources_share_fixed_window_and_one_joint_request(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    models = PolicyModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        operations = []
        deadlines = []
        for index, text in enumerate(
            [
                "Production Atlas owned by Lin rolls back at 5 percent failures.",
                "Production Atlas rollback requires manual approval.",
            ]
        ):
            retained = await client.post(
                "/v1/retain",
                headers={"Idempotency-Key": f"batch-{index}"},
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
            assert retained.status_code == 202, retained.text
            process_source(config, queue, content, models=models, worker_id=f"source-{index}")
            operation = (
                await client.get(f"/v1/users/scope/operations/{retained.json()['operation_id']}")
            ).json()
            operations.append(operation)
            with psycopg.connect(dsn) as connection:
                deadlines.append(
                    connection.execute(
                        "SELECT available_at FROM work_units "
                        "WHERE tenant_id=%s AND operation_id=%s",
                        (tenant, operation["stage_state"]["consolidation_operation_id"]),
                    ).fetchone()[0]
                )
        batch_id = operations[0]["stage_state"]["consolidation_operation_id"]
        assert operations[1]["stage_state"]["consolidation_operation_id"] == batch_id
        assert deadlines[0] == deadlines[1]
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() "
                "WHERE tenant_id=%s AND operation_id=%s",
                (tenant, batch_id),
            )
        assert process_one(config, queue, content, models=models, worker_id="joint-l2")
        assert len(models.groups) == 1
        assert len(models.groups[0]["seed_ids"]) == 2
        for original in operations:
            operation = (
                await client.get(f"/v1/users/scope/operations/{original['operation_id']}")
            ).json()
            assert operation["stage_state"]["insights_ready"] is True
        batch = (await client.get(f"/v1/users/scope/operations/{batch_id}")).json()
        assert batch["status"] == "succeeded", batch
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM insights WHERE tenant_id=%s AND visible_to IS NULL",
                    (tenant,),
                ).fetchone()[0]
                == 1
            )
        app.state.runtime.models = models
        recalled = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "Production Atlas rollback approval",
                "recall_level": "fast",
            },
        )
        assert recalled.status_code == 200, recalled.text
        assert "manual approval" in recalled.json()["context"]
        assert any(item["kind"] == "insight" for item in recalled.json()["items"])


@pytest.fixture
async def batch_runtime(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = PolicyModels()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        yield SimpleNamespace(
            dsn=dsn,
            tenant=tenant,
            client=client,
            config=config,
            models=models,
            queue=make_conninfo(dsn, options="-c role=oam_queue"),
            content=make_conninfo(dsn, options="-c role=oam_content"),
        )


async def ready_source(runtime, key, texts, *, config=None):
    response = await runtime.client.post(
        "/v1/retain",
        headers={"Idempotency-Key": key},
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
                for text in texts
            ],
        },
    )
    assert response.status_code == 202, response.text
    process_source(
        config or runtime.config,
        runtime.queue,
        runtime.content,
        models=runtime.models,
        worker_id=key,
    )
    receipt = await runtime.client.get(
        f"/v1/users/scope/operations/{response.json()['operation_id']}"
    )
    assert receipt.status_code == 200, receipt.text
    return receipt.json()


@pytest.mark.parametrize("offset,joins", [(-1, True), (0, False), (1, False)])
async def test_exact_admission_boundary_does_not_depend_on_worker_start(
    batch_runtime, offset, joins
):
    from psycopg.rows import dict_row

    from oam.indexing.scheduling import enqueue_consolidation

    r = batch_runtime
    first = await ready_source(r, "first", ["Production Atlas rollback needs approval."])
    batch_id = first["stage_state"]["consolidation_operation_id"]
    off = r.config.model_copy(
        update={"index": r.config.index.model_copy(update={"insights_enabled": False})}
    )
    second = await ready_source(r, "second", ["Production Atlas owned by Lin."], config=off)
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE", (r.tenant, "scope")
        )
        due = connection.execute(
            "SELECT (stage_state->>'l2_due_at')::timestamptz "
            "FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        ).fetchone()[0]
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT o.source_id,o.source_version,w.generation_id,w.input_digest "
                "FROM operations o JOIN work_units w USING(tenant_id,user_id,operation_id) "
                "WHERE o.tenant_id=%s AND o.operation_id=%s AND w.stage='index_source'",
                (r.tenant, second["operation_id"]),
            )
            source = cursor.fetchone()

        class AdmissionClock:
            def execute(self, query, parameters=()):
                if query == "SELECT clock_timestamp()":
                    return SimpleNamespace(fetchone=lambda: (due + timedelta(seconds=offset),))
                return connection.execute(query, parameters)

        assigned = enqueue_consolidation(
            AdmissionClock(),
            r.config,
            key=(r.tenant, "scope"),
            source_operation_id=second["operation_id"],
            source=source,
            generation_id=source["generation_id"],
        )
        assert (assigned == batch_id) is joins


async def test_old_claimer_cannot_take_batch_and_new_claims_are_serialized(batch_runtime):
    r = batch_runtime
    first = await ready_source(r, "first", ["Production Atlas rollback needs approval."])
    batch_id = first["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() "
            "WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        )
    with psycopg.connect(r.queue) as connection:
        assert (
            connection.execute(
                "SELECT * FROM oam_claim_work('old','old-attempt',90,4,true)"
            ).fetchone()
            is None
        )
        new = connection.execute(
            "SELECT * FROM oam_claim_work('new','new-attempt',90,4,true,true)"
        ).fetchone()
        assert new[4] == "consolidate_batch"
        assert (
            connection.execute(
                "SELECT * FROM oam_claim_work('other','other-attempt',90,4,true,true)"
            ).fetchone()
            is None
        )


async def test_saved_response_is_reused_after_publication_failure(batch_runtime, monkeypatch):
    from oam.indexing import consolidation
    from oam.indexing.batch import BatchStore

    r = batch_runtime
    original = await ready_source(
        r,
        "first",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = original["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() "
            "WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        )
    publish = consolidation._publish_group

    def stop_before_publish(*args, **kwargs):
        raise RuntimeError("simulated process failure after durable response")

    monkeypatch.setattr(consolidation, "_publish_group", stop_before_publish)
    # A killed process cannot settle failure; its live claim expires for another worker.
    monkeypatch.setattr(BatchStore, "fail", lambda self, error: None)
    with pytest.raises(RuntimeError, match="simulated process failure"):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="stopped")
    assert len(r.models.groups) == 1
    with psycopg.connect(r.dsn) as connection:
        row = connection.execute(
            "SELECT inputs,output,published FROM l2_batch_groups "
            "WHERE tenant_id=%s AND batch_id=%s AND stage_key='insight'",
            (r.tenant, batch_id),
        ).fetchone()
        assert len(row[0]["envelope"]["seed_ids"]) == 2
        assert row[1] is not None and row[2] is False
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        )
    monkeypatch.setattr(consolidation, "_publish_group", publish)
    assert process_one(r.config, r.queue, r.content, models=r.models, worker_id="resumed")
    assert len(r.models.groups) == 1
    receipt = (await r.client.get(f"/v1/users/scope/operations/{batch_id}")).json()
    assert receipt["status"] == "succeeded", receipt


async def test_stale_batch_publication_requeues_without_spinning(batch_runtime, monkeypatch):
    from oam.indexing import consolidation
    from oam.indexing.store import StaleConsolidationInputs

    r = batch_runtime
    original = await ready_source(
        r,
        "first",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = original["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() "
            "WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        )
    calls = []

    def changed_target(*args, **kwargs):
        calls.append(True)
        assert len(calls) == 1, "stale group spun inside the same claimed attempt"
        raise StaleConsolidationInputs("target changed")

    monkeypatch.setattr(consolidation, "_publish_group", changed_target)
    with pytest.raises(StaleConsolidationInputs, match="target changed"):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="stale")
    with psycopg.connect(r.dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (r.tenant, batch_id),
            ).fetchone()[0]
            == "queued"
        )
        assert (
            connection.execute(
                "SELECT stage_state->'l2_examined_ids' FROM operations "
                "WHERE tenant_id=%s AND operation_id=%s",
                (r.tenant, batch_id),
            ).fetchone()[0]
            is None
        )


async def test_batch_candidate_lookahead_is_bounded_before_related_selection(batch_runtime):
    import json

    from oam.indexing.consolidation import _pending_group

    r = batch_runtime
    operations = []
    for number in range(3):
        operations.append(
            await ready_source(
                r, f"candidate-{number}", [f"Production Atlas deployment fact number {number}."]
            )
        )
    batch_id = operations[0]["stage_state"]["consolidation_operation_id"]
    config = r.config.model_copy(
        update={"extraction": r.config.extraction.model_copy(update={"max_facts_per_group": 2})}
    )
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "SELECT set_config('oam.tenant_id',%s,true),set_config('oam.user_id','scope',true)",
            (r.tenant,),
        )
        rows = connection.execute(
            "SELECT source_id,unit_id,generation_id FROM search_units "
            "WHERE tenant_id=%s AND kind='fact' ORDER BY created_at,unit_id",
            (r.tenant,),
        ).fetchall()
        connection.execute(
            "UPDATE search_units SET embedding=%s::vector WHERE tenant_id=%s AND unit_id=%s",
            (json.dumps([0.0, 1.0] + [0.0] * 1022), r.tenant, rows[1][1]),
        )
        store = SimpleNamespace(batch_id=batch_id, generation_id=rows[0][2], config=config)
        group = _pending_group(store, connection, None, set())
        # The third related fact is outside the oldest two candidates, so remains pending.
        assert [row["unit_id"] for row in group] == [rows[0][1]]


async def test_changed_generation_fails_batch_without_leaving_live_claim(batch_runtime):
    from oam.indexing.store import StaleIndexWork

    r = batch_runtime
    operation = await ready_source(r, "old-generation", ["Production Atlas rollback approval."])
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE users SET index_generation_id='new-generation' WHERE tenant_id=%s", (r.tenant,)
        )
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() "
            "WHERE tenant_id=%s AND operation_id=%s",
            (r.tenant, batch_id),
        )
    with pytest.raises(StaleIndexWork):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="stale-generation")
    with psycopg.connect(r.dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (r.tenant, batch_id),
            ).fetchone()[0]
            == "failed_terminal"
        )
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE tenant_id=%s AND operation_id=%s",
                (r.tenant, batch_id),
            ).fetchone()[0]
            == "partial_failed"
        )
    assert not r.models.groups


async def test_retry_admitted_after_failure_is_not_overwritten_by_old_settlement(
    batch_runtime,
    monkeypatch,
):
    from oam.auth.credentials import AuthorizedUser
    from oam.indexing import consolidation
    from oam.indexing.store import IndexStore
    from oam.operations.repository import OperationRepository

    r = batch_runtime
    operation = await ready_source(
        r,
        "retry-race",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() WHERE operation_id=%s",
            (batch_id,),
        )
    fail = IndexStore.fail

    def fail_then_admit_retry(store, error):
        fail(store, error)
        with psycopg.connect(r.dsn) as connection:
            principal = connection.execute(
                "SELECT principal_id FROM operations WHERE operation_id=%s", (batch_id,)
            ).fetchone()[0]
            OperationRepository().retry(
                connection,
                AuthorizedUser(
                    "test",
                    r.tenant,
                    principal,
                    "scope",
                    frozenset({"memory:read", "memory:append"}),
                ),
                original_operation_id=batch_id,
                new_operation_id="retry-after-failure",
                idempotency_key="retry-after-failure",
                request_hash=b"r" * 32,
                attempts_remaining=3,
                max_pending_per_user=256,
            )

    def failed_publish(*args, **kwargs):
        raise RuntimeError("interrupted publication")

    monkeypatch.setattr(IndexStore, "fail", fail_then_admit_retry)
    monkeypatch.setattr(consolidation, "_publish_group", failed_publish)
    with pytest.raises(RuntimeError, match="interrupted publication"):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="failing-owner")
    with psycopg.connect(r.dsn) as connection:
        assert connection.execute(
            "SELECT status FROM l2_batch_members WHERE batch_id=%s", (batch_id,)
        ).fetchall() == [("pending",)]


async def test_cancelled_batch_reports_failed_members_and_profile_inputs(batch_runtime):
    from oam.auth.credentials import AuthorizedUser
    from oam.operations.repository import OperationRepository
    from oam.profiles.inputs import PENDING_PROFILE_INPUTS_SQL

    r = batch_runtime
    operation = await ready_source(r, "cancelled", ["Production Atlas calibration approval."])
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        principal = connection.execute(
            "SELECT principal_id FROM operations WHERE operation_id=%s", (batch_id,)
        ).fetchone()[0]
        OperationRepository().cancel(
            connection,
            AuthorizedUser("test", r.tenant, principal, "scope", frozenset({"memory:append"})),
            batch_id,
        )
        assert connection.execute(
            "SELECT status FROM l2_batch_members WHERE batch_id=%s", (batch_id,)
        ).fetchall() == [("failed",)]
        pending = connection.execute(
            PENDING_PROFILE_INPUTS_SQL, (r.tenant, "scope", True, True)
        ).fetchall()
        assert [row[2] for row in pending] == ["failed"]
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE operation_id=%s", (batch_id,)
        ).fetchone()[0]
        assert [member["source_id"] for member in state["failed_sources"]] == [
            operation["source_id"]
        ]


async def test_local_embedding_rejection_is_not_an_unknown_provider_outcome(
    batch_runtime, monkeypatch
):
    from oam.auth.credentials import AuthorizedUser
    from oam.models.embedding_inputs import EmbeddingInputLimitError
    from oam.operations.repository import OperationRepository

    r = batch_runtime
    operation = await ready_source(
        r,
        "local-embedding-limit",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() WHERE operation_id=%s",
            (batch_id,),
        )

    def rejected_before_dispatch(*args, **kwargs):
        raise EmbeddingInputLimitError("local admission rejected this indivisible input")

    embed = r.models.embed
    monkeypatch.setattr(r.models, "embed", rejected_before_dispatch)
    with pytest.raises(EmbeddingInputLimitError):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="embedding-rejected")
    assert len(r.models.groups) == 1
    with psycopg.connect(r.dsn) as connection:
        assert connection.execute(
            "SELECT a.disposition FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) WHERE w.operation_id=%s",
            (batch_id,),
        ).fetchall() == [("failed",)]
        assert connection.execute(
            "SELECT output IS NOT NULL FROM l2_batch_groups "
            "WHERE batch_id=%s AND stage_key='insight'",
            (batch_id,),
        ).fetchone()[0]
        principal = connection.execute(
            "SELECT principal_id FROM operations WHERE operation_id=%s", (batch_id,)
        ).fetchone()[0]
        OperationRepository().retry(
            connection,
            AuthorizedUser("test", r.tenant, principal, "scope", frozenset({"memory:append"})),
            original_operation_id=batch_id,
            new_operation_id="embedding-retry",
            idempotency_key="embedding-retry",
            request_hash=b"e" * 32,
            attempts_remaining=3,
            max_pending_per_user=256,
        )
    monkeypatch.setattr(r.models, "embed", embed)
    assert process_one(r.config, r.queue, r.content, models=r.models, worker_id="embedding-resumed")
    assert len(r.models.groups) == 1
    with psycopg.connect(r.dsn) as connection:
        producer = connection.execute(
            "SELECT w.operation_id FROM l2_batch_groups g JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) "
            "WHERE g.batch_id=%s AND g.stage_key='insight_embed'",
            (batch_id,),
        ).fetchone()[0]
        assert producer == "embedding-retry"
        receipt = connection.execute(
            "SELECT status,stage_state FROM operations WHERE operation_id='embedding-retry'"
        ).fetchone()
        assert receipt[0] == "succeeded"
        assert "error" not in receipt[1]


@pytest.mark.parametrize("unknown_response", [False, True])
async def test_final_attempt_worker_loss_settles_members_and_preserves_unknown_outcome(
    batch_runtime,
    monkeypatch,
    unknown_response,
):
    from oam.indexing import consolidation
    from oam.indexing.batch import BatchStore

    r = batch_runtime
    operation = await ready_source(
        r,
        "final-lease",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp(),attempts_remaining=1 "
            "WHERE operation_id=%s",
            (batch_id,),
        )

    def stopped(*args, **kwargs):
        raise RuntimeError("worker lost")

    monkeypatch.setattr(BatchStore, "fail", lambda self, error: None)
    if unknown_response:
        monkeypatch.setattr(r.models, "generate", stopped)
    else:
        monkeypatch.setattr(consolidation, "_publish_group", stopped)
    with pytest.raises(RuntimeError, match="worker lost"):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="last-owner")
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",
            (batch_id,),
        )
    assert not process_one(r.config, r.queue, r.content, models=r.models, worker_id="lease-settler")
    with psycopg.connect(r.dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id=%s", (batch_id,)
            ).fetchone()[0]
            == "partial_failed"
        )
        assert connection.execute(
            "SELECT status FROM l2_batch_members WHERE batch_id=%s", (batch_id,)
        ).fetchall() == [("failed",)]
        assert connection.execute(
            "SELECT a.disposition FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) WHERE w.operation_id=%s",
            (batch_id,),
        ).fetchone()[0] == ("unknown_external_outcome" if unknown_response else "failed")
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE operation_id=%s", (batch_id,)
        ).fetchone()[0]
        assert len(state["failed_sources"]) == 1


async def test_saved_no_insight_expiry_retry_preserves_known_usage(batch_runtime, monkeypatch):
    import json

    from oam.auth.credentials import AuthorizedUser
    from oam.indexing import consolidation
    from oam.indexing.batch import BatchStore
    from oam.indexing.budget import _historical_batch_tokens
    from oam.models.runtime import ModelResult, Usage
    from oam.operations.repository import OperationRepository

    r = batch_runtime
    operation = await ready_source(
        r,
        "saved-no-insight",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = operation["stage_state"]["consolidation_operation_id"]
    calls = []

    def no_insight(role, **kwargs):
        assert role == "insights"
        envelope = json.loads(kwargs["user"])
        calls.append(envelope)
        return ModelResult(
            content=json.dumps(
                {"actions": [{"action": "NO_INSIGHT", "seed_ids": envelope["seed_ids"]}]}
            ),
            usage=Usage(
                input_tokens=25,
                output_tokens=12,
                total_tokens=37,
                cached_input_tokens=0,
                uncached_input_tokens=25,
                cache_creation_input_tokens=0,
                reasoning_tokens=0,
                coverage="complete",
            ),
        )

    def stopped(*args, **kwargs):
        raise RuntimeError("worker lost before no-insight publication")

    publish = consolidation._publish_group
    fail = BatchStore.fail
    monkeypatch.setattr(r.models, "generate", no_insight)
    monkeypatch.setattr(BatchStore, "fail", lambda self, error: None)
    monkeypatch.setattr(consolidation, "_publish_group", stopped)
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp(),attempts_remaining=1 "
            "WHERE operation_id=%s",
            (batch_id,),
        )
    with pytest.raises(RuntimeError, match="worker lost"):
        process_one(r.config, r.queue, r.content, models=r.models, worker_id="no-insight-owner")
    with psycopg.connect(r.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=%s",
            (batch_id,),
        )
    assert not process_one(
        r.config, r.queue, r.content, models=r.models, worker_id="no-insight-settler"
    )
    with psycopg.connect(r.dsn) as connection:
        principal = connection.execute(
            "SELECT principal_id FROM operations WHERE operation_id=%s", (batch_id,)
        ).fetchone()[0]
        OperationRepository().retry(
            connection,
            AuthorizedUser("test", r.tenant, principal, "scope", frozenset({"memory:append"})),
            original_operation_id=batch_id,
            new_operation_id="no-insight-retry",
            idempotency_key="no-insight-retry",
            request_hash=b"n" * 32,
            attempts_remaining=3,
            max_pending_per_user=256,
        )

    def publish_and_check_budget(store, connection, **kwargs):
        member = connection.execute(
            "SELECT source_id,source_version FROM l2_batch_members WHERE batch_id=%s",
            (batch_id,),
        ).fetchone()
        assert _historical_batch_tokens(store, connection, member) == 37
        return publish(store, connection, **kwargs)

    monkeypatch.setattr(BatchStore, "fail", fail)
    monkeypatch.setattr(consolidation, "_publish_group", publish_and_check_budget)
    assert process_one(
        r.config, r.queue, r.content, models=r.models, worker_id="no-insight-resumed"
    )
    assert len(calls) == 1
    with psycopg.connect(r.dsn) as connection:
        status, state = connection.execute(
            "SELECT status,stage_state FROM operations WHERE operation_id='no-insight-retry'"
        ).fetchone()
        assert status == "succeeded"
        assert state["usage"]["total_tokens"] == 37
        assert state["usage"]["coverage"] == "complete"
