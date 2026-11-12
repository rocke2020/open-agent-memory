"""Upgrade coverage for user activity and directory isolation."""

import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.db.migrations import apply_migrations

pytestmark = pytest.mark.integration
MIGRATIONS = Path(__file__).parents[2] / "migrations"


def test_user_activity_upgrade_backfills_public_roles_and_enforces_tenant_policy(tmp_path):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    name = f"oam_users_upgrade_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(dsn, dbname=name)
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in MIGRATIONS.glob("*.sql"):
        if int(migration.name[:4]) <= 36:
            shutil.copy2(migration, previous / migration.name)
    assert apply_migrations(dsn, previous) == 36
    with psycopg.connect(dsn) as connection:
        for tenant in ("a", "b"):
            connection.execute(
                "INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,)
            )
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
                "VALUES (%s,'user','user','personal','persistent')",
                (tenant,),
            )
        for index, role in enumerate(
            ("interaction", "evidence_resource", "profile_resource", "imported_claim"), start=1
        ):
            connection.execute(
                "INSERT INTO sources(tenant_id,user_id,source_id,input_role,status) "
                "VALUES ('a','user',%s,%s,'active')",
                (role, role),
            )
            connection.execute(
                "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,"
                "media_type,original_bytes,content_sha256,byte_length,status,received_at) "
                "VALUES ('a','user',%s,1,'text/plain',%s,%s,1,%s,%s::timestamptz)",
                (
                    role,
                    b"x",
                    hashlib.sha256(b"x").digest(),
                    "excluded" if index == 3 else "superseded",
                    f"2026-11-0{index}T00:00:00Z",
                ),
            )
    assert apply_migrations(dsn, MIGRATIONS) == 1
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT tenant_id,last_write_at::text FROM users ORDER BY tenant_id"
        ).fetchall()
        assert rows == [("a", "2026-11-03 00:00:00+00"), ("b", None)]
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (37,)
        connection.execute("SET LOCAL ROLE oam_user_list_owner")
        connection.execute("SELECT set_config('oam.admin_tenant_id', 'a', true)")
        assert connection.execute("SELECT tenant_id FROM users ORDER BY tenant_id").fetchall() == [
            ("a",)
        ]
