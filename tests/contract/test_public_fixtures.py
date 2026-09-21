from pathlib import Path

from oam.contracts.evidence import EvidenceAppendRequest
from oam.contracts.profile import ProfileRefreshRequest
from oam.contracts.resources import EvidenceResourceCreate
from oam.contracts.search import SearchRequest

FIXTURES = Path(__file__).parents[2] / "conformance" / "fixtures" / "contracts"


def test_public_request_fixtures_validate_against_the_runtime_contracts() -> None:
    fixtures = {
        "evidence-append.json": EvidenceAppendRequest,
        "resource-create.json": EvidenceResourceCreate,
        "search-fast.json": SearchRequest,
        "profile-refresh.json": ProfileRefreshRequest,
    }

    validated = {
        name: contract.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))
        for name, contract in fixtures.items()
    }

    assert validated["evidence-append.json"].messages[1].learning_eligible is False
    assert validated["search-fast.json"].profile_sections == []
    assert [section.value for section in validated["profile-refresh.json"].sections] == [
        "main",
        "preferences",
    ]
