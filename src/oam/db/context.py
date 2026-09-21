"""Scoped PostgreSQL transaction context."""

from __future__ import annotations

from typing import Protocol

from oam.auth.credentials import AuthorizedScope

SET_SCOPE_CONTEXT_SQL = (
    "SELECT set_config('oam.tenant_id', %s, true), set_config('oam.scope_id', %s, true)"
)

UPSERT_TURN_DEADLINE_SQL = """
INSERT INTO turn_deadlines (tenant_id, scope_id, turn_key, deadline_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (tenant_id, scope_id, turn_key)
DO UPDATE SET deadline_at = LEAST(turn_deadlines.deadline_at, EXCLUDED.deadline_at)
RETURNING deadline_at
""".strip()


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...


def bind_scope(cursor: Cursor, authority: AuthorizedScope) -> None:
    """Bind credential-derived tenant/scope authority for the current transaction."""
    cursor.execute(SET_SCOPE_CONTEXT_SQL, (authority.tenant_id, authority.scope_id))
