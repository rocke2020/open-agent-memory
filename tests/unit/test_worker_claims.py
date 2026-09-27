from datetime import UTC, datetime

from oam.workers import claims


class ClaimCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.executed: tuple[str, tuple[object, ...]] | None = None

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.executed = (query, parameters)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


def test_claim_one_calls_the_allowlisted_database_function() -> None:
    lease = datetime(2026, 9, 21, tzinfo=UTC)
    cursor = ClaimCursor(
        (
            "tenant-1",
            "scope-1",
            "unit-1",
            "op-1",
            "extract",
            "attempt-2",
            8,
            lease,
        )
    )

    claimed = claims.claim_one(
        cursor,
        worker_id="worker-1",
        attempt_id="attempt-2",
        lease_seconds=90,
        user_concurrency=2,
    )

    assert cursor.executed == (
        "SELECT tenant_id, user_id, work_unit_id, operation_id, stage, attempt_id, "
        "fence, lease_expires_at FROM oam_claim_work(%s, %s, %s, %s)",
        ("worker-1", "attempt-2", 90, 2),
    )
    assert claimed == claims.ClaimedWork(
        tenant_id="tenant-1",
        user_id="scope-1",
        work_unit_id="unit-1",
        operation_id="op-1",
        stage="extract",
        attempt_id="attempt-2",
        fence=8,
        lease_expires_at=lease,
    )


def test_claim_one_returns_none_when_no_work_is_ready() -> None:
    cursor = ClaimCursor(None)

    assert (
        claims.claim_one(
            cursor,
            worker_id="worker-1",
            attempt_id="attempt-1",
            lease_seconds=90,
            user_concurrency=2,
        )
        is None
    )


def test_heartbeat_is_guarded_by_owner_fence_status_and_live_lease() -> None:
    sql = claims.RENEW_LEASE_SQL

    assert "owner_id = %s" in sql
    assert "fence = %s" in sql
    assert "status = 'running'" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
    assert "RETURNING lease_expires_at" in sql
