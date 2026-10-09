import re
from pathlib import Path

import pytest

from oam.db import migrations

SCOPED_TABLES = (
    "scopes",
    "grants",
    "sources",
    "source_versions",
    "evidence",
    "passages",
    "memories",
    "support",
    "insights",
    "insight_support",
    "profiles",
    "operations",
    "work_units",
    "attempts",
    "usage_records",
    "turn_deadlines",
    "traces",
    "exports",
)


def _initial_sql() -> str:
    path = Path(__file__).parents[2] / "migrations" / "0001_initial.sql"
    return path.read_text(encoding="utf-8").lower()


def _table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"create table {table}\s*\((.*?)\n\);",
        sql,
        flags=re.DOTALL,
    )
    assert match is not None, table
    return match.group(1)


def test_initial_schema_has_required_records_and_scoped_composite_identities() -> None:
    sql = _initial_sql()

    for table in ("tenants", "principals", "credentials", *SCOPED_TABLES):
        assert f"create table {table}" in sql, table
    for table in SCOPED_TABLES:
        body = _table_body(sql, table)
        assert "tenant_id text not null" in body, table
        assert "scope_id text not null" in body, table
        assert "primary key (tenant_id, scope_id" in body, table


def test_original_source_versions_are_durable_postgresql_bytes() -> None:
    body = _table_body(_initial_sql(), "source_versions")

    assert "original_bytes bytea not null" in body
    assert "content_sha256 bytea not null" in body


def test_operation_identity_binds_principal_source_and_acceptance_sequence() -> None:
    body = _table_body(_initial_sql(), "operations")

    assert "principal_id text not null" in body
    for field in ("source_id text", "source_version integer", "accepted_seq bigint"):
        assert field in body
    assert "unique (tenant_id, scope_id, principal_id, kind, dedup_key)" in body
    assert "retry_of_operation_id text" in body


def test_source_less_operations_remain_possible_for_profile_refresh_and_export() -> None:
    body = _table_body(_initial_sql(), "operations")

    assert "source_id text," in body
    assert "source_version integer," in body
    assert "accepted_seq bigint," in body


def test_every_scoped_table_enables_and_forces_rls_with_denial_by_missing_context() -> None:
    sql = _initial_sql()

    assert "current_setting('oam.tenant_id', true)" in sql
    assert "current_setting('oam.scope_id', true)" in sql
    for table in SCOPED_TABLES:
        assert f"alter table {table} enable row level security" in sql, table
        assert f"alter table {table} force row level security" in sql, table
        assert f"create policy {table}_scope_policy on {table}" in sql, table


def test_runtime_roles_cannot_bypass_rls_and_queue_role_only_executes_claim_function() -> None:
    sql = _initial_sql()

    for role in ("oam_api", "oam_content", "oam_queue"):
        role_declaration = re.search(rf"create role {role} (.*?);", sql)
        assert role_declaration is not None, role
        assert "nosuperuser" in role_declaration.group(1)
        assert "nobypassrls" in role_declaration.group(1)
    assert "grant oam_content to oam_api" in sql
    assert "grant execute on function oam_claim_work" in sql
    assert not re.search(r"grant .* on (?:table )?\w+ to oam_queue(?:\s|;)", sql)
    assert "create policy operations_queue_owner_policy on operations" in sql
    assert "grant select on operations to oam_queue_owner" in sql


def test_claim_function_uses_skip_locked_database_time_fence_and_lease() -> None:
    sql = _initial_sql()

    function = re.search(
        r"create function oam_claim_work\(.*?\$function\$;",
        sql,
        flags=re.DOTALL,
    )
    assert function is not None
    text = function.group(0)
    assert "for update of w skip locked" in text
    assert "clock_timestamp()" in text
    assert "fence = candidate.fence + 1" in text
    assert "lease_expires_at" in text
    assert "security definer" in text
    assert "set search_path = pg_catalog" in text
    assert "p_attempt_id text" in text
    assert "insert into public.attempts" in text


def test_claim_function_recovers_expired_leases_but_not_cancelled_or_unsealed_work() -> None:
    sql = _initial_sql()
    function = re.search(
        r"create function oam_claim_work\(.*?\$function\$;",
        sql,
        flags=re.DOTALL,
    )
    assert function is not None
    text = function.group(0)
    assert "w.status = 'running' and w.lease_expires_at <= clock_timestamp()" in text
    assert "join public.operations as operation" in text
    assert "operation.cancellation_revision = 0" in text
    assert "operation.status in ('accepted', 'running')" in text
    assert "(operation.plan_sealed or w.stage = 'plan')" in text


def test_usage_trace_and_export_records_are_explicit_scoped_contracts() -> None:
    sql = _initial_sql()

    usage = _table_body(sql, "usage_records")
    assert "work_unit_id text not null" in usage
    assert "attempt_id text not null" in usage
    assert "meter text not null" in usage
    trace = _table_body(sql, "traces")
    assert "read_seq bigint not null" in trace
    assert "policy_epoch bigint not null" in trace
    export = _table_body(sql, "exports")
    assert "manifest jsonb not null" in export
    assert "artifact_bytes bytea" in export


def test_migration_file_does_not_record_a_placeholder_checksum() -> None:
    assert "recorded-by-migration-runner" not in _initial_sql()


def test_memory_removal_migration_has_scoped_target_operations() -> None:
    path = Path(__file__).parents[2] / "migrations" / "0026_memory_removal.sql"
    sql = path.read_text(encoding="utf-8").lower()

    assert "alter table operations add column removal_target_kind text" in sql
    assert "alter table operations add column removal_target_id text" in sql
    assert "check ((removal_target_kind is null) = (removal_target_id is null))" in sql
    assert "create unique index operations_removal_target_idx" in sql
    assert "where removal_target_kind is not null" in sql


def test_migration_loader_orders_versions_and_checksums_bytes(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("select 2;\n", encoding="utf-8")
    (tmp_path / "0001_first.sql").write_text("select 1;\n", encoding="utf-8")

    loaded = migrations.load_migrations(tmp_path)

    assert [item.version for item in loaded] == [1, 2]
    assert [item.name for item in loaded] == ["first", "second"]
    assert loaded[0].sha256 == "4a45092ccf992ea92250053a80b931b787924ba61648f420555511b84f10ab6c"


def test_migration_loader_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("select 1;\n", encoding="utf-8")
    (tmp_path / "0001_other.sql").write_text("select 2;\n", encoding="utf-8")

    with pytest.raises(migrations.MigrationError, match="duplicate migration version 1"):
        migrations.load_migrations(tmp_path)


def test_repository_migrations_can_be_loaded_for_a_fresh_database() -> None:
    directory = Path(__file__).parents[2] / "migrations"
    loaded = migrations.load_migrations(directory)
    assert loaded[0].version == 1
    assert loaded[-1].version == len(loaded)
