"""Deferred link lifecycle on isolated PostgreSQL with deterministic models."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
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
from oam.indexing.store import IndexStore, StaleIndexWork, input_digest, stable_id
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
            (fixture.tenant, Jsonb({"quote": "x"})),
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
                    "insights_enabled": False,
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


def legacy_direct_completion(store, *, semantic_seed_ids, schedule_consolidation=False):
    """Persist the previous producer's manifest/work shape for upgrade coverage."""

    seeds = sorted(set(semantic_seed_ids))
    manifest = {
        "source_id": store.source["source_id"],
        "source_version": store.source["source_version"],
        "generation_id": store.generation_id,
        "seed_ids": seeds,
        "recipe": "oam-semantic-links-v1",
    }
    with store.transaction() as connection:
        store.guard(connection)
        if seeds:
            digest = input_digest(manifest)
            work_id = stable_id("work_semantic_links", store.claim.operation_id)
            existing = connection.execute(
                "SELECT input_digest FROM work_units WHERE tenant_id=%s AND user_id=%s "
                "AND work_unit_id=%s",
                (*store.key, work_id),
            ).fetchone()
            if existing and bytes(existing[0]) != digest:
                raise StaleIndexWork("legacy semantic work identity is incompatible")
            connection.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,dependency_ids,status,attempts_remaining) "
                "VALUES (%s,%s,%s,%s,'index_semantic_links',%s,%s,%s,'queued',%s) "
                "ON CONFLICT DO NOTHING",
                (
                    *store.key,
                    work_id,
                    store.claim.operation_id,
                    digest,
                    store.generation_id,
                    [store.claim.work_unit_id],
                    store.config.retry.max_attempts,
                ),
            )
        store.readiness(
            connection,
            memory_ready=True,
            temporal_links_ready=True,
            links_ready=not seeds,
            semantic_link_manifest=manifest,
            semantic_links_status="pending" if seeds else "succeeded",
        )
    store.finish(schedule_consolidation=schedule_consolidation)
    if seeds:
        with psycopg.connect(store.dsn) as connection:
            from oam.db.context import bind_user

            bind_user(connection.cursor(), store.authority)
            connection.execute(
                "UPDATE operations SET status='running' WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (*store.key, store.claim.operation_id),
            )


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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=[])
    with psycopg.connect(fixture.dsn) as connection:
        assert connection.execute("SELECT stage FROM work_units").fetchall() == [("index_source",)]
        status, state = connection.execute("SELECT status,stage_state FROM operations").fetchone()
        assert status == "succeeded" and state["links_ready"] is True


def test_dependency_blocks_claim_until_direct_work_succeeds(direct_store):
    fixture = direct_store
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute("UPDATE work_units SET status='running' WHERE stage='index_source'")
    assert claim_store(fixture.dsn, fixture.config) is None
    with psycopg.connect(fixture.dsn) as connection:
        connection.execute("UPDATE work_units SET status='succeeded' WHERE stage='index_source'")
    assert claim_store(fixture.dsn, fixture.config).claim.stage == "index_semantic_links"


def test_duplicate_direct_completion_keeps_one_compatible_manifest(direct_store):
    fixture = direct_store
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed", "b", "a", "seed"])
    with psycopg.connect(fixture.dsn) as connection:
        before = connection.execute(
            "SELECT work_unit_id,input_digest FROM work_units WHERE stage='index_semantic_links'"
        ).fetchone()
        connection.execute("UPDATE work_units SET status='running' WHERE stage='index_source'")
    legacy_direct_completion(fixture.store, semantic_seed_ids=["a", "b", "seed"])
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
        legacy_direct_completion(fixture.store, semantic_seed_ids=["c"])


def test_changed_manifest_cannot_publish(direct_store):
    fixture = direct_store
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"], schedule_consolidation=True)
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


@pytest.mark.parametrize(
    ("direct_store", "expected_delay_seconds"),
    [
        ({"insights_enabled": True}, 86400),
        ({"insights_enabled": True, "insight_consolidation_delay_hours": 2}, 7200),
    ],
    indirect=["direct_store"],
)
def test_semantic_completion_preserves_scheduled_consolidation(
    direct_store, expected_delay_seconds
):
    fixture = direct_store
    with psycopg.connect(fixture.dsn) as connection:
        before = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        legacy_direct_completion(
            fixture.store, semantic_seed_ids=["seed"], schedule_consolidation=True
        )
        after = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        child = connection.execute(
            "SELECT operation_id,available_at,status FROM work_units WHERE stage='consolidate'"
        ).fetchone()
        assert child[2] == "queued"
        delay = timedelta(seconds=expected_delay_seconds)
        assert before + delay <= child[1] <= after + delay
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=["seed"])
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
    legacy_direct_completion(fixture.store, semantic_seed_ids=seeds)
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


async def test_source_publishes_recall_before_queued_links_complete(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_api_only_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipt = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "inline-source"},
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
        )
        assert receipt.status_code == 202, receipt.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="inline-worker",
        )
        complete = (
            await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        ).json()
        assert complete["status"] == "processing"
        assert complete["readiness"]["memory_ready"]
        assert complete["readiness"]["temporal_links_ready"]
        assert complete["readiness"]["links_ready"] is False
        app.state.runtime.models = models
        early = await client.post(
            "/v1/recall",
            json={"user_id": "scope", "query": "Lin morning", "allow_partial": True},
        )
        assert early.status_code == 200, early.text
        assert "Lin prefers updates in the morning." in early.json()["context"]
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="link-worker",
        )
        complete = (
            await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        ).json()
        assert complete["status"] == "succeeded"
        assert complete["readiness"]["links_ready"]
        assert complete["stage_state"]["consolidation_operation_id"]
        with psycopg.connect(dsn) as connection:
            assert set(
                connection.execute(
                    "SELECT stage,status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                    (tenant, receipt.json()["operation_id"]),
                ).fetchall()
            ) == {
                ("index_source", "succeeded"),
                ("index_semantic_links", "succeeded"),
            }
            assert (
                connection.execute(
                    "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'",
                    (tenant,),
                ).fetchone()[0]
                == 2
            )
        app.state.runtime.models = models
        for mode in ("fast", "normal"):
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "Lin morning explanations",
                    "recall_level": mode,
                },
            )
            assert recalled.status_code == 200, recalled.text
            assert "Lin prefers short explanations." in recalled.json()["context"]
            assert "Lin prefers updates in the morning." in recalled.json()["context"]
            assert recalled.json()["diagnostics"]["readiness"] == "ready"


def test_queued_empty_seed_manifest_settles_without_ann(direct_store, monkeypatch):
    fixture = direct_store
    manifest = {
        "source_id": fixture.store.source["source_id"],
        "source_version": fixture.store.source["source_version"],
        "generation_id": fixture.store.generation_id,
        "seed_ids": [],
        "recipe": "oam-semantic-links-v1",
    }
    with fixture.store.transaction() as connection:
        fixture.store.guard(connection)
        fixture.store.readiness(
            connection,
            memory_ready=True,
            temporal_links_ready=True,
            links_ready=False,
            semantic_link_manifest=manifest,
            semantic_links_status="pending",
        )
        fixture.store.finish(enqueue_semantic=True, connection=connection)
    monkeypatch.setattr(
        "oam.workers.semantic_links.propose_semantic_links",
        lambda *a: pytest.fail("valid no-facts source started ANN"),
    )
    assert run_semantic(fixture)
    with psycopg.connect(fixture.dsn) as connection:
        status, state = connection.execute(
            "SELECT status,stage_state FROM operations WHERE operation_id='source-op'"
        ).fetchone()
        assert status == "succeeded"
        assert state["links_ready"] is True
        assert state["link_coverage"]["semantic_links"] == 0


async def test_closed_import_holds_links_until_slow_source_is_ready(
    core_database, monkeypatch, capsys
):
    from oam.indexing.links import propose_semantic_links

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    held = config.model_copy(
        update={"worker": config.worker.model_copy(update={"claim_semantic_links": False})}
    )
    app = create_api_only_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    started, release = Event(), Event()
    original_generate = models.generate

    def slow_generate(role, **kwargs):
        if role == "extraction" and "morning" in kwargs["user"]:
            started.set()
            assert release.wait(5), "slow source did not drain"
        return original_generate(role, **kwargs)

    monkeypatch.setattr(models, "generate", slow_generate)

    def step(configuration):
        return process_one(
            configuration,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="closed-import-worker",
        )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipts = []
        for index, text in enumerate(
            ("Lin prefers short explanations.", "Lin prefers updates in the morning.")
        ):
            receipt = await client.post(
                "/v1/retain",
                headers={"Idempotency-Key": f"closed-{index}"},
                json={
                    "user_id": "scope",
                    "input_type": "interaction",
                    "messages": [
                        {
                            "role": "user",
                            "content": text,
                            "event_time": "2026-09-01T09:00:00Z",
                            "learning_eligible": True,
                        }
                    ],
                },
            )
            assert receipt.status_code == 202, receipt.text
            receipts.append(receipt.json()["operation_id"])
        assert step(held)
        with ThreadPoolExecutor(max_workers=1) as executor:
            slow = executor.submit(step, held)
            try:
                assert started.wait(2), "second source never started extraction"
                assert step(held) is False
                with psycopg.connect(dsn) as connection:
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM unit_links WHERE kind='semantic'"
                        ).fetchone()[0]
                        == 0
                    )
                    assert connection.execute(
                        "SELECT status,attempts_remaining FROM work_units "
                        "WHERE stage='index_semantic_links'"
                    ).fetchall() == [("queued", config.retry.max_attempts)]
            finally:
                release.set()
                assert slow.result(timeout=3)
        for operation in receipts:
            value = (await client.get(f"/v1/users/scope/operations/{operation}")).json()
            assert value["status"] == "processing"
            assert value["readiness"]["memory_ready"]
            assert value["readiness"]["temporal_links_ready"]
            assert value["readiness"]["links_ready"] is False

        attempts = 0

        def retry_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic retryable link failure")
            return propose_semantic_links(*args, **kwargs)

        monkeypatch.setattr("oam.workers.semantic_links.propose_semantic_links", retry_once)
        with pytest.raises(OSError, match="synthetic retryable"):
            step(config)
        assert step(config)
        assert step(config)
        assert models.generation_calls == ["extraction", "extraction"]
        for operation in receipts:
            value = (await client.get(f"/v1/users/scope/operations/{operation}")).json()
            assert value["status"] == "succeeded"
            assert value["readiness"]["links_ready"]
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM unit_links l JOIN search_units a "
                    "ON a.unit_id=l.source_id JOIN search_units b ON b.unit_id=l.target_id "
                    "WHERE l.kind='semantic' AND a.source_id<>b.source_id"
                ).fetchone()[0]
                == 2
            )
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        failures = [event for event in events if event["stage"] == "index_claim_failed"]
        assert len(failures) == 1
        assert failures[0]["operation_id"] in receipts
        assert failures[0]["claim_stage"] == "index_semantic_links"
        assert failures[0]["error_type"] == "OSError"
