"""PostgreSQL work claim statements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

CLAIM_ONE_SQL = (
    "SELECT tenant_id, user_id, work_unit_id, operation_id, stage, attempt_id, "
    "fence, lease_expires_at FROM oam_claim_work(%s, %s, %s, %s)"
)

RENEW_LEASE_SQL = """
UPDATE work_units
SET lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
    updated_at = clock_timestamp()
WHERE tenant_id = %s
  AND user_id = %s
  AND work_unit_id = %s
  AND owner_id = %s
  AND fence = %s
  AND status = 'running'
  AND lease_expires_at > clock_timestamp()
RETURNING lease_expires_at
""".strip()


class ClaimCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    tenant_id: str
    user_id: str
    work_unit_id: str
    operation_id: str
    stage: str
    attempt_id: str
    fence: int
    lease_expires_at: datetime


def claim_one(
    cursor: ClaimCursor,
    *,
    worker_id: str,
    attempt_id: str,
    lease_seconds: int,
    user_concurrency: int,
) -> ClaimedWork | None:
    if not worker_id.strip():
        raise ValueError("worker_id cannot be empty")
    if not attempt_id.strip():
        raise ValueError("attempt_id cannot be empty")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if user_concurrency <= 0:
        raise ValueError("user_concurrency must be positive")
    cursor.execute(CLAIM_ONE_SQL, (worker_id, attempt_id, lease_seconds, user_concurrency))
    row = cursor.fetchone()
    if row is None:
        return None
    return ClaimedWork(
        tenant_id=str(row[0]),
        user_id=str(row[1]),
        work_unit_id=str(row[2]),
        operation_id=str(row[3]),
        stage=str(row[4]),
        attempt_id=str(row[5]),
        fence=int(row[6]),
        lease_expires_at=row[7],  # type: ignore[arg-type]
    )
