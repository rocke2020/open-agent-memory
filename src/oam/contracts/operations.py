from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperationStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationReadiness(FrozenContract):
    accepted: bool = False
    source_searchable: bool = False
    memory_ready: bool = False
    insights_ready: bool = False
    profile_current: bool = False


class OperationError(FrozenContract):
    code: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class OperationResponse(FrozenContract):
    operation_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: OperationStatus
    readiness: OperationReadiness
    error: OperationError | None = None


class ReconcileAttemptRequest(FrozenContract):
    work_unit_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    fence: PositiveInt
    disposition: Literal["succeeded", "failed"]
    response_ref: str = Field(min_length=1)
    usage: dict[str, Any] = Field(default_factory=dict)
