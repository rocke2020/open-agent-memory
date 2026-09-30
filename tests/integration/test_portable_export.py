"""Portable exports preserve current originals and expire on source change."""

import hashlib
import json

import httpx
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, use_created_user_key
from test_core_http import core_database as core_database
from test_recall_repository import (
    add_fact,
    add_insight,
)
from test_recall_repository import (
    recall_database as recall_database,
)
from test_source_version_admission import command
from test_source_version_admission import source_database as source_database

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.sources import SourceRepository
from oam.exports.repository import ExportPolicyChanged, ExportRepository
from oam.exports.validation import validate_directory
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


async def test_public_export_includes_eligible_profile_fields(core_database, tmp_path):
    dsn, _, root_headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=root_headers
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        created = await client.post(
            "/v1/users/lin/resources",
            json={
                "resource_id": "basic",
                "resource_type": "profile",
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
            config, queue_dsn, content_dsn, models=object(), worker_id="export-resource"
        )
        requested = await client.post("/v1/users/lin/profile/refresh", json={})
        assert requested.status_code == 202, requested.text
        assert process_one(
            config,
            queue_dsn,
            content_dsn,
            models=object(),
            worker_id="export-profile",
        )
        profile = await client.get("/v1/users/lin/profile")
        assert "1990-05-10" in profile.json()["content"]

        receipt = await client.post("/v1/users/lin/exports")
        assert receipt.status_code == 202, receipt.text
        export_id = receipt.json()["export_id"]
        manifest = (await client.get(f"/v1/users/lin/exports/{export_id}")).json()
        assert manifest["profiles"]
        assert manifest["counts"]["profile"] == 1
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        records = []
        for part in manifest["parts"]:
            response = await client.get(
                f"/v1/users/lin/exports/{export_id}/records/{part['index']}"
            )
            assert response.status_code == 200, response.text
            destination = tmp_path / part["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
            records.extend(json.loads(line) for line in response.content.splitlines())
        for original in manifest["originals"]:
            response = await client.get(
                f"/v1/users/lin/exports/{export_id}/originals/{original['index']}"
            )
            assert response.status_code == 200, response.text
            destination = tmp_path / original["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
        exported_profile = next(record for record in records if record["kind"] == "profile")
        assert "1990-05-10" in exported_profile["content"]
        assert exported_profile["references"][0]["resource_id"] == "basic"
        assert validate_directory(tmp_path)["profile"] == 1

        deleted = await client.delete("/v1/users/lin/resources/basic")
        assert deleted.status_code == 202, deleted.text
        assert deleted.json()["excluded_versions"] == [1]
        assert (await client.get(f"/v1/users/lin/exports/{export_id}")).status_code == 409
        current = await client.get("/v1/users/lin/profile")
        assert current.json()["content"] is None


def test_export_manifest_parts_and_original_expire_on_deletion(source_database):
    connection, authority = source_database
    SourceRepository().accept(
        connection,
        authority,
        command(
            source_version=None,
            input_role="evidence_resource",
            resource_type="evidence",
            operation_kind="index_resource",
        ),
    )
    connection.execute(
        "UPDATE operations SET stage_state=%s::jsonb WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='source' AND kind='index_resource'",
        (
            json.dumps({"abstract_state": "current", "abstract": "A source summary."}),
            authority.tenant_id,
            authority.user_id,
        ),
    )
    export_authority = AuthorizedUser(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        authority.user_id,
        frozenset({"export:read"}),
    )
    repository = ExportRepository(load_config(ROOT / "configs/oam.yaml").config)

    receipt = repository.create(
        connection, export_authority, export_id="export", retention_hours=24
    )
    manifest = repository.get_manifest(connection, export_authority, "export")
    part = repository.get_part(connection, export_authority, "export", 0)
    original = repository.get_original(connection, export_authority, "export", 0)

    assert receipt == {"export_id": "export", "operation_id": "export", "state": "ready"}
    assert manifest["format_version"] == 1
    assert manifest["counts"]["source"] == 1
    assert manifest["counts"]["resource_abstract"] == 1
    assert manifest["coverage"]["complete"] is False
    assert hashlib.sha256(part).hexdigest() == manifest["parts"][0]["sha256"]
    assert any(json.loads(line)["kind"] == "source" for line in part.splitlines())
    assert original == (b"source version 1", "text/plain")

    SourceRepository().delete(
        connection,
        authority,
        source_id="source",
        resource_only=True,
        operation_id="cleanup",
        work_unit_id="cleanup-work",
        attempts_remaining=2,
    )
    with pytest.raises(ExportPolicyChanged):
        repository.get_manifest(connection, export_authority, "export")


def test_export_keeps_layer_kinds_and_support_links(recall_database):
    connection, authority = recall_database
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) "
        "VALUES (%s,'reader','user','active')",
        (authority.tenant_id,),
    )
    add_fact(connection, authority, "first", "First supported claim")
    add_fact(connection, authority, "second", "Second supported claim")
    add_insight(connection, authority, "combined", ("first", "second"))
    export_authority = AuthorizedUser(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        authority.user_id,
        frozenset({"export:read"}),
    )

    repository = ExportRepository(load_config(ROOT / "configs/oam.yaml").config)
    repository.create(connection, export_authority, export_id="export", retention_hours=24)
    manifest = repository.get_manifest(connection, export_authority, "export")
    records = [
        json.loads(line)
        for part in manifest["parts"]
        for line in repository.get_part(connection, export_authority, "export", part["index"])
        .decode()
        .splitlines()
    ]

    assert manifest["counts"] == {
        "source": 2,
        "evidence": 2,
        "passage": 2,
        "memory": 2,
        "support": 2,
        "insight": 1,
        "insight_support": 2,
    }
    assert {record["kind"] for record in records} == set(manifest["counts"])
    assert {record["memory_id"] for record in records if record["kind"] == "insight_support"} == {
        "first",
        "second",
    }


def test_export_expires_when_older_source_publishes_after_newer_source(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    repository.accept(connection, authority, command(source_version=None))
    repository.accept(
        connection,
        authority,
        command(
            2,
            source_id="newer",
            source_version=None,
            evidence_id="newer-evidence",
            operation_id="newer-operation",
            work_unit_id="newer-work",
        ),
    )
    connection.execute(
        "UPDATE operations SET status='succeeded' WHERE tenant_id=%s AND user_id=%s "
        "AND operation_id='newer-operation'",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "UPDATE users SET published_seq=2 WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    export_authority = AuthorizedUser(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        authority.user_id,
        frozenset({"export:read"}),
    )
    exporter = ExportRepository(load_config(ROOT / "configs/oam.yaml").config)
    exporter.create(connection, export_authority, export_id="snapshot", retention_hours=24)
    connection.execute(
        "UPDATE operations SET status='succeeded' WHERE tenant_id=%s AND user_id=%s "
        "AND operation_id='operation1'",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "UPDATE users SET projection_epoch=projection_epoch+1 WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    assert (
        connection.execute(
            "SELECT published_seq FROM users WHERE tenant_id=%s AND user_id=%s",
            (authority.tenant_id, authority.user_id),
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(ExportPolicyChanged):
        exporter.get_manifest(connection, export_authority, "snapshot")


def test_export_expires_on_projection_change_before_operation_finishes(source_database):
    connection, authority = source_database
    SourceRepository().accept(connection, authority, command(source_version=None))
    export_authority = AuthorizedUser(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        authority.user_id,
        frozenset({"export:read"}),
    )
    exporter = ExportRepository(load_config(ROOT / "configs/oam.yaml").config)
    exporter.create(connection, export_authority, export_id="pending", retention_hours=24)
    connection.execute(
        "UPDATE users SET projection_epoch=projection_epoch+1 WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    assert (
        connection.execute(
            "SELECT status FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id='operation1'",
            (authority.tenant_id, authority.user_id),
        ).fetchone()[0]
        == "accepted"
    )
    with pytest.raises(ExportPolicyChanged):
        exporter.get_manifest(connection, export_authority, "pending")
