"""Public core path against an explicitly isolated PostgreSQL database."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.api.app import create_app
from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture
def core_database(monkeypatch):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    database_name = f"oam_core_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = make_conninfo(dsn, dbname=database_name)
    apply_migrations(dsn, ROOT / "migrations")
    tenant = f"core-{uuid4().hex}"
    secret = f"synthetic-{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants VALUES (%s, 'active', now())", (tenant,))
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES (%s,'admin','administrator','active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES (%s,'key','admin',%s,'active')",
            (tenant, digest_secret(secret)),
        )
    monkeypatch.setenv("OAM_API_DSN", make_conninfo(dsn, options="-c role=oam_api"))
    return dsn, tenant, {"Authorization": f"Bearer {secret}"}


async def test_public_accept_commits_exact_mixed_eligibility_input_and_reuses_receipt(
    core_database,
):
    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    body = {
        "user_id": "scope",
        "input_type": "interaction",
        "messages": [
            {
                "role": "user",
                "content": "I prefer short explanations.",
                "event_time": "2026-09-01T09:00:00Z",
                "learning_eligible": True,
            },
            {
                "role": "assistant",
                "content": "The report says glucose 9.",
                "event_time": "2026-09-01T09:01:00Z",
                "learning_eligible": False,
            },
        ],
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        scope = await client.post("/v1/users", json={"user_id": "scope"})
        assert scope.status_code == 201, scope.text
        first = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "input-1"})
        assert first.status_code == 202, first.text
        second = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "input-1"})
        assert second.status_code == 202, second.text
        assert second.json()["operation_id"] == first.json()["operation_id"]
        receipt = await client.get(f"/v1/users/scope/operations/{first.json()['operation_id']}")
        assert receipt.status_code == 200
        assert receipt.json()["readiness"]["accepted"] is True
    with psycopg.connect(dsn) as connection:
        original = bytes(
            connection.execute(
                "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
        )
        stored = json.loads(original)
        assert [m["learning_eligible"] for m in stored["messages"]] == [True, False]
        assert stored["messages"][1]["content"] == "The report says glucose 9."
        assert (
            connection.execute(
                "SELECT count(*) FROM operations WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 1
        )


def test_index_schema_initializes_dimension_and_rejects_cross_scope_graph_endpoints(core_database):
    dsn, tenant, _ = core_database
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute("SELECT embedding_dimensions FROM index_settings").fetchone()[0]
            == 1024
        )
        for scope in ("a", "b"):
            connection.execute(
                "INSERT INTO scopes(tenant_id,scope_id,subject,namespace,lifetime) "
                "VALUES (%s,%s,'synthetic','test','persistent')",
                (tenant, scope),
            )
            connection.execute(
                "INSERT INTO memories(tenant_id,scope_id,memory_id,canonical_claim,"
                "assertion_kind,generation_id,visible_from) "
                "VALUES (%s,%s,%s,'synthetic','stated','g',1)",
                (tenant, scope, scope),
            )
            connection.execute(
                "INSERT INTO search_units(tenant_id,scope_id,unit_id,memory_id,kind,origin,"
                "content_text,"
                "generation_id,embedding,lexical_text,locator) "
                "VALUES (%s,%s,%s,%s,'fact','memory','synthetic','g',%s::vector,'synthetic','{}')",
                (tenant, scope, scope, scope, str([1.0] + [0.0] * 1023)),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                "INSERT INTO unit_links(tenant_id,scope_id,source_id,target_id,kind,weight) "
                "VALUES (%s,'a','a','b','semantic',0.9)",
                (tenant,),
            )


class SyntheticIndexModels:
    def __init__(self, *, invalid_fact=False):
        self.generation_calls = []
        self.invalid_fact = invalid_fact

    def generate(self, role, **kwargs):
        from oam.models.runtime import ModelResult, Usage

        self.generation_calls.append(role)
        envelope = json.loads(kwargs["user"])
        if role == "extraction":
            center = envelope["center"]
            output = {
                "outcome": "facts",
                "facts": [
                    {
                        "text": center["text"],
                        "category": "preference",
                        "quote": center["text"],
                        "start": center["start"],
                        "end": center["end"],
                        "entities": ["Lin"],
                        "caused_by": [],
                        "updates": [],
                        "annotations": [],
                    }
                ],
            }
        elif role == "insights":
            output = {
                "outcome": "insight",
                "subtype": "general",
                "text": "Lin prefers concise morning updates.",
                "support_ids": [fact["id"] for fact in envelope["facts"]],
            }
        else:
            output = {"abstract": "Synthetic medical measurements."}
        if role == "extraction" and self.invalid_fact:
            output["facts"].append({**output["facts"][0], "quote": "Not in the source"})
        return ModelResult(
            content=json.dumps(output),
            usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"),
        )

    def embed(self, texts, **kwargs):
        from oam.models.runtime import EmbeddingResult, Usage

        return EmbeddingResult(
            vectors=tuple((1.0,) + (0.0,) * 1023 for _ in texts),
            usage=Usage(
                input_tokens=len(texts),
                total_tokens=len(texts),
                output_tokens=0,
                coverage="complete",
            ),
        )


async def test_html_upload_is_searchable_evidence_without_learned_facts(core_database):

    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    original = b"<table><tr><td>Glucose</td><td>5.2 mmol/L</td></tr></table>"
    metadata = {
        "user_id": "scope",
        "input_type": "document",
        "document_id": "report",
        "update_mode": "replace",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        assert (await client.post("/v1/users", json={"user_id": "scope"})).status_code == 201
        uploaded = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "report"},
            data={"metadata": json.dumps(metadata)},
            files={"original": ("report.html", original, "text/html")},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="resource-worker",
        )
        app.state.runtime.models = models
        recalled = await client.post(
            "/v1/recall", json={"user_id": "scope", "query": "glucose mmol"}
        )
        assert recalled.status_code == 200, recalled.text
        assert recalled.json()["items"][0]["origin"] == "evidence"
        assert "5.2 mmol/L" in recalled.json()["context"]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM insights WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 0
        )
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
    assert models.generation_calls == ["abstract"]


@pytest.mark.parametrize("invalid_fact", [False, True])
async def test_worker_publishes_positive_l2_with_l0_support_and_excludes_ineligible_message(
    core_database,
    invalid_fact,
):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels(invalid_fact=invalid_fact)
    body = {
        "user_id": "scope",
        "input_type": "interaction",
        "messages": [
            {
                "role": "user",
                "content": "Lin prefers short explanations.",
                "event_time": "2026-09-01T09:00:00Z",
                "learning_eligible": True,
            },
            {
                "role": "user",
                "content": "Lin prefers updates in the morning.",
                "event_time": "2026-09-02T09:00:00Z",
                "learning_eligible": True,
            },
            {
                "role": "assistant",
                "content": "Report glucose is 9.",
                "event_time": "2026-09-02T09:01:00Z",
                "learning_eligible": False,
            },
        ],
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        assert (await client.post("/v1/users", json={"user_id": "scope"})).status_code == 201
        receipt = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "chat"})
        assert receipt.status_code == 202
        assert (
            process_one(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=models,
                worker_id="synthetic-worker",
            )
            is True
        )
        app.state.runtime.models = models
        recalled = await client.post(
            "/v1/recall",
            json={"user_id": "scope", "query": "Lin morning explanations", "profile_sections": []},
        )
        assert recalled.status_code == 200, recalled.text
        assert any(item["kind"] == "insight" for item in recalled.json()["items"])
        assert "glucose" not in recalled.json()["context"]
        assert recalled.json()["context_tokens"] > 0
        finished = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        assert finished.json()["readiness"]["insights_ready"] is True
        assert finished.json()["usage"]["total_tokens"] == 90
        assert finished.json()["usage"]["coverage"] == "complete"
        assert finished.json()["embedding_usage"]["total_tokens"] == 5
        assert finished.json()["stage_state"]["links_ready"] is True
        assert finished.json()["stage_state"]["extraction_coverage"] == (
            "partial" if invalid_fact else "complete"
        )
    with psycopg.connect(dsn) as connection:
        facts = connection.execute(
            "SELECT canonical_claim FROM memories WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert {row[0] for row in facts} == {
            "Lin prefers short explanations.",
            "Lin prefers updates in the morning.",
        }
        insight = connection.execute(
            "SELECT synthesis_text FROM insights WHERE tenant_id=%s", (tenant,)
        ).fetchone()
        assert insight[0] == "Lin prefers concise morning updates."
        assert (
            connection.execute(
                "SELECT count(*) FROM insight_support WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM support WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 2
        )
        indexed = connection.execute(
            "SELECT content_text FROM search_units WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert all("glucose" not in row[0] for row in indexed)
        assert (
            connection.execute(
                "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'", (tenant,)
            ).fetchone()[0]
            == 2
        )
    assert models.generation_calls == ["extraction", "extraction", "insights"]
