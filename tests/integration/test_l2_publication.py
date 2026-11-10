"""Incremental worker publication preserves identity and accumulated support."""

import json

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from test_core_http import core_database as core_database
from test_core_http import use_created_user_key
from test_incremental_l2 import ROOT, PolicyModels, retain_and_consolidate

from oam.config import load_config
from oam.models.runtime import ModelResult, Usage

pytestmark = pytest.mark.integration


class CorroboratingModels(PolicyModels):
    def generate(self, role, **kwargs):
        if role != "insights":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        self.groups.append(envelope)
        targets = envelope.get("targets", [])
        seeds = envelope.get("seed_ids", [fact["id"] for fact in envelope["facts"]])
        text = "Production Atlas requires manual approval before rollback."
        if "seed_ids" not in envelope:
            body = {
                "outcome": "insight",
                "title": "Production Atlas rollback",
                "text": text,
                "subtype": "projects",
                "retrieval_text": "Production Atlas rollback",
                "support_ids": [fact["id"] for fact in envelope["facts"]],
            }
        else:
            target = targets[0] if targets else None
            body = {
                "actions": [
                    {
                        "action": "KEEP_TEXT" if target else "CREATE",
                        "target_id": target["id"] if target else None,
                        "text": None if target else text,
                        "title": None if target else "Production Atlas rollback",
                        "retrieval_text": None if target else "Production Atlas rollback",
                        "subtype": None if target else "projects",
                        "support_ids": seeds,
                        "seed_ids": seeds,
                    }
                ]
            }
        return ModelResult(content=json.dumps(body), usage=Usage())


async def test_corroborating_increment_reuses_embedding_and_retains_old_support(
    core_database, capsys
):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = CorroboratingModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "first",
            [
                "Production Atlas rollback requires manual approval.",
                "Production Atlas release owner confirms manual rollback approval.",
            ],
        )
        with psycopg.connect(dsn) as connection:
            original_id, original_vector = connection.execute(
                "SELECT insight_id,embedding::text FROM search_units WHERE tenant_id=%s "
                "AND kind='insight'",
                (tenant,),
            ).fetchone()
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "corroboration",
            ["Production Atlas operations team also confirms manual approval before rollback."],
        )
        with psycopg.connect(dsn) as connection:
            current = connection.execute(
                "SELECT insight_id,revision FROM insights WHERE tenant_id=%s "
                "AND visible_to IS NULL",
                (tenant,),
            ).fetchall()
            assert current == [(original_id, 2)]
            assert (
                connection.execute(
                    "SELECT count(*) FROM insight_support WHERE tenant_id=%s AND insight_id=%s "
                    "AND insight_revision=2",
                    (tenant, original_id),
                ).fetchone()[0]
                == 3
            )
            assert (
                connection.execute(
                    "SELECT embedding::text FROM search_units WHERE tenant_id=%s "
                    "AND insight_id=%s AND insight_revision=2",
                    (tenant, original_id),
                ).fetchone()[0]
                == original_vector
            )
            consolidation_embeddings = connection.execute(
                "SELECT count(*) FROM index_stage_results WHERE tenant_id=%s "
                "AND stage_key='insight_embed'",
                (tenant,),
            ).fetchone()[0]
            assert consolidation_embeddings == 1

            # Re-examine the same retained seeds to exercise KEEP_TEXT's no-write outcome.
            operation_id = connection.execute(
                "SELECT operation_id FROM operations WHERE tenant_id=%s AND kind='consolidate' "
                "ORDER BY accepted_seq DESC LIMIT 1",
                (tenant,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE operations SET status='accepted',stage_state=stage_state||"
                '\'{"l2_examined_ids":[],"insights_ready":false}\'::jsonb '
                "WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation_id),
            )
            connection.execute(
                "UPDATE work_units SET status='queued',available_at=clock_timestamp(),"
                "owner_id=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation_id),
            )
        from psycopg.conninfo import make_conninfo

        from oam.workers.index_source import process_one

        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="retained-support",
        )
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT revision FROM insights WHERE tenant_id=%s ORDER BY revision",
                (tenant,),
            ).fetchall() == [(1,), (2,)]
            assert (
                connection.execute(
                    "SELECT count(*) FROM index_stage_results WHERE tenant_id=%s "
                    "AND stage_key='insight_embed'",
                    (tenant,),
                ).fetchone()[0]
                == 1
            )
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        final = [event for event in events if event["stage"] == "insight_published"][-1]
        assert final["model_actions"] == ["KEEP_TEXT"]
        assert final["actions"] == ["NO_CHANGE"]
        assert final["published_count"] == 0


@pytest.mark.parametrize("race", ["revision", "support", "direct_removal"])
async def test_stale_publication_keeps_previous_revision_and_seeds_retryable(
    core_database, monkeypatch, race
):
    from oam.indexing import consolidation
    from oam.indexing.store import StaleIndexWork

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = CorroboratingModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        first = await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "first",
            [
                "Production Atlas rollback requires manual approval.",
                "Production Atlas release owner confirms manual rollback approval.",
            ],
        )
        with psycopg.connect(dsn) as connection:
            original_id = connection.execute(
                "SELECT insight_id FROM insights WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
        publish = consolidation._publish_group
        affected_operation = []

        def race_before_publish(store, connection, **kwargs):
            affected_operation.append(store.claim.operation_id)
            with psycopg.connect(dsn) as concurrent:
                if race == "revision":
                    concurrent.execute(
                        "UPDATE insights SET visible_to=999 WHERE tenant_id=%s AND insight_id=%s",
                        (tenant, original_id),
                    )
                    concurrent.execute(
                        "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,"
                        "subtype,input_digest,recipe_id,generation_id,visible_from) SELECT "
                        "tenant_id,user_id,insight_id,2,synthesis_text,subtype,input_digest,"
                        "recipe_id,generation_id,999 FROM insights WHERE tenant_id=%s "
                        "AND insight_id=%s AND revision=1",
                        (tenant, original_id),
                    )
                    concurrent.execute(
                        "INSERT INTO insight_support SELECT tenant_id,user_id,insight_id,2,"
                        "memory_id,support_revision FROM insight_support WHERE tenant_id=%s "
                        "AND insight_id=%s AND insight_revision=1",
                        (tenant, original_id),
                    )
                elif race == "support":
                    concurrent.execute(
                        "UPDATE memories SET lifecycle_state='excluded' WHERE tenant_id=%s "
                        "AND memory_id=(SELECT memory_id FROM insight_support WHERE tenant_id=%s "
                        "AND insight_id=%s ORDER BY memory_id LIMIT 1)",
                        (tenant, tenant, original_id),
                    )
                elif race == "direct_removal":
                    concurrent.execute(
                        "UPDATE operations SET removal_target_kind='insight',removal_target_id=%s "
                        "WHERE tenant_id=%s AND operation_id=%s",
                        (original_id, tenant, first["operation_id"]),
                    )
            return publish(store, connection, **kwargs)

        monkeypatch.setattr(consolidation, "_publish_group", race_before_publish)
        with pytest.raises(StaleIndexWork):
            await retain_and_consolidate(
                client,
                dsn,
                tenant,
                config,
                models,
                "later",
                ["Production Atlas operations confirms manual approval before rollback."],
            )
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT insight_id,revision FROM insights WHERE tenant_id=%s "
                "AND visible_to IS NULL",
                (tenant,),
            ).fetchall() == [(original_id, 2 if race == "revision" else 1)]
            state, work_status = connection.execute(
                "SELECT o.stage_state,w.status FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.operation_id=%s",
                (tenant, affected_operation[0]),
            ).fetchone()
            assert not state.get("l2_examined_ids")
            assert not state.get("insights_ready")
            assert work_status == ("failed_terminal" if race == "generation" else "queued")


class GroupModels(CorroboratingModels):
    def generate(self, role, **kwargs):
        if role != "insights":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        self.groups.append(envelope)
        return ModelResult(
            content=json.dumps(
                {
                    "actions": [
                        {
                            "action": "NO_INSIGHT",
                            "target_id": None,
                            "text": None,
                            "retrieval_text": None,
                            "subtype": None,
                            "support_ids": [],
                            "seed_ids": envelope["seed_ids"],
                        }
                    ]
                }
            ),
            usage=Usage(),
        )

    def embed(self, texts, **kwargs):
        from oam.models.runtime import EmbeddingResult

        return EmbeddingResult(
            vectors=tuple(
                (0.0, 1.0) + (0.0,) * 1022 if "Garden" in text else (1.0, 0.0) + (0.0,) * 1022
                for text in texts
            ),
            usage=Usage(),
        )


async def test_related_groups_keep_other_topics_pending_and_respect_seed_cap(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = GroupModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "groups",
            [
                *(f"Production Atlas rollback observation {index}." for index in range(10)),
                "Garden pruning happens in spring.",
                "Garden trees need regular watering.",
            ],
        )
        assert sorted(len(group["seed_ids"]) for group in models.groups) == [2, 2, 8]
        accounted = []
        for group in models.groups:
            facts = {fact["id"]: fact["text"] for fact in group["facts"]}
            assert len({"Garden" in facts[seed] for seed in group["seed_ids"]}) == 1
            accounted.extend(group["seed_ids"])
        assert len(accounted) == len(set(accounted)) == 12


async def test_oversized_complete_target_fails_without_examining_seed(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = CorroboratingModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "first",
            [
                "Production Atlas rollback requires manual approval.",
                "Production Atlas release owner confirms manual rollback approval.",
            ],
        )
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE insights SET synthesis_text=%s WHERE tenant_id=%s",
                ("complete knowledge " * 10000, tenant),
            )
            connection.execute(
                "UPDATE search_units SET content_text=%s WHERE tenant_id=%s AND kind='insight'",
                ("complete knowledge " * 10000, tenant),
            )
        with pytest.raises(ValueError, match="one seed and its complete target context"):
            await retain_and_consolidate(
                client,
                dsn,
                tenant,
                config,
                models,
                "overflow",
                ["Production Atlas operations confirms manual approval before rollback."],
            )
        with psycopg.connect(dsn) as connection:
            states = connection.execute(
                "SELECT stage_state FROM operations WHERE tenant_id=%s "
                "AND kind='consolidate' ORDER BY accepted_seq",
                (tenant,),
            ).fetchall()
            assert not states[-1][0].get("l2_examined_ids")
            assert not states[-1][0].get("insights_ready")
            assert (
                connection.execute(
                    "SELECT count(*) FROM insights WHERE tenant_id=%s", (tenant,)
                ).fetchone()[0]
                == 1
            )
        assert len(models.groups) == 1


class MultiActionModels(CorroboratingModels):
    def generate(self, role, **kwargs):
        if role != "insights":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        seeds = envelope["seed_ids"]
        return ModelResult(
            content=json.dumps(
                {
                    "actions": [
                        {
                            "action": "CREATE",
                            "target_id": None,
                            "text": f"Supported synthesis {slot}.",
                            "title": f"Project facet {slot}",
                            "retrieval_text": f"Project facet {slot}",
                            "subtype": "projects",
                            "support_ids": seeds[slot : slot + 2],
                            "seed_ids": seeds[slot : slot + 2],
                        }
                        for slot in (0, 2)
                    ]
                }
            ),
            usage=Usage(),
        )


async def test_second_action_failure_rolls_back_first_action_and_all_dispositions(
    core_database, monkeypatch
):
    from oam.indexing.store import IndexStore

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = MultiActionModels()
    insert_unit = IndexStore.insert_unit
    published = []

    def fail_second_insight(store, connection, **kwargs):
        if kwargs["kind"] == "insight":
            published.append(kwargs["insight_id"])
            if len(published) == 2:
                raise RuntimeError("synthetic second-action publication failure")
        return insert_unit(store, connection, **kwargs)

    monkeypatch.setattr(IndexStore, "insert_unit", fail_second_insight)
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        with pytest.raises(RuntimeError, match="second-action publication failure"):
            await retain_and_consolidate(
                client,
                dsn,
                tenant,
                config,
                models,
                "batch",
                [
                    "Production Atlas trigger is five percent.",
                    "Production Atlas needs approval.",
                    "Production Atlas release occurs weekly.",
                    "Production Atlas logs each release.",
                ],
            )
        assert len(published) == 2
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM insights WHERE tenant_id=%s", (tenant,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM insight_support WHERE tenant_id=%s", (tenant,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM search_units WHERE tenant_id=%s AND kind='insight'",
                    (tenant,),
                ).fetchone()[0]
                == 0
            )
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE tenant_id=%s AND kind='consolidate'",
                (tenant,),
            ).fetchone()[0]
            assert not state.get("l2_examined_ids")
            assert not state.get("insights_ready")
