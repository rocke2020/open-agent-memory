"""Prepare existing source work before a requested Profile generation."""

import hashlib
import secrets

from oam.operations.repository import (
    OperationNotRetryable,
    OperationRepository,
    RetryRequiresPlanning,
    RetryRequiresReconciliation,
)

_INPUT_WORK_SQL = """
SELECT o.source_id,o.operation_id,o.status,w.work_unit_id,w.status,
       o.retry_of_operation_id,EXISTS (
           SELECT 1 FROM operations child
           WHERE child.tenant_id=o.tenant_id AND child.user_id=o.user_id
             AND child.retry_of_operation_id=o.operation_id
       ) AS has_retry,
       NOT (COALESCE(o.stage_state,'{}'::jsonb) ?|
            ARRAY['insight_repairs','removal_operation_id']) AS can_retry_generic
FROM sources s JOIN operations o ON
  (o.tenant_id,o.user_id,o.source_id,o.source_version)=
  (s.tenant_id,s.user_id,s.source_id,s.active_version)
JOIN work_units w ON (w.tenant_id,w.user_id,w.operation_id)=
  (o.tenant_id,o.user_id,o.operation_id)
WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=ANY(%s)
  AND s.status='active' AND s.input_role='interaction'
  AND ((o.kind='index_interaction' AND w.stage IN ('index_source','index_semantic_links'))
       OR (o.kind='consolidate' AND w.stage='consolidate'))
  AND o.status IN ('accepted','running','partial_failed','failed_terminal','cancelled')
  AND w.status IN ('queued','running','retry_wait','failed_terminal','cancelled')
ORDER BY o.source_id,o.created_at,o.operation_id,w.work_unit_id
""".strip()


class InputsPreparationUnavailable(RuntimeError):
    """A required input cannot be prepared by a bounded automatic retry."""


def prepare_profile_dependencies(
    connection, authority, config, source_ids: tuple[str, ...]
) -> list[str]:
    """Reuse active work or retry failed roots once inside the caller's transaction.

    The caller holds the user's admission lock. Existing operation retry owns
    authorization, capacity and unresolved-outcome checks; callers must roll back
    the whole preparation transaction if any required source cannot proceed.
    """
    if not source_ids:
        return []
    source_ids = tuple(sorted(set(source_ids)))
    parameters = (authority.tenant_id, authority.user_id, list(source_ids))
    rows = connection.execute(_INPUT_WORK_SQL, parameters).fetchall()
    active_sources = {row[0] for row in rows if row[4] in {"queued", "running", "retry_wait"}}
    retry_ids = {
        row[1]
        for row in rows
        if row[0] not in active_sources
        and row[2] in {"partial_failed", "failed_terminal", "cancelled"}
        and row[4] in {"failed_terminal", "cancelled"}
        and row[5] is None
        and not row[6]
        and row[7]
    }
    for operation_id in sorted(retry_ids):
        retry_key = "profile-input:" + operation_id
        try:
            OperationRepository().retry(
                connection,
                authority,
                original_operation_id=operation_id,
                new_operation_id="op_profile_input_" + secrets.token_hex(12),
                idempotency_key=retry_key,
                request_hash=hashlib.sha256(retry_key.encode()).digest(),
                attempts_remaining=config.retry.max_attempts,
                max_pending_per_user=config.queue.max_pending_per_user,
            )
        except (OperationNotRetryable, RetryRequiresPlanning, RetryRequiresReconciliation) as error:
            raise InputsPreparationUnavailable(operation_id) from error
    if retry_ids:
        rows = connection.execute(_INPUT_WORK_SQL, parameters).fetchall()
    active = [row for row in rows if row[4] in {"queued", "running", "retry_wait"}]
    if {row[0] for row in active} != set(source_ids):
        raise InputsPreparationUnavailable("Some Profile inputs require operator attention")
    dependencies = sorted({row[3] for row in active})
    connection.execute(
        "UPDATE work_units SET available_at=clock_timestamp(),updated_at=clock_timestamp() "
        "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=ANY(%s) "
        "AND status IN ('queued','retry_wait') AND available_at>clock_timestamp()",
        (authority.tenant_id, authority.user_id, dependencies),
    )
    return dependencies
