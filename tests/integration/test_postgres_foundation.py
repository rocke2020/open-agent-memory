import os
from pathlib import Path

import psycopg
import pytest

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
