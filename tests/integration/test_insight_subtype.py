"""Insight subtype persistence uses one descriptive, constrained column."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from test_core_http import SyntheticIndexModels, insight_reply, use_created_user_key
from test_core_http import core_database as core_database
from test_incremental_l2 import retain_and_consolidate

from oam.config import load_config
from oam.models.runtime import ModelResult, Usage


@pytest.fixture
def insight_connection():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    connection = psycopg.connect(dsn)
    tenant = "subtype_" + uuid4().hex
    try:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES (%s,'scope','subject','default','persistent')",
            (tenant,),
        )
        yield connection, tenant
    finally:
        connection.rollback()
        connection.close()


def insert_insight(connection, tenant, *, subtype=None, include_subtype=True):
    columns = (
        "tenant_id,user_id,insight_id,revision,synthesis_text,input_digest,"
        "recipe_id,generation_id,visible_from"
    )
    placeholders = "%s,'scope',%s,1,'Supported synthesis',%s,'recipe','generation',1"
    parameters = [tenant, "insight_" + uuid4().hex, b"x" * 32]
    if include_subtype:
        columns += ",subtype"
        placeholders += ",%s"
        parameters.append(subtype)
    return connection.execute(
        f"INSERT INTO insights({columns}) VALUES ({placeholders}) RETURNING subtype",
        parameters,
    ).fetchone()[0]


def test_persisted_insight_subtypes_and_historical_default(insight_connection):
    connection, tenant = insight_connection
    for subtype in (
        "preferences",
        "patterns",
        "projects",
        "relationships",
        "experiences",
        "general_insights",
    ):
        assert insert_insight(connection, tenant, subtype=subtype) == subtype
    assert insert_insight(connection, tenant, include_subtype=False) == "general_insights"


@pytest.mark.parametrize("subtype", [None, "fact", "unknown"])
def test_persisted_insight_rejects_absent_or_unknown_subtype(insight_connection, subtype):
    connection, tenant = insight_connection
    with pytest.raises(psycopg.IntegrityError):
        insert_insight(connection, tenant, subtype=subtype)


@pytest.mark.parametrize("retrieval_text", [None, "Instrument restoration"])
async def test_worker_publishes_checkpoint_subtype_with_supported_synthesis(
    core_database, retrieval_text, capsys
):
    dsn, tenant, headers = core_database
    root = Path(__file__).parents[2]
    config = load_config(root / "configs/oam.yaml").config

    class SubtypeModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role != "insights":
                return super().generate(role, **kwargs)
            envelope = json.loads(kwargs["user"])
            return ModelResult(
                content=json.dumps(
                    insight_reply(
                        envelope,
                        {
                            "text": "Repeated supported behavior.",
                            "subtype": "patterns",
                            "support_ids": [fact["id"] for fact in envelope["facts"]],
                            "retrieval_text": retrieval_text,
                        },
                    )
                ),
                usage=Usage(),
            )

    models = SubtypeModels()
    app = create_app(root / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        app.state.runtime.models = models
        await retain_and_consolidate(
            client,
            dsn,
            tenant,
            config,
            models,
            "subtype",
            [
                "The user restored a violin on Monday.",
                "The user restored a violin on Tuesday.",
            ],
        )
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT subtype,synthesis_text FROM insights WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("patterns", "Repeated supported behavior.")
        locator = connection.execute(
            "SELECT locator FROM search_units WHERE tenant_id=%s AND kind='insight'", (tenant,)
        ).fetchone()[0]
        assert len(locator["support_ids"]) == 2
        assert locator["retrieval_text"] == retrieval_text
    assert models.embedding_batches[-1] == (retrieval_text or "Repeated supported behavior.",)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    published = next(event for event in events if event["stage"] == "insight_published")
    assert published["published"][0]["insight_id"].startswith("insight_")
    assert published["published"][0]["revision"] == 1
    assert published["actions"] == ["CREATE"]
    assert "input_tokens" in published["usage"]
