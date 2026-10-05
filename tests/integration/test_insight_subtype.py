"""Insight subtype persistence uses one descriptive, constrained column."""

import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from oam.config import load_config
from oam.workers.index_source import _insights


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


def test_worker_publishes_checkpoint_subtype_with_supported_synthesis(insight_connection):
    connection, tenant = insight_connection
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    published_units = []

    class PublicationStore:
        key = (tenant, "scope")
        generation_id = "generation"
        source = {
            "accepted_seq": 1,
            "source_id": "source-a",
            "source_version": 1,
        }
        examined = set()
        last_outcome = "no_insight"

        @contextmanager
        def transaction(self):
            yield self

        def guard(self, connection):
            pass

        def execute(self, query, parameters=None):
            if "SELECT unit_id FROM eligible WHERE kind='fact'" in query:
                examined_ids = set(parameters[3] or []) if parameters else set()
                rows = [(value,) for value in ("f1", "f2") if value not in examined_ids]
                return SimpleNamespace(fetchall=lambda: rows)
            if "SELECT candidate.unit_id" in query:
                rows = [
                    ("f1", "A", "source-a", 1, None, None, None, None),
                    ("f2", "B", "source-b", 1, None, None, None, None),
                ]
                return SimpleNamespace(fetchall=lambda: rows)
            if "SELECT unit_id FROM eligible WHERE unit_id=ANY" in query:
                rows = [("f1",), ("f2",)]
                return SimpleNamespace(fetchall=lambda: rows)
            if query.startswith("INSERT INTO insight_support"):
                return None
            return connection.execute(query, parameters)

        def l2_progress(self, connection):
            return set(self.examined), self.last_outcome

        def mark_l2_group(self, connection, unit_ids, outcome):
            assert set(unit_ids) == {"f1", "f2"}
            assert outcome == "insight"
            self.examined.update(unit_ids)
            self.last_outcome = outcome

        def checkpoint(self, stage, inputs, produce):
            if stage == "insight_embed":
                return {"vectors": [[1.0, 0.0]]}
            assert stage == "insight"
            return {
                "outcome": "insight",
                "subtype": "patterns",
                "text": "Repeated supported behavior.",
                "support_ids": ["f1", "f2"],
                "usage": {},
            }

        def insert_unit(self, connection, **unit):
            published_units.append(unit)

        def readiness(self, connection, **stages):
            pass

    store = PublicationStore()
    store.config = config
    _insights(store, None, ["f1", "f2"])

    row = connection.execute(
        "SELECT subtype,synthesis_text FROM insights WHERE tenant_id=%s", (tenant,)
    ).fetchone()
    assert row == ("patterns", "Repeated supported behavior.")
    assert published_units[0]["kind"] == "insight"
