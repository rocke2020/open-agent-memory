"""Tenant and user PostgreSQL transaction context."""

from __future__ import annotations

from typing import Protocol

from oam.auth.credentials import AuthorizedUser

SET_USER_CONTEXT_SQL = (
    "SELECT set_config('oam.tenant_id', %s, true), set_config('oam.user_id', %s, true)"
)

UPSERT_TURN_DEADLINE_SQL = """
INSERT INTO turn_deadlines (tenant_id, user_id, turn_key, deadline_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (tenant_id, user_id, turn_key)
DO UPDATE SET deadline_at = LEAST(turn_deadlines.deadline_at, EXCLUDED.deadline_at)
RETURNING deadline_at
""".strip()


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...


def bind_user(cursor: Cursor, authority: AuthorizedUser) -> None:
    """Bind credential-derived tenant/user authority for the current transaction."""
    cursor.execute(SET_USER_CONTEXT_SQL, (authority.tenant_id, authority.user_id))
