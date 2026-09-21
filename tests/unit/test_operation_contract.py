from oam.contracts.operations import OperationReadiness, OperationResponse, OperationStatus


def test_operation_keeps_memory_and_insight_readiness_separate() -> None:
    operation = OperationResponse(
        operation_id="op_1",
        kind="retain",
        status="processing",
        readiness={
            "accepted": True,
            "source_searchable": True,
            "memory_ready": True,
            "insights_ready": False,
            "profile_current": False,
        },
    )

    assert operation.status is OperationStatus.PROCESSING
    assert operation.readiness == OperationReadiness(
        accepted=True,
        source_searchable=True,
        memory_ready=True,
        insights_ready=False,
        profile_current=False,
    )


def test_operation_error_is_structured_and_safe() -> None:
    operation = OperationResponse(
        operation_id="op_2",
        kind="resource",
        status="failed",
        readiness={"accepted": True},
        error={
            "code": "DocumentParseFailed",
            "stage": "parse",
            "retryable": False,
            "details": {"classification": "encrypted"},
        },
    )

    assert operation.error is not None
    assert operation.error.code == "DocumentParseFailed"
    assert "bytes" not in operation.error.model_dump()
