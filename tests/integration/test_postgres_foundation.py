import os
import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.auth.credentials import digest_secret
from oam.db.migrations import MigrationError, apply_migrations

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


def test_bilingual_migration_preserves_active_legacy_fts_until_rebuild(tmp_path) -> None:
    dsn = _isolated_dsn()
    database_name = "oam_fts_upgrade_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    candidate_dsn = make_conninfo(dsn, dbname=database_name)
    migrations = Path(__file__).parents[2] / "migrations"
    previous = tmp_path / "v7"
    previous.mkdir()
    for path in sorted(migrations.glob("*.sql")):
        if int(path.name[:4]) <= 7:
            shutil.copy2(path, previous / path.name)
    assert apply_migrations(candidate_dsn, previous) == 7
    with psycopg.connect(candidate_dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('t','active')")
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES ('t','u','synthetic','test','persistent','old_generation')"
        )
        connection.execute(
            "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,assertion_kind,"
            "generation_id,visible_from) "
            "VALUES ('t','u','fact','running','stated','old_generation',1)"
        )
        connection.execute(
            "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,memory_id,"
            "generation_id,content_text,locator,embedding,lexical_text) "
            "VALUES ('t','u','fact','fact','memory','fact','old_generation','running','{}',"
            "%s::vector,'running')",
            (str([1.0] + [0.0] * 1023),),
        )
        assert connection.execute(
            "SELECT lexical @@ plainto_tsquery('simple','running') FROM search_units"
        ).fetchone()[0]
    assert apply_migrations(candidate_dsn, migrations) >= 1
    with psycopg.connect(candidate_dsn) as connection:
        legacy_match, new_match, generation = connection.execute(
            "SELECT lexical @@ plainto_tsquery('simple','running'),"
            "lexical_bilingual @@ plainto_tsquery('english','running'),"
            "u.index_generation_id FROM search_units s JOIN users u USING (tenant_id,user_id)"
        ).fetchone()
    assert (legacy_match, new_match, generation) == (True, True, "old_generation")


def test_asserted_user_grants_upgrade_without_expanding_keyed_user_grants(tmp_path) -> None:
    dsn = _isolated_dsn()
    database_name = "oam_asserted_grants_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    candidate_dsn = make_conninfo(dsn, dbname=database_name)
    migrations = Path(__file__).parents[2] / "migrations"
    previous = tmp_path / "v11"
    previous.mkdir()
    for path in sorted(migrations.glob("*.sql")):
        if int(path.name[:4]) <= 11:
            shutil.copy2(path, previous / path.name)
    assert apply_migrations(candidate_dsn, previous) == 11
    with psycopg.connect(candidate_dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('tenant','active')")
        registered = connection.execute(
            "SELECT oam_register_asserted_user('tenant','alice')"
        ).fetchone()[0]
        assert registered is True
        before = connection.execute(
            "SELECT capabilities FROM grants WHERE tenant_id='tenant' AND user_id='alice'"
        ).fetchone()[0]
        assert "resource:write" not in before
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES ('tenant','restricted','synthetic','personal','persistent')"
        )
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES ('tenant','user:restricted','user','active')"
        )
        connection.execute(
            "INSERT INTO grants(tenant_id,user_id,principal_id,capabilities) "
            "VALUES ('tenant','restricted','user:restricted',"
            "ARRAY['memory:read','memory:append','operation:reconcile'])"
        )
        connection.execute(
            "INSERT INTO credentials(tenant_id,credential_id,principal_id,secret_digest,state) "
            "VALUES ('tenant','restricted','user:restricted',%s,'active')",
            (b"r" * 32,),
        )
    assert apply_migrations(candidate_dsn, migrations) == 3
    with psycopg.connect(candidate_dsn) as connection:
        after = connection.execute(
            "SELECT capabilities FROM grants WHERE tenant_id='tenant' AND user_id='alice'"
        ).fetchone()[0]
        assert {"resource:write", "source:delete", "debug:read", "export:read"} <= set(after)
        restricted = connection.execute(
            "SELECT capabilities FROM grants WHERE tenant_id='tenant' AND user_id='restricted'"
        ).fetchone()[0]
        assert set(restricted) == {"memory:read", "memory:append", "operation:reconcile"}


def test_legacy_asserted_v12_upgrades_and_restricts_function(tmp_path) -> None:
    dsn = _isolated_dsn()
    database_name = "oam_asserted_legacy_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    candidate_dsn = make_conninfo(dsn, dbname=database_name)
    migrations = Path(__file__).parents[2] / "migrations"
    previous = tmp_path / "legacy_v12"
    previous.mkdir()
    for path in sorted(migrations.glob("*.sql")):
        if int(path.name[:4]) <= 11:
            shutil.copy2(path, previous / path.name)
    shutil.copy2(
        migrations / "0013_asserted_user_resource_grants.sql",
        previous / "0012_asserted_user_resource_grants.sql",
    )
    assert apply_migrations(candidate_dsn, previous) == 12

    assert apply_migrations(candidate_dsn, migrations) == 2
    assert apply_migrations(candidate_dsn, migrations) == 0
    with psycopg.connect(candidate_dsn) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(version,) for version in range(1, 15)]
        owner, api_can_execute, public_can_execute = connection.execute(
            "SELECT pg_get_userbyid(p.proowner),"
            "has_function_privilege('oam_api',p.oid,'EXECUTE'),"
            "EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,"
            "acldefault('f',p.proowner))) acl WHERE acl.grantee=0 "
            "AND acl.privilege_type='EXECUTE') "
            "FROM pg_proc p WHERE p.oid="
            "'public.oam_register_asserted_user(text,text)'::regprocedure"
        ).fetchone()
        assert (owner, api_can_execute, public_can_execute) == ("oam_auth_owner", True, False)

    tampered = tmp_path / "tampered_v12"
    tampered.mkdir()
    for path in migrations.glob("*.sql"):
        shutil.copy2(path, tampered / path.name)
    canonical_v12 = tampered / "0012_asserted_resource_grants.sql"
    canonical_v12.write_text(canonical_v12.read_text() + "-- checksum drift\n")
    with pytest.raises(MigrationError, match="applied migration 12 checksum changed"):
        apply_migrations(candidate_dsn, tampered)


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
