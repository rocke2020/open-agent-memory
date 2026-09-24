"""Prove credential rotation and cache invalidation through the public HTTP API."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from oam.api.app import create_app
from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

pytestmark = pytest.mark.integration


async def test_rotation_commits_before_return_and_invalidates_cached_old_key(monkeypatch):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    root = Path(__file__).parents[2]
    apply_migrations(dsn, root / "migrations")
    tenant = f"http-rotation-{uuid4().hex}"
    root_secret = f"synthetic-root-{uuid4().hex}"
    old_user_secret = f"synthetic-user-{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES (%s,'admin','administrator','active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES (%s,'scope','synthetic','test','persistent')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES (%s,'user:scope','user','active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES (%s,'root-key','admin',%s,'active'),"
            "(%s,'old-user-key','user:scope',%s,'active')",
            (tenant, digest_secret(root_secret), tenant, digest_secret(old_user_secret)),
        )
        connection.execute(
            "INSERT INTO grants(tenant_id,user_id,principal_id,capabilities) "
            "VALUES (%s,'scope','user:scope',ARRAY['memory:read'])",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
            "schema_version,status) VALUES "
            "(%s,'scope','operation','user:scope','test',1,'accepted')",
            (tenant,),
        )
    monkeypatch.setenv("OAM_API_DSN", make_conninfo(dsn, options="-c role=oam_api"))
    root_headers = {"Authorization": f"Bearer {root_secret}"}
    old_headers = {"Authorization": f"Bearer {old_user_secret}"}
    app = create_app(root / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        route = "/v1/users/scope/operations/operation"
        assert (await client.get(route, headers=old_headers)).status_code == 200
        assert (await client.get(route, headers=root_headers)).status_code == 404
        assert (await client.get("/v1/credentials", headers=old_headers)).status_code == 403
        issued = await client.post(
            "/v1/credentials", headers=root_headers, json={"user_id": "scope"}
        )
        assert issued.status_code == 201
        new_key = issued.json()
        new_headers = {"Authorization": f"Bearer {new_key['secret']}"}
        assert new_key["credential_id"] != "old-user-key"
        assert new_key["principal_id"] == "user:scope"
        assert (await client.get(route, headers=old_headers)).status_code == 200
        assert (await client.get(route, headers=new_headers)).status_code == 200
        listed = await client.get("/v1/credentials", headers=root_headers)
        assert listed.status_code == 200
        assert {item["credential_id"] for item in listed.json()["items"]} == {
            "root-key",
            "old-user-key",
            new_key["credential_id"],
        }
        assert "secret" not in listed.text and "digest" not in listed.text
        before = datetime.now(UTC)
        revoked = await client.delete("/v1/credentials/old-user-key", headers=root_headers)
        assert revoked.status_code == 200
        effective_by = datetime.fromisoformat(revoked.json()["revocation_effective_by"])
        assert (
            before + timedelta(seconds=30)
            <= effective_by
            <= (datetime.now(UTC) + timedelta(seconds=30))
        )
        assert (await client.get(route, headers=old_headers)).status_code == 401
        assert (await client.get(route, headers=new_headers)).status_code == 200
        assert (
            await client.delete("/v1/credentials/old-user-key", headers=root_headers)
        ).status_code == 200
        assert (
            await client.delete("/v1/credentials/unknown", headers=root_headers)
        ).status_code == 200
        assert (
            await client.get("/v1/credentials?limit=51", headers=root_headers)
        ).status_code == 422
