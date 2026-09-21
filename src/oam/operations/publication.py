"""Stale-publication eligibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from oam.operations.state import OperationStatus, WorkUnitStatus


class PublicationDenial(StrEnum):
    CANCELLED_OPERATION = "cancelled_operation"
    UNIT_NOT_RUNNING = "unit_not_running"
    STALE_OWNER = "stale_owner"
    STALE_FENCE = "stale_fence"
    EXPIRED_LEASE = "expired_lease"
    GENERATION_CHANGED = "generation_changed"
    INPUT_CHANGED = "input_changed"
    POLICY_CHANGED = "policy_changed"
    INELIGIBLE_INPUT = "ineligible_input"


@dataclass(frozen=True, slots=True)
class PublicationContext:
    now: datetime
    operation_status: OperationStatus
    work_unit_status: WorkUnitStatus
    claim_owner_id: str
    attempt_owner_id: str
    claim_fence: int
    attempt_fence: int
    lease_expires_at: datetime
    operation_cancellation_revision: int
    current_generation_id: str
    attempt_generation_id: str
    current_input_digest: bytes
    attempt_input_digest: bytes
    current_policy_epoch: int
    attempt_policy_epoch: int
    input_eligible: bool


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    allowed: bool
    denial: PublicationDenial | None


def check_publication(context: PublicationContext) -> PublicationDecision:
    """Apply every volatile authority check immediately before publishing."""
    if (
        context.operation_status is OperationStatus.CANCELLED
        or context.operation_cancellation_revision > 0
    ):
        return _deny(PublicationDenial.CANCELLED_OPERATION)
    if context.work_unit_status is not WorkUnitStatus.RUNNING:
        return _deny(PublicationDenial.UNIT_NOT_RUNNING)
    if context.claim_owner_id != context.attempt_owner_id:
        return _deny(PublicationDenial.STALE_OWNER)
    if context.claim_fence != context.attempt_fence:
        return _deny(PublicationDenial.STALE_FENCE)
    if context.lease_expires_at <= context.now:
        return _deny(PublicationDenial.EXPIRED_LEASE)
    if context.current_generation_id != context.attempt_generation_id:
        return _deny(PublicationDenial.GENERATION_CHANGED)
    if context.current_input_digest != context.attempt_input_digest:
        return _deny(PublicationDenial.INPUT_CHANGED)
    if context.current_policy_epoch != context.attempt_policy_epoch:
        return _deny(PublicationDenial.POLICY_CHANGED)
    if not context.input_eligible:
        return _deny(PublicationDenial.INELIGIBLE_INPUT)
    return PublicationDecision(allowed=True, denial=None)


def _deny(reason: PublicationDenial) -> PublicationDecision:
    return PublicationDecision(allowed=False, denial=reason)
