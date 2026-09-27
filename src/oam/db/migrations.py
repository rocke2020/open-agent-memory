"""Versioned migration discovery."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from oam.config import ConfigurationError, load_config
from oam.paths import default_config_path, default_migration_directory

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9][a-z0-9_-]*)\.sql$")
_LEGACY_ASSERTED_USER_V12_SHA256 = (
    "faa9644764132e555bfb2af50dd72f59e3ef187b451ebe34e2885bf5ac6bc93f"
)
_CANONICAL_ASSERTED_RESOURCE_V12_SHA256 = (
    "5e80a5aab6f9309f5df201357d30df1a6cd75af0ceda6a5e1f678fcb9bbba31c"
)


class MigrationError(ValueError):
    """A migration directory does not define one deterministic version sequence."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sha256: str


def load_migrations(directory: str | Path) -> tuple[Migration, ...]:
    """Load named SQL migrations in ascending version order."""
    discovered: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(Path(directory).glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version {version}")
        seen_versions.add(version)
        discovered.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(sorted(discovered, key=lambda migration: migration.version))


def apply_migrations(
    dsn: str, directory: str | Path, *, embedding_dimensions: int | None = None
) -> int:
    """Apply each new migration and reject checksum drift in applied versions."""
    planned = load_migrations(directory)
    if embedding_dimensions is None:
        embedding_dimensions = load_config(default_config_path()).config.index.embedding_dimensions
    if type(embedding_dimensions) is not int or not 1 <= embedding_dimensions <= 2000:
        raise MigrationError("embedding dimension must be between 1 and 2000")
    applied_count = 0
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('open-agent-memory:migrations'))")
        cursor.execute("SELECT to_regclass('public.schema_migrations')")
        registry_exists = cursor.fetchone()[0] is not None
        applied: dict[int, str] = {}
        if registry_exists:
            cursor.execute("SELECT version, sha256 FROM schema_migrations")
            applied = {int(version): str(sha256) for version, sha256 in cursor.fetchall()}

        for migration in planned:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != migration.sha256:
                    legacy_v12 = (
                        migration.version == 12
                        and migration.name == "asserted_resource_grants"
                        and migration.sha256 == _CANONICAL_ASSERTED_RESOURCE_V12_SHA256
                        and recorded == _LEGACY_ASSERTED_USER_V12_SHA256
                        and any(
                            item.version == 13
                            and item.name == "asserted_user_resource_grants"
                            and item.sha256 == _LEGACY_ASSERTED_USER_V12_SHA256
                            for item in planned
                        )
                        and any(
                            item.version == 14 and item.name == "asserted_user_function_permissions"
                            for item in planned
                        )
                    )
                    if not legacy_v12:
                        raise MigrationError(
                            f"applied migration {migration.version} checksum changed"
                        )
                continue
            cursor.execute(
                migration.path.read_text(encoding="utf-8").replace(
                    "__OAM_EMBEDDING_DIMENSIONS__", str(embedding_dimensions)
                )
            )
            cursor.execute(
                "INSERT INTO schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.sha256),
            )
            applied_count += 1
        if any(migration.name == "indexing_recall" for migration in planned):
            cursor.execute("SELECT embedding_dimensions FROM index_settings WHERE singleton")
            if cursor.fetchone()[0] != embedding_dimensions:
                raise MigrationError("initialized embedding dimension differs from configuration")
    return applied_count


def migrate_from_environment(config_path: str | Path) -> int:
    """CLI boundary for applying migrations with the configured operator DSN."""
    try:
        resolved = load_config(config_path)
    except ConfigurationError:
        print("invalid OAM configuration", file=sys.stderr)
        return 2
    dsn_environment_name = resolved.config.database.migration_dsn_env
    dsn = os.environ.get(dsn_environment_name)
    if not dsn:
        print(
            f"missing required environment variable {dsn_environment_name}",
            file=sys.stderr,
        )
        return 2
    configured_root = os.environ.get(resolved.config.auth.root_key_env)
    if configured_root == "":
        print(f"{resolved.config.auth.root_key_env} cannot be empty", file=sys.stderr)
        return 2
    if resolved.config.auth.mode == "dev" and configured_root is not None:
        print("dev authentication cannot use a configured root key", file=sys.stderr)
        return 2
    if configured_root is not None and os.environ.get("OAM_BOOTSTRAP_OUTPUT"):
        print("choose a supplied root key or an output destination", file=sys.stderr)
        return 2
    try:
        apply_migrations(
            dsn,
            default_migration_directory(),
            embedding_dimensions=resolved.config.index.embedding_dimensions,
        )
    except (MigrationError, OSError, psycopg.Error):
        print("PostgreSQL migration failed", file=sys.stderr)
        return 1
    from oam.auth.bootstrap import bootstrap_from_environment

    if configured_root is None and (
        resolved.config.auth.mode == "dev"
        or (
            resolved.config.auth.mode in (None, "trusted")
            and not os.environ.get("OAM_BOOTSTRAP_OUTPUT")
        )
    ):
        with psycopg.connect(dsn) as connection:
            phase = connection.execute(
                "SELECT phase FROM bootstrap_state WHERE singleton = true"
            ).fetchone()[0]
            has_credentials = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM credentials)"
            ).fetchone()[0]
            if phase == "uninitialized" and (
                resolved.config.auth.mode == "dev" or not has_credentials
            ):
                connection.execute(
                    "INSERT INTO tenants(tenant_id,state) VALUES ('default','active') "
                    "ON CONFLICT (tenant_id) DO NOTHING"
                )
                return 0
            if phase == "handed_off" or (phase == "uninitialized" and has_credentials):
                has_active_root = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM credentials c JOIN principals p "
                    "ON (p.tenant_id,p.principal_id)=(c.tenant_id,c.principal_id) "
                    "JOIN tenants t ON t.tenant_id=c.tenant_id "
                    "WHERE c.state='active' AND p.state='active' "
                    "AND p.role='administrator' AND t.state='active' "
                    "AND (c.expires_at IS NULL OR c.expires_at > statement_timestamp()))"
                ).fetchone()[0]
                if has_active_root:
                    return 0
                print("keyed database has no active root credential", file=sys.stderr)
                return 1
    return bootstrap_from_environment(config_path)
