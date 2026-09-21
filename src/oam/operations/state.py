"""Operation and work-unit state transitions."""

from __future__ import annotations

from enum import StrEnum


class InvalidStateTransition(ValueError):
    """A durable record cannot take the requested state transition."""


class OperationStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


class WorkUnitStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


_OPERATION_TRANSITIONS = {
    OperationStatus.ACCEPTED: frozenset(
        {OperationStatus.RUNNING, OperationStatus.FAILED_TERMINAL, OperationStatus.CANCELLED}
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.PARTIAL_FAILED,
            OperationStatus.FAILED_TERMINAL,
            OperationStatus.CANCELLED,
        }
    ),
}

_WORK_UNIT_TRANSITIONS = {
    WorkUnitStatus.QUEUED: frozenset({WorkUnitStatus.RUNNING, WorkUnitStatus.CANCELLED}),
    WorkUnitStatus.RUNNING: frozenset(
        {
            WorkUnitStatus.RETRY_WAIT,
            WorkUnitStatus.SUCCEEDED,
            WorkUnitStatus.FAILED_TERMINAL,
            WorkUnitStatus.CANCELLED,
        }
    ),
    WorkUnitStatus.RETRY_WAIT: frozenset(
        {WorkUnitStatus.RUNNING, WorkUnitStatus.FAILED_TERMINAL, WorkUnitStatus.CANCELLED}
    ),
}


def transition_operation(current: OperationStatus, target: OperationStatus) -> OperationStatus:
    if target in _OPERATION_TRANSITIONS.get(current, frozenset()):
        return target
    suffix = " (terminal state)" if current not in _OPERATION_TRANSITIONS else ""
    raise InvalidStateTransition(
        f"operation cannot transition from {current.value} to {target.value}{suffix}"
    )


def transition_work_unit(current: WorkUnitStatus, target: WorkUnitStatus) -> WorkUnitStatus:
    if target in _WORK_UNIT_TRANSITIONS.get(current, frozenset()):
        return target
    suffix = " (terminal state)" if current not in _WORK_UNIT_TRANSITIONS else ""
    raise InvalidStateTransition(
        f"work unit cannot transition from {current.value} to {target.value}{suffix}"
    )
