"""Public maintenance recovery preserves exclusion and removes owned vectors."""

import pytest
from test_memory_removal import (
    DEPENDENT_INSIGHT,
    INSIGHT,
    OTHER_MEMORIES,
    REMOVED_MEMORY,
    SURVIVING_MEMORY,
    USER,
    _client,
    _content,
    _items,
)
from test_memory_removal import core_database as core_database
from test_memory_removal import removal_database as removal_database

pytestmark = pytest.mark.integration


def _limit_repair_to_one_attempt(database):
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "UPDATE work_units SET attempts_remaining=1 WHERE stage='consolidate' "
            "AND status='queued'"
        )


@pytest.mark.parametrize("collection,target", [("memories", REMOVED_MEMORY), ("insights", INSIGHT)])
async def test_recover_exhausted_cleanup_removes_owned_vectors_without_new_receipt(
    removal_database, monkeypatch, collection, target
):
    database = removal_database
    key = (database.tenant, USER)
    memory_target = REMOVED_MEMORY if collection == "memories" else ""
    insight_targets = [INSIGHT, DEPENDENT_INSIGHT] if memory_target else [INSIGHT]
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/{collection}/{target}")
        assert removed.status_code == 202, removed.text
        operation_id = removed.json()["operation_id"]
        with _content(database.dsn, database.tenant) as connection:
            connection.execute(
                "UPDATE work_units SET attempts_remaining=1 WHERE tenant_id=%s "
                "AND user_id=%s AND operation_id=%s",
                (*key, operation_id),
            )
            unrelated_vectors = connection.execute(
                "SELECT unit_id,embedding::text FROM search_units "
                "WHERE memory_id=ANY(%s) ORDER BY unit_id",
                ([SURVIVING_MEMORY, *OTHER_MEMORIES],),
            ).fetchall()
            original_work = connection.execute(
                "SELECT work_unit_id,dependency_ids FROM work_units ORDER BY work_unit_id"
            ).fetchall()

        def fail_cleanup(*args, **kwargs):
            raise RuntimeError("synthetic cleanup outage")

        with monkeypatch.context() as patch:
            patch.setattr("oam.governance.cleanup._remove_projection_checkpoints", fail_cleanup)
            with pytest.raises(RuntimeError, match="synthetic cleanup outage"):
                database.cleanup()

        failed = await client.get(f"/v1/users/{USER}/operations/{operation_id}")
        assert failed.json()["status"] == "partial"
        assert failed.json()["stage_state"]["cleanup_status"] == "failed"
        assert not any(
            item == target or item.startswith(f"{target}:r") for item in await _items(client)
        )
        with _content(database.dsn, database.tenant) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM search_units WHERE memory_id=%s OR insight_id=ANY(%s)",
                    (memory_target, insight_targets),
                ).fetchone()[0]
                > 0
            )
        repeated_delete = await client.delete(f"/v1/users/{USER}/{collection}/{target}")
        assert repeated_delete.json()["operation_id"] == operation_id
        assert repeated_delete.json()["cleanup_status"] == "failed"
        generic_retry = await client.post(
            f"/v1/users/{USER}/operations/{operation_id}/retry",
            headers={"Idempotency-Key": "must-not-clone-removal"},
        )
        assert generic_retry.status_code == 409

        recovered = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["operation_id"] == operation_id
        assert (
            recovered.json()["stage_state"]["repair_inputs"]
            == failed.json()["stage_state"]["repair_inputs"]
        )
        with _content(database.dsn, database.tenant) as connection:
            recovered_budget = connection.execute(
                "SELECT attempts_remaining FROM work_units WHERE operation_id=%s", (operation_id,)
            ).fetchone()
        duplicate = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert duplicate.status_code == 200, duplicate.text
        with _content(database.dsn, database.tenant) as connection:
            assert (
                connection.execute(
                    "SELECT attempts_remaining FROM work_units WHERE operation_id=%s",
                    (operation_id,),
                ).fetchone()
                == recovered_budget
            )
            assert (
                connection.execute(
                    "SELECT work_unit_id,dependency_ids FROM work_units ORDER BY work_unit_id"
                ).fetchall()
                == original_work
            )

        database.cleanup()
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT count(*) FROM search_units WHERE memory_id=%s OR insight_id=ANY(%s)",
                (memory_target, insight_targets),
            ).fetchone() == (0,)
            assert (
                connection.execute(
                    "SELECT unit_id,embedding::text FROM search_units "
                    "WHERE memory_id=ANY(%s) ORDER BY unit_id",
                    ([SURVIVING_MEMORY, *OTHER_MEMORIES],),
                ).fetchall()
                == unrelated_vectors
            )
            assert connection.execute(
                "SELECT lifecycle_state FROM memories WHERE memory_id=%s", (REMOVED_MEMORY,)
            ).fetchone() == (("excluded" if memory_target else "active"),)
        completed = await client.delete(f"/v1/users/{USER}/{collection}/{target}")
        assert completed.json()["operation_id"] == operation_id
        assert completed.json()["cleanup_status"] == "complete"


async def test_recover_failed_repair_preserves_completed_cleanup_and_inputs(removal_database):
    from test_memory_removal import RepairModels, _run_repair

    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        database.cleanup()
        _limit_repair_to_one_attempt(database)
        with pytest.raises(ValueError, match="not valid JSON"):
            _run_repair(database, RepairModels(malformed=True))
        with _content(database.dsn, database.tenant) as connection:
            repair_id, repair_state = connection.execute(
                "SELECT operation_id,stage_state FROM operations WHERE kind='consolidate'"
            ).fetchone()
            assert connection.execute(
                "SELECT status FROM work_units WHERE operation_id=%s", (repair_id,)
            ).fetchone() == ("failed_terminal",)
            cleanup_work = connection.execute(
                "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                "WHERE operation_id=%s",
                (operation_id,),
            ).fetchone()
        recovered = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert recovered.status_code == 200, recovered.text
        with _content(database.dsn, database.tenant) as connection:
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE operation_id=%s", (repair_id,)
            ).fetchone()[0]
            assert state["insight_repairs"] == repair_state["insight_repairs"]
            assert (
                connection.execute(
                    "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                    "WHERE operation_id=%s",
                    (operation_id,),
                ).fetchone()
                == cleanup_work
            )
        models = RepairModels()
        assert _run_repair(database, models)
        assert models.calls == ["insights", "embedding"]
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT revision,synthesis_text FROM insights WHERE insight_id=%s", (INSIGHT,)
            ).fetchall() == [(3, "Regenerated eligible synthesis.")]
            assert connection.execute(
                "SELECT count(*) FROM search_units WHERE memory_id=%s", (REMOVED_MEMORY,)
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM search_units WHERE insight_id=%s AND insight_revision=3 "
                "AND embedding IS NOT NULL",
                (INSIGHT,),
            ).fetchone() == (1,)


async def test_recover_does_not_resume_cancelled_linked_repair(removal_database):
    from test_memory_removal import _run_repair

    database = removal_database
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        database.cleanup()
        status = await client.get(f"/v1/users/{USER}/operations/{operation_id}")
        repair_id = status.json()["stage_state"]["consolidation_operation_ids"][0]
        cancelled = await client.post(f"/v1/users/{USER}/operations/{repair_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        recovered = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert recovered.status_code == 409, recovered.text
        assert not _run_repair(database, object())
        repair = await client.get(f"/v1/users/{USER}/operations/{repair_id}")
        assert repair.json()["status"] == "cancelled"


async def test_recover_is_scoped_and_rejects_nonremoval_operations(removal_database):
    database = removal_database
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        status = await client.get(f"/v1/users/{USER}/operations/{operation_id}")
        repair_id = status.json()["stage_state"]["consolidation_operation_ids"][0]
        wrong_kind = await client.post(f"/v1/users/{USER}/operations/{repair_id}/recover")
        assert wrong_kind.status_code == 409, wrong_kind.text
        unknown = await client.post(f"/v1/users/{USER}/operations/not-visible/recover")
        assert unknown.status_code == 404, unknown.text
        async with _client(database.other_headers) as other:
            foreign = await other.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
            assert foreign.status_code == 404, foreign.text


async def test_recover_unknown_model_outcome_requires_reconciliation_without_mutation(
    removal_database,
):
    from test_memory_removal import RepairModels, _run_repair

    class UnknownEmbeddingModels(RepairModels):
        def embed(self, texts, **kwargs):
            self.calls.append("embedding")
            raise RuntimeError("synthetic unknown embedding outcome")

    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        database.cleanup()
        with pytest.raises(RuntimeError, match="synthetic unknown embedding outcome"):
            _run_repair(database, UnknownEmbeddingModels())
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT count(*) FROM attempts WHERE disposition='unknown_external_outcome'"
            ).fetchone() == (1,)
            before = connection.execute(
                "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                "ORDER BY work_unit_id"
            ).fetchall()
        response = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert response.status_code == 409, response.text
        with _content(database.dsn, database.tenant) as connection:
            assert (
                connection.execute(
                    "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                    "ORDER BY work_unit_id"
                ).fetchall()
                == before
            )
            assert connection.execute(
                "SELECT count(*) FROM attempts WHERE disposition='unknown_external_outcome'"
            ).fetchone() == (1,)


@pytest.mark.parametrize("stale", ["source", "generation"])
async def test_recover_checks_source_and_allows_old_generation(removal_database, stale):
    from test_memory_removal import RepairModels, _run_repair

    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        database.cleanup()
        _limit_repair_to_one_attempt(database)
        with pytest.raises(ValueError, match="not valid JSON"):
            _run_repair(database, RepairModels(malformed=True))
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT status FROM work_units WHERE stage='consolidate'"
            ).fetchone() == ("failed_terminal",)
            if stale == "source":
                connection.execute("UPDATE sources SET status='deleted'")
            else:
                connection.execute(
                    "UPDATE users SET index_generation_id='synthetic-new-generation'"
                )
            before = connection.execute(
                "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                "ORDER BY work_unit_id"
            ).fetchall()
        response = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert response.status_code == (409 if stale == "source" else 200), response.text
        with _content(database.dsn, database.tenant) as connection:
            after = connection.execute(
                "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                "ORDER BY work_unit_id"
            ).fetchall()
            if stale == "source":
                assert after == before
            else:
                assert any(row[1] == "queued" for row in after)


async def test_recovery_queue_cap_rolls_back_new_roots_and_allows_shared_root(removal_database):
    from test_memory_removal import ROOT, RepairModels, _run_repair

    from oam.auth.credentials import AuthorizedUser
    from oam.config import load_config
    from oam.db.queue_admission import QueueAdmissionFull
    from oam.operations.repository import OperationRepository

    database = removal_database
    key = (database.tenant, USER)
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (*key, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        removed = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = removed.json()["operation_id"]
        database.cleanup()
        _limit_repair_to_one_attempt(database)
        with pytest.raises(ValueError, match="not valid JSON"):
            _run_repair(database, RepairModels(malformed=True))
        with _content(database.dsn, database.tenant) as connection:
            principal = connection.execute(
                "SELECT principal_id FROM operations WHERE operation_id=%s", (operation_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
                "schema_version,status,plan_sealed) "
                "VALUES (%s,%s,'synthetic-active-root',%s,'index_interaction',1,'accepted',true)",
                (*key, principal),
            )
            before = connection.execute(
                "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                "ORDER BY work_unit_id"
            ).fetchall()
        authority = AuthorizedUser(
            "synthetic-key", database.tenant, principal, USER, frozenset({"source:delete"})
        )
        repository = OperationRepository()
        with _content(database.dsn, database.tenant) as connection:
            with pytest.raises(QueueAdmissionFull):
                repository.recover_removal(
                    connection,
                    authority,
                    operation_id,
                    attempts_remaining=load_config(
                        ROOT / "configs/oam.yaml"
                    ).config.retry.max_attempts,
                    max_pending_per_user=1,
                )
            assert (
                connection.execute(
                    "SELECT work_unit_id,status,attempts_remaining,fence FROM work_units "
                    "ORDER BY work_unit_id"
                ).fetchall()
                == before
            )
            # Sharing an already active source root must not count as a new admission.
            connection.execute(
                "UPDATE operations SET stage_state=stage_state||"
                '\'{"source_operation_id":"synthetic-active-root"}\'::jsonb '
                "WHERE kind='consolidate'"
            )
        with _content(database.dsn, database.tenant) as connection:
            recovered = repository.recover_removal(
                connection,
                authority,
                operation_id,
                attempts_remaining=load_config(ROOT / "configs/oam.yaml").config.retry.max_attempts,
                max_pending_per_user=1,
            )
            assert recovered.operation_id == operation_id
            assert connection.execute(
                "SELECT status FROM work_units WHERE stage='consolidate'"
            ).fetchone() == ("queued",)


async def test_profile_coverage_keeps_failed_repair_until_its_own_recovery_completes(
    removal_database,
):
    from test_memory_removal import SOURCE, RepairModels, _run_repair

    from oam.profiles.inputs import PENDING_PROFILE_INPUTS_SQL

    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    parameters = (database.tenant, USER)
    async with _client(database.headers) as client:
        first = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        operation_id = first.json()["operation_id"]
        database.cleanup()
        _limit_repair_to_one_attempt(database)
        with pytest.raises(ValueError, match="not valid JSON"):
            _run_repair(database, RepairModels(malformed=True))
        second = await client.delete(f"/v1/users/{USER}/memories/{OTHER_MEMORIES[0]}")
        assert second.status_code == 202, second.text
        database.cleanup()
        assert _run_repair(database, object())
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(PENDING_PROFILE_INPUTS_SQL, parameters).fetchall() == [
                (SOURCE, 1, "failed")
            ]
        recovered = await client.post(f"/v1/users/{USER}/operations/{operation_id}/recover")
        assert recovered.status_code == 200, recovered.text
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(PENDING_PROFILE_INPUTS_SQL, parameters).fetchall() == [
                (SOURCE, 1, "pending")
            ]
        assert _run_repair(database, RepairModels())
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(PENDING_PROFILE_INPUTS_SQL, parameters).fetchall() == []
