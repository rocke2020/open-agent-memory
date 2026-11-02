import json
from functools import partial

import pytest

from oam.config import TokenizerConfig
from oam.profiles.selection import pack_profile_request as pack
from oam.tokens import resolve_counter

pack_profile_request = partial(
    pack, counter=resolve_counter(TokenizerConfig(type="o200k_base")), max_memory_tokens=10000
)


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
    items = [{"id": "one", "text": "A" * 400}, {"id": "two", "text": "B" * 400}]
    packed = pack_profile_request(
        items,
        exact_fields=[{"value": "fixed"}],
        instruction="Use bullets",
        system="system instructions",
        max_input_tokens=140,
    )
    assert [item["id"] for item in packed.inputs] == ["one"]
    assert packed.uncovered == 1
    assert json.loads(packed.user)["exact_fields"] == [{"value": "fixed"}]
    assert (
        resolve_counter(TokenizerConfig(type="o200k_base")).count(
            "system instructions" + packed.user
        )
        <= 140
    )


def test_exact_fields_expose_their_citable_identifier_to_the_model() -> None:

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

    with pytest.raises(ValueError, match="budget"):
        pack_profile_request(
            [],
            exact_fields=[{"value": "x" * 1000}],
            instruction=None,
            system="system",
            max_input_tokens=50,
        )


def test_large_source_detail_does_not_exclude_a_supported_fact() -> None:

    item = {
        "id": "fact",
        "kind": "fact",
        "text": "偏" * 500,
        "source_passages": [{"source_id": "source", "passage_id": "p", "text": "源" * 1000}],
    }
    packed = pack_profile_request(
        [item], exact_fields=[], instruction=None, system="x" * 4059, max_input_tokens=1500
    )
    assert packed.inputs[0]["text"] == item["text"]
    assert packed.inputs[0]["source_passages"][0]["source_id"] == "source"
    assert packed.inputs[0]["source_passage_text_omitted"] is True


def test_request_without_any_supported_input_fails_before_generation() -> None:

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


def test_profile_budget_counts_tokens_instead_of_utf8_bytes() -> None:

    items = [{"id": "one", "text": "A" * 400}, {"id": "two", "text": "B" * 400}]
    packed = pack_profile_request(
        items, exact_fields=[], instruction=None, system="system", max_input_tokens=300
    )
    assert [item["id"] for item in packed.inputs] == ["one", "two"]
    assert packed.uncovered == 0


def test_profile_memory_budget_is_independent_of_complete_request_budget(tmp_path) -> None:
    from oam.config import load_config

    path = tmp_path / "profile.yaml"
    path.write_text("profile:\n  max_input_tokens: 16000\n  max_memory_tokens: 8000\n")
    config = load_config(path).config.profile
    assert config.max_input_tokens == 16000
    assert getattr(config, "max_memory_tokens", None) == 8000


def test_memory_budget_is_independent_from_system_and_enforces_total_cap() -> None:
    items = [{"id": "one", "text": "A" * 400}, {"id": "two", "text": "B" * 400}]
    short = pack_profile_request(
        items,
        system="system",
        max_input_tokens=2000,
        exact_fields=[],
        instruction=None,
        max_memory_tokens=180,
    )
    long = pack_profile_request(
        items,
        system="system " * 300,
        max_input_tokens=2000,
        exact_fields=[],
        instruction=None,
        max_memory_tokens=180,
    )
    assert [item["id"] for item in short.inputs] == ["one", "two"]
    assert long.inputs == short.inputs
    limited = pack_profile_request(
        items,
        system="system",
        max_input_tokens=2000,
        exact_fields=[],
        instruction=None,
        max_memory_tokens=160,
    )
    assert [item["id"] for item in limited.inputs] == ["one"]
    assert limited.uncovered == 1
    tight = pack_profile_request(
        items,
        system="system " * 300,
        max_input_tokens=400,
        exact_fields=[],
        instruction=None,
        max_memory_tokens=180,
    )
    assert len(tight.inputs) < len(long.inputs)


def test_memory_budget_omits_large_passage_text_before_dropping_fact() -> None:
    item = {
        "id": "fact",
        "text": "prefers English",
        "source_passages": [{"source_id": "s", "text": "源" * 1000}],
    }
    packed = pack_profile_request(
        [item],
        exact_fields=[],
        instruction=None,
        system="system",
        max_input_tokens=4000,
        max_memory_tokens=100,
    )
    assert len(packed.inputs) == 1
    assert packed.inputs[0]["source_passage_text_omitted"] is True
    assert packed.inputs[0]["source_passages"][0]["source_id"] == "s"
    assert packed.uncovered == 0


def test_profile_identity_changes_with_memory_budget() -> None:
    from oam.profiles.service import profile_identity

    assert profile_identity(b"i" * 32, None, 16000, "", "o200k_base", 8000) != (
        profile_identity(b"i" * 32, None, 16000, "", "o200k_base", 4000)
    )
