from pathlib import Path

import pytest

from oam.config import load_config
from oam.contracts.resources import EvidenceResourceCreate, ResourceMediaType


def test_evidence_resource_rejects_bytes_over_the_release_limit() -> None:
    config = load_config(Path(__file__).parents[2] / "configs" / "oam.yaml").config
    resource = EvidenceResourceCreate(
        resource_id="report-1",
        media_type=ResourceMediaType.PDF,
        filename="report.pdf",
        byte_length=20 * 1024 * 1024 + 1,
        sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="20971520"):
        resource.validate_limits(config)


def test_evidence_resource_accepts_exact_limit_and_never_exposes_learning_flag() -> None:
    resource = EvidenceResourceCreate(
        resource_id="report-1",
        media_type="application/pdf",
        filename="report.pdf",
        byte_length=20 * 1024 * 1024,
        sha256="a" * 64,
    )

    assert resource.resource_type == "evidence"
    assert "learning_eligible" not in resource.model_dump()


@pytest.mark.parametrize("limit", [10, 30_000_000])
def test_resource_byte_limit_follows_selected_config(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text(f"resource:\n  max_bytes: {limit}\n")
    config = load_config(path).config
    values = {
        "resource_id": "report-1",
        "media_type": "application/pdf",
        "filename": "report.pdf",
        "sha256": "a" * 64,
    }

    EvidenceResourceCreate(**values, byte_length=limit).validate_limits(config)
    with pytest.raises(ValueError, match=str(limit)):
        EvidenceResourceCreate(**values, byte_length=limit + 1).validate_limits(config)
