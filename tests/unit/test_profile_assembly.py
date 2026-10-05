from pathlib import Path

import pytest

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.profiles import assembly


def test_recall_includes_the_whole_stored_document_by_default(monkeypatch) -> None:
    content = "## Main content\n\nThe user likes short answers [1].\n\n## References\n\n1. Chat"
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config

    class Profile:
        def __init__(self, _config, _pool):
            pass

        def get(self, _authority, _limit):
            return {"content": content, "policy_epoch": 1, "references": [{"source_id": "chat"}]}

        def check_policy(self, _authority, _epoch):
            pass

    class Recall:
        def search(self, _authority, request, *, deadline_at=None):
            assert request.include_profile is False
            return {"context": "Memory detail", "context_tokens": 4}

    monkeypatch.setattr(assembly, "ProfileService", Profile)
    result = assembly.search_with_profile(config, None, Recall(), None, SearchRequest(query="q"))

    assert content in result["context"]
    assert "Memory detail" in result["context"]


def test_explicit_profile_opt_out_skips_profile_read(monkeypatch) -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config

    class NoProfileRead:
        def __init__(self, *_args):
            raise AssertionError("Profile should not be read")

    class Recall:
        def search(self, _authority, request, *, deadline_at=None):
            return {"context": "Memory detail", "context_tokens": 4}

    monkeypatch.setattr(assembly, "ProfileService", NoProfileRead)
    result = assembly.search_with_profile(
        config,
        None,
        Recall(),
        None,
        SearchRequest(query="q", include_profile=False),
    )
    assert result["context"] == "Memory detail"


def test_disabled_profile_feature_does_not_read_or_inject_profile(monkeypatch) -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"profile": config.profile.model_copy(update={"enabled": False})}
    )

    class NoProfileRead:
        def __init__(self, *_args):
            raise AssertionError("Profile should not be read")

    class Recall:
        def search(self, _authority, request, *, deadline_at=None):
            return {"context": "Memory detail", "context_tokens": 4}

    monkeypatch.setattr(assembly, "ProfileService", NoProfileRead)
    with pytest.raises(NotImplementedError, match="Profile feature is disabled"):
        assembly.search_with_profile(
            config,
            None,
            Recall(),
            None,
            SearchRequest(query="q", include_profile=True),
        )
