"""Intake capacity derived from unfinished durable operations."""

from __future__ import annotations

from typing import Protocol

from oam.auth.credentials import AuthorizedUser


class QueueAdmissionFull(RuntimeError):
    """This user's unfinished root requests occupy the configured intake capacity."""


class Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


PENDING_ROOTS_SQL = """
WITH active AS (
    SELECT o.tenant_id,o.user_id,o.operation_id,o.kind,o.retry_of_operation_id,o.stage_state
    FROM operations o
    WHERE o.tenant_id = %s AND o.user_id = %s
      AND o.kind <> 'cleanup_source'
      AND (o.status IN ('accepted', 'running') OR EXISTS (
          SELECT 1 FROM work_units w
          WHERE (w.tenant_id,w.user_id,w.operation_id)=
                (o.tenant_id,o.user_id,o.operation_id)
            AND w.status IN ('queued','running','retry_wait')
      ))
), obligations AS (
    SELECT CASE WHEN kind='consolidate' AND retry_of_operation_id IS NULL
                THEN COALESCE(NULLIF(stage_state->>'source_operation_id',''),operation_id)
                ELSE operation_id END AS root_id
    FROM active
    WHERE NOT (kind='consolidate' AND retry_of_operation_id IS NULL
               AND stage_state->>'l2_batch_id' IS NOT NULL)
    UNION
    SELECT bm.source_operation_id
    FROM l2_batch_members bm
    JOIN active a ON (a.tenant_id,a.user_id,a.stage_state->>'l2_batch_id')=
                     (bm.tenant_id,bm.user_id,bm.batch_id)
    WHERE bm.status='pending'
)
SELECT count(DISTINCT root_id) FROM obligations
""".strip()


def require_queue_capacity(cursor: Cursor, authority: AuthorizedUser, max_pending: int) -> None:
    """Check after reuse, under the acceptance transaction's user row lock.

    A URL precheck may call this without a lock because acceptance repeats it
    authoritatively after fetching. Required delayed consolidation shares its
    source operation's slot; caller-requested retries have their own slot.
    """
    if max_pending <= 0:
        raise ValueError("max_pending must be positive")
    cursor.execute(PENDING_ROOTS_SQL, (authority.tenant_id, authority.user_id))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("queue capacity could not be read")
    pending_roots = row[0]
    if not isinstance(pending_roots, int):
        raise RuntimeError("queue capacity count is invalid")
    if pending_roots >= max_pending:
        raise QueueAdmissionFull("user queue capacity is full")
