from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oam.operations import publication, state


def test_operation_follows_accepted_running_success_path() -> None:
    current = state.transition_operation(
        state.OperationStatus.ACCEPTED, state.OperationStatus.RUNNING
    )

    assert state.transition_operation(current, state.OperationStatus.SUCCEEDED) is (
        state.OperationStatus.SUCCEEDED
    )


@pytest.mark.parametrize(
    "terminal",
    [
        "succeeded",
        "partial_failed",
        "failed_terminal",
        "cancelled",
    ],
)
def test_terminal_operation_cannot_return_to_running(terminal: str) -> None:
    with pytest.raises(state.InvalidStateTransition, match="terminal"):
        state.transition_operation(state.OperationStatus(terminal), state.OperationStatus.RUNNING)


def test_work_unit_retries_only_through_retry_wait() -> None:
    waiting = state.transition_work_unit(
        state.WorkUnitStatus.RUNNING, state.WorkUnitStatus.RETRY_WAIT
    )

    assert state.transition_work_unit(waiting, state.WorkUnitStatus.RUNNING) is (
        state.WorkUnitStatus.RUNNING
    )
    with pytest.raises(state.InvalidStateTransition):
        state.transition_work_unit(
            state.WorkUnitStatus.FAILED_TERMINAL, state.WorkUnitStatus.RUNNING
        )


def _publication_context(**changes: object) -> publication.PublicationContext:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    values: dict[str, object] = {
        "now": now,
        "operation_status": state.OperationStatus.RUNNING,
        "work_unit_status": state.WorkUnitStatus.RUNNING,
        "claim_owner_id": "worker-1",
        "attempt_owner_id": "worker-1",
        "claim_fence": 7,
        "attempt_fence": 7,
        "lease_expires_at": now + timedelta(seconds=30),
        "operation_cancellation_revision": 0,
        "current_generation_id": "gen-1",
        "attempt_generation_id": "gen-1",
        "current_input_digest": b"a" * 32,
        "attempt_input_digest": b"a" * 32,
        "current_policy_epoch": 4,
        "attempt_policy_epoch": 4,
        "input_eligible": True,
    }
    values.update(changes)
    return publication.PublicationContext(**values)


def test_live_matching_claim_is_eligible_to_publish() -> None:
    decision = publication.check_publication(_publication_context())

    assert decision.allowed is True
    assert decision.denial is None


@pytest.mark.parametrize(
    ("changes", "denial"),
    [
        ({"attempt_fence": 6}, "stale_fence"),
        (
            {"lease_expires_at": datetime(2026, 9, 21, tzinfo=UTC)},
            "expired_lease",
        ),
        (
            {"operation_cancellation_revision": 1},
            "cancelled_operation",
        ),
        (
            {"attempt_generation_id": "old"},
            "generation_changed",
        ),
        ({"input_eligible": False}, "ineligible_input"),
        (
            {"attempt_policy_epoch": 3},
            "policy_changed",
        ),
    ],
)
def test_stale_or_ineligible_attempt_cannot_publish(
    changes: dict[str, object], denial: str
) -> None:
    decision = publication.check_publication(_publication_context(**changes))

    assert decision.allowed is False
    assert decision.denial is publication.PublicationDenial(denial)
