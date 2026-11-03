import pytest
from pydantic import ValidationError

from oam.contracts.profile import (
    ProfileRefreshAccepted,
    ProfileRefreshRequest,
    ProfileRefreshUnchanged,
)


def test_profile_refresh_accepts_one_optional_instruction_and_explicit_reset() -> None:
    request = ProfileRefreshRequest()

    assert request.instruction is None
    assert not request.reset_instruction
    assert (
        ProfileRefreshRequest(instruction="Return a JSON summary").instruction
        == "Return a JSON summary"
    )
    assert ProfileRefreshRequest(reset_instruction=True).reset_instruction
    with pytest.raises(ValidationError):
        ProfileRefreshRequest(instruction="JSON", reset_instruction=True)
    with pytest.raises(ValidationError):
        ProfileRefreshRequest.model_validate({"sections": ["main"]})
    with pytest.raises(ValidationError):
        ProfileRefreshRequest(instruction="   ")


def test_unchanged_profile_response_has_no_operation() -> None:
    response = ProfileRefreshUnchanged(profile_revision=7, generation_calls=0)

    assert response.status == "unchanged"
    assert response.generation_calls == 0
    assert not hasattr(response, "operation_id")


def test_changed_profile_response_is_an_async_receipt() -> None:
    response = ProfileRefreshAccepted(operation_id="op_123")

    assert response.status == "accepted"
    assert response.operation_id == "op_123"


def test_profile_refresh_accepts_explicit_full_or_incremental_mode() -> None:
    assert ProfileRefreshRequest().mode == "incremental"
    assert ProfileRefreshRequest(mode="full").mode == "full"
    assert ProfileRefreshRequest(mode="incremental").mode == "incremental"
    with pytest.raises(ValidationError):
        ProfileRefreshRequest.model_validate({"mode": "auto"})
    with pytest.raises(ValidationError):
        ProfileRefreshRequest.model_validate({"mode": "replace"})
