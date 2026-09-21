from datetime import datetime

from oam.auth.credentials import AuthenticatedPrincipal, AuthorizedScope
from oam.db import context


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.calls.append((query, parameters))


def test_bind_scope_sets_transaction_local_tenant_and_scope_from_authority() -> None:
    principal = AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="writer-1",
        role="writer",
        expires_at=None,
    )
    authorized = AuthorizedScope.from_database_grant(
        principal,
        scope_id="scope-1",
        capabilities={"memory:read"},
    )
    cursor = RecordingCursor()

    context.bind_scope(cursor, authorized)

    assert cursor.calls == [
        (
            "SELECT set_config('oam.tenant_id', %s, true), set_config('oam.scope_id', %s, true)",
            ("tenant-1", "scope-1"),
        )
    ]


def test_turn_deadline_upsert_keeps_the_first_absolute_deadline() -> None:
    sql = context.UPSERT_TURN_DEADLINE_SQL

    assert "ON CONFLICT (tenant_id, scope_id, turn_key)" in sql
    assert "DO UPDATE SET deadline_at = LEAST" in sql
    assert "RETURNING deadline_at" in sql
    assert datetime.fromisoformat("2026-09-21T00:00:00+00:00").tzinfo is not None
