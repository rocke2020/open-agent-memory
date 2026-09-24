import os
import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

pytestmark = pytest.mark.integration


def _isolated_dsn() -> str:
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    explicitly_isolated = os.environ.get("OAM_TEST_POSTGRES_ISOLATED") == "1"
    if not dsn or not explicitly_isolated:
        pytest.skip(
            "set OAM_TEST_POSTGRES_DSN and OAM_TEST_POSTGRES_ISOLATED=1 for an empty test database"
        )
    return dsn


def test_initial_migration_creates_forced_rls_and_non_bypass_roles() -> None:
    dsn = _isolated_dsn()
    migration_directory = Path(__file__).parents[2] / "migrations"

    apply_migrations(dsn, migration_directory)

    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('sources', 'operations', 'work_units', 'traces', 'exports')"
        )
        tables = {name: (rls, forced) for name, rls, forced in cursor.fetchall()}
        cursor.execute(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname IN ('oam_api', 'oam_content', 'oam_queue')"
        )
        roles = {name: (superuser, bypass) for name, superuser, bypass in cursor.fetchall()}

    assert tables == {
        "exports": (True, True),
        "operations": (True, True),
        "sources": (True, True),
        "traces": (True, True),
        "work_units": (True, True),
    }
    assert roles == {
        "oam_api": (False, False),
        "oam_content": (False, False),
        "oam_queue": (False, False),
    }


def test_current_content_schema_uses_tenant_and_user_identity() -> None:
    dsn = _isolated_dsn()
    apply_migrations(dsn, Path(__file__).parents[2] / "migrations")
    with psycopg.connect(dsn) as connection:
        columns = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name IN "
            "('users','grants','sources','source_versions','search_units','work_units') "
            "AND column_name IN ('user_id','user_id') ORDER BY table_name,column_name"
        ).fetchall()
    assert columns == [
        ("grants", "user_id"),
        ("search_units", "user_id"),
        ("source_versions", "user_id"),
        ("sources", "user_id"),
        ("users", "user_id"),
        ("work_units", "user_id"),
    ]


def test_user_key_migration_refuses_existing_principal_name_collision(tmp_path) -> None:
    dsn = _isolated_dsn()
    database_name = "oam_auth_collision_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    candidate_dsn = make_conninfo(dsn, dbname=database_name)
    migrations = Path(__file__).parents[2] / "migrations"
    old_migrations = tmp_path / "v4"
    old_migrations.mkdir()
    for path in sorted(migrations.glob("*.sql")):
        if int(path.name[:4]) <= 4:
            shutil.copy2(path, old_migrations / path.name)
    assert apply_migrations(candidate_dsn, old_migrations) == 4
    with psycopg.connect(candidate_dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        for user_id in ("alice", "bob"):
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
                "VALUES ('t',%s,%s,'personal','persistent')",
                (user_id, user_id),
            )
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('t','user:alice','user','active')"
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES ('t','old','user:alice',%s,'active')",
            (digest_secret("synthetic-old-key"),),
        )
        connection.execute(
            "INSERT INTO grants(tenant_id,user_id,principal_id,capabilities) "
            "VALUES ('t','bob','user:alice',ARRAY['memory:read'])"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="principal name collision"):
        apply_migrations(candidate_dsn, migrations)
    with psycopg.connect(candidate_dsn) as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT count(*) FROM grants WHERE tenant_id='t' AND user_id='alice'"
            ).fetchone()[0]
            == 0
        )
