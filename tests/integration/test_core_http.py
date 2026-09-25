"""Public core path against an explicitly isolated PostgreSQL database."""

import base64
import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pypdf import PdfWriter

from oam.api.app import create_app
from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

ROOT = Path(__file__).parents[2]
MEDICAL = ROOT / "conformance/fixtures/medical"
BILINGUAL = MEDICAL / "bilingual"
pytestmark = pytest.mark.integration


async def use_created_user_key(client: httpx.AsyncClient, user_id: str) -> httpx.Response:
    created = await client.post("/v1/users", json={"user_id": user_id})
    assert created.status_code == 201, created.text
    client.headers["Authorization"] = f"Bearer {created.json()['secret']}"
    return created


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
            "/v1/retain",
            json={
                "user_id": "alice",
                "input_type": "document",
                "document_id": "root-attempt",
                "update_mode": "replace",
                "text": "must not be accepted",
            },
            headers={"Idempotency-Key": "root-attempt"},
        )
        assert root_write.status_code == 404
        retained = await client.post(
            "/v1/retain",
            json={
                "user_id": "alice",
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
        self.invalid_fact = invalid_fact
        self.vision_text = vision_text
        self.vision_blocks = vision_blocks

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

    def generate_vision(self, image, **kwargs):
        from oam.models.runtime import ModelResult, Usage

        self.generation_calls.append("document_vision")
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
        await use_created_user_key(client, "scope")
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
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/retain",
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
    assert ("document_vision" in models.generation_calls) is uses_vision


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
            "/v1/retain",
            headers={"Idempotency-Key": "mixed-report"},
            data={
                "metadata": json.dumps(
                    {
                        "user_id": "scope",
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
    assert models.generation_calls == ["document_vision", "abstract"]
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
        "user_id": "scope",
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
            "/v1/retain",
            json={**request, "url": "https://127.0.0.1/private.pdf"},
            headers={"Idempotency-Key": "unsafe-report"},
        )
        assert unsafe.status_code == 422
        assert attempts == 0
        first = await client.post(
            "/v1/retain", json=request, headers={"Idempotency-Key": "retry-report"}
        )
        assert first.status_code == 502
        assert first.json()["detail"]["code"] == "ResourceFetchUnavailable"
        second = await client.post(
            "/v1/retain", json=request, headers={"Idempotency-Key": "retry-report"}
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
        await use_created_user_key(client, "scope")
        uploaded = await client.post(
            "/v1/retain",
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
        assert ("document_vision" in models.generation_calls) is (kind in {"scanned_pdf", "image"})
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
        "user_id": "scope",
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
            "/v1/retain", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert denied.status_code == 404
        assert calls == []
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/retain", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert accepted.status_code == 202, (accepted.text, calls)
        repeated = await client.post(
            "/v1/retain", json=request, headers={"Idempotency-Key": "url-report"}
        )
        assert repeated.status_code == 202 and repeated.json()["reused"] is True
        assert repeated.json()["operation_id"] == accepted.json()["operation_id"]
        changed = await client.post(
            "/v1/retain",
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
        "user_id": "scope",
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
            "/v1/retain",
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
        assert operation.json()["usage"]["total_tokens"] == (23 if failure_kind == "invalid" else 0)
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
        assert attempt[:2] == (
            "failed" if failure_kind == "invalid" else "unknown_external_outcome",
            "vision:pdf:page:0002",
        )
        if failure_kind == "invalid":
            assert attempt[2] == "vision:pdf:page:0002"


async def test_chat_and_html_report_share_one_cited_recall_budget(core_database):
    import tiktoken

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
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="chat-worker",
        )
        retained_report = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "report"},
            data={
                "metadata": json.dumps(
                    {
                        "user_id": "scope",
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
        for mode in ("fast", "mid", "deep"):
            recalled = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "Lin brief glucose 92 mg/dL",
                    "recall_level": mode,
                    "max_context_tokens": 2048,
                    "profile_sections": [],
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
            assert payload["context_tokens"] == len(
                tiktoken.get_encoding(payload["tokenizer"]).encode_ordinary(payload["context"])
            )
            assert payload["context_tokens"] <= 2048
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
        await use_created_user_key(client, "scope")
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
