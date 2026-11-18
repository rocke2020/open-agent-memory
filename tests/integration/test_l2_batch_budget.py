"""Shared requests conserve source allowances and reject atomically before dispatch."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_core_http import SyntheticIndexModels, process_source, use_created_user_key
from test_core_http import core_database as core_database
from test_l2_batch_window import batch_runtime as batch_runtime
from test_l2_batch_window import ready_source

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.context import bind_user
from oam.indexing import budget
from oam.indexing.extraction import InsightInput, consolidation_envelope
from oam.indexing.store import input_digest
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


class RecordingModels:
    def __init__(self):
        self.calls = []

    def generate(self, role, **kwargs):
        self.calls.append((role, kwargs))
        return {"one_actual_result": True}


@pytest.fixture
async def batch_budget(core_database, tmp_path):
    dsn, tenant, headers = core_database
    path = tmp_path / "off.yaml"
    path.write_text("index:\n  insights_enabled: false\n")
    config = load_config(path).config
    operations = []
    app = create_api_only_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        for number in range(2):
            accepted = await client.post(
                "/v1/retain",
                headers={"Idempotency-Key": f"budget-source-{number}"},
                json={
                    "input_type": "interaction",
                    "user_id": "scope",
                    "messages": [
                        {
                            "role": "user",
                            "content": "I prefer tea after dinner.",
                            "learning_eligible": True,
                            "event_time": "2026-11-18T00:00:00Z",
                        }
                    ],
                },
            )
            assert accepted.status_code == 202, accepted.text
            operations.append(accepted.json()["operation_id"])
            process_source(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=SyntheticIndexModels(),
                worker_id=f"source-{number}",
            )
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT o.source_id,o.source_version,w.generation_id FROM operations o "
            "JOIN work_units w USING(tenant_id,user_id,operation_id) "
            "WHERE o.operation_id=ANY(%s) AND w.stage='index_source' ORDER BY o.accepted_seq",
            (operations,),
        ).fetchall()
    members = [(row[0], row[1]) for row in rows]
    facts = tuple(
        InsightInput(
            id=f"m{number}", text="x" * (1 + number * 32), source_refs=(f"{source}:{version}",)
        )
        for number, (source, version) in enumerate(members)
    )
    historical = InsightInput(id="history", text="h" * 5000, source_refs=("unowned:1",))
    inputs = {
        "private_facts": [fact.model_dump(mode="json") for fact in (*facts, historical)],
        "envelope": consolidation_envelope((*facts, historical), ("m0", "m1"), ()),
    }
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
            "schema_version,status,plan_sealed,stage_state) SELECT tenant_id,user_id,"
            "'batch',principal_id,'consolidate',schema_version,'running',true,%s "
            "FROM operations WHERE operation_id=%s",
            (Jsonb({"l2_batch_id": "batch"}), operations[0]),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) VALUES "
            "(%s,'scope','budget-work','batch','consolidate_batch',%s,%s,'running',1)",
            (tenant, input_digest(inputs), rows[0][2]),
        )
        connection.execute(
            "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,ordinal,"
            "owner_id,fence,disposition) VALUES "
            "(%s,'scope','budget-work','batch-attempt',1,'worker',1,'claimed')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO l2_batch_groups(tenant_id,user_id,batch_id,stage_key,input_digest,"
            "inputs,work_unit_id,attempt_id) VALUES "
            "(%s,'scope','batch','insight',%s,%s,'budget-work','batch-attempt')",
            (tenant, input_digest(inputs), Jsonb(inputs)),
        )
    authority = AuthorizedUser("worker", tenant, "worker", "scope", frozenset())

    @contextmanager
    def transaction():
        with psycopg.connect(make_conninfo(dsn, options="-c role=oam_content")) as connection:
            bind_user(connection.cursor(), authority)
            yield connection

    def guard(connection):
        assert connection.execute(
            "SELECT 1 FROM users WHERE tenant_id=%s AND user_id='scope' FOR UPDATE", (tenant,)
        ).fetchone() == (1,)

    store = SimpleNamespace(
        key=(tenant, "scope"),
        batch_id="batch",
        generation_id=rows[0][2],
        config=config,
        claim=SimpleNamespace(attempt_id="batch-attempt", operation_id="batch"),
        transaction=transaction,
        guard=guard,
    )

    def states(*values):
        with psycopg.connect(dsn) as connection:
            if values:
                for operation, value in zip(operations, values, strict=True):
                    connection.execute(
                        "UPDATE operations SET stage_state=stage_state-'index_generation_budget' "
                        "WHERE operation_id=%s",
                        (operation,),
                    )
                    if value is not None:
                        connection.execute(
                            "UPDATE operations SET stage_state=stage_state||%s "
                            "WHERE operation_id=%s",
                            (Jsonb({"index_generation_budget": value}), operation),
                        )
            return [
                connection.execute(
                    "SELECT stage_state->'index_generation_budget' FROM operations "
                    "WHERE operation_id=%s",
                    (operation,),
                ).fetchone()[0]
                for operation in operations
            ]

    return SimpleNamespace(
        store=store, inputs=inputs, members=members, states=states, dsn=dsn, operations=operations
    )


REQUEST = {"system": "s", "user": "u", "max_output_tokens": 1024, "timeout_seconds": 5}
# Public seed projections serialize to 22 and 54 bytes; historical context is not a seed.
# The configured OpenAI chat request serializes to 234 bytes, plus its 1024 output allowance.


async def test_shared_allowance_is_weighted_by_public_new_seed_bytes(batch_budget):
    fixture = batch_budget
    fixture.states(
        {"limit": 10000, "reserved_upper_tokens": 7}, {"limit": 10000, "reserved_upper_tokens": 9}
    )

    class InspectingModels(RecordingModels):
        def generate(self, role, **kwargs):
            # A separate connection sees the allocation commit before provider dispatch.
            with psycopg.connect(fixture.dsn) as connection:
                allocation = connection.execute(
                    "SELECT allocation FROM l2_batch_groups WHERE batch_id='batch'"
                ).fetchone()[0]
            assert len(allocation["calls"]) == 1
            assert allocation["calls"][0]["request_upper_tokens"] == 1258
            return super().generate(role, **kwargs)

    models = InspectingModels()
    wrapped = budget.BatchBudgetModels(fixture.store, models, fixture.inputs)
    assert wrapped.generate("insights", **REQUEST) == {"one_actual_result": True}
    state = fixture.states()
    assert [value["reserved_upper_tokens"] for value in state] == [371, 903]
    assert len(models.calls) == 1
    assert models.calls[0] == ("insights", REQUEST)
    with psycopg.connect(fixture.dsn) as connection:
        allocation = connection.execute(
            "SELECT allocation FROM l2_batch_groups WHERE batch_id='batch'"
        ).fetchone()[0]
    assert len(allocation["calls"]) == 1
    assert allocation["calls"][0]["request_upper_tokens"] == 1258
    assert sorted(
        share["attributed_upper_tokens"] for share in allocation["calls"][0]["source_shares"]
    ) == [364, 894]


async def test_one_exhausted_owner_rejects_without_any_balance_changes(batch_budget):
    fixture = batch_budget
    before = fixture.states(
        {"limit": 10000, "reserved_upper_tokens": 7}, {"limit": 10, "reserved_upper_tokens": 9}
    )
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetExceeded) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == [fixture.members[1]]
    assert fixture.states() == before
    assert models.calls == []


async def test_disabled_owner_keeps_its_share_without_transferring_it(batch_budget):
    fixture = batch_budget
    fixture.states(None, {"limit": 10000, "reserved_upper_tokens": 9})
    budget.BatchBudgetModels(fixture.store, RecordingModels(), fixture.inputs).generate(
        "insights", **REQUEST
    )
    assert fixture.states() == [None, {"limit": 10000, "reserved_upper_tokens": 903}]


async def test_disabled_budgets_still_record_attribution_without_adding_limits(batch_budget):
    fixture = batch_budget
    fixture.states(None, None)
    budget.BatchBudgetModels(fixture.store, RecordingModels(), fixture.inputs).generate(
        "insights", **REQUEST
    )
    assert fixture.states() == [None, None]
    with psycopg.connect(fixture.dsn) as connection:
        allocation = connection.execute(
            "SELECT allocation FROM l2_batch_groups WHERE batch_id='batch'"
        ).fetchone()[0]
    assert allocation["calls"][0]["request_upper_tokens"] == 1258
    assert all(not share["budget_enabled"] for share in allocation["calls"][0]["source_shares"])


async def test_missing_frozen_group_rolls_back_every_source_reservation(batch_budget):
    fixture = batch_budget
    before = fixture.states(
        {"limit": 10000, "reserved_upper_tokens": 7}, {"limit": 10000, "reserved_upper_tokens": 9}
    )
    fixture.store.batch_id = "missing-batch"
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetUnavailable) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == sorted(fixture.members)
    assert fixture.states() == before
    assert models.calls == []


async def test_unbounded_protocol_rejects_only_members_with_enforced_ceilings(batch_budget):
    fixture = batch_budget
    before = fixture.states({"limit": 10000, "reserved_upper_tokens": 7}, None)
    fixture.store.config = fixture.store.config.model_copy(
        update={
            "models": fixture.store.config.models.model_copy(
                update={
                    "vlm": fixture.store.config.models.vlm.model_copy(
                        update={"protocol": "openai_responses"}
                    )
                }
            )
        }
    )
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetUnavailable) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == [fixture.members[0]]
    assert fixture.states() == before
    assert models.calls == []


async def test_stored_limit_remains_pinned_when_configuration_is_disabled(batch_budget):
    fixture = batch_budget
    fixture.states({"limit": 1, "reserved_upper_tokens": 0}, None)
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetExceeded) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == [fixture.members[0]]
    assert models.calls == []


async def test_missing_owner_is_explicit_and_does_not_reserve_healthy_member(batch_budget):
    fixture = batch_budget
    before = fixture.states(
        {"limit": 10000, "reserved_upper_tokens": 7}, {"limit": 10000, "reserved_upper_tokens": 9}
    )
    fixture.inputs["private_facts"][1]["source_refs"] = ["missing:1"]
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetUnavailable) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == [("missing", 1)]
    assert fixture.states() == before
    assert models.calls == []


def add_batch_history(fixture, *, disposition="succeeded", output=True):
    """Keep real foreign keys and the same frozen-input attribution as runtime groups."""
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
            "schema_version,status,plan_sealed,stage_state) "
            "SELECT tenant_id,user_id,'history-batch',principal_id,'consolidate',"
            "schema_version,'partial_failed',true,%s FROM operations WHERE operation_id=%s",
            (Jsonb({"l2_batch_id": "history-batch"}), fixture.operations[0]),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) VALUES "
            "(%s,%s,'history-work','history-batch','consolidate_batch',%s,%s,'failed_terminal',1)",
            (*fixture.store.key, bytes(32), fixture.store.generation_id),
        )
        usage = {"total_tokens": 82, "coverage": "complete"}
        connection.execute(
            "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,ordinal,"
            "owner_id,fence,disposition,request_ref,usage) "
            "VALUES(%s,%s,'history-work','history-attempt',1,'worker',1,%s,%s,%s)",
            (
                *fixture.store.key,
                disposition,
                "insight:" + input_digest(fixture.inputs).hex(),
                Jsonb(usage) if disposition == "failed" else None,
            ),
        )
        for member, operation in zip(fixture.members, fixture.operations, strict=True):
            connection.execute(
                "INSERT INTO l2_batch_members(tenant_id,user_id,source_id,source_version,"
                "generation_id,batch_id,source_operation_id,admitted_at) "
                "VALUES(%s,%s,%s,%s,%s,'history-batch',%s,clock_timestamp())",
                (*fixture.store.key, *member, fixture.store.generation_id, operation),
            )
        connection.execute(
            "INSERT INTO l2_batch_groups(tenant_id,user_id,batch_id,stage_key,input_digest,"
            "inputs,output,usage,invalidated,work_unit_id,attempt_id) "
            "VALUES(%s,%s,'history-batch','insight',%s,%s,%s,%s,true,"
            "'history-work','history-attempt')",
            (
                *fixture.store.key,
                input_digest(fixture.inputs),
                Jsonb(fixture.inputs),
                Jsonb({"completed": True}) if output else None,
                Jsonb(usage) if output else Jsonb({}),
            ),
        )


@pytest.mark.parametrize("failed_call", [False, True])
async def test_new_budget_includes_paid_invalidated_groups_and_known_failures(
    batch_budget, failed_call
):
    fixture = batch_budget
    fixture.states(None, None)
    add_batch_history(
        fixture, disposition="failed" if failed_call else "succeeded", output=not failed_call
    )
    fixture.store.config = fixture.store.config.model_copy(
        update={
            "index": fixture.store.config.index.model_copy(
                update={"generation_token_budget": 10000}
            )
        }
    )
    budget.BatchBudgetModels(fixture.store, RecordingModels(), fixture.inputs).generate(
        "insights", **REQUEST
    )
    # Source extraction: 30 each; old shared call: 24/58; new allowance: 364/894.
    assert fixture.states() == [
        {"limit": 10000, "reserved_upper_tokens": 418},
        {"limit": 10000, "reserved_upper_tokens": 982},
    ]


async def test_new_budget_rejects_unknown_historical_batch_usage_atomically(batch_budget):
    fixture = batch_budget
    fixture.states(None, None)
    add_batch_history(fixture, disposition="unknown_external_outcome", output=False)
    fixture.store.config = fixture.store.config.model_copy(
        update={
            "index": fixture.store.config.index.model_copy(
                update={"generation_token_budget": 10000}
            )
        }
    )
    models = RecordingModels()
    with pytest.raises(budget.BatchGenerationBudgetUnavailable) as raised:
        budget.BatchBudgetModels(fixture.store, models, fixture.inputs).generate(
            "insights", **REQUEST
        )
    assert raised.value.members == sorted(fixture.members)
    assert fixture.states() == [None, None]
    assert models.calls == []


async def test_pinned_reservations_do_not_recount_historical_batch_usage(batch_budget):
    fixture = batch_budget
    fixture.states(
        {"limit": 10000, "reserved_upper_tokens": 7}, {"limit": 10000, "reserved_upper_tokens": 9}
    )
    add_batch_history(fixture)
    budget.BatchBudgetModels(fixture.store, RecordingModels(), fixture.inputs).generate(
        "insights", **REQUEST
    )
    assert [value["reserved_upper_tokens"] for value in fixture.states()] == [371, 903]


async def test_worker_rejects_exhausted_member_and_completes_healthy_source(batch_runtime):
    r = batch_runtime
    exhausted = await ready_source(r, "exhausted", ["Production Atlas rollback needs approval."])
    healthy = await ready_source(
        r,
        "healthy",
        [
            "Production Atlas owned by Lin rolls back at 5 percent failures.",
            "Production Atlas rollback requires manual approval.",
        ],
    )
    batch_id = exhausted["stage_state"]["consolidation_operation_id"]
    assert healthy["stage_state"]["consolidation_operation_id"] == batch_id
    assert r.config.index.generation_token_budget is None
    with psycopg.connect(r.dsn) as connection:
        for original, state in [
            (exhausted, {"limit": 1, "reserved_upper_tokens": 0}),
            (healthy, {"limit": 200000, "reserved_upper_tokens": 7}),
        ]:
            connection.execute(
                "UPDATE operations SET stage_state=stage_state||%s WHERE operation_id=%s",
                (Jsonb({"index_generation_budget": state}), original["operation_id"]),
            )
        healthy_seeds = {
            row[0]
            for row in connection.execute(
                "SELECT u.memory_id FROM search_units u JOIN operations o ON "
                "(o.tenant_id,o.user_id,o.source_id,o.source_version)="
                "(u.tenant_id,u.user_id,u.source_id,u.source_version) "
                "WHERE o.operation_id=%s AND u.kind='fact'",
                (healthy["operation_id"],),
            ).fetchall()
        }
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() WHERE operation_id=%s",
            (batch_id,),
        )
    assert process_one(r.config, r.queue, r.content, models=r.models, worker_id="mixed-budget")
    assert len(r.models.groups) == 1
    assert set(r.models.groups[0]["seed_ids"]) == healthy_seeds
    for original, ready in [(exhausted, False), (healthy, True)]:
        receipt = (
            await r.client.get(f"/v1/users/scope/operations/{original['operation_id']}")
        ).json()
        assert receipt["stage_state"]["insights_ready"] is ready
        assert receipt["stage_state"]["consolidation_status"] == (
            "succeeded" if ready else "failed"
        )
    receipt = (await r.client.get(f"/v1/users/scope/operations/{batch_id}")).json()
    assert receipt["status"] == "partial", receipt
    assert receipt["stage_state"]["l2_member_counts"] == {
        "pending": 0,
        "succeeded": 1,
        "excluded": 0,
        "failed": 1,
    }
    assert [item["source_id"] for item in receipt["stage_state"]["failed_sources"]] == [
        exhausted["source_id"]
    ]
    with psycopg.connect(r.dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id=%s", (batch_id,)
            ).fetchone()[0]
            == "partial_failed"
        )
        assert connection.execute(
            "SELECT stage_state->'index_generation_budget' FROM operations WHERE operation_id=%s",
            (exhausted["operation_id"],),
        ).fetchone()[0] == {"limit": 1, "reserved_upper_tokens": 0}
        assert (
            connection.execute(
                "SELECT count(*) FROM l2_batch_groups WHERE batch_id=%s "
                "AND stage_key='insight' AND output IS NOT NULL",
                (batch_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM l2_batch_groups WHERE batch_id=%s "
                "AND stage_key='insight' AND invalidated AND output IS NULL",
                (batch_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("outcome", ["completed", "failed", "unknown"])
async def test_source_repair_budget_includes_prior_shared_generation(batch_budget, outcome):
    fixture = batch_budget
    fixture.states(None, None)
    add_batch_history(
        fixture,
        disposition={
            "completed": "succeeded",
            "failed": "failed",
            "unknown": "unknown_external_outcome",
        }[outcome],
        output=outcome == "completed",
    )
    fixture.store.config = fixture.store.config.model_copy(
        update={
            "index": fixture.store.config.index.model_copy(update={"generation_token_budget": 1300})
        }
    )
    source_id, version = fixture.members[0]
    fixture.store.source = {"source_id": source_id, "source_version": version}
    models = RecordingModels()
    # Extraction used 30; known shared generation adds 24; this request needs 1258.
    # 1312 exceeds 1300. An unknown shared outcome cannot establish any safe balance.
    rejection = (
        budget.GenerationBudgetUnavailable
        if outcome == "unknown"
        else budget.GenerationBudgetExceeded
    )
    with pytest.raises(rejection):
        budget.SourceBudgetModels(fixture.store, models).generate("insights", **REQUEST)
    assert models.calls == []


@pytest.mark.parametrize("unrelated_outcome", ["unknown", "partial"])
async def test_source_budget_ignores_usage_from_other_members_only_group(
    batch_budget, unrelated_outcome
):
    fixture = batch_budget
    fixture.states(None, None)
    fixture.inputs["envelope"]["seed_ids"] = ["m1"]
    add_batch_history(
        fixture,
        disposition="unknown_external_outcome" if unrelated_outcome == "unknown" else "succeeded",
        output=unrelated_outcome == "partial",
    )
    if unrelated_outcome == "partial":
        with psycopg.connect(fixture.dsn) as connection:
            connection.execute(
                "UPDATE l2_batch_groups SET usage=%s WHERE batch_id='history-batch'",
                (Jsonb({"coverage": "partial", "total_tokens": None}),),
            )
    fixture.store.config = fixture.store.config.model_copy(
        update={
            "index": fixture.store.config.index.model_copy(update={"generation_token_budget": 1300})
        }
    )
    source_id, version = fixture.members[0]
    fixture.store.source = {"source_id": source_id, "source_version": version}
    models = RecordingModels()
    assert budget.SourceBudgetModels(fixture.store, models).generate("insights", **REQUEST) == {
        "one_actual_result": True
    }
    assert fixture.states()[0] == {"limit": 1300, "reserved_upper_tokens": 1288}
