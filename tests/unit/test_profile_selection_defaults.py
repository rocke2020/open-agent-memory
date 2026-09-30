from oam.contracts.retain import RecallRequest
from oam.contracts.search import SearchManyRequest, SearchRequest


def test_public_recall_defaults_to_main_and_explicit_empty_disables_profile() -> None:
    assert RecallRequest(user_id="alice", query="q").include_profile is True
    assert SearchRequest(query="q").include_profile is True
    assert SearchManyRequest(queries=["q"]).include_profile is True
    assert RecallRequest(user_id="alice", query="q", include_profile=False).include_profile is False
