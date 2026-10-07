"""The public lifecycle path excludes content before durable cleanup completes."""

import json

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_core_http import ROOT, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


async def test_profile_data_collection_crud_and_resource_boundary(core_database, monkeypatch):
    monkeypatch.setattr("oam.db.inspection.INSPECTION_PAGE_SIZE", 1)
    _, _, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        original = {
            "profile_data_id": "basic",
            "subtype": "basic-profile/v1",
            "content": {"date_of_birth": "1990-05-10"},
        }
        created = await client.post(
            "/v1/users/lin/profile-data", json=original, headers={"Idempotency-Key": "basic-v1"}
        )
        assert created.status_code == 202, created.text
        document = await client.post(
            "/v1/users/lin/resources",
            json={
                "input_type": "document",
                "document_id": "report",
                "update_mode": "replace",
                "text": "Report.",
            },
            headers={"Idempotency-Key": "report"},
        )
        assert document.status_code == 202, document.text
        listing = await client.get("/v1/users/lin/profile-data")
        assert listing.status_code == 200, listing.text
        assert [item["profile_data_id"] for item in listing.json()["items"]] == ["basic"]
        assert "resource_type" not in listing.json()["items"][0]
        resources = await client.get("/v1/users/lin/resources")
        assert [item["resource_id"] for item in resources.json()["items"]] == ["report"]
        detail = await client.get("/v1/users/lin/profile-data/basic")
        assert detail.status_code == 200, detail.text
        assert detail.json()["mapped_fields"][0]["value"] == "1990-05-10"
        assert detail.json()["mapped_fields"][0]["support"]["profile_data_id"] == "basic"
        response = await client.get("/v1/users/lin/profile-data/basic/original")
        assert response.json() == original
        for path in (
            "resources/basic",
            "resources/basic/original",
            "profile-data/report",
            "profile-data/report/original",
        ):
            assert (await client.get(f"/v1/users/lin/{path}")).status_code == 404
        for path in ("resources/basic", "profile-data/report"):
            assert (await client.delete(f"/v1/users/lin/{path}")).status_code == 404
        revised = {**original, "content": {"date_of_birth": "1991-05-10"}}
        missing_version = await client.put(
            "/v1/users/lin/profile-data/basic",
            json=revised,
            headers={"Idempotency-Key": "basic-v2"},
        )
        assert missing_version.status_code == 422
        changed = await client.put(
            "/v1/users/lin/profile-data/basic",
            json=revised,
            headers={"Idempotency-Key": "basic-v2", "If-Match": "1"},
        )
        assert changed.status_code == 202, changed.text
        retry = await client.put(
            "/v1/users/lin/profile-data/basic",
            json=revised,
            headers={"Idempotency-Key": "basic-v2", "If-Match": "1"},
        )
        assert retry.json()["operation_id"] == changed.json()["operation_id"]
        stale = await client.put(
            "/v1/users/lin/profile-data/basic",
            json=original,
            headers={"Idempotency-Key": "basic-stale", "If-Match": "1"},
        )
        assert stale.status_code == 409, stale.text
        wrong_id = await client.put(
            "/v1/users/lin/profile-data/basic",
            json={**revised, "profile_data_id": "other"},
            headers={"Idempotency-Key": "wrong", "If-Match": "2"},
        )
        assert wrong_id.status_code in (403, 422)
        assert (await client.get("/v1/users/lin/profile-data/basic")).json()["active_version"] == 2
        deleted = await client.delete("/v1/users/lin/profile-data/basic")
        assert deleted.status_code == 202, deleted.text
        assert (await client.get("/v1/users/lin/profile-data/basic")).status_code == 404
        assert (await client.get("/v1/users/lin/resources/report")).status_code == 200


async def test_profile_resource_delete_excludes_exact_field_and_reuses_cleanup_receipt(
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
        await use_created_user_key(client, "lin")
        created = await client.post(
            "/v1/users/lin/profile-data",
            json={
                "profile_data_id": "basic",
                "subtype": "basic-profile/v1",
                "content": {"date_of_birth": "1990-05-10"},
            },
            headers={"Idempotency-Key": "basic-v1"},
        )
        assert created.status_code == 202, created.text
        config = load_config(ROOT / "configs/oam.yaml").config
        queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")
        content_dsn = make_conninfo(dsn, options="-c role=oam_content")
        assert process_one(
            config, queue_dsn, content_dsn, models=object(), worker_id="resource-before-profile"
        )
        requested = await client.post("/v1/users/lin/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(
            config,
            queue_dsn,
            content_dsn,
            models=object(),
            worker_id="profile-before-delete",
        )
        before = await client.get("/v1/users/lin/profile")
        assert "1990-05-10" in before.json()["content"]

        deleted = await client.delete("/v1/users/lin/profile-data/basic")
        assert deleted.status_code == 202, deleted.text
        assert deleted.json()["status"] == "excluded"
        assert deleted.json()["excluded_versions"] == [1]
        repeated = await client.delete("/v1/users/lin/profile-data/basic")
        assert repeated.status_code == 202, repeated.text
        assert repeated.json()["cleanup_operation_id"] == deleted.json()["cleanup_operation_id"]
        after = await client.get("/v1/users/lin/profile")
        assert after.json()["content"] is None
        assert (await client.get("/v1/users/lin/profile-data/basic")).status_code == 404
        config = load_config(ROOT / "configs/oam.yaml").config
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=object(),
            worker_id="profile-cleanup",
        )
        operation = await client.get(
            f"/v1/users/lin/operations/{deleted.json()['cleanup_operation_id']}"
        )
        assert operation.status_code == 200, operation.text
        assert operation.json()["status"] == "succeeded"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT byte_length FROM source_versions WHERE tenant_id=%s "
                "AND user_id='lin' AND source_id='basic'",
                (tenant,),
            ).fetchone()[0]
            == 0
        )


async def test_profile_resource_write_requires_resource_grant(core_database):
    dsn, tenant, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE grants SET capabilities=ARRAY['resource:write'] "
                "WHERE tenant_id=%s AND user_id='lin'",
                (tenant,),
            )
        body = {
            "profile_data_id": "basic",
            "subtype": "basic-profile/v1",
            "content": {"date_of_birth": "1990-05-10"},
        }
        allowed = await client.post(
            "/v1/users/lin/profile-data", json=body, headers={"Idempotency-Key": "grant-allowed"}
        )
        assert allowed.status_code == 202, allowed.text

        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE grants SET capabilities=ARRAY['memory:append'] "
                "WHERE tenant_id=%s AND user_id='lin'",
                (tenant,),
            )
        denied = await client.post(
            "/v1/users/lin/profile-data",
            json={**body, "profile_data_id": "other"},
            headers={"Idempotency-Key": "grant-denied"},
        )
        assert denied.status_code == 404, denied.text


async def test_public_resource_delete_cleans_export_and_keeps_new_independent_source(core_database):
    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        body = {
            "user_id": "lin",
            "input_type": "document",
            "document_id": "withdrawn",
            "update_mode": "replace",
            "text": "Synthetic private report.",
        }
        retained = await client.post(
            "/v1/users/lin/resources",
            json={**{k: v for k, v in body.items() if k != "user_id"}},
            headers={"Idempotency-Key": "v1"},
        )
        assert retained.status_code == 202, retained.text
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE operations SET stage_state=%s WHERE tenant_id=%s AND user_id='lin' "
                "AND operation_id=%s",
                (
                    Jsonb({"abstract_state": "current", "abstract": "withdrawn secret marker"}),
                    tenant,
                    retained.json()["operation_id"],
                ),
            )
        lin_key = client.headers["Authorization"]
        for method, path in (
            ("GET", "/v1/users/lin/resources"),
            ("DELETE", "/v1/users/lin/resources/withdrawn"),
            ("POST", "/v1/users/lin/exports"),
        ):
            denied = await client.request(method, path, headers=headers)
            assert denied.status_code == 404, denied.text
        second_user = await client.post("/v1/users", json={"user_id": "other"}, headers=headers)
        assert second_user.status_code == 201, second_user.text
        denied = await client.get(
            "/v1/users/lin/resources",
            headers={"Authorization": f"Bearer {second_user.json()['secret']}"},
        )
        assert denied.status_code == 404
        client.headers["Authorization"] = lin_key
        body["text"] = "Revised synthetic report."
        revised = await client.put(
            "/v1/users/lin/resources/withdrawn",
            json=body,
            headers={"Idempotency-Key": "v2", "If-Match": "1"},
        )
        assert revised.status_code == 202, revised.text
        prior_operation = await client.get(
            f"/v1/users/lin/operations/{retained.json()['operation_id']}"
        )
        assert prior_operation.status_code == 200
        assert "withdrawn secret marker" not in prior_operation.text
        exported = await client.post("/v1/users/lin/exports")
        assert exported.status_code == 202, exported.text
        deleted = await client.delete("/v1/users/lin/resources/withdrawn")
        assert deleted.status_code == 202, deleted.text
        old_operation = await client.get(
            f"/v1/users/lin/operations/{retained.json()['operation_id']}"
        )
        assert old_operation.status_code == 200
        assert "withdrawn secret marker" not in old_operation.text
        assert (await client.get("/v1/users/lin/resources/withdrawn/original")).status_code == 404
        assert (
            await client.get(f"/v1/users/lin/exports/{exported.json()['export_id']}")
        ).status_code == 409

        config = load_config(ROOT / "configs/oam.yaml").config
        assert process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=object(),
            worker_id="test-cleanup",
        )
        operation = await client.get(
            f"/v1/users/lin/operations/{deleted.json()['cleanup_operation_id']}"
        )
        assert operation.status_code == 200
        assert operation.json()["status"] == "succeeded"
        body.update(document_id="independent", text="Synthetic independent report.")
        other = await client.post(
            "/v1/users/lin/resources",
            json={**{k: v for k, v in body.items() if k != "user_id"}},
            headers={"Idempotency-Key": "other"},
        )
        assert other.status_code == 202, other.text
        listing = await client.get("/v1/users/lin/resources")
        assert [item["resource_id"] for item in listing.json()["items"]] == ["independent"]
        original = await client.get("/v1/users/lin/resources/independent/original")
        assert original.content == b"Synthetic independent report."
        metadata = {
            "user_id": "lin",
            "input_type": "document",
            "document_id": "scan",
            "update_mode": "replace",
        }
        first_png = (ROOT / "conformance/fixtures/medical/bilingual/en-report.png").read_bytes()
        second_png = (ROOT / "conformance/fixtures/medical/bilingual/zh-report.png").read_bytes()
        created_file = await client.post(
            "/v1/users/lin/resources",
            data={
                "metadata": json.dumps(
                    {
                        **{k: v for k, v in metadata.items() if k != "user_id"},
                    }
                )
            },
            files={"original": ("en-report.png", first_png, "image/png")},
            headers={"Idempotency-Key": "scan-v1"},
        )
        assert created_file.status_code == 202, created_file.text
        revised_file = await client.put(
            "/v1/users/lin/resources/scan",
            data={"metadata": json.dumps(metadata)},
            files={"original": ("zh-report.png", second_png, "image/png")},
            headers={"Idempotency-Key": "scan-v2", "If-Match": "1"},
        )
        assert revised_file.status_code == 202, revised_file.text
        assert revised_file.json()["revision"] == 2
        stale_file = await client.put(
            "/v1/users/lin/resources/scan",
            data={"metadata": json.dumps(metadata)},
            files={"original": ("en-report.png", first_png, "image/png")},
            headers={"Idempotency-Key": "scan-stale", "If-Match": "1"},
        )
        assert stale_file.status_code == 409
        current_file = await client.get("/v1/users/lin/resources/scan/original")
        assert current_file.content == second_png
        chat = await client.post(
            "/v1/retain",
            json={
                "user_id": "lin",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prefer concise answers.",
                        "event_time": "2026-09-26T00:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
            headers={"Idempotency-Key": "chat"},
        )
        assert chat.status_code == 202, chat.text
        chat_id = chat.json()["source_id"]
        source = await client.get(f"/v1/users/lin/evidence/{chat_id}")
        assert source.status_code == 200, source.text
        assert source.json()["original"]["messages"][0]["content"] == ("I prefer concise answers.")
        assert (await client.delete(f"/v1/users/lin/evidence/{chat_id}")).status_code == 202
        assert (await client.get(f"/v1/users/lin/evidence/{chat_id}")).status_code == 404

    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM export_parts WHERE tenant_id=%s AND user_id='lin'",
                (tenant,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT byte_length FROM source_versions WHERE tenant_id=%s AND user_id='lin' "
                "AND source_id='withdrawn'",
                (tenant,),
            ).fetchone()[0]
            == 0
        )
