"""Accepted message provenance reaches the real L2 worker's model request."""

import asyncio
import json

import httpx
import pytest
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.models.runtime import ModelResult, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


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
