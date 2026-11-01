import json

import pytest


def test_profile_candidate_query_scopes_before_limit_and_preserves_ranked_order() -> None:
    from oam.profiles.selection import rank_profile_candidates

    class Connection:
        def execute(self, statement, params):
            assert "origin='memory'" in statement
            assert "kind IN ('fact','insight')" in statement
            assert statement.index("kind IN") < statement.index("LIMIT")
            assert params[1] == 3
            return self

        def fetchall(self):
            return [("relevant-fact",), ("relevant-insight",)]

    assert rank_profile_candidates(Connection(), (0.1, 0.2), 3) == [
        "relevant-fact",
        "relevant-insight",
    ]


def test_request_budget_includes_system_fields_and_does_not_split_inputs() -> None:
    from oam.profiles.selection import pack_profile_request
    from oam.tokens import estimate_units, estimated_tokens

    items = [{"id": "one", "text": "A" * 400}, {"id": "two", "text": "B" * 400}]
    packed = pack_profile_request(
        items,
        exact_fields=[{"value": "fixed"}],
        instruction="Use bullets",
        system="system instructions",
        max_input_tokens=560,
    )
    assert [item["id"] for item in packed.inputs] == ["one"]
    assert packed.uncovered == 1
    assert json.loads(packed.user)["exact_fields"] == [{"value": "fixed"}]
    assert estimated_tokens(estimate_units("system instructions" + packed.user)) <= 560
    assert len(("system instructions" + packed.user).encode("utf-8")) <= 560


def test_exact_fields_expose_their_citable_identifier_to_the_model() -> None:
    from oam.profiles.selection import pack_profile_request

    field = {
        "key": "response_language",
        "value": "English",
        "supports": [{"resource_id": "profile", "resource_version": 2}],
    }
    packed = pack_profile_request(
        [], exact_fields=[field], instruction=None, system="system", max_input_tokens=500
    )
    assert json.loads(packed.user)["exact_fields"][0]["id"] == "profile_resource:profile:2"


def test_request_fails_if_required_exact_fields_exceed_budget() -> None:
    from oam.profiles.selection import pack_profile_request

    with pytest.raises(ValueError, match="budget"):
        pack_profile_request(
            [],
            exact_fields=[{"value": "x" * 1000}],
            instruction=None,
            system="system",
            max_input_tokens=50,
        )


def test_large_source_detail_does_not_exclude_a_supported_fact() -> None:
    from oam.profiles.selection import pack_profile_request

    item = {
        "id": "fact",
        "kind": "fact",
        "text": "偏" * 500,
        "source_passages": [{"source_id": "source", "passage_id": "p", "text": "源" * 1000}],
    }
    packed = pack_profile_request(
        [item], exact_fields=[], instruction=None, system="x" * 4059, max_input_tokens=8192
    )
    assert packed.inputs[0]["text"] == item["text"]
    assert packed.inputs[0]["source_passages"][0]["source_id"] == "source"
    assert packed.inputs[0]["source_passage_text_omitted"] is True
    assert len(("x" * 4059 + packed.user).encode()) <= 8192


def test_request_without_any_supported_input_fails_before_generation() -> None:
    from oam.profiles.selection import pack_profile_request

    with pytest.raises(ValueError, match="no .* evidence"):
        pack_profile_request(
            [], exact_fields=[], instruction=None, system="system", max_input_tokens=500
        )


def test_profile_input_budget_is_configurable_and_positive(tmp_path) -> None:
    from oam.config import ConfigurationError, load_config

    path = tmp_path / "profile.yaml"
    path.write_text("profile:\n  max_input_tokens: 4321\n")
    assert load_config(path).config.profile.max_input_tokens == 4321
    path.write_text("profile:\n  max_input_tokens: 0\n")
    with pytest.raises(ConfigurationError):
        load_config(path)
