from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from oam.db import migrations


def test_repository_migrations_have_unique_versions() -> None:
    repository_migrations = migrations.load_migrations(Path(__file__).parents[2] / "migrations")
    assert [migration.version for migration in repository_migrations] == list(range(1, 22))


class MigrationCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._registry_checked = False

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))

    def fetchone(self) -> tuple[object, ...]:
        self._registry_checked = True
        return (None,)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class MigrationConnection:
    def __init__(self) -> None:
        self.cursor_value = MigrationCursor()
        self.events: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        finally:
            self.events.append("end")

    def cursor(self):
        return _CursorContext(self.cursor_value)


class _CursorContext:
    def __init__(self, cursor: MigrationCursor) -> None:
        self.cursor = cursor

    def __enter__(self) -> MigrationCursor:
        return self.cursor

    def __exit__(self, *args: object) -> None:
        return None


def test_migration_and_real_checksum_marker_share_one_serialized_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    migration_sql = "CREATE TABLE schema_migrations (version integer, name text, sha256 text);\n"
    (tmp_path / "0001_initial.sql").write_text(migration_sql, encoding="utf-8")
    connection = MigrationConnection()
    connect_arguments: list[tuple[str, bool]] = []

    def connect(dsn: str, *, autocommit: bool):
        connect_arguments.append((dsn, autocommit))
        return connection

    monkeypatch.setattr(migrations.psycopg, "connect", connect)

    applied = migrations.apply_migrations("postgresql://redacted", tmp_path)

    assert applied == 1
    assert connect_arguments == [("postgresql://redacted", True)]
    assert connection.events == ["begin", "end"]
    calls = connection.cursor_value.calls
    assert "pg_advisory_xact_lock" in calls[0][0]
    migration_index = next(index for index, call in enumerate(calls) if call[0] == migration_sql)
    marker_index = next(
        index for index, call in enumerate(calls) if "INSERT INTO schema_migrations" in call[0]
    )
    assert migration_index < marker_index
    assert calls[marker_index][1][2] == migrations.load_migrations(tmp_path)[0].sha256
