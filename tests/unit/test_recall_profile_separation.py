from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from psycopg_pool import ConnectionPool

from oam.api.runtime import DatabaseRuntime
from oam.config import ModelsConfig, load_config
from oam.contracts.retain import RecallRequest
from oam.contracts.search import RecallLevel
from oam.profiles.service import ProfileService
from oam.retrieval.service import RecallService


@pytest.mark.parametrize("profiles_enabled", [True, False])
def test_recall_dispatch_uses_full_budget_without_profile_access(
    monkeypatch, profiles_enabled
) -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "models": ModelsConfig(),
            "profile": config.profile.model_copy(update={"enabled": profiles_enabled}),
        }
    )
    searched = []

    class Pool:
        @contextmanager
        def connection(self):
            yield object()

    def authorize(self, connection, actor, user_id, capability):
        assert (user_id, capability) == ("alice", "memory:read")
        return "authorized-alice"

    def search(self, authority, request, *, deadline_at):
        searched.append((authority, request, deadline_at))
        return {"items": [], "context": "eligible memory", "context_tokens": 7}

    def no_profile_read(*args, **kwargs):
        raise AssertionError("Recall must not read the Profile")

    monkeypatch.setattr(ProfileService, "get", no_profile_read)
    monkeypatch.setattr(DatabaseRuntime, "_authorize", authorize)
    monkeypatch.setattr(RecallService, "search", search)
    runtime = DatabaseRuntime(config, cast(ConnectionPool, Pool()), models=object())
    try:
        request = RecallRequest(user_id="alice", query="q", recall_level=RecallLevel.FAST)
        result = runtime.search("caller", "alice", request)
    finally:
        runtime._http.close()

    assert result["context"] == "eligible memory"
    assert len(searched) == 1
    authority, dispatched, deadline = searched[0]
    assert authority == "authorized-alice"
    assert dispatched is request
    assert dispatched.context_limit(config) == config.search.fast.context_max_tokens
    assert deadline is None
