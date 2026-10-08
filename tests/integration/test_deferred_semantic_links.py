"""Deferred link lifecycle on isolated PostgreSQL with deterministic models."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import perf_counter
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app
from psycopg.conninfo import make_conninfo
from test_core_http import SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database
from test_index_links import add_fact

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore, StaleIndexWork
from oam.workers.claims import claim_one
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


def test_support_validation_uses_point_lookups_before_background_statistics(direct_store):
    from oam.db.context import bind_user
    from oam.indexing.links import _seeds

    fixture = direct_store
    with psycopg.connect(fixture.dsn) as connection:
        for index in range(128):
            add_fact(
                connection,
                fixture.store.authority,
                f"history-{index:03}",
                generation=fixture.store.generation_id,
            )
        for table in ("search_units", "memories", "support"):
            connection.execute(f"ANALYZE {table}")
    with psycopg.connect(fixture.store.dsn) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL jit=off")
        bind_user(connection.cursor(), fixture.store.authority)
        parameters = {
            "tenant": fixture.tenant,
            "user": "scope",
            "generation": fixture.store.generation_id,
            "ids": ["seed"],
        }
        sql = _seeds() + "SELECT unit_id FROM seeds"
        assert connection.execute(sql, parameters).fetchall() == [("seed",)]
        plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, parameters).fetchone()[
            0
        ][0]["Plan"]
    lookups = []

    def collect(node):
        if node.get("Relation Name") in {"sources", "source_versions"} and node.get(
            "Actual Loops", 0
        ):
            lookups.append(node)
        for child in node.get("Plans", []):
            collect(child)

    collect(plan)
    assert lookups
    # A support must look up its exact parent, rather than repeatedly joining all sources.
    assert all(
        node["Actual Rows"] <= 1 and "source_id" in node.get("Index Cond", "") for node in lookups
    )


def test_semantic_proposals_process_a_common_source_in_one_bounded_batch(direct_store):
    from oam.db.context import bind_user
    from oam.indexing.links import propose_semantic_links

    fixture = direct_store
    seeds = ["seed"] + [f"common-{index:02}" for index in range(63)]
    with psycopg.connect(fixture.dsn) as connection:
        for seed in seeds[1:]:
            add_fact(
                connection, fixture.store.authority, seed, generation=fixture.store.generation_id
            )
    with psycopg.connect(fixture.store.dsn) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        bind_user(connection.cursor(), fixture.store.authority)
        proposals = propose_semantic_links(
            connection,
            fixture.store.authority,
            fixture.config,
            fixture.store.generation_id,
            seeds,
        )
    assert len(proposals) == 128
    assert {seed for seed, _, _ in proposals} == set(seeds)


@pytest.mark.parametrize(
    "invalid", ["superseded", "ineligible", "generation", "hidden", "identity", "unsupported"]
)
def test_every_support_must_remain_eligible(direct_store, invalid):
    from psycopg.types.json import Jsonb

    from oam.db.context import bind_user
    from oam.indexing.links import _seeds

    fixture = direct_store
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute(
            "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
            "support_kind,span) VALUES (%s,'scope','seed','a','a','quote',%s)",
            (fixture.tenant, Jsonb({"start": 0, "end": 1})),
        )

    def eligible():
        with psycopg.connect(fixture.store.dsn) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            bind_user(connection.cursor(), fixture.store.authority)
            return connection.execute(
                _seeds() + "SELECT unit_id FROM seeds",
                {
                    "tenant": fixture.tenant,
                    "user": "scope",
                    "generation": fixture.store.generation_id,
                    "ids": ["seed"],
                },
            ).fetchall()

    assert eligible() == [("seed",)]
    mutations = {
        "superseded": "UPDATE source_versions SET status='superseded' WHERE source_id='a'",
        "ineligible": "UPDATE evidence SET learning_eligible=false WHERE evidence_id='a'",
        "generation": "UPDATE passages SET generation_id='old' WHERE passage_id='a'",
        "hidden": "UPDATE passages SET visible_to=2 WHERE passage_id='a'",
        "identity": "UPDATE passages SET evidence_id='b' WHERE passage_id='a'",
    }
    with psycopg.connect(fixture.dsn) as connection:
        if invalid == "unsupported":
            connection.execute(
                "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,"
                "assertion_kind,generation_id,visible_from) "
                "VALUES (%s,'scope','orphan','Unsupported claim','stated',%s,1)",
                (fixture.tenant, fixture.store.generation_id),
            )
            connection.execute("UPDATE search_units SET memory_id='orphan' WHERE unit_id='seed'")
        else:
            connection.execute(mutations[invalid])
    assert eligible() == []


def claim_store(dsn, config, worker="semantic-worker"):
    with psycopg.connect(make_conninfo(dsn, options="-c role=oam_queue")) as connection:
        claim = claim_one(
            connection.cursor(),
            worker_id=worker,
            attempt_id="attempt-" + os.urandom(8).hex(),
            lease_seconds=config.lease.seconds,
            user_concurrency=10,
        )
    return (
        None
        if claim is None
        else IndexStore(
            make_conninfo(dsn, options="-c role=oam_content"),
            config,
            claim,
            worker,
            index_generation(config, os.environ),
        )
    )


@pytest.fixture
def direct_store(core_database, request):
    dsn, tenant, _ = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "index": config.index.model_copy(
                update={
                    "semantic_link_max_neighbors": 2,
                    **getattr(request, "param", {}),
                }
            ),
            "retry": config.retry.model_copy(update={"max_attempts": 2}),
        }
    )
    generation = index_generation(config, os.environ)
    authority = AuthorizedUser("worker", tenant, "admin", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,'scope','synthetic','test','persistent',%s)",
            (tenant, generation),
        )
        for identity in ("seed", "a", "b", "c"):
            add_fact(connection, authority, identity, day=0, generation=generation)
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
            "source_version,accepted_seq,kind,schema_version,status,plan_sealed) "
            "VALUES (%s,'scope','source-op','admin','seed',1,1,"
            "'index_interaction',1,'accepted',true)",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) "
            "VALUES (%s,'scope','direct-work','source-op','index_source',%s,%s,'queued',2)",
            (tenant, b"x" * 32, generation),
        )
    store = claim_store(dsn, config, "direct-worker")
    return SimpleNamespace(dsn=dsn, config=config, store=store, tenant=tenant)


def run_semantic(fixture):
    return process_one(
        fixture.config,
        make_conninfo(fixture.dsn, options="-c role=oam_queue"),
        make_conninfo(fixture.dsn, options="-c role=oam_content"),
        models=None,
        worker_id="semantic-worker",
    )


def test_exhausted_expired_semantic_claim_settles_partial_readiness(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    semantic = claim_store(fixture.dsn, fixture.config)
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second',"
            "attempts_remaining=0 WHERE work_unit_id=%s",
            (semantic.claim.work_unit_id,),
        )
    assert claim_store(fixture.dsn, fixture.config) is None
    with psycopg.connect(fixture.dsn) as connection:
        status, state = connection.execute(
            "SELECT status,stage_state FROM operations WHERE operation_id='source-op'"
        ).fetchone()
        assert status == "partial_failed"
        assert state["semantic_links_status"] == "failed"
        assert state["memory_ready"] is True and state["links_ready"] is False


def test_semantic_retry_reuses_work_and_terminal_failure_preserves_facts(direct_store, monkeypatch):
    from oam.workers import semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    with psycopg.connect(fixture.dsn) as connection:
        work_id = connection.execute(
            "SELECT work_unit_id FROM work_units WHERE stage='index_semantic_links'"
        ).fetchone()[0]

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("synthetic statement timeout")

    monkeypatch.setattr(semantic_links, "propose_semantic_links", timeout)
    for expected in ("queued", "failed_terminal"):
        with pytest.raises(psycopg.errors.QueryCanceled):
            run_semantic(fixture)
        with psycopg.connect(fixture.dsn) as connection:
            assert connection.execute(
                "SELECT work_unit_id,status FROM work_units WHERE stage='index_semantic_links'"
            ).fetchall() == [(work_id, expected)]
    with psycopg.connect(fixture.dsn) as connection:
        status, state = connection.execute(
            "SELECT status,stage_state FROM operations WHERE operation_id='source-op'"
        ).fetchone()
        assert status == "partial_failed"
        assert state["semantic_links_status"] == "failed"
        assert state["memory_ready"] and state["temporal_links_ready"] and not state["links_ready"]
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 4


def test_proposal_read_allows_same_user_publication_without_epoch_change(direct_store, monkeypatch):
    from oam.workers import semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    original = semantic_links.propose_semantic_links
    reading = Event()
    release = Event()
    observed = {}

    def pause(connection, *args):
        result = original(connection, *args)
        observed["readonly"] = connection.execute("SHOW transaction_read_only").fetchone()[0]
        observed["epoch"] = connection.execute("SELECT projection_epoch FROM users").fetchone()[0]
        reading.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(semantic_links, "propose_semantic_links", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_semantic, fixture)
        try:
            assert reading.wait(5)
            with psycopg.connect(fixture.dsn) as connection:
                # NOWAIT detects a held user-row lock instead of hiding it behind a timeout.
                epoch = connection.execute(
                    "SELECT projection_epoch FROM users FOR UPDATE NOWAIT"
                ).fetchone()[0]
                assert epoch == observed["epoch"]
                assert observed["readonly"] == "on"
                connection.execute("UPDATE users SET published_seq=published_seq+1")
        finally:
            release.set()
        assert future.result(timeout=5)


@pytest.mark.parametrize(
    "change", ["lease", "fence", "cancel", "generation", "withdraw", "supersede", "endpoint"]
)
def test_publication_rejects_changes_after_proposal_read(direct_store, monkeypatch, change):
    from oam.workers import semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    original = semantic_links.propose_semantic_links
    mutations = {
        "lease": (
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE stage='index_semantic_links'"
        ),
        "fence": "UPDATE work_units SET fence=fence+1 WHERE stage='index_semantic_links'",
        "cancel": (
            "UPDATE operations SET cancellation_revision=1,status='cancelled' "
            "WHERE operation_id='source-op'"
        ),
        "generation": "UPDATE users SET index_generation_id='changed'",
        "withdraw": "UPDATE sources SET status='deleted' WHERE source_id='seed'",
        "supersede": "UPDATE source_versions SET status='superseded' WHERE source_id='seed'",
        "endpoint": "UPDATE memories SET visible_to=2 WHERE memory_id='a'",
    }

    def mutate(connection, *args):
        proposals = original(connection, *args)
        assert proposals
        with psycopg.connect(fixture.dsn) as administrator:
            administrator.execute(mutations[change])
        return proposals

    monkeypatch.setattr(semantic_links, "propose_semantic_links", mutate)
    with pytest.raises(StaleIndexWork):
        run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT count(*) FROM unit_links").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT stage_state->>'links_ready' FROM operations WHERE operation_id='source-op'"
            ).fetchone()[0]
            == "false"
        )


def test_publication_deduplicates_and_rechecks_remaining_cap(direct_store):
    from oam.indexing.links import publish_semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    semantic = claim_store(fixture.dsn, fixture.config)
    with semantic.transaction() as connection:
        connection.execute("SET LOCAL jit = on")
        semantic.guard(connection)
        assert connection.execute("SHOW jit").fetchone()[0] == "off"
        connection.execute("SET LOCAL jit = on")
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,'scope','seed','c','semantic',1)",
            (fixture.tenant,),
        )
        proposals = [("seed", "c", 1.0), ("seed", "b", 0.8), ("seed", "a", 0.9), ("seed", "a", 0.8)]
        assert (
            publish_semantic_links(
                connection,
                semantic.authority,
                fixture.config,
                semantic.generation_id,
                ["seed"],
                proposals,
            )
            == 1
        )
        assert connection.execute("SHOW jit").fetchone()[0] == "off"
        assert (
            publish_semantic_links(
                connection,
                semantic.authority,
                fixture.config,
                semantic.generation_id,
                ["seed"],
                proposals,
            )
            == 0
        )
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute(
            "SELECT target_id FROM unit_links ORDER BY target_id"
        ).fetchall() == [("a",), ("c",)]


def test_zero_seed_source_completes_without_semantic_work(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=[])
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT stage FROM work_units").fetchall() == [("index_source",)]
        status, state = connection.execute("SELECT status,stage_state FROM operations").fetchone()
        assert status == "succeeded" and state["links_ready"] is True


def test_dependency_blocks_claim_until_direct_work_succeeds(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute("UPDATE work_units SET status='running' WHERE stage='index_source'")
    assert claim_store(fixture.dsn, fixture.config) is None
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute("UPDATE work_units SET status='succeeded' WHERE stage='index_source'")
    assert claim_store(fixture.dsn, fixture.config).claim.stage == "index_semantic_links"


def test_duplicate_direct_completion_keeps_one_compatible_manifest(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed", "b", "a", "seed"])
    with psycopg.connect(fixture.dsn) as connection:
        before = connection.execute(
            "SELECT work_unit_id,input_digest FROM work_units WHERE stage='index_semantic_links'"
        ).fetchone()
        connection.execute("UPDATE work_units SET status='running' WHERE stage='index_source'")
    fixture.store.finish(semantic_seed_ids=["a", "b", "seed"])
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute(
            "SELECT work_unit_id,input_digest FROM work_units WHERE stage='index_semantic_links'"
        ).fetchall() == [before]
        manifest = connection.execute(
            "SELECT stage_state->'semantic_link_manifest' FROM operations"
        ).fetchone()[0]
        assert manifest["seed_ids"] == ["a", "b", "seed"]
        connection.execute("UPDATE work_units SET status='running' WHERE stage='index_source'")
    with pytest.raises(StaleIndexWork, match="incompatible"):
        fixture.store.finish(semantic_seed_ids=["c"])


def test_changed_manifest_cannot_publish(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute(
            "UPDATE operations SET stage_state=jsonb_set(stage_state,"
            "'{semantic_link_manifest,seed_ids}','[\"a\"]')"
        )
    with pytest.raises(StaleIndexWork, match="manifest"):
        run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT count(*) FROM unit_links").fetchone()[0] == 0


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
@pytest.mark.parametrize("direct_store", [{"insights_enabled": True}], indirect=True)
def test_semantic_settlement_preserves_direct_usage_with_completed_consolidation(
    direct_store, monkeypatch, outcome
):
    from oam.workers import semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"], schedule_consolidation=True)
    with psycopg.connect(fixture.dsn) as connection:
        before = connection.execute(
            "SELECT stage_state->'usage',stage_state->'embedding_usage' FROM operations "
            "WHERE operation_id='source-op'"
        ).fetchone()
        connection.execute(
            "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,ordinal,"
            "owner_id,fence,disposition) SELECT tenant_id,user_id,work_unit_id,"
            "'child-attempt',1,'child-worker',1,'succeeded' FROM work_units "
            "WHERE stage='consolidate'"
        )
        connection.execute(
            "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
            "generation_id,stage_key,input_digest,output,usage,work_unit_id,attempt_id) "
            "SELECT o.tenant_id,o.user_id,o.source_id,o.source_version,w.generation_id,"
            "'insight',w.input_digest,'{}',"
            '\'{"input_tokens":6,"output_tokens":4,"total_tokens":10,'
            '"reasoning_tokens":0,"coverage":"complete"}\',w.work_unit_id,\'child-attempt\' '
            "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
            "WHERE w.stage='consolidate'"
        )
        connection.execute("UPDATE work_units SET status='succeeded' WHERE stage='consolidate'")
        connection.execute("UPDATE operations SET status='succeeded' WHERE kind='consolidate'")
        connection.execute(
            "UPDATE work_units SET attempts_remaining=1 WHERE stage='index_semantic_links'"
        )
    if outcome == "failed":

        def fail(*args):
            raise psycopg.errors.QueryCanceled("synthetic timeout")

        monkeypatch.setattr(semantic_links, "propose_semantic_links", fail)
        with pytest.raises(psycopg.errors.QueryCanceled):
            run_semantic(fixture)
    else:
        assert run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        after = connection.execute(
            "SELECT stage_state->'usage',stage_state->'embedding_usage' FROM operations "
            "WHERE operation_id='source-op'"
        ).fetchone()
        assert after == before


@pytest.mark.parametrize("direct_store", [{"insights_enabled": True}], indirect=True)
def test_semantic_completion_preserves_scheduled_consolidation(direct_store):
    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"], schedule_consolidation=True)
    with psycopg.connect(fixture.dsn) as connection:
        child = connection.execute(
            "SELECT operation_id,available_at,status FROM work_units WHERE stage='consolidate'"
        ).fetchone()
        assert child[2] == "queued"
        assert connection.execute(
            "SELECT available_at>clock_timestamp()+interval '7 hours' FROM work_units "
            "WHERE stage='consolidate'"
        ).fetchone()[0]
    assert run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute(
            "SELECT operation_id,available_at,status FROM work_units WHERE stage='consolidate'"
        ).fetchall() == [child]
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE operation_id='source-op'"
        ).fetchone()[0]
        assert state["links_ready"] is True and state["insights_ready"] is False
        assert state["consolidation_status"] == "pending"


def test_expiry_during_publication_rolls_back_the_batch(direct_store, monkeypatch):
    import time

    from oam.workers import semantic_links

    fixture = direct_store
    fixture.store.finish(semantic_seed_ids=["seed"])
    original = semantic_links.publish_semantic_links

    def expire(connection, *args):
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()+interval '20 milliseconds' "
            "WHERE stage='index_semantic_links'"
        )
        result = original(connection, *args)
        time.sleep(0.04)
        return result

    monkeypatch.setattr(semantic_links, "publish_semantic_links", expire)
    with pytest.raises(StaleIndexWork):
        run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT count(*) FROM unit_links").fetchone()[0] == 0


def test_retry_after_one_committed_batch_recomputes_without_duplicate_edges(
    direct_store, monkeypatch
):
    from oam.indexing.links import SEMANTIC_SEED_BATCH_SIZE
    from oam.workers import semantic_links

    fixture = direct_store
    seeds = ["seed"] + [f"seed-{index:04}" for index in range(1001)]
    with psycopg.connect(fixture.dsn) as connection:
        for seed in seeds[1:]:
            add_fact(
                connection, fixture.store.authority, seed, generation=fixture.store.generation_id
            )
        # Populate planner statistics for the persistent ANN population. Parent
        # source tables deliberately retain their fresh-database statistics.
        for table in ("search_units", "memories", "support"):
            connection.execute(f"ANALYZE {table}")
    fixture.store.finish(semantic_seed_ids=seeds)
    original = semantic_links.propose_semantic_links
    batches = []

    def interrupt(connection, authority, config, generation, batch):
        batches.append(len(batch))
        if len(batches) == 2:
            raise psycopg.errors.QueryCanceled("synthetic second-batch timeout")
        return original(connection, authority, config, generation, batch)

    monkeypatch.setattr(semantic_links, "propose_semantic_links", interrupt)
    with pytest.raises(psycopg.errors.QueryCanceled):
        run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT count(*) FROM unit_links").fetchone()[0] == 2000
        assert (
            connection.execute("SELECT stage_state->>'links_ready' FROM operations").fetchone()[0]
            == "false"
        )
    assert run_semantic(fixture)
    assert batches == [SEMANTIC_SEED_BATCH_SIZE, 2, SEMANTIC_SEED_BATCH_SIZE, 2]
    with psycopg.connect(fixture.dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM unit_links GROUP BY source_id ORDER BY source_id"
            ).fetchall()
            == [(2,)] * 1002
        )
        state = connection.execute("SELECT stage_state FROM operations").fetchone()[0]
        assert state["links_ready"] is True
        assert state["link_coverage"]["semantic_links"] == 2004


async def test_direct_ready_operation_defers_semantic_links_under_same_operation(
    core_database, tmp_path
):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_api_only_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()

    def process():
        return process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="deferred-worker",
        )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipt = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "event_time": f"2026-09-01T09:00:0{index}Z",
                        "learning_eligible": True,
                    }
                    for index, text in enumerate(
                        ("Lin prefers short explanations.", "Lin prefers updates in the morning.")
                    )
                ],
            },
            headers={"Idempotency-Key": "deferred-source"},
        )
        assert receipt.status_code == 202, receipt.text
        route = f"/v1/users/scope/operations/{receipt.json()['operation_id']}"
        started = perf_counter()
        assert process()
        direct_ms = (perf_counter() - started) * 1000
        direct = (await client.get(route)).json()
        assert direct["status"] == "processing"
        assert direct["readiness"]["memory_ready"] is True
        assert direct["readiness"]["temporal_links_ready"] is True
        assert direct["readiness"]["links_ready"] is False
        assert direct["stage_state"]["semantic_links_status"] == "pending"
        with psycopg.connect(dsn) as connection:
            work = connection.execute(
                "SELECT work_unit_id,stage,status,dependency_ids FROM work_units "
                "WHERE tenant_id=%s AND operation_id=%s ORDER BY stage",
                (tenant, receipt.json()["operation_id"]),
            ).fetchall()
            assert len(work) == 2
            assert work[0][1:3] == ("index_semantic_links", "queued")
            assert work[1][1:3] == ("index_source", "succeeded")
            assert work[0][3] == [work[1][0]]
            assert (
                connection.execute(
                    "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'",
                    (tenant,),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM temporal_links WHERE tenant_id=%s", (tenant,)
                ).fetchone()[0]
                > 0
            )
        app.state.runtime.models = models
        recalls = {}
        for mode in ("fast", "mid"):
            started = perf_counter()
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "Lin morning explanations",
                    "recall_level": mode,
                    "allow_partial": True,
                },
            )
            assert recalled.status_code == 200, recalled.text
            assert "Lin prefers short explanations." in recalled.json()["context"]
            assert "Lin prefers updates in the morning." in recalled.json()["context"]
            assert recalled.json()["diagnostics"]["readiness"] == "pending"
            recalls[mode] = {
                "elapsed_ms": (perf_counter() - started) * 1000,
                "response": recalled.json(),
            }
        generation_calls = list(models.generation_calls)
        started = perf_counter()
        assert process()
        semantic_ms = (perf_counter() - started) * 1000
        complete = (await client.get(route)).json()
        assert complete["status"] == "succeeded"
        assert complete["readiness"]["links_ready"] is True
        assert complete["stage_state"]["semantic_links_status"] == "succeeded"
        assert complete["usage"] == direct["usage"]
        assert models.generation_calls == generation_calls
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'",
                    (tenant,),
                ).fetchone()[0]
                == 2
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                    (tenant, receipt.json()["operation_id"]),
                ).fetchone()[0]
                == 2
            )
        (tmp_path / "public-http.json").write_text(
            json.dumps(
                {
                    "boundary": "real PostgreSQL and public HTTP; deterministic model responses",
                    "direct_ms": direct_ms,
                    "semantic_ms": semantic_ms,
                    "direct": direct,
                    "complete": complete,
                    "recalls": recalls,
                },
                indent=2,
            )
        )
