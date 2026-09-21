from oam.contracts.profile import (
    ProfileRefreshAccepted,
    ProfileRefreshRequest,
    ProfileRefreshUnchanged,
    ProfileSection,
)


def test_profile_refresh_defaults_to_main_section() -> None:
    request = ProfileRefreshRequest()

    assert request.sections == [ProfileSection.MAIN]


def test_unchanged_profile_response_has_no_operation() -> None:
    response = ProfileRefreshUnchanged(profile_revision=7, generation_calls=0)

    assert response.status == "unchanged"
    assert response.generation_calls == 0
    assert not hasattr(response, "operation_id")


def test_changed_profile_response_is_an_async_receipt() -> None:
    response = ProfileRefreshAccepted(operation_id="op_123")

    assert response.status == "accepted"
    assert response.operation_id == "op_123"
