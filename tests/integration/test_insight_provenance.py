"""Accepted message provenance reaches the real L2 worker's model request."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, SyntheticIndexModels, process_source, use_created_user_key
from test_core_http import core_database as core_database

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.indexing import consolidation
from oam.models.runtime import ModelResult, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


async def test_fact_context_does_not_rescan_the_eligible_collection_per_candidate(
    core_database, monkeypatch
):
    import psycopg

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
        accepted = await client.post(
            "/v1/retain",
            json={
                "input_type": "interaction",
                "user_id": "scope",
                "messages": [
                    {
                        "role": "user",
                        "content": f"The user practices violin at venue {i}.",
                        "learning_eligible": True,
                        "event_time": "2026-05-03T18:00:00Z",
                    }
                    for i in range(8)
                ],
            },
            headers={"Idempotency-Key": "bounded-seed-lookup"},
        )
        assert accepted.status_code == 202, accepted.text
        process_source(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=SyntheticIndexModels(),
            worker_id="lookup-worker",
        )
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT set_config('oam.tenant_id',%s,true)", (tenant,))
        connection.execute("SELECT set_config('oam.user_id','scope',true)")
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT unit_id FROM search_units WHERE tenant_id=%s AND kind='fact' "
                "ORDER BY unit_id",
                (tenant,),
            )
        ]
        assert len(identifiers) == 8
        generation = connection.execute(
            "SELECT generation_id FROM search_units WHERE tenant_id=%s AND unit_id=%s",
            (tenant, identifiers[0]),
        ).fetchone()[0]
        with monkeypatch.context() as patch:
            patch.setattr(consolidation, "_rows", lambda _, query, params, **kw: (query, params))
            query, params = consolidation._fact_context(
                SimpleNamespace(
                    config=config,
                    generation_id=generation,
                    authority=AuthorizedUser("key", tenant, "worker", "scope", frozenset()),
                ),
                connection,
                [{"unit_id": identifier} for identifier in identifiers[:2]],
                [],
            )
        plan = connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, params).fetchone()[0][
            0
        ]["Plan"]
        assert len(connection.execute(query, params).fetchall()) == len(identifiers)

    def eligible_scans(node):
        if node.get("Node Type") == "CTE Scan" and node.get("CTE Name") == "eligible":
            yield node["Actual Loops"]
        for child in node.get("Plans", []):
            yield from eligible_scans(child)

    # Distance comparisons may visit the small seed set for each candidate;
    # they must not repeatedly scan the full eligible collection to find it.
    assert max(eligible_scans(plan), default=0) <= 1


async def test_l2_worker_preserves_accepted_roles_and_message_dates(core_database, tmp_path):
    dsn, tenant, headers = core_database
    raw = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    raw["index"]["insight_consolidation_delay_hours"] = 1 / 3600
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path).config

    class CapturingModels(SyntheticIndexModels):
        insight_inputs = None

        def generate(self, role, **kwargs):
            if role != "insights":
                return super().generate(role, **kwargs)
            envelope = json.loads(kwargs["user"])
            self.insight_inputs = envelope["facts"]
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

    models = CapturingModels()
    app = create_app(path)
    messages = [
        {
            "role": role,
            "content": text,
            "event_time": "2026-05-03T18:00:00Z",
            "learning_eligible": True,
        }
        for role, text in (
            ("user", "Please send factual stories each evening."),
            ("assistant", "The assistant generated a fictional family story."),
            ("system", "A daily delivery task was observed."),
            ("tool", "The delivery service reported one successful execution."),
        )
    ]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipt = await client.post(
            "/v1/retain",
            json={"input_type": "interaction", "user_id": "scope", "messages": messages},
            headers={"Idempotency-Key": "source-roles"},
        )
        assert receipt.status_code == 202, receipt.text
        for _ in range(100):
            process_one(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=models,
                worker_id="provenance-worker",
            )
            if models.insight_inputs is not None:
                break
            await asyncio.sleep(0.05)

    assert models.insight_inputs is not None
    supplied = {fact["text"]: fact for fact in models.insight_inputs}
    assert {text: fact["message_role"] for text, fact in supplied.items()} == {
        "Please send factual stories each evening.": "user",
        "The assistant generated a fictional family story.": "assistant",
        "A daily delivery task was observed.": "system",
        "The delivery service reported one successful execution.": "tool",
    }
    assert {fact["mentioned_at"] for fact in supplied.values()} == {"2026-05-03T18:00:00Z"}
    assert all("source_refs" not in fact for fact in supplied.values())
    # The lean model view must not discard stored provenance.
    import psycopg

    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT DISTINCT source_id,source_version FROM search_units "
            "WHERE tenant_id=%s AND kind='fact'",
            (tenant,),
        ).fetchall() == [(receipt.json()["source_id"], 1)]
