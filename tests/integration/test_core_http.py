"""Public core path against an explicitly isolated PostgreSQL database."""

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import psycopg
import pytest
import tiktoken
from api_test_app import create_api_only_app as create_app
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

ROOT = Path(__file__).parents[2]
MEDICAL = ROOT / "conformance/fixtures/medical"
BILINGUAL = MEDICAL / "bilingual"
pytestmark = pytest.mark.integration


def process_source(config, queue_dsn, content_dsn, *, models, worker_id):
    """Run the source claim and its dependent linking claim before later consumers."""
    from oam.workers.index_source import process_one

    assert process_one(config, queue_dsn, content_dsn, models=models, worker_id=worker_id)
    return process_one(
        config, queue_dsn, content_dsn, models=models, worker_id=worker_id + "-links"
    )


async def use_created_user_key(client: httpx.AsyncClient, user_id: str) -> httpx.Response:
    created = await client.post("/v1/users", json={"user_id": user_id})
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['secret']}"
    return created


@pytest.fixture
def core_database(monkeypatch, synthetic_model_environment):
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


async def test_root_creates_user_key_but_cannot_read_personal_operations(core_database):
    _, _, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        created = await client.post("/v1/users", json={"user_id": "alice"})
        assert created.status_code == 201, created.text
        alice_headers = {"Authorization": f"Bearer {created.json()['secret']}"}
        root_write = await client.post(
            "/v1/users/alice/resources",
            json={
                "input_type": "document",
                "document_id": "root-attempt",
                "update_mode": "replace",
                "text": "must not be accepted",
            },
            headers={"Idempotency-Key": "root-attempt"},
        )
        assert root_write.status_code == 404
        retained = await client.post(
            "/v1/users/alice/resources",
            json={
                "input_type": "document",
                "document_id": "private",
                "update_mode": "replace",
                "text": "Alice's private document",
            },
            headers={**alice_headers, "Idempotency-Key": "alice-private"},
        )
        assert retained.status_code == 202, retained.text
        route = f"/v1/users/alice/operations/{retained.json()['operation_id']}"
        assert (await client.get(route)).status_code == 404
        assert (
            await client.post("/v1/recall", json={"user_id": "alice", "query": "private"})
        ).status_code == 404
        assert (await client.get(route, headers=alice_headers)).status_code == 200
        bob = await client.post("/v1/users", json={"user_id": "bob"})
        assert bob.status_code == 201, bob.text
        assert (
            await client.get(route, headers={"Authorization": f"Bearer {bob.json()['secret']}"})
        ).status_code == 404


async def test_user_listing_filters_by_inclusive_last_write_and_paginates(core_database):
    dsn, tenant, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        for user_id in ("alice", "bob", "carol"):
            created = await client.post("/v1/users", json={"user_id": user_id})
            assert created.status_code == 201, created.text
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE users SET last_write_at = CASE user_id "
                "WHEN 'alice' THEN '2026-11-10T00:00:00Z'::timestamptz "
                "WHEN 'bob' THEN '2026-11-11T00:00:00Z'::timestamptz "
                "ELSE NULL END WHERE tenant_id = %s",
                (tenant,),
            )
            connection.execute(
                "INSERT INTO tenants(tenant_id,state) VALUES ('foreign-users','active')"
            )
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,last_write_at) "
                "VALUES ('foreign-users','z-user','z-user','personal','persistent',"
                "'2026-11-12T00:00:00Z')"
            )

        first = await client.get(
            "/v1/users",
            params={"last_write_at_gte": "2026-11-10T00:00:00Z", "limit": 1, "offset": 0},
        )
        assert first.status_code == 200, first.text
        assert first.json() == {
            "items": [{"user_id": "alice", "last_write_at": "2026-11-10T00:00:00Z"}],
            "total": 2,
            "limit": 1,
            "offset": 0,
        }
        second = await client.get(
            "/v1/users",
            params={"last_write_at_gte": "2026-11-10T08:00:00+08:00", "limit": 1, "offset": 1},
        )
        assert second.status_code == 200, second.text
        assert second.json() == {
            "items": [{"user_id": "bob", "last_write_at": "2026-11-11T00:00:00Z"}],
            "total": 2,
            "limit": 1,
            "offset": 1,
        }
        empty = await client.get(
            "/v1/users",
            params={"last_write_at_gte": "2026-11-10T00:00:00Z", "offset": 9},
        )
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"items": [], "total": 2, "limit": 100, "offset": 9}
        large_offset = await client.get("/v1/users", params={"offset": 2**31})
        assert large_offset.status_code == 200, large_offset.text
        assert large_offset.json() == {"items": [], "total": 3, "limit": 100, "offset": 2**31}
        unfiltered = await client.get("/v1/users", params={"limit": 10})
        assert unfiltered.status_code == 200, unfiltered.text
        assert [item["user_id"] for item in unfiltered.json()["items"]] == ["alice", "bob", "carol"]
        assert [item["last_write_at"] for item in unfiltered.json()["items"]] == [
            "2026-11-10T00:00:00Z",
            "2026-11-11T00:00:00Z",
            None,
        ]


async def test_user_listing_requires_root_admin_and_valid_query(core_database):
    _, _, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/v1/users")).status_code == 401
        created = await client.post("/v1/users", json={"user_id": "alice"}, headers=root_headers)
        assert created.status_code == 201, created.text
        user_headers = {"Authorization": f"Bearer {created.json()['secret']}"}
        assert (await client.get("/v1/users", headers=user_headers)).status_code == 403
        assert (await client.get("/v1/users?limit=10001", headers=root_headers)).status_code == 422
        assert (await client.get("/v1/users?offset=-1", headers=root_headers)).status_code == 422
        assert (
            await client.get("/v1/users", params={"offset": 2**63}, headers=root_headers)
        ).status_code == 422
        assert (
            await client.get(
                "/v1/users?last_write_at_gte=2026-11-10T00%3A00%3A00",
                headers=root_headers,
            )
        ).status_code == 422
        assert (
            await client.get("/v1/users?last_write_at_gte=not-a-time", headers=root_headers)
        ).status_code == 422


async def test_accepted_input_advances_user_last_write_and_idempotent_replay_does_not(
    core_database,
):
    dsn, tenant, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        created = await client.post("/v1/users", json={"user_id": "alice"})
        assert created.status_code == 201, created.text
        alice_headers = {"Authorization": f"Bearer {created.json()['secret']}"}
        before = await client.get("/v1/users")
        assert before.status_code == 200, before.text
        assert before.json()["items"] == [{"user_id": "alice", "last_write_at": None}]
        payload = {
            "user_id": "alice",
            "input_type": "interaction",
            "messages": [
                {
                    "role": "user",
                    "content": "I prefer concise answers.",
                    "event_time": "2026-11-12T08:30:00Z",
                    "learning_eligible": True,
                }
            ],
        }
        retained = await client.post(
            "/v1/retain", json=payload, headers={**alice_headers, "Idempotency-Key": "stable-input"}
        )
        assert retained.status_code == 202, retained.text
        accepted = await client.get(
            "/v1/users", params={"last_write_at_gte": "2000-01-01T00:00:00Z"}
        )
        assert accepted.status_code == 200, accepted.text
        first_time = accepted.json()["items"][0]["last_write_at"]
        assert first_time is not None
        replay = await client.post(
            "/v1/retain", json=payload, headers={**alice_headers, "Idempotency-Key": "stable-input"}
        )
        assert replay.status_code == 202, replay.text
        after_replay = await client.get(
            "/v1/users", params={"last_write_at_gte": "2000-01-01T00:00:00Z"}
        )
        assert after_replay.status_code == 200, after_replay.text
        assert after_replay.json()["items"][0]["last_write_at"] == first_time
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT last_write_at IS NOT NULL FROM users "
                "WHERE tenant_id=%s AND user_id='alice'",
                (tenant,),
            ).fetchone() == (True,)


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
        await use_created_user_key(client, "scope")
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
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
                "VALUES (%s,%s,'synthetic','test','persistent')",
                (tenant, scope),
            )
            connection.execute(
                "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,"
                "assertion_kind,generation_id,visible_from) "
                "VALUES (%s,%s,%s,'synthetic','stated','g',1)",
                (tenant, scope, scope),
            )
            connection.execute(
                "INSERT INTO search_units(tenant_id,user_id,unit_id,memory_id,kind,origin,"
                "content_text,"
                "generation_id,embedding,lexical_text,locator) "
                "VALUES (%s,%s,%s,%s,'fact','memory','synthetic','g',%s::vector,'synthetic','{}')",
                (tenant, scope, scope, scope, str([1.0] + [0.0] * 1023)),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                "INSERT INTO unit_links(tenant_id,user_id,source_id,target_id,kind,weight) "
                "VALUES (%s,'a','a','b','semantic',0.9)",
                (tenant,),
            )


class SyntheticIndexModels:
    def __init__(self, *, invalid_fact=False, vision_text="Glucose 92 mg/dL", vision_blocks=None):
        self.generation_calls = []
        self.embedding_batches = []
        self.invalid_fact = invalid_fact
        self.vision_text = vision_text
        self.vision_blocks = vision_blocks

    def generate(self, role, **kwargs):
        from oam.models.runtime import ModelResult, Usage

        self.generation_calls.append(role)
        envelope = json.loads(kwargs["user"])
        if role == "extraction":
            if "passages" in envelope:
                facts = [
                    {
                        "text": p["center"]["text"],
                        "quote": p["center"]["text"],
                        "passage_id": p["passage_id"],
                        "category": "preference",
                        "entities": [],
                    }
                    for p in envelope["passages"]
                ]
                if self.invalid_fact:
                    facts.append({**facts[0], "quote": "Not in the source"})
                return ModelResult(
                    content=json.dumps({"outcome": "facts", "facts": facts}),
                    usage=Usage(
                        input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                    ),
                )
            center = envelope["center"]
            output = {
                "outcome": "facts",
                "facts": [
                    {
                        "text": center["text"],
                        "category": "preference",
                        "quote": center["text"],
                        "entities": [],
                        "caused_by": [],
                        "updates": [],
                        "annotations": [],
                    }
                ],
            }
        elif role == "insights":
            output = insight_reply(
                envelope,
                {
                    "outcome": "insight",
                    "subtype": "general_insights",
                    "text": "Lin prefers concise morning updates.",
                    "support_ids": [fact["id"] for fact in envelope["facts"]],
                },
            )
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

        self.embedding_batches.append(tuple(texts))
        return EmbeddingResult(
            vectors=tuple((1.0,) + (0.0,) * 1023 for _ in texts),
            usage=Usage(
                input_tokens=len(texts),
                total_tokens=len(texts),
                output_tokens=0,
                coverage="complete",
            ),
        )

    def generate_vision(self, image, **kwargs):
        from oam.models.runtime import ModelResult, Usage

        self.generation_calls.append("extraction")
        return ModelResult(
            content=json.dumps({"blocks": self.vision_blocks or [{"text": self.vision_text}]}),
            usage=Usage(input_tokens=15, output_tokens=8, total_tokens=23, coverage="complete"),
        )


async def test_html_upload_is_searchable_evidence_without_learned_facts(core_database):

    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    original = (
        b"<html><head><title>Allergy report</title></head><body><h1>Lab results</h1>"
        b"<table><caption>Electrolytes</caption><tr><td>Glucose</td>"
        b"<td>5.2 mmol/L</td></tr></table></body></html>"
    )
    metadata = {
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
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/users/scope/resources",
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
                "SELECT projection_epoch FROM users WHERE tenant_id=%s AND user_id='scope'",
                (tenant,),
            ).fetchone()[0]
            > 0
        )
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
        weighted = connection.execute(
            "SELECT ts_rank_cd(ARRAY[0,0,0,1]::float4[],lexical_bilingual,"
            "plainto_tsquery('english','allergy'))>0,"
            "ts_rank_cd(ARRAY[0,0,1,0]::float4[],lexical_bilingual,"
            "plainto_tsquery('english','lab'))>0,"
            "ts_rank_cd(ARRAY[0,1,0,0]::float4[],lexical_bilingual,"
            "plainto_tsquery('english','electrolytes'))>0 "
            "FROM search_units WHERE tenant_id=%s AND content_text='Glucose 5.2 mmol/L'",
            (tenant,),
        ).fetchone()
        assert weighted == (True, True, True)
    assert models.generation_calls == ["abstract"]


@pytest.mark.parametrize(
    ("fixture_name", "media_type", "block", "expected_text", "uses_vision"),
    [
        (
            "synthetic-lab-report.pdf.b64",
            "application/pdf",
            "pdf:page:0001",
            "Glucose: 92 mg/dL",
            False,
        ),
        (
            "synthetic-lab-report.png.b64",
            "image/png",
            "image:0001:block:0001",
            "Glucose 92 mg/dL",
            True,
        ),
    ],
)
async def test_public_pdf_and_image_retain_to_cited_recall(
    core_database, fixture_name, media_type, block, expected_text, uses_vision
):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    original = base64.b64decode((MEDICAL / fixture_name).read_text(encoding="ascii"))
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    metadata = {
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
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": "report"},
            data={"metadata": json.dumps(metadata)},
            files={
                "original": (
                    "report.pdf" if media_type == "application/pdf" else "report.png",
                    original,
                    media_type,
                )
            },
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
            "/v1/recall", json={"user_id": "scope", "query": "Glucose 92 mg/dL"}
        )
        assert recalled.status_code == 200, recalled.text
        item = recalled.json()["items"][0]
        assert item["origin"] == "evidence"
        assert item["citations"][0]["block"] == block
        assert expected_text in recalled.json()["context"]
        operation = await client.get(
            f"/v1/users/scope/operations/{uploaded.json()['operation_id']}"
        )
        assert operation.json()["readiness"]["source_searchable"] is True
        assert operation.json()["usage"]["total_tokens"] == (53 if uses_vision else 30)
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM insights").fetchone()[0] == 0
    assert ("extraction" in models.generation_calls) is uses_vision


async def test_structured_native_pdf_late_detail_reaches_fast_and_normal_context(
    core_database, tmp_path
):
    import yaml

    from oam.config import load_config
    from oam.contracts.resources import ResourceMediaType
    from oam.resources.originals import OriginalDescriptor
    from oam.resources.pdf import parse_pdf
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    values = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    values["chunk"]["ingestion"]["target_estimated_tokens"] = 512
    values["chunk"]["ingestion"]["max_estimated_tokens"] = 1024
    config_path = tmp_path / "pdf-chunks.yaml"
    config_path.write_text(yaml.safe_dump(values))
    config = load_config(config_path).config
    writer = PdfWriter()
    target = "If the seal is broken, retain the sealed backup and do not restart."
    lines = [
        "General safety procedure: preserve labels, commands, exceptions and exact units."
    ] * 120 + [target]
    for page_lines in (["Manual overview: use the current source."], lines):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        commands = ["BT /F1 4 Tf 5 TL 40 750 Td"]
        for index, line in enumerate(page_lines):
            if index:
                commands.append("T*")
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj")
        commands.append("ET")
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    original = output.getvalue()
    (tmp_path / "structured-native.pdf").write_bytes(original)
    models = SyntheticIndexModels()
    app = create_app(config_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": "structured-native"},
            data={
                "metadata": json.dumps(
                    {
                        "input_type": "document",
                        "document_id": "structured-native",
                        "update_mode": "replace",
                    }
                )
            },
            files={"original": ("manual.pdf", original, "application/pdf")},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="structured-native-worker",
        )
        app.state.runtime.models = models
        measurements = []
        for mode, query in (
            (mode, query)
            for mode in ("fast", "normal")
            for query in (
                "seal broken sealed backup restart",
                "What should I do when the seal is broken?",
            )
        ):
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": query,
                    "recall_level": mode,
                    "include": {"chunks": True},
                },
            )
            assert recalled.status_code == 200, recalled.text
            payload = recalled.json()
            assert target in payload["context"]
            assert payload["tokenizer"] == "o200k_base"
            assert payload["context_tokens"] == len(
                tiktoken.get_encoding("o200k_base").encode(
                    payload["context"], disallowed_special=()
                )
            )
            assert payload["context_tokens"] <= getattr(config.search, mode).context_max_tokens
            selected = next(item for item in payload["items"] if target in item["text"])
            citation = selected["citations"][0]
            assert selected["origin"] == "evidence"
            assert citation["document_id"] == "structured-native"
            assert citation["page"] == 2 and citation["parse_revision"] == 2
            measurements.append(
                {
                    "mode": mode,
                    "query": query,
                    "context_tokens": payload["context_tokens"],
                    "latency_seconds": recalled.elapsed.total_seconds(),
                    "citation": citation,
                    "context": payload["context"],
                }
            )
        (tmp_path / "context-results.json").write_text(
            json.dumps(measurements, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        operation = await client.get(
            f"/v1/users/scope/operations/{uploaded.json()['operation_id']}"
        )
        assert operation.json()["readiness"]["source_searchable"] is True
    parsed = parse_pdf(
        OriginalDescriptor.from_bytes(ResourceMediaType.PDF, original), config.resource
    )
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
        rows = connection.execute(
            "SELECT normalized_text,parse_revision,locator FROM passages WHERE tenant_id=%s",
            (tenant,),
        ).fetchall()
        assert len(rows) > 2
        for text, revision, locator in rows:
            assert revision == locator["parse_revision"] == 2
            page_text = parsed.pages[locator["page"] - 1].text
            assert page_text[locator["start"] : locator["end"]] == text
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM insights").fetchone()[0] == 0
    assert models.generation_calls == ["abstract"]


async def test_mixed_native_and_image_pdf_keeps_both_cited_details(core_database):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    original = (BILINGUAL / "en-mixed-native-image.pdf").read_bytes()
    assert hashlib.sha256(original).hexdigest() == (
        "9c298ebe62fc00b161334ad9d580793f8cdbf7b47d0d93942ee5e2e71fd9ac44"
    )
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels(
        vision_blocks=[
            {"text": "Sodium 140 mmol/L"},
            {"text": "Glucose 92 mg/dL"},
            {"text": "Potassium 4.2 mmol/L"},
        ]
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": "mixed-report"},
            data={
                "metadata": json.dumps(
                    {
                        "input_type": "document",
                        "document_id": "mixed-report",
                        "update_mode": "replace",
                    }
                )
            },
            files={"original": ("mixed.pdf", original, "application/pdf")},
        )
        assert accepted.status_code == 202, accepted.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="mixed-pdf-worker",
        )
        app.state.runtime.models = models
        for query, expected_text, method in (
            ("Sodium 140 mmol/L", "Sodium: 140 mmol/L", "native_text"),
            ("Glucose 92 mg/dL", "Glucose 92 mg/dL", "vision"),
            ("Potassium 4.2 mmol/L", "Potassium 4.2 mmol/L", "vision"),
        ):
            recalled = await client.post("/v1/recall", json={"user_id": "scope", "query": query})
            assert recalled.status_code == 200, recalled.text
            assert any(
                expected_text in item["text"]
                and item["citations"][0]["page"] == 1
                and item["citations"][0]["extraction_method"] == method
                for item in recalled.json()["items"]
            )
        operation = await client.get(
            f"/v1/users/scope/operations/{accepted.json()['operation_id']}"
        )
        assert operation.json()["readiness"]["source_searchable"] is True
        assert operation.json()["stage_state"]["parse_coverage"] == "complete"
    assert models.generation_calls == ["extraction", "abstract"]
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        methods = connection.execute(
            "SELECT locator->>'extraction_method' FROM search_units WHERE tenant_id=%s "
            "AND kind='resource_passage' ORDER BY locator->>'block'",
            (tenant,),
        ).fetchall()
        assert sorted(row[0] for row in methods) == ["native_text", "vision", "vision"]


async def test_failed_url_fetch_does_not_reserve_the_idempotency_key(core_database):
    from oam.resources.url_fetch import ResourceFetchUnavailable

    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    original = (BILINGUAL / "en-report.html").read_bytes()
    attempts = 0

    def fetch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ResourceFetchUnavailable("synthetic outage")
        return original

    request = {
        "input_type": "document_url",
        "document_id": "retry-report",
        "update_mode": "replace",
        "media_type": "text/html",
        "url": "https://reports.example.test/retry-report.html",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        app.state.runtime.url_fetcher = fetch
        unsafe = await client.post(
            "/v1/users/scope/resources",
            json={**request, "url": "https://127.0.0.1/private.pdf"},
            headers={"Idempotency-Key": "unsafe-report"},
        )
        assert unsafe.status_code == 422
        assert attempts == 0
        first = await client.post(
            "/v1/users/scope/resources", json=request, headers={"Idempotency-Key": "retry-report"}
        )
        assert first.status_code == 502
        assert first.json()["detail"]["code"] == "ResourceFetchUnavailable"
        second = await client.post(
            "/v1/users/scope/resources", json=request, headers={"Idempotency-Key": "retry-report"}
        )
        assert second.status_code == 202, second.text
    assert attempts == 2
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM operations WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("language", ("en", "zh"))
@pytest.mark.parametrize("kind", ("html", "native_pdf", "scanned_pdf", "image", "allergy_pdf"))
async def test_bilingual_report_public_retain_to_cited_recall(core_database, language, kind):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    manifest = json.loads((BILINGUAL / "manifest.json").read_text(encoding="utf-8"))
    original = (BILINGUAL / manifest["languages"][language][kind]["file"]).read_bytes()
    media_type = {
        "html": "text/html",
        "native_pdf": "application/pdf",
        "scanned_pdf": "application/pdf",
        "image": "image/png",
        "allergy_pdf": "application/pdf",
    }[kind]
    if kind == "allergy_pdf":
        term = "Horse dander" if language == "en" else "马皮屑"
    else:
        term = "Glucose 92 mg/dL" if language == "en" else "葡萄糖 92 mg/dL"
    models = SyntheticIndexModels(vision_text=term)
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    metadata = {
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
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": f"{language}-{kind}"},
            data={"metadata": json.dumps(metadata)},
            files={"original": (f"report.{kind}", original, media_type)},
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
        recalled = await client.post("/v1/recall", json={"user_id": "scope", "query": term})
        assert recalled.status_code == 200, recalled.text
        assert term in recalled.json()["context"]
        item = recalled.json()["items"][0]
        assert item["origin"] == "evidence"
        citation = item["citations"][0]
        assert citation["document_id"] == "report"
        if kind in {"native_pdf", "scanned_pdf"}:
            assert citation["page"] == 1
        if kind == "allergy_pdf":
            assert any(
                candidate["citations"][0]["page"] == 3 and term in candidate["text"]
                for candidate in recalled.json()["items"]
            )
        assert ("extraction" in models.generation_calls) is (kind in {"scanned_pdf", "image"})
        operation = await client.get(
            f"/v1/users/scope/operations/{uploaded.json()['operation_id']}"
        )
        assert operation.json()["readiness"]["source_searchable"] is True
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kind", "media_type"),
    (("html", "text/html"), ("native_pdf", "application/pdf"), ("image", "image/png")),
)
async def test_public_url_evidence_fetches_once_and_reuses_the_stored_original(
    core_database, kind, media_type
):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    entry = json.loads((BILINGUAL / "manifest.json").read_text())["languages"]["en"][kind]
    original = (BILINGUAL / entry["file"]).read_bytes()
    url = f"https://reports.example.test/{entry['file']}?sig=synthetic-private-token"
    calls = []

    def fetch(source_url, expected_media_type, *, max_bytes, timeout_seconds):
        calls.append((source_url, expected_media_type, max_bytes, timeout_seconds))
        assert source_url == url and expected_media_type == media_type
        return original

    request = {
        "input_type": "document_url",
        "document_id": "url-report",
        "update_mode": "replace",
        "media_type": media_type,
        "url": url,
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        app.state.runtime.url_fetcher = fetch
        denied = await client.post(
            "/v1/users/scope/resources", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert denied.status_code == 404
        assert calls == []
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/users/scope/resources", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert accepted.status_code == 202, (accepted.text, calls)
        repeated = await client.post(
            "/v1/users/scope/resources", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert repeated.status_code == 202 and repeated.json()["reused"] is True
        assert repeated.json()["operation_id"] == accepted.json()["operation_id"]
        changed = await client.post(
            "/v1/users/scope/resources",
            json={**request, "url": url.replace("sig=", "sig=changed-")},
            headers={"Idempotency-Key": "url-report"},
        )
        assert changed.status_code == 409
        assert len(calls) == 1
        assert calls[0][2:] == (
            config.resource.max_bytes,
            config.resource.url_fetch_timeout_seconds,
        )
        models = SyntheticIndexModels()
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="url-worker",
        )
        app.state.runtime.models = models
        recalled = await client.post(
            "/v1/recall", json={"user_id": "scope", "query": "Glucose 92 mg/dL"}
        )
        assert recalled.status_code == 200, recalled.text
        assert "92 mg/dL" in recalled.json()["context"]
        assert recalled.json()["items"][0]["citations"][0]["document_id"] == "url-report"
        assert "synthetic-private-token" not in recalled.text
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
        assert bytes(stored) == original
        envelopes = connection.execute(
            "SELECT content_text FROM evidence WHERE tenant_id=%s AND content_text LIKE %s",
            (tenant, '%"source_url"%'),
        ).fetchall()
        assert len(envelopes) == 1
        assert json.loads(envelopes[0][0])["source_url"] == url.split("?", 1)[0]


@pytest.mark.parametrize("failure_kind", ("invalid", "timeout"))
async def test_partial_pdf_page_requires_explicit_partial_recall(core_database, failure_kind):
    from oam.config import load_config
    from oam.models.runtime import ModelOutputError, Usage
    from oam.workers.index_source import process_one

    class FailedVision(SyntheticIndexModels):
        def generate_vision(self, image, **kwargs):
            if failure_kind == "timeout":
                raise httpx.ReadTimeout("synthetic page vision timeout")
            raise ModelOutputError(
                "synthetic page vision failure",
                usage=Usage(input_tokens=15, output_tokens=8, total_tokens=23, coverage="complete"),
            )

    dsn, _, headers = core_database
    writer = PdfWriter()
    writer.append(BytesIO((BILINGUAL / "en-native.pdf").read_bytes()))
    writer.append(BytesIO((BILINGUAL / "en-scanned.pdf").read_bytes()))
    output = BytesIO()
    writer.write(output)
    original = output.getvalue()
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = FailedVision()
    metadata = {
        "input_type": "document",
        "document_id": "partial-report",
        "update_mode": "replace",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": "partial-report"},
            data={"metadata": json.dumps(metadata)},
            files={"original": ("partial.pdf", original, "application/pdf")},
        )
        assert uploaded.status_code == 202, uploaded.text
        failure_type = ModelOutputError if failure_kind == "invalid" else httpx.ReadTimeout
        with pytest.raises(failure_type, match="synthetic page vision"):
            process_one(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=models,
                worker_id="resource-worker",
            )
        app.state.runtime.models = models
        request = {"user_id": "scope", "query": "Glucose 92 mg/dL"}
        assert (await client.post("/v1/recall", json=request)).status_code == 503
        recalled = await client.post("/v1/recall", json={**request, "allow_partial": True})
        assert recalled.status_code == 200, recalled.text
        assert "Glucose 92 mg/dL" in recalled.json()["context"]
        assert recalled.json()["diagnostics"]["partial"] is True
        assert recalled.json()["items"][0]["citations"][0]["page"] == 1
        operation = await client.get(
            f"/v1/users/scope/operations/{uploaded.json()['operation_id']}"
        )
        assert operation.json()["readiness"]["source_searchable"] is True
        assert operation.json()["stage_state"]["parse_coverage"] == "partial"
        assert operation.json()["usage"]["total_tokens"] == (
            23 if failure_kind == "invalid" else None
        )
        assert operation.json()["usage"]["coverage"] == (
            "complete" if failure_kind == "invalid" else "partial"
        )
        assert operation.json()["embedding_usage"]["total_tokens"] == 1
    with psycopg.connect(dsn) as connection:
        attempt = connection.execute(
            "SELECT disposition,request_ref,response_ref FROM attempts WHERE work_unit_id IN "
            "(SELECT work_unit_id FROM work_units WHERE tenant_id=%s)",
            (core_database[1],),
        ).fetchone()
        assert attempt == ("failed", "vision:pdf:page:0002", "vision:pdf:page:0002")
        work = connection.execute(
            "SELECT status,attempts_remaining FROM work_units WHERE tenant_id=%s "
            "AND operation_id=%s AND stage='index_source'",
            (core_database[1], uploaded.json()["operation_id"]),
        ).fetchone()
        assert work == ("failed_terminal" if failure_kind == "invalid" else "queued", 2)


async def test_chat_and_html_report_share_one_cited_recall_budget(core_database):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    chat = {
        "user_id": "scope",
        "input_type": "interaction",
        "messages": [
            {
                "role": "user",
                "content": "Lin prefers brief explanations.",
                "event_time": "2026-08-13T09:00:00Z",
                "learning_eligible": True,
            },
            {
                "role": "user",
                "content": "Lin prefers updates in the morning.",
                "event_time": "2026-08-14T09:00:00Z",
                "learning_eligible": True,
            },
            {
                "role": "assistant",
                "content": "The report says glucose is 9 mg/dL.",
                "event_time": "2026-08-14T10:00:00Z",
                "learning_eligible": False,
            },
        ],
    }
    report = (MEDICAL / "synthetic-lab-report.html").read_bytes()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        retained_chat = await client.post(
            "/v1/retain", json=chat, headers={"Idempotency-Key": "chat"}
        )
        assert retained_chat.status_code == 202, retained_chat.text
        assert process_source(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="chat-worker",
        )
        retained_report = await client.post(
            "/v1/users/scope/resources",
            headers={"Idempotency-Key": "report"},
            data={
                "metadata": json.dumps(
                    {
                        "input_type": "document",
                        "document_id": "synthetic-report",
                        "update_mode": "replace",
                    }
                )
            },
            files={"original": ("report.html", report, "text/html")},
        )
        assert retained_report.status_code == 202, retained_report.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="report-worker",
        )
        app.state.runtime.models = models
        generation_calls = list(models.generation_calls)
        for mode in ("fast", "normal"):
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "Lin brief glucose 92 mg/dL",
                    "recall_level": mode,
                    "include": {"chunks": True},
                },
            )
            assert recalled.status_code == 200, recalled.text
            payload = recalled.json()
            assert {item["origin"] for item in payload["items"]} == {"memory", "evidence"}
            assert any(
                item["origin"] == "evidence"
                and item["citations"][0]["document_id"] == "synthetic-report"
                for item in payload["items"]
            )
            assert "Lin prefers brief explanations" in payload["context"]
            assert "Glucose 92 mg/dL" in payload["context"]
            assert payload["tokenizer"] == "o200k_base"
            assert payload["context_tokens"] == len(
                tiktoken.get_encoding("o200k_base").encode(
                    payload["context"], disallowed_special=()
                )
            )
            assert payload["context_tokens"] <= getattr(config.search, mode).context_max_tokens
            assert payload["diagnostics"]["generative_calls"] == 0
        assert models.generation_calls == generation_calls
    with psycopg.connect(dsn) as connection:
        facts = connection.execute(
            "SELECT canonical_claim FROM memories WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert facts
        assert all("glucose" not in row[0].lower() for row in facts)


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
                "event_time": "2026-09-01T16:00:00Z",
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
        await use_created_user_key(client, "scope")
        receipt = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "chat"})
        assert receipt.status_code == 202
        assert (
            process_source(
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
            json={
                "user_id": "scope",
                "query": "Lin morning explanations",
            },
        )
        assert recalled.status_code == 200, recalled.text
        assert recalled.json()["diagnostics"]["readiness"] == (
            "partial" if invalid_fact else "ready"
        )
        assert {(item["kind"], item["text"]) for item in recalled.json()["items"]} == {
            ("fact", "Lin prefers short explanations."),
            ("fact", "Lin prefers updates in the morning."),
            ("passage", "Lin prefers short explanations."),
            ("passage", "Lin prefers updates in the morning."),
        }
        assert all(
            item["citations"][0].get("role", item["citations"][0].get("span", {}).get("role"))
            == "user"
            for item in recalled.json()["items"]
        )
        assert not any(item["kind"] == "insight" for item in recalled.json()["items"])
        assert "glucose" not in recalled.json()["context"]
        assert recalled.json()["context_tokens"] > 0
        dated = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "Lin morning preference",
                "recall_level": "fast",
                "time_basis": "mentioned",
                "time_window": {
                    "start": "2026-09-02T00:00:00Z",
                    "end": "2026-09-03T00:00:00Z",
                },
            },
        )
        assert dated.status_code == 200, dated.text
        assert any(
            item["kind"] == "fact" and "morning" in item["text"] for item in dated.json()["items"]
        )
        finished = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        assert finished.json()["status"] == "succeeded"
        assert finished.json()["readiness"]["memory_ready"] is True
        assert finished.json()["readiness"]["insights_ready"] is False
        assert finished.json()["readiness"]["links_ready"] is True
        assert finished.json()["readiness"]["temporal_links_ready"] is True
        assert finished.json()["stage_state"]["links_ready"] is True
        assert finished.json()["stage_state"]["temporal_links_ready"] is True
        assert [len(batch) for batch in models.embedding_batches[:2]] == [2, 2]
        with psycopg.connect(dsn) as connection:
            semantic_before = connection.execute(
                "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'",
                (tenant,),
            ).fetchone()[0]
            temporal_before = connection.execute(
                "SELECT count(*) FROM temporal_links WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            assert semantic_before == 2
            assert temporal_before >= 1
        consolidation_id = finished.json()["stage_state"]["consolidation_operation_id"]
        with psycopg.connect(dsn) as connection:
            due = connection.execute(
                "SELECT w.available_at>clock_timestamp(),w.status FROM work_units w "
                "WHERE w.tenant_id=%s AND w.operation_id=%s",
                (tenant, consolidation_id),
            ).fetchone()
            assert due == (True, "queued")
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() WHERE tenant_id=%s "
                "AND operation_id=%s",
                (tenant, consolidation_id),
            )
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
        recalled = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "Lin morning explanations",
            },
        )
        assert any(item["kind"] == "insight" for item in recalled.json()["items"])
        assert recalled.json()["diagnostics"]["readiness"] == (
            "partial" if invalid_fact else "ready"
        )
        completed = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        assert completed.json()["readiness"]["insights_ready"] is True
        consolidation = await client.get(f"/v1/users/scope/operations/{consolidation_id}")
        assert consolidation.json()["status"] == "succeeded"
        assert consolidation.json()["usage"]["total_tokens"] == 30
        assert consolidation.json()["embedding_usage"]["total_tokens"] == 1
        assert "link_coverage" not in consolidation.json()["stage_state"]
        assert finished.json()["usage"]["total_tokens"] == 30
        assert finished.json()["usage"]["coverage"] == "complete"
        assert finished.json()["embedding_usage"]["total_tokens"] == 4
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND kind='semantic'",
                    (tenant,),
                ).fetchone()[0]
                == semantic_before
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM temporal_links WHERE tenant_id=%s", (tenant,)
                ).fetchone()[0]
                == temporal_before
            )
        assert finished.json()["stage_state"]["extraction_coverage"] == (
            "partial" if invalid_fact else "complete"
        )
    with psycopg.connect(dsn) as connection:
        facts = connection.execute(
            "SELECT canonical_claim,category FROM memories WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert set(facts) == {
            ("Lin prefers short explanations.", "preference"),
            ("Lin prefers updates in the morning.", "preference"),
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
    assert models.generation_calls == ["extraction", "insights"]


def insight_reply(envelope, result):
    """Model double for ordinary actions and the separate complete-support repair call."""
    result = {**result, "title": result.get("title", "Supported subject")}
    if "seed_ids" not in envelope:
        return result
    return {
        "actions": [
            {
                "action": "CREATE",
                "title": result["title"],
                "target_id": None,
                "text": result["text"],
                "retrieval_text": result.get("retrieval_text"),
                "subtype": result["subtype"],
                "support_ids": result["support_ids"],
                "seed_ids": envelope["seed_ids"],
            }
        ]
    }


def synthetic_profile_insight(user):
    from oam.models.runtime import ModelResult, Usage

    envelope = json.loads(user)
    facts = envelope["facts"]
    return ModelResult(
        content=json.dumps(
            insight_reply(
                envelope,
                {
                    "outcome": "insight",
                    "subtype": "general_insights",
                    "text": " ".join(fact["text"] for fact in facts),
                    "support_ids": [fact["id"] for fact in facts],
                },
            )
        ),
        usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"),
    )


async def test_profile_refresh_and_recall_remain_independent(core_database, tmp_path, monkeypatch):
    from oam.config import load_config
    from oam.exports.validation import validate_directory
    from oam.models.runtime import ModelResult, Usage
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class ProfileModels(SyntheticIndexModels):
        fail_profile = False
        timeout_profile = False

        def generate(self, role, **kwargs):
            if role == "insights":
                self.generation_calls.append(role)
                return synthetic_profile_insight(kwargs["user"])
            if role != "profile":
                return super().generate(role, **kwargs)
            assert kwargs["max_output_tokens"] == config.profile.max_output_tokens
            assert kwargs["timeout_seconds"] == config.profile.attempt_timeout_seconds
            self.generation_calls.append(role)
            if self.timeout_profile:
                raise httpx.ReadTimeout("synthetic uncertain Profile response")
            prompt = json.loads(kwargs["user"])
            assert "glucose 9" not in json.dumps(prompt).lower()
            assert "report-only" not in json.dumps(prompt).lower()
            assert "1990-05-10" not in json.dumps(prompt["inputs"])
            birth_date = next(
                field["value"]
                for field in prompt["exact_fields"]
                if field["key"] == "date_of_birth"
            )
            assert all(item["kind"] in {"fact", "insight"} for item in prompt["inputs"])
            insight = next(
                item for item in prompt["inputs"] if "brief explanations" in item["text"]
            )
            support_id = "report-only" if self.fail_profile else insight["id"]
            body = (
                json.dumps(
                    {"summary": "The user prefers brief replies.", "date_of_birth": birth_date}
                )
                if prompt["instruction"]
                else "The user prefers brief replies. Profile-only portrait [1]."
            )
            return ModelResult(
                content=json.dumps(
                    {
                        "content": body,
                        "support_ids": [support_id],
                    }
                ),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = ProfileModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        root_invalid_resource = await client.post(
            "/v1/users/scope/profile-data",
            json={
                "profile_data_id": "unauthorized",
                "subtype": "basic-profile/v1",
                "content": {"unknown": "value"},
            },
            headers={**headers, "Idempotency-Key": "unauthorized-profile"},
        )
        assert root_invalid_resource.status_code == 404
        chat = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prefer brief explanations.",
                        "event_time": "2026-09-01T09:00:00Z",
                        "learning_eligible": True,
                    },
                    {
                        "role": "user",
                        "content": "I prefer short examples.",
                        "event_time": "2026-09-01T09:01:00Z",
                        "learning_eligible": True,
                    },
                    {
                        "role": "assistant",
                        "content": "The report says glucose 9.",
                        "event_time": "2026-09-01T09:02:00Z",
                        "learning_eligible": False,
                    },
                ],
            },
            headers={"Idempotency-Key": "profile-chat"},
        )
        assert chat.status_code == 202, chat.text
        assert process_source(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-chat"
        )
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() WHERE stage='consolidate'"
            )
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="profile-l2")
        report = await client.post(
            "/v1/users/scope/resources",
            json={
                "input_type": "document",
                "document_id": "report-only",
                "update_mode": "replace",
                "text": "Report-only glucose 9 mg/dL.",
            },
            headers={"Idempotency-Key": "profile-report"},
        )
        assert report.status_code == 202, report.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-report"
        )
        resource = await client.post(
            "/v1/users/scope/profile-data",
            json={
                "profile_data_id": "basic",
                "subtype": "basic-profile/v1",
                "content": {"date_of_birth": "1990-05-10"},
            },
            headers={"Idempotency-Key": "profile-basic"},
        )
        assert resource.status_code == 202, resource.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-resource"
        )
        before = await client.get("/v1/users/scope/profile")
        assert before.status_code == 200, before.text
        assert before.json()["freshness"] != "current"
        requested = await client.post("/v1/users/scope/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-refresh"
        )
        stored = await client.get("/v1/users/scope/profile")
        assert stored.status_code == 200, stored.text
        payload = stored.json()
        assert payload["freshness"] == "current"
        assert datetime.fromisoformat(payload["updated_at"]).tzinfo is not None
        assert "1990-05-10" in payload["content"]
        assert "brief" in payload["content"]
        exported_profile = await client.post("/v1/users/scope/exports")
        assert exported_profile.status_code == 202, exported_profile.text
        export_id = exported_profile.json()["export_id"]
        profile_manifest = (await client.get(f"/v1/users/scope/exports/{export_id}")).json()
        assert profile_manifest["counts"]["profile"] == 1
        (tmp_path / "manifest.json").write_text(json.dumps(profile_manifest), encoding="utf-8")
        exported_records = []
        for part in profile_manifest["parts"]:
            response = await client.get(
                f"/v1/users/scope/exports/{export_id}/records/{part['index']}"
            )
            assert response.status_code == 200, response.text
            destination = tmp_path / part["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
            exported_records.extend(json.loads(line) for line in response.content.splitlines())
        for original in profile_manifest["originals"]:
            response = await client.get(
                f"/v1/users/scope/exports/{export_id}/originals/{original['index']}"
            )
            assert response.status_code == 200, response.text
            destination = tmp_path / original["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
        assert validate_directory(tmp_path)["profile"] == 1
        l3_record = next(record for record in exported_records if record["kind"] == "profile")
        assert "brief" in l3_record["content"]
        calls = len(models.generation_calls)
        unchanged = await client.post("/v1/users/scope/profile/refresh", json={})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["generation_calls"] == 0
        assert len(models.generation_calls) == calls
        app.state.runtime.models = models
        for path, body in (
            ("/v1/recall", {"user_id": "scope", "query": "brief explanations"}),
            (
                "/v1/users/scope/search-many",
                {"queries": ["brief explanations"]},
            ),
            ("/v1/users/scope/decompose-and-search", {"query": "brief explanations"}),
        ):
            for selection in (True, False):
                rejected = await client.post(path, json={**body, "include_profile": selection})
                assert rejected.status_code == 422, rejected.text
                assert any(
                    error["loc"] == ["body", "include_profile"]
                    and error["type"] == "extra_forbidden"
                    for error in rejected.json()["detail"]
                )
        recalled = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "brief explanations",
            },
        )
        assert recalled.status_code == 200, recalled.text
        assert "profile" not in recalled.json()
        assert "brief explanations" in recalled.json()["context"]
        assert "Profile-only portrait" not in recalled.json()["context"]
        assert "1990-05-10" not in recalled.json()["context"]
        assert recalled.json()["context_tokens"] <= config.search.normal.context_max_tokens
        assert recalled.json()["diagnostics"]["generative_calls"] == 0
        assert len(models.generation_calls) == calls
        assert "Profile-only portrait" in payload["content"]
        assert payload["generation_calls"] == 0
        over_budget = await client.get("/v1/users/scope/profile?max_tokens=0")
        assert over_budget.status_code == 200, over_budget.text
        assert over_budget.json()["content"] is None
        assert {"reason": "budget"} in over_budget.json()["omitted"]

        from oam.profiles.service import ProfileService

        def no_profile_read(*args, **kwargs):
            raise AssertionError("Recall must not read the stored Profile")

        with monkeypatch.context() as separation:
            separation.setattr(ProfileService, "get", no_profile_read)
            for enabled in (True, False):
                app.state.runtime.config = config.model_copy(
                    update={"profile": config.profile.model_copy(update={"enabled": enabled})}
                )
                independent = await client.post(
                    "/v1/recall",
                    json={
                        "user_id": "scope",
                        "query": "brief explanations",
                        "recall_level": "fast",
                    },
                )
                assert independent.status_code == 200, independent.text
                assert "brief explanations" in independent.json()["context"]
                assert "profile" not in independent.json()
                assert "Profile-only portrait" not in independent.json()["context"]
                assert independent.json()["context_tokens"] <= config.search.fast.context_max_tokens
                assert independent.json()["diagnostics"]["generative_calls"] == 0
                batch = await client.post(
                    "/v1/users/scope/search-many",
                    json={"queries": ["brief explanations", "short examples"]},
                )
                assert batch.status_code == 200, batch.text
                assert all(row["status"] == "ok" for row in batch.json()["results"])
                assert all("profile" not in row["result"] for row in batch.json()["results"])
                if not enabled:
                    disabled_read = await client.get("/v1/users/scope/profile")
                    disabled_refresh = await client.post("/v1/users/scope/profile/refresh", json={})
                    for response in (disabled_read, disabled_refresh):
                        assert response.status_code == 503, response.text
                        assert response.json()["detail"]["code"] == "CapabilityUnavailable"
            app.state.runtime.config = config
        assert len(models.generation_calls) == calls

        superseded = await client.post(
            "/v1/users/scope/profile/refresh",
            json={"instruction": "Return one JSON object with summary and date_of_birth."},
        )
        assert superseded.status_code == 202, superseded.text
        reset_before_generation = await client.post(
            "/v1/users/scope/profile/refresh", json={"reset_instruction": True}
        )
        assert reset_before_generation.status_code == 200, reset_before_generation.text
        cancelled = await client.get(
            f"/v1/users/scope/operations/{superseded.json()['operation_id']}"
        )
        assert cancelled.json()["status"] == "cancelled"
        custom = await client.post(
            "/v1/users/scope/profile/refresh",
            json={"instruction": "Return one JSON object with summary and date_of_birth."},
        )
        assert custom.status_code == 202, custom.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-custom"
        )
        custom_view = (await client.get("/v1/users/scope/profile")).json()
        assert json.loads(custom_view["content"])["date_of_birth"] == "1990-05-10"
        assert (
            custom_view["instruction"] == "Return one JSON object with summary and date_of_birth."
        )
        assert (await client.post("/v1/users/scope/profile/refresh", json={})).status_code == 200
        reset = await client.post(
            "/v1/users/scope/profile/refresh", json={"reset_instruction": True}
        )
        assert reset.status_code == 202, reset.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-default"
        )
        assert (
            (await client.get("/v1/users/scope/profile"))
            .json()["content"]
            .startswith("## Main content")
        )

        revised_body = {
            "profile_data_id": "basic",
            "subtype": "basic-profile/v1",
            "content": {"date_of_birth": "1991-05-10"},
        }
        wrong_revision = await client.put(
            "/v1/users/scope/profile-data/basic",
            json=revised_body,
            headers={"Idempotency-Key": "profile-basic-wrong", "If-Match": "9"},
        )
        assert wrong_revision.status_code == 409
        revised = await client.put(
            "/v1/users/scope/profile-data/basic",
            json=revised_body,
            headers={"Idempotency-Key": "profile-basic-v2", "If-Match": "1"},
        )
        assert revised.status_code == 202, revised.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-resource-v2"
        )
        dirty = await client.get("/v1/users/scope/profile")
        assert dirty.json()["freshness"] == "stale"
        assert dirty.json()["content"] is None
        models.fail_profile = True
        failed = await client.post("/v1/users/scope/profile/refresh", json={})
        assert failed.status_code == 202
        with pytest.raises(ValueError, match="unsupported"):
            process_one(config, queue_dsn, content_dsn, models=models, worker_id="profile-failed")
        preserved = await client.get("/v1/users/scope/profile")
        assert preserved.json()["content"] is None
        models.fail_profile = False
        retry = await client.post("/v1/users/scope/profile/refresh", json={})
        assert retry.status_code == 202
        assert retry.json()["operation_id"] != failed.json()["operation_id"]
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="profile-retry")
        assert (await client.get("/v1/users/scope/profile")).json()["freshness"] == "current"
        evidence_only = await client.post(
            "/v1/users/scope/resources",
            json={
                "input_type": "document",
                "document_id": "second-report",
                "update_mode": "replace",
                "text": "Independent report evidence.",
            },
            headers={"Idempotency-Key": "second-report"},
        )
        assert evidence_only.status_code == 202
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="second-report")
        assert (await client.get("/v1/users/scope/profile")).json()["freshness"] == "current"
        profile_calls_before_new_chat = models.generation_calls.count("profile")
        more_chat = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I use Python for small data checks.",
                        "event_time": "2026-09-02T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "more-profile-chat"},
        )
        assert more_chat.status_code == 202
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="more-chat")
        pending = await client.get("/v1/users/scope/profile")
        assert pending.json()["freshness"] == "dirty"
        assert models.generation_calls.count("profile") == profile_calls_before_new_chat
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() "
                "WHERE stage='consolidate' AND status='queued'"
            )
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="more-l2")
        after_l2 = await client.get("/v1/users/scope/profile")
        assert after_l2.json()["freshness"] == "dirty"
        assert models.generation_calls.count("profile") == profile_calls_before_new_chat
        changed = await client.post("/v1/users/scope/profile/refresh", json={})
        assert changed.status_code == 202, changed.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-changed-inputs"
        )
        changed_operation = await client.get(
            f"/v1/users/scope/operations/{changed.json()['operation_id']}"
        )
        assert changed_operation.json()["status"] == "succeeded"
        updated = await client.get("/v1/users/scope/profile")
        assert updated.status_code == 200, updated.text
        updated_profile = updated.json()
        assert updated_profile["freshness"] == (
            "uncovered" if updated_profile["uncovered_inputs"] else "current"
        )
        coverage = updated_profile["selection_coverage"]
        assert updated_profile["uncovered_inputs"] == (
            coverage["unexamined"] + coverage["budget_omitted"]
        )
        assert updated.json()["profile_revision"] > after_l2.json()["profile_revision"]
        assert "brief" in updated.json()["content"]
        assert updated.json()["generation_calls"] == 0
        deleted_chat = await client.delete(f"/v1/users/scope/evidence/{chat.json()['source_id']}")
        assert deleted_chat.status_code == 202, deleted_chat.text
        assert deleted_chat.json()["status"] == "excluded"
        assert (await client.get(f"/v1/users/scope/exports/{export_id}")).status_code == 409
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="chat-cleanup")
        withdrawn = await client.get("/v1/users/scope/profile")
        assert withdrawn.status_code == 200
        assert withdrawn.json()["freshness"] == "stale"
        assert withdrawn.json()["content"] is None
        after_delete_export = await client.post("/v1/users/scope/exports")
        assert after_delete_export.status_code == 202, after_delete_export.text
        new_export_id = after_delete_export.json()["export_id"]
        new_manifest = (await client.get(f"/v1/users/scope/exports/{new_export_id}")).json()
        new_profiles = [
            record
            for part in new_manifest["parts"]
            for record in (
                json.loads(line)
                for line in (
                    await client.get(
                        f"/v1/users/scope/exports/{new_export_id}/records/{part['index']}"
                    )
                ).content.splitlines()
            )
            if record["kind"] == "profile"
        ]
        assert len(new_profiles) == 1
        assert new_profiles[0]["content"] is None
        timeout_resource = await client.put(
            "/v1/users/scope/profile-data/basic",
            json={
                **revised_body,
                "content": {"date_of_birth": "1992-05-10"},
            },
            headers={"Idempotency-Key": "profile-basic-v3", "If-Match": "2"},
        )
        assert timeout_resource.status_code == 202
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="profile-v3")
        timeout_instruction = {"instruction": "Return one JSON object with date_of_birth."}
        uncertain = await client.post("/v1/users/scope/profile/refresh", json=timeout_instruction)
        assert uncertain.status_code == 202
        models.timeout_profile = True
        with pytest.raises(httpx.ReadTimeout):
            process_one(config, queue_dsn, content_dsn, models=models, worker_id="profile-timeout")
        again = await client.post("/v1/users/scope/profile/refresh", json=timeout_instruction)
        assert again.status_code == 409


async def test_empty_user_profile_refresh_publishes_without_a_model_call(core_database):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = SyntheticIndexModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "new-user")
        requested = await client.post("/v1/users/new-user/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="empty-profile",
        )
        profile = await client.get("/v1/users/new-user/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["freshness"] == "current"
        assert profile.json()["content"].startswith("## Main content")
        assert models.generation_calls == []


async def test_profile_accepts_l1_when_insights_are_disabled(core_database, tmp_path):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config_path = tmp_path / "profile-without-insights.yaml"
    config_path.write_text("index:\n  insights_enabled: false\n")
    config = load_config(config_path).config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class ProfileModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role != "profile":
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            fact = next(
                item for item in json.loads(kwargs["user"])["inputs"] if item["kind"] == "fact"
            )
            return ModelResult(
                content=json.dumps(
                    {"content": "The user prefers English [1].", "support_ids": [fact["id"]]}
                ),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = ProfileModels()
    app = create_app(config_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "profile-l1")
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "profile-l1",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "Please reply in English.",
                        "event_time": "2026-10-01T00:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "l1-only"},
        )
        assert retained.status_code == 202
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="l1-only")
        requested = await client.post("/v1/users/profile-l1/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="l1-profile")
        profile = await client.get("/v1/users/profile-l1/profile")
        assert "English" in profile.json()["content"]
        assert profile.json()["references"][0]["kind"] == "fact"
        unchanged = await client.post("/v1/users/profile-l1/profile/refresh", json={})
        assert unchanged.status_code == 200 and unchanged.json()["generation_calls"] == 0


@pytest.mark.parametrize("edit_prefix", ["- ", ""])
async def test_profile_full_and_incremental_refresh_keep_supported_claims(
    core_database, tmp_path, edit_prefix
):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config_path = tmp_path / "profile-refresh-modes.yaml"
    config_path.write_text("index:\n  insights_enabled: false\n")
    config = load_config(config_path).config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class ProfileModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role != "profile":
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            inputs = json.loads(kwargs["user"])["inputs"]
            if "Existing blocks:" in kwargs["system"]:
                assert len(inputs) == 1
                output = {
                    "no_change": False,
                    "edits": [
                        {
                            "action": "add",
                            "text": edit_prefix + "The user studies memory systems [1].",
                            "support_ids": [inputs[0]["id"]],
                        }
                    ],
                }
            else:
                output = {
                    "content": "\n".join(
                        f"- {item['text']} [{i}]." for i, item in enumerate(inputs, 1)
                    ),
                    "support_ids": [item["id"] for item in inputs],
                }
            return ModelResult(
                content=json.dumps(output),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = ProfileModels()
    app = create_app(config_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "profile-modes")

        async def retain(text, key):
            response = await client.post(
                "/v1/retain",
                json={
                    "user_id": "profile-modes",
                    "input_type": "interaction",
                    "messages": [
                        {
                            "role": "user",
                            "content": text,
                            "event_time": "2026-10-01T00:00:00Z",
                            "learning_eligible": True,
                        }
                    ],
                },
                headers={"Idempotency-Key": key},
            )
            assert response.status_code == 202, response.text
            assert process_source(config, queue_dsn, content_dsn, models=models, worker_id=key)

        await retain("The user prefers English", "first-source")
        requested = await client.post(
            "/v1/users/profile-modes/profile/refresh", json={"mode": "full"}
        )
        assert requested.status_code == 202, requested.text
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="full-profile")
        initial = (await client.get("/v1/users/profile-modes/profile")).json()
        assert "prefers English" in initial["content"]
        await retain("The user studies memory systems", "second-source")
        requested = await client.post(
            "/v1/users/profile-modes/profile/refresh", json={"mode": "incremental"}
        )
        assert requested.status_code == 202, requested.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="incremental-profile"
        )
        updated = (await client.get("/v1/users/profile-modes/profile")).json()
        assert "prefers English" in updated["content"]
        assert "\n- The user studies memory systems [2]." in updated["content"]
        assert len(updated["references"]) == 2
        operation = (
            await client.get(
                f"/v1/users/profile-modes/operations/{requested.json()['operation_id']}"
            )
        ).json()
        assert operation["stage_state"]["mode"] == "incremental"
        assert operation["status"] == "succeeded"
        assert models.generation_calls.count("profile") == 2
        unchanged = await client.post("/v1/users/profile-modes/profile/refresh", json={})
        assert unchanged.status_code == 200
        assert models.generation_calls.count("profile") == 2
        forced = await client.post("/v1/users/profile-modes/profile/refresh", json={"mode": "full"})
        assert forced.status_code == 202, forced.text
        assert process_one(
            config, queue_dsn, content_dsn, models=models, worker_id="forced-profile"
        )


async def test_profile_rejects_source_revision_changed_during_generation(core_database, tmp_path):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.profiles.worker import StaleProfileWork
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config_path = tmp_path / "profile-source-revision.yaml"
    config_path.write_text("index:\n  insights_enabled: false\n")
    config = load_config(config_path).config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")
    source_id = None

    class ProfileModels(SyntheticIndexModels):
        change_source = False

        def generate(self, role, **kwargs):
            if role != "profile":
                return super().generate(role, **kwargs)
            fact = next(
                item for item in json.loads(kwargs["user"])["inputs"] if item["kind"] == "fact"
            )
            if self.change_source:
                with psycopg.connect(dsn) as connection:
                    connection.execute(
                        "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,"
                        "media_type,original_bytes,content_sha256,byte_length,status) "
                        "SELECT tenant_id,user_id,source_id,2,media_type,original_bytes,"
                        "content_sha256,"
                        "byte_length,'active' FROM source_versions WHERE tenant_id=%s AND "
                        "user_id='profile-race' AND source_id=%s AND source_version=1",
                        (tenant, source_id),
                    )
                    connection.execute(
                        "UPDATE source_versions SET status='superseded' WHERE tenant_id=%s AND "
                        "user_id='profile-race' AND source_id=%s AND source_version=1",
                        (tenant, source_id),
                    )
                    connection.execute(
                        "UPDATE sources SET active_version=2 WHERE tenant_id=%s AND "
                        "user_id='profile-race' AND source_id=%s",
                        (tenant, source_id),
                    )
            return ModelResult(
                content=json.dumps({"content": "English [1]", "support_ids": [fact["id"]]}),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = ProfileModels()
    app = create_app(config_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "profile-race")
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "profile-race",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "Please reply in English.",
                        "event_time": "2026-10-01T00:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "profile-race"},
        )
        assert retained.status_code == 202
        source_id = retained.json()["source_id"]
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="race-input")
        first = await client.post("/v1/users/profile-race/profile/refresh", json={})
        assert first.status_code == 202
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="race-first")
        models.change_source = True
        second = await client.post(
            "/v1/users/profile-race/profile/refresh", json={"instruction": "Use bullets"}
        )
        assert second.status_code == 202
        with pytest.raises(StaleProfileWork):
            process_one(config, queue_dsn, content_dsn, models=models, worker_id="race-second")
        profile = await client.get("/v1/users/profile-race/profile")
        assert profile.json()["content"] is None
        with psycopg.connect(dsn) as connection:
            count = connection.execute(
                "SELECT count(*) FROM profiles WHERE tenant_id=%s AND user_id='profile-race'",
                (tenant,),
            ).fetchone()[0]
        assert count == 1


@pytest.mark.parametrize("first_running", [False, True])
async def test_new_interaction_gets_a_new_profile_refresh_while_one_is_active(
    core_database, first_running
):
    from oam.config import load_config
    from oam.workers.claims import claim_one
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")
    models = SyntheticIndexModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        original = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I like concise answers.",
                        "event_time": "2026-09-28T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "original-profile-input"},
        )
        assert original.status_code == 202, original.text
        assert process_source(
            config, queue_dsn, content_dsn, models=models, worker_id="original-input"
        )
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() WHERE stage='consolidate'"
            )
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="original-l2")
        first = await client.post("/v1/users/scope/profile/refresh", json={})
        assert first.status_code == 202, first.text
        if first_running:
            with psycopg.connect(queue_dsn) as connection:
                claim = claim_one(
                    connection.cursor(),
                    worker_id="first-profile",
                    attempt_id="first-profile-attempt",
                    lease_seconds=90,
                    user_concurrency=2,
                )
            assert claim is not None and claim.stage == "refresh_profile"
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE attempts SET disposition='unknown_external_outcome' "
                    "WHERE tenant_id=%s AND user_id='scope' AND attempt_id=%s",
                    (tenant, claim.attempt_id),
                )
                connection.execute(
                    "UPDATE operations SET status='running' WHERE tenant_id=%s "
                    "AND user_id='scope' AND operation_id=%s",
                    (tenant, claim.operation_id),
                )
        else:
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE work_units SET available_at=clock_timestamp()+interval '10 minutes' "
                    "WHERE tenant_id=%s AND user_id='scope' AND operation_id=%s",
                    (tenant, first.json()["operation_id"]),
                )
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prefer clear examples.",
                        "event_time": "2026-09-29T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "new-profile-input"},
        )
        assert retained.status_code == 202, retained.text
        before_indexing = await client.post("/v1/users/scope/profile/refresh", json={})
        assert before_indexing.status_code == 409, before_indexing.text
        assert before_indexing.json()["detail"]["code"] == "ProfileInputsNotReady"
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="new-input")
        second = await client.post("/v1/users/scope/profile/refresh", json={})
        assert second.status_code == 202, second.text
        assert second.json()["operation_id"] != first.json()["operation_id"]
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT w.dependency_ids FROM work_units w WHERE w.tenant_id=%s "
                "AND w.user_id='scope' AND w.operation_id=%s",
                (tenant, second.json()["operation_id"]),
            ).fetchone()
            assert row is not None and len(row[0]) == 1


async def test_waiting_profile_refresh_does_not_absorb_a_new_processed_source(core_database):
    from oam.config import load_config
    from oam.profiles.worker import StaleProfileWork
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")
    models = SyntheticIndexModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")

        async def retain_two(label):
            response = await client.post(
                "/v1/retain",
                json={
                    "user_id": "scope",
                    "input_type": "interaction",
                    "messages": [
                        {
                            "role": "user",
                            "content": f"{label} preference {index}.",
                            "event_time": f"2026-09-29T09:0{index}:00Z",
                            "learning_eligible": True,
                        }
                        for index in range(2)
                    ],
                },
                headers={"Idempotency-Key": f"source-{label}"},
            )
            assert response.status_code == 202, response.text
            return response

        await retain_two("A")
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="source-a")
        refresh_a = await client.post("/v1/users/scope/profile/refresh", json={})
        assert refresh_a.status_code == 202, refresh_a.text
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp()+interval '10 minutes' "
                "WHERE tenant_id=%s AND user_id='scope' AND operation_id=%s",
                (tenant, refresh_a.json()["operation_id"]),
            )
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="insight-a")

        await retain_two("B")
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="source-b")
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() "
                "WHERE stage='consolidate' AND status='queued'"
            )
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="insight-b")
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id='scope' AND operation_id=%s",
                (tenant, refresh_a.json()["operation_id"]),
            )
        with pytest.raises(StaleProfileWork, match="scope"):
            process_one(config, queue_dsn, content_dsn, models=models, worker_id="old-profile")
        assert "profile" not in models.generation_calls


async def test_failed_initial_indexing_keeps_profile_dirty_without_partial(core_database):
    from oam.config import load_config
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    models = SyntheticIndexModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        initial = await client.post("/v1/users/scope/profile/refresh", json={})
        assert initial.status_code == 202, initial.text
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="empty-profile",
        )
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I maintain the community archive.",
                        "event_time": "2026-09-29T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "failed-first-stage"},
        )
        assert retained.status_code == 202, retained.text
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET status='failed_terminal' WHERE tenant_id=%s "
                "AND user_id='scope' AND operation_id=%s",
                (tenant, retained.json()["operation_id"]),
            )
            connection.execute(
                "UPDATE operations SET status='partial_failed' WHERE tenant_id=%s "
                "AND user_id='scope' AND operation_id=%s",
                (tenant, retained.json()["operation_id"]),
            )
        profile = await client.get("/v1/users/scope/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["freshness"] == "dirty"
        assert profile.json()["pending_inputs"] == 1
        assert profile.json()["blocked_inputs"] == 1
        assert profile.json()["failed_inputs"] == 0
        retry = await client.post("/v1/users/scope/profile/refresh", json={})
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"]["code"] == "ProfileInputsNotReady"


@pytest.mark.parametrize("insight_failure", [False, True])
async def test_profile_refresh_runs_pending_insights_before_building_profile(
    core_database, insight_failure
):
    from oam.config import load_config
    from oam.indexing.identity import index_generation
    from oam.models.runtime import ModelResult, Usage
    from oam.profiles.service import profile_identity
    from oam.profiles.worker import process_profile_claim
    from oam.workers.claims import claim_one
    from oam.workers.index_source import process_one

    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class ProfileModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role == "insights" and insight_failure:
                self.generation_calls.append(role)
                raise ValueError("synthetic Insight failure")
            if role != "profile":
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            prompt_inputs = json.loads(kwargs["user"])["inputs"]
            support = next(
                item
                for item in prompt_inputs
                if item["kind"] == ("fact" if insight_failure else "insight")
            )
            return ModelResult(
                content=json.dumps(
                    {
                        "content": "The user prefers brief replies [1].",
                        "support_ids": [support["id"]],
                    }
                ),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = ProfileModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "Please keep replies brief.",
                        "event_time": "2026-09-29T09:00:00Z",
                        "learning_eligible": True,
                    },
                    {
                        "role": "user",
                        "content": "I also like clear examples.",
                        "event_time": "2026-09-29T09:01:00Z",
                        "learning_eligible": True,
                    },
                ],
            },
            headers={"Idempotency-Key": "pending-profile-input"},
        )
        assert retained.status_code == 202, retained.text
        assert process_source(
            config, queue_dsn, content_dsn, models=models, worker_id="profile-input"
        )
        if insight_failure:
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE work_units SET attempts_remaining=1 WHERE operation_id IN "
                    "(SELECT operation_id FROM operations WHERE tenant_id=%s "
                    "AND user_id='scope' AND kind='consolidate')",
                    (tenant,),
                )
        before = await client.get("/v1/users/scope/profile")
        assert before.status_code == 200, before.text
        assert before.json()["pending_inputs"] == 1
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,"
                "sections,content,"
                "input_identity,policy_epoch,generation_id,visible_from) "
                "VALUES (%s,'scope','user',1,'{}'::jsonb,%s,%s,%s,%s,"
                "(SELECT published_seq+1 FROM users WHERE tenant_id=%s AND user_id='scope'))",
                (
                    tenant,
                    "## Main content\n\nOld\n\n## References\n\nNo references yet.",
                    bytes.fromhex(before.json()["input_identity"]),
                    before.json()["policy_epoch"],
                    index_generation(config, os.environ),
                    tenant,
                ),
            )
            connection.execute(
                "UPDATE users SET published_seq=published_seq+1 "
                "WHERE tenant_id=%s AND user_id='scope'",
                (tenant,),
            )
        waiting = await client.get("/v1/users/scope/profile")
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["freshness"] == "dirty"

        requested = await client.post("/v1/users/scope/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        again = await client.post("/v1/users/scope/profile/refresh", json={})
        assert again.json()["operation_id"] == requested.json()["operation_id"]
        if insight_failure:
            with pytest.raises(ValueError, match="synthetic Insight failure"):
                process_one(
                    config, queue_dsn, content_dsn, models=models, worker_id="profile-l2-first"
                )
        else:
            assert process_one(
                config, queue_dsn, content_dsn, models=models, worker_id="profile-l2-first"
            )
        assert "insights" in models.generation_calls
        assert "profile" not in models.generation_calls
        after_insight = await client.post("/v1/users/scope/profile/refresh", json={})
        assert after_insight.status_code == 202, after_insight.text
        assert after_insight.json()["operation_id"] == requested.json()["operation_id"]
        if insight_failure:
            assert process_one(
                config, queue_dsn, content_dsn, models=models, worker_id="profile-next"
            )
        else:
            with psycopg.connect(queue_dsn) as connection:
                claim = claim_one(
                    connection.cursor(),
                    worker_id="profile-next",
                    attempt_id="profile-running-attempt",
                    lease_seconds=90,
                    user_concurrency=2,
                )
            assert claim is not None and claim.stage == "refresh_profile"
            current = await client.get("/v1/users/scope/profile")
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE work_units SET input_digest=%s,dependency_ids='{}' "
                    "WHERE tenant_id=%s AND user_id='scope' AND work_unit_id=%s",
                    (
                        profile_identity(
                            bytes.fromhex(current.json()["input_identity"]),
                            None,
                            config.profile.max_input_tokens,
                            (
                                config.models.embedding.query_prefix
                                if config.models.embedding
                                else ""
                            ),
                            "o200k_base",
                            config.profile.max_memory_tokens,
                        ),
                        tenant,
                        claim.work_unit_id,
                    ),
                )
                connection.execute(
                    "UPDATE attempts SET disposition='unknown_external_outcome' "
                    "WHERE tenant_id=%s AND user_id='scope' AND attempt_id=%s",
                    (tenant, claim.attempt_id),
                )
                connection.execute(
                    "UPDATE operations SET status='running' WHERE tenant_id=%s "
                    "AND user_id='scope' AND operation_id=%s",
                    (tenant, claim.operation_id),
                )
            while_running = await client.post("/v1/users/scope/profile/refresh", json={})
            assert while_running.status_code == 202, while_running.text
            assert while_running.json()["operation_id"] == requested.json()["operation_id"]
            process_profile_claim(
                config, content_dsn, models=models, claim=claim, worker_id="profile-next"
            )
        operation = await client.get(
            f"/v1/users/scope/operations/{requested.json()['operation_id']}"
        )
        assert operation.json()["stage_state"]["pending_inputs"] == (1 if insight_failure else 0)
        assert operation.json()["status"] == "succeeded"
        assert operation.json()["readiness"]["profile_current"] is not insight_failure
        profile = await client.get("/v1/users/scope/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["freshness"] == ("partial" if insight_failure else "current")
        assert profile.json()["pending_inputs"] == (1 if insight_failure else 0)
        assert profile.json()["failed_inputs"] == (1 if insight_failure else 0)
        if insight_failure:
            assert "brief" in profile.json()["content"]
            assert profile.json()["references"][0]["kind"] == "fact"
        else:
            assert "brief" in profile.json()["content"]
            assert profile.json()["references"][0]["kind"] == "insight"
        unchanged = await client.post("/v1/users/scope/profile/refresh", json={})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["status"] == "unchanged"
        assert models.generation_calls.count("profile") == 1


@pytest.mark.parametrize(
    ("subtype", "value", "messages"),
    [
        (
            "projects",
            "The user maintains the community archive as an ongoing responsibility.",
            ["I maintain the community archive.", "I coordinate its ongoing work."],
        ),
        (
            "relationships",
            "Maya is the user's business partner.",
            ["Maya is my business partner.", "Maya and I run the archive together."],
        ),
    ],
)
async def test_profile_main_can_use_supported_project_or_relationship_insight(
    core_database, subtype, value, messages
):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class MainModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role == "insights":
                envelope = json.loads(kwargs["user"])
                facts = envelope["facts"]
                self.generation_calls.append(role)
                content = insight_reply(
                    envelope,
                    {
                        "outcome": "insight",
                        "subtype": subtype,
                        "text": value,
                        "support_ids": [fact["id"] for fact in facts],
                    },
                )
            elif role == "profile":
                self.generation_calls.append(role)
                assert kwargs["max_output_tokens"] == config.profile.max_output_tokens
                assert kwargs["timeout_seconds"] == config.profile.attempt_timeout_seconds
                insight = next(
                    item
                    for item in json.loads(kwargs["user"])["inputs"]
                    if item["kind"] == "insight" and item["subtype"] == subtype
                )
                content = {
                    "content": f"{value} [1].",
                    "support_ids": [insight["id"]],
                }
            else:
                return super().generate(role, **kwargs)
            return ModelResult(
                content=json.dumps(content),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = MainModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": message,
                        "event_time": f"2026-09-29T09:0{index}:00Z",
                        "learning_eligible": True,
                    }
                    for index, message in enumerate(messages)
                ],
            },
            headers={"Idempotency-Key": f"main-{subtype}"},
        )
        assert retained.status_code == 202, retained.text
        assert process_source(config, queue_dsn, content_dsn, models=models, worker_id="main-input")
        requested = await client.post("/v1/users/scope/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="main-l2")
        memories = await client.get("/v1/users/scope/memories")
        assert memories.status_code == 200, memories.text
        insights = [item for item in memories.json()["items"] if item["kind"] == "insight"]
        assert len(insights) == 1
        assert insights[0]["subtype"] == subtype
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="main-profile")
        profile = await client.get("/v1/users/scope/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["freshness"] == "current"
        assert value in profile.json()["content"]
        assert profile.json()["references"][0]["kind"] == "insight"


async def test_profile_main_builds_dated_age_career_and_family_from_supported_insight(
    core_database,
):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")

    class BasicProfileModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role == "insights":
                self.generation_calls.append(role)
                return synthetic_profile_insight(kwargs["user"])
            if role != "profile":
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            assert kwargs["max_output_tokens"] == config.profile.max_output_tokens
            assert kwargs["timeout_seconds"] == config.profile.attempt_timeout_seconds
            inputs = json.loads(kwargs["user"])["inputs"]
            insight = next(item for item in inputs if item["kind"] == "insight")
            for claim in ("44 years old", "engineer", "maternal uncles"):
                assert claim in insight["text"]
            assert len(insight["support_memory_ids"]) == 3
            assert any(
                passage["event_time"] == "2026-09-29T09:00:00+00:00"
                for passage in insight["source_passages"]
            )
            return ModelResult(
                content=json.dumps(
                    {
                        "content": "The user was 44 years old as of 2026-09-29 [1]. "
                        "The user is an engineer [1]. The user has four maternal uncles [1].",
                        "support_ids": [insight["id"]],
                    }
                ),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = BasicProfileModels()
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                        "event_time": f"2026-09-29T09:0{index}:00Z",
                        "learning_eligible": True,
                    }
                    for index, content in enumerate(
                        [
                            "I am 44 years old.",
                            "I am an engineer.",
                            "I have four maternal uncles.",
                        ]
                    )
                ],
            },
            headers={"Idempotency-Key": "basic-profile-input"},
        )
        assert retained.status_code == 202, retained.text
        assert process_source(
            config, queue_dsn, content_dsn, models=models, worker_id="basic-input"
        )
        requested = await client.post("/v1/users/scope/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="basic-l2")
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="basic-profile")
        profile = await client.get("/v1/users/scope/profile")
        assert profile.status_code == 200, profile.text
        content = profile.json()["content"]
        assert "44 years old as of 2026-09-29" in content
        assert "The user is an engineer" in content
        assert "The user has four maternal uncles" in content
        assert len(profile.json()["references"]) == 1
        assert profile.json()["references"][0]["kind"] == "insight"


async def test_profile_read_withholds_superseded_resource_support(core_database):
    from oam.config import load_config
    from oam.contracts.profile import ProfileDataRequest
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        resource = {
            "profile_data_id": "basic",
            "subtype": "basic-profile/v1",
            "content": {"date_of_birth": "1990-05-10"},
        }
        accepted = await client.post(
            "/v1/users/scope/profile-data",
            json=resource,
            headers={"Idempotency-Key": "profile-before-revision"},
        )
        assert accepted.status_code == 202, accepted.text
        config = load_config(ROOT / "configs/oam.yaml").config
        queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
        content_dsn = make_conninfo(dsn, options="-c role=oam_content")
        assert process_one(
            config, queue_dsn, content_dsn, models=object(), worker_id="resource-before-race"
        )
        refresh = await client.post("/v1/users/scope/profile/refresh", json={})
        assert refresh.status_code == 202, refresh.text
        assert process_one(
            config, queue_dsn, content_dsn, models=object(), worker_id="profile-before-race"
        )
        secret = client.headers["Authorization"].removeprefix("Bearer ")

        def revise():
            receipt = app.state.runtime.retain_profile_data(
                secret,
                "scope",
                ProfileDataRequest.model_validate(
                    {**resource, "content": {"date_of_birth": "1991-06-11"}}
                ),
                "profile-during-read",
                expected_version=1,
            )
            assert receipt["revision"] == 2

        current = await client.get("/v1/users/scope/profile")
        assert current.status_code == 200, current.text
        assert "1990-05-10" in current.json()["content"]
        assert current.json()["generation_calls"] == 0

        revise()
        superseded = await client.get("/v1/users/scope/profile")
        assert superseded.status_code == 200, superseded.text
        assert superseded.json()["content"] is None
        assert superseded.json()["freshness"] == "stale"
        assert superseded.json()["generation_calls"] == 0


async def test_new_profile_instruction_cancels_running_generation(core_database):
    from oam.config import load_config
    from oam.models.runtime import ModelResult, Usage
    from oam.profiles.worker import StaleProfileWork
    from oam.workers.index_source import process_one

    dsn, _, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
    content_dsn = make_conninfo(dsn, options="-c role=oam_content")
    dispatched, release = Event(), Event()

    class Models:
        def generate(self, role, **kwargs):
            assert role == "profile"
            prompt = json.loads(kwargs["user"])
            if prompt["instruction"] == "First JSON format":
                dispatched.set()
                assert release.wait(10), "Cancelled generation was not released"
            return ModelResult(
                content=json.dumps(
                    {
                        "content": json.dumps({"format": prompt["instruction"], "sex": "female"}),
                        "support_ids": ["profile_resource:basic:1"],
                    }
                ),
                usage=Usage(
                    input_tokens=10, output_tokens=10, total_tokens=20, coverage="complete"
                ),
            )

    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/users/scope/profile-data",
            json={
                "profile_data_id": "basic",
                "subtype": "basic-profile/v1",
                "content": {"sex": "female"},
            },
            headers={"Idempotency-Key": "running-profile-resource"},
        )
        assert accepted.status_code == 202, accepted.text
        assert process_one(config, queue_dsn, content_dsn, models=object(), worker_id="resource")
        first = await client.post(
            "/v1/users/scope/profile/refresh", json={"instruction": "First JSON format"}
        )
        assert first.status_code == 202, first.text
        models = Models()
        running = asyncio.create_task(
            asyncio.to_thread(
                process_one, config, queue_dsn, content_dsn, models=models, worker_id="old-profile"
            )
        )
        try:
            assert await asyncio.to_thread(dispatched.wait, 5)
            second = await client.post(
                "/v1/users/scope/profile/refresh", json={"instruction": "Second JSON format"}
            )
            assert second.status_code == 202, second.text
            assert second.json()["operation_id"] != first.json()["operation_id"]
            old = await client.get(f"/v1/users/scope/operations/{first.json()['operation_id']}")
            assert old.json()["status"] == "cancelled"
        finally:
            release.set()
            with pytest.raises(StaleProfileWork):
                await running
        assert process_one(config, queue_dsn, content_dsn, models=models, worker_id="new-profile")
        profile = (await client.get("/v1/users/scope/profile")).json()
        assert profile["instruction"] == "Second JSON format"
        assert json.loads(profile["content"])["format"] == "Second JSON format"
        assert profile["profile_revision"] == 1
        returned = await client.post(
            "/v1/users/scope/profile/refresh", json={"instruction": "First JSON format"}
        )
        assert returned.status_code == 202, returned.text


async def test_new_source_keeps_existing_user_namespace_after_recipe_change(core_database):
    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "namespace-user")
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE users SET index_generation_id=%s WHERE tenant_id=%s AND user_id=%s",
                ("old-namespace", tenant, "namespace-user"),
            )
        response = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "new-source"},
            json={
                "user_id": "namespace-user",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prefer tea.",
                        "event_time": "2026-09-01T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
        )
        assert response.status_code == 202, response.text
        with psycopg.connect(dsn) as connection:
            generation = connection.execute(
                "SELECT generation_id FROM work_units WHERE tenant_id=%s "
                "AND user_id=%s AND operation_id=%s",
                (tenant, "namespace-user", response.json()["operation_id"]),
            ).fetchone()[0]
        assert generation == "old-namespace"


async def test_profile_refresh_uses_existing_user_namespace_after_recipe_change(core_database):
    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "profile-namespace")
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE users SET index_generation_id=%s WHERE tenant_id=%s AND user_id=%s",
                ("old-namespace", tenant, "profile-namespace"),
            )
        response = await client.post("/v1/users/profile-namespace/profile/refresh", json={})
        assert response.status_code == 202, response.text
        with psycopg.connect(dsn) as connection:
            generation = connection.execute(
                "SELECT generation_id FROM work_units WHERE tenant_id=%s "
                "AND user_id=%s AND operation_id=%s",
                (tenant, "profile-namespace", response.json()["operation_id"]),
            ).fetchone()[0]
        assert generation == "old-namespace"
