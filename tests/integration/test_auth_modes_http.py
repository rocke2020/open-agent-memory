import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.auth.credentials import digest_secret
from oam.config import ConfigurationError
from oam.db.migrations import apply_migrations, migrate_from_environment

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_auth_database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL test database")
    database_name = "oam_auth_modes_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    selected_dsn = make_conninfo(dsn, dbname=database_name)
    apply_migrations(selected_dsn, ROOT / "migrations")
    return selected_dsn


@pytest.fixture
def auth_database(isolated_auth_database, monkeypatch):
    dsn = isolated_auth_database
    monkeypatch.delenv("OAM_ROOT_API_KEY", raising=False)
    tenant = f"auth-modes-{uuid4().hex}"
    root_secret = f"synthetic-root-{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES (%s,'root','administrator','active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES (%s,'root','root',%s,'active')",
            (tenant, digest_secret(root_secret)),
        )
    monkeypatch.setenv("OAM_API_DSN", make_conninfo(dsn, options="-c role=oam_api"))
    return dsn, tenant, root_secret


@pytest.mark.parametrize("mode", ["dev", "trusted"])
async def test_asserted_user_can_manage_profile_resource(auth_database, tmp_path: Path, mode):
    _, tenant, _ = auth_database
    selected = tmp_path / "auth.yaml"
    selected.write_text(f"auth:\n  mode: {mode}\n", encoding="utf-8")
    app = create_app(selected)
    headers = {"X-OAM-Tenant": tenant, "X-OAM-User": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/v1/users/alice/resources",
            headers={**headers, "Idempotency-Key": "asserted-profile"},
            json={
                "resource_id": "basic",
                "resource_type": "profile",
                "subtype": "basic-profile/v1",
                "content": {"date_of_birth": "1990-05-10"},
            },
        )
        assert created.status_code == 202, created.text
        assert (
            await client.get("/v1/users/alice/resources/basic", headers=headers)
        ).status_code == 200
        deleted = await client.delete("/v1/users/alice/resources/basic", headers=headers)
        assert deleted.status_code == 202, deleted.text


def test_asserted_grant_migration_preserves_existing_restricted_grant(auth_database):
    dsn, tenant, _ = auth_database
    restricted = ["memory:read", "memory:append", "operation:reconcile"]
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT oam_register_asserted_user(%s, 'restricted')", (tenant,)
        ).fetchone() == (True,)
        connection.execute(
            "UPDATE grants SET capabilities = %s WHERE tenant_id = %s AND user_id = 'restricted'",
            (restricted, tenant),
        )
        connection.execute(
            (ROOT / "migrations" / "0012_asserted_resource_grants.sql").read_text(encoding="utf-8")
        )
        capabilities = connection.execute(
            "SELECT capabilities FROM grants WHERE tenant_id = %s AND user_id = 'restricted'",
            (tenant,),
        ).fetchone()[0]
    assert capabilities == restricted


@pytest.fixture
def fresh_rootless_database(isolated_auth_database, monkeypatch):
    rootless_dsn = isolated_auth_database
    monkeypatch.delenv("OAM_ROOT_API_KEY", raising=False)
    monkeypatch.setenv("OAM_API_DSN", make_conninfo(rootless_dsn, options="-c role=oam_api"))
    monkeypatch.setenv("OAM_MIGRATION_DSN", rootless_dsn)
    monkeypatch.delenv("OAM_BOOTSTRAP_OUTPUT", raising=False)
    return rootless_dsn


async def test_local_trusted_identity_registers_user_without_key_and_cannot_switch_user(
    auth_database, tmp_path: Path
) -> None:
    _, tenant, root_secret = auth_database
    config = tmp_path / "trusted.yaml"
    config.write_text("auth:\n  mode: trusted\n", encoding="utf-8")
    app = create_app(config)
    alice_headers = {"X-OAM-Tenant": tenant, "X-OAM-User": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        retained = await client.post(
            "/v1/users/alice/resources",
            headers={**alice_headers, "Idempotency-Key": "trusted-alice"},
            json={
                "resource_type": "evidence",
                "input_type": "document",
                "document_id": "private",
                "update_mode": "replace",
                "text": "Alice's private content",
            },
        )
        assert retained.status_code == 202, retained.text
        route = f"/v1/users/alice/operations/{retained.json()['operation_id']}"
        assert (await client.get(route, headers=alice_headers)).status_code == 200
        assert (
            await client.get(route, headers={"X-OAM-Tenant": tenant, "X-OAM-User": "bob"})
        ).status_code == 404
        assert (
            await client.post(
                "/v1/users/bob/resources",
                headers={**alice_headers, "Idempotency-Key": "user-mismatch"},
                json={
                    "resource_type": "evidence",
                    "input_type": "document",
                    "document_id": "wrong-user",
                    "update_mode": "replace",
                    "text": "must not be accepted",
                },
            )
        ).status_code == 404
        assert (
            await client.get(
                route,
                headers={"X-OAM-Tenant": "unknown-tenant", "X-OAM-User": "alice"},
            )
        ).status_code == 404
        assert (await client.get(route, headers={"X-OAM-Tenant": tenant})).status_code == 401
        assert (await client.post("/v1/users", json={"user_id": "bob"})).status_code == 401
        assert (
            await client.put(
                "/v1/users/alice/grants/other",
                headers=alice_headers,
                json={"capabilities": ["memory:read"]},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/v1/users",
                headers={"Authorization": f"Bearer {root_secret}"},
                json={"user_id": "bob"},
            )
        ).status_code == 201


async def test_rootless_trusted_local_uses_default_tenant_after_migration(
    fresh_rootless_database, tmp_path: Path
) -> None:
    config = tmp_path / "trusted.yaml"
    config.write_text("auth:\n  mode: trusted\n", encoding="utf-8")
    assert migrate_from_environment(config) == 0
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get(
            "/v1/users/alice/operations/missing",
            headers={"X-OAM-Tenant": "default", "X-OAM-User": "alice"},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "OperationNotFound"


async def test_trusted_role_header_requires_root_and_cannot_assert_root(
    auth_database, tmp_path: Path
) -> None:
    _, tenant, root_secret = auth_database
    config = tmp_path / "trusted.yaml"
    config.write_text("auth:\n  mode: trusted\n", encoding="utf-8")
    app = create_app(config)
    route = "/v1/users/alice/operations/missing"
    headers = {"X-OAM-Tenant": tenant, "X-OAM-User": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (
            await client.get(route, headers={**headers, "X-OAM-Role": "admin"})
        ).status_code == 401
        assert (
            await client.get(
                route,
                headers={
                    **headers,
                    "X-OAM-Role": "root",
                    "Authorization": f"Bearer {root_secret}",
                },
            )
        ).status_code == 400
        assert (
            await client.get(
                route,
                headers={
                    **headers,
                    "X-OAM-Role": "admin",
                    "Authorization": f"Bearer {root_secret}",
                },
            )
        ).status_code == 404


async def test_dev_mode_accepts_local_identity_without_key_and_disables_admin(
    auth_database, tmp_path: Path
) -> None:
    _, tenant, _ = auth_database
    config = tmp_path / "dev.yaml"
    config.write_text("auth:\n  mode: dev\n", encoding="utf-8")
    app = create_app(config)
    headers = {"X-OAM-Tenant": tenant, "X-OAM-User": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.post("/v1/users", json={"user_id": "bob"})).status_code == 503
        retained = await client.post(
            "/v1/users/alice/resources",
            headers={**headers, "Idempotency-Key": "dev-alice"},
            json={
                "resource_type": "evidence",
                "input_type": "document",
                "document_id": "private",
                "update_mode": "replace",
                "text": "development content",
            },
        )
        assert retained.status_code == 202, retained.text
        route = f"/v1/users/alice/operations/{retained.json()['operation_id']}"
        assert (await client.get(route, headers=headers)).status_code == 200


def test_dev_mode_refuses_nonlocal_bind(tmp_path: Path) -> None:
    config = tmp_path / "dev.yaml"
    config.write_text("auth:\n  mode: dev\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="loopback"):
        create_app(config, bind_host="0.0.0.0")


async def test_automatic_rootless_dev_refuses_nonlocal_bind(fresh_rootless_database) -> None:
    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 0
    app = create_app(ROOT / "configs/oam.yaml", bind_host="0.0.0.0")

    with pytest.raises(ConfigurationError, match="loopback"):
        async with app.router.lifespan_context(app):
            pass


def test_nonlocal_trusted_refuses_startup_without_database_authority(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OAM_API_DSN", raising=False)
    config = tmp_path / "trusted.yaml"
    config.write_text("auth:\n  mode: trusted\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="root key"):
        create_app(config, bind_host="0.0.0.0")


async def test_nonlocal_trusted_data_requires_root_key_for_asserted_tenant(
    auth_database, tmp_path: Path
) -> None:
    dsn, tenant, root_secret = auth_database
    foreign_tenant = "other-" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (foreign_tenant,)
        )
    config = tmp_path / "trusted.yaml"
    config.write_text("auth:\n  mode: trusted\n", encoding="utf-8")
    app = create_app(config, bind_host="0.0.0.0")
    headers = {"X-OAM-Tenant": tenant, "X-OAM-User": "alice"}
    body = {
        "resource_type": "evidence",
        "input_type": "document",
        "document_id": "private",
        "update_mode": "replace",
        "text": "nonlocal trusted content",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (
            await client.post(
                "/v1/users/alice/resources",
                headers={**headers, "Idempotency-Key": "missing"},
                json=body,
            )
        ).status_code == 401
        assert (
            await client.post(
                "/v1/users/alice/resources",
                headers={
                    **headers,
                    "Authorization": "Bearer invalid",
                    "Idempotency-Key": "invalid",
                },
                json=body,
            )
        ).status_code == 401
        retained = await client.post(
            "/v1/users/alice/resources",
            headers={
                **headers,
                "Authorization": f"Bearer {root_secret}",
                "Idempotency-Key": "valid",
            },
            json=body,
        )
        assert retained.status_code == 202, retained.text
        assert (
            await client.post(
                "/v1/users/alice/resources",
                headers={
                    **headers,
                    "X-OAM-Tenant": foreign_tenant,
                    "Authorization": f"Bearer {root_secret}",
                    "Idempotency-Key": "foreign-root",
                },
                json=body,
            )
        ).status_code == 404


async def test_omitted_mode_selects_dev_only_for_fresh_rootless_database(
    fresh_rootless_database,
) -> None:
    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 0
    with psycopg.connect(fresh_rootless_database) as connection:
        assert connection.execute(
            "SELECT state FROM tenants WHERE tenant_id='default'"
        ).fetchone() == ("active",)
        assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/v1/users/default/operations/missing")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "OperationNotFound"
    restarted = create_app(ROOT / "configs/oam.yaml")
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/v1/users/default/operations/missing")).status_code == 404


@pytest.mark.parametrize(("mode", "bind_host"), [("api_key", "127.0.0.1"), ("trusted", "0.0.0.0")])
async def test_modes_requiring_root_key_fail_startup_without_one(
    fresh_rootless_database, tmp_path: Path, mode: str, bind_host: str
) -> None:
    config = tmp_path / "auth.yaml"
    config.write_text(f"auth:\n  mode: {mode}\n", encoding="utf-8")
    app = create_app(config, bind_host=bind_host)

    with pytest.raises(ConfigurationError, match="root key"):
        async with app.router.lifespan_context(app):
            pass


async def test_incomplete_key_bootstrap_cannot_become_rootless_dev(
    fresh_rootless_database, tmp_path: Path
) -> None:
    with psycopg.connect(fresh_rootless_database) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('t','root','administrator','active')"
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES ('t','root','root',%s,'active')",
            (digest_secret("synthetic-unhanded-root"),),
        )
        connection.execute(
            "UPDATE bootstrap_state SET phase='committed', winner_id='interrupted', "
            "tenant_id='t', principal_id='root', credential_id='root' WHERE singleton=true"
        )

    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 1
    config = tmp_path / "keyed.yaml"
    config.write_text("auth:\n  mode: api_key\n", encoding="utf-8")
    app = create_app(config)
    with pytest.raises(ConfigurationError, match="root key"):
        async with app.router.lifespan_context(app):
            pass


def test_existing_user_key_without_active_root_cannot_auto_select_keyed_mode(
    fresh_rootless_database,
) -> None:
    with psycopg.connect(fresh_rootless_database) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('t','user:alice','user','active')"
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES ('t','user-key','user:alice',%s,'active')",
            (digest_secret("synthetic-user-only-key"),),
        )

    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 1


async def test_operator_supplied_root_secret_selects_keyed_mode_without_handoff_file(
    fresh_rootless_database, monkeypatch
) -> None:
    root_secret = "synthetic-operator-root-" + uuid4().hex
    monkeypatch.setenv("OAM_ROOT_API_KEY", root_secret)
    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 0
    with psycopg.connect(fresh_rootless_database) as connection:
        assert connection.execute(
            "SELECT phase FROM bootstrap_state WHERE singleton=true"
        ).fetchone() == ("handed_off",)
        assert (
            connection.execute(
                "SELECT count(*) FROM credentials WHERE secret_digest=%s AND state='active'",
                (digest_secret(root_secret),),
            ).fetchone()[0]
            == 1
        )
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.post("/v1/users", json={"user_id": "alice"})).status_code == 401
        created = await client.post(
            "/v1/users",
            headers={"Authorization": f"Bearer {root_secret}"},
            json={"user_id": "alice"},
        )
        assert created.status_code == 201, created.text


def test_existing_active_root_accepts_matching_operator_secret_without_rebootstrap(
    fresh_rootless_database, monkeypatch
) -> None:
    root_secret = "synthetic-existing-root-" + uuid4().hex
    with psycopg.connect(fresh_rootless_database) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('t','root','administrator','active')"
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES ('t','root','root',%s,'active')",
            (digest_secret(root_secret),),
        )
    monkeypatch.setenv("OAM_ROOT_API_KEY", root_secret)

    assert migrate_from_environment(ROOT / "configs/oam.yaml") == 0
    with psycopg.connect(fresh_rootless_database) as connection:
        assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1


async def test_configured_root_secret_must_match_active_database_key(
    auth_database, monkeypatch
) -> None:
    monkeypatch.setenv("OAM_ROOT_API_KEY", "synthetic-wrong-root")
    app = create_app(ROOT / "configs/oam.yaml")

    with pytest.raises(ConfigurationError, match="configured root key"):
        async with app.router.lifespan_context(app):
            pass


def test_dev_mode_rejects_configured_root_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OAM_ROOT_API_KEY", "synthetic-root")
    config = tmp_path / "dev.yaml"
    config.write_text("auth:\n  mode: dev\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="root key"):
        create_app(config)


def test_dev_migration_rejects_configured_root_secret(
    fresh_rootless_database, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OAM_ROOT_API_KEY", "synthetic-root")
    config = tmp_path / "dev.yaml"
    config.write_text("auth:\n  mode: dev\n", encoding="utf-8")

    assert migrate_from_environment(config) == 2
    with psycopg.connect(fresh_rootless_database) as connection:
        assert connection.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
