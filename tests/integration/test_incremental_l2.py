"""Two retained increments evolve a current Insight through the real worker/API path."""

import json
from pathlib import Path

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import SyntheticIndexModels, process_source, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.models.runtime import ModelResult, Usage
from oam.workers.index_source import process_one

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


class PolicyModels(SyntheticIndexModels):
    def __init__(self):
        super().__init__()
        self.groups = []

    def generate(self, role, **kwargs):
        if role != "insights":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        self.groups.append(envelope)
        facts = envelope["facts"]
        seeds = envelope.get("seed_ids", [fact["id"] for fact in facts])
        supplied = {fact["id"]: fact for fact in facts}
        distinct = any("research" in supplied[seed]["text"].lower() for seed in seeds)
        revised = any("2 percent" in supplied[seed]["text"] for seed in seeds)
        text = (
            "Research Atlas owned by Mei runs experiments; its rollback needs lab approval."
            if distinct
            else "Production Atlas owned by Lin requires manual approval. "
            "On October 19 the rollback "
            "trigger changed from 5 percent failures to 2 percent failures."
            if revised
            else "Production Atlas owned by Lin rolls back at 5 percent failures "
            "with manual approval."
        )
        targets = envelope.get("targets", [])
        target = next((item for item in targets if "Production Atlas" in item["text"]), None)
        if "seed_ids" in envelope:
            output = {
                "actions": [
                    {
                        "action": "UPDATE" if target and not distinct else "CREATE",
                        "target_id": target["id"] if target and not distinct else None,
                        "text": text,
                        "title": "Research Atlas rollback"
                        if distinct
                        else "Production Atlas rollback",
                        "retrieval_text": "Research Atlas rollback"
                        if distinct
                        else "Production Atlas rollback",
                        "subtype": "projects",
                        "support_ids": list(seeds)
                        if target and not distinct
                        else [
                            fact["id"]
                            for fact in facts
                            if ("research" in fact["text"].lower()) == distinct
                        ],
                        "seed_ids": seeds,
                    }
                ]
            }
        else:
            output = {
                "outcome": "insight",
                "subtype": "projects",
                "text": text,
                "title": "Research Atlas rollback" if distinct else "Production Atlas rollback",
                "support_ids": [fact["id"] for fact in facts],
            }
        return ModelResult(content=json.dumps(output), usage=Usage())


async def retain_and_consolidate(client, dsn, tenant, config, models, key, texts):
    accepted = await client.post(
        "/v1/retain",
        json={
            "input_type": "interaction",
            "user_id": "scope",
            "messages": [
                {
                    "role": "user",
                    "content": text,
                    "learning_eligible": True,
                    "event_time": "2026-10-19T00:00:00Z",
                }
                for text in texts
            ],
        },
        headers={"Idempotency-Key": key},
    )
    assert accepted.status_code == 202, accepted.text
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    process_source(config, queue, content, models=models, worker_id=key)
    operation = await client.get(f"/v1/users/scope/operations/{accepted.json()['operation_id']}")
    assert operation.json()["status"] == "succeeded", operation.text
    consolidation = operation.json()["stage_state"]["consolidation_operation_id"]
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() "
            "WHERE tenant_id=%s AND operation_id=%s",
            (tenant, consolidation),
        )
    assert process_one(config, queue, content, models=models, worker_id=key + "-l2")
    receipt = await client.get(f"/v1/users/scope/operations/{consolidation}")
    assert receipt.json()["status"] == "succeeded", receipt.text
    return accepted.json()


async def test_two_increments_keep_identity_support_and_current_fast_normal_context(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = PolicyModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        app.state.runtime.models = models
        first = await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "policy",
            [
                "Production Atlas owned by Lin rolls back at 5 percent failures.",
                "Production Atlas rollback requires manual approval.",
            ],
        )
        with psycopg.connect(dsn) as connection:
            original_id = connection.execute(
                "SELECT insight_id FROM insights WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "revision",
            ["Production Atlas rollback trigger changed to 2 percent failures on October 19."],
        )
        with psycopg.connect(dsn) as connection:
            current = connection.execute(
                "SELECT insight_id,revision FROM insights "
                "WHERE tenant_id=%s AND visible_to IS NULL",
                (tenant,),
            ).fetchall()
            assert current == [(original_id, 2)]
            supports = connection.execute(
                "SELECT count(*) FROM insight_support WHERE tenant_id=%s "
                "AND insight_id=%s AND insight_revision=2",
                (tenant, original_id),
            ).fetchone()[0]
            assert supports == 3
        assert models.groups[1]["targets"][0]["title"] == "Production Atlas rollback"
        inspected = await client.get(f"/v1/users/scope/memories/{original_id}?memory_type=insight")
        assert inspected.json()["title"] == "Production Atlas rollback"
        listed = await client.get("/v1/users/scope/memories?memory_type=insight")
        assert listed.json()["items"][0]["title"] == "Production Atlas rollback"
        for mode in ("fast", "normal"):
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "Production Atlas rollback trigger approval",
                    "recall_level": mode,
                },
            )
            assert recalled.status_code == 200, recalled.text
            insights = [item for item in recalled.json()["items"] if item["kind"] == "insight"]
            assert [(item["insight_id"], item["insight_revision"]) for item in insights] == [
                (original_id, 2)
            ]
            assert "5 percent" in recalled.json()["context"]
            assert "2 percent" in recalled.json()["context"]
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "distinct",
            [
                "Research Atlas owned by Mei runs experiments in a separate laboratory.",
                "Research Atlas rollback requires lab approval.",
            ],
        )
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM insights WHERE tenant_id=%s AND visible_to IS NULL",
                    (tenant,),
                ).fetchone()[0]
                == 2
            )
        replay = await client.post(
            "/v1/retain",
            json={
                "input_type": "interaction",
                "user_id": "scope",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "learning_eligible": True,
                        "event_time": "2026-10-19T00:00:00Z",
                    }
                    for text in [
                        "Production Atlas owned by Lin rolls back at 5 percent failures.",
                        "Production Atlas rollback requires manual approval.",
                    ]
                ],
            },
            headers={"Idempotency-Key": "policy"},
        )
        assert replay.status_code == 202
        assert replay.json()["operation_id"] == first["operation_id"]
        assert len(models.groups) == 3
