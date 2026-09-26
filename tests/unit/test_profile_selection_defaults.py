from oam.contracts.retain import RecallRequest
from oam.contracts.search import SearchManyRequest, SearchRequest


def test_public_recall_defaults_to_main_and_explicit_empty_disables_profile() -> None:
    assert RecallRequest(user_id="alice", query="q").profile_sections == ["main"]
    assert SearchRequest(query="q").profile_sections == ["main"]
    assert SearchManyRequest(queries=["q"]).profile_sections == ["main"]
    assert RecallRequest(user_id="alice", query="q", profile_sections=[]).profile_sections == []
