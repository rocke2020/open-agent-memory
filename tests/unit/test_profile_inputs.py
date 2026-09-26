import json
from contextlib import nullcontext

import pytest

from oam.auth.credentials import AuthorizedUser


class ProfileCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, query, parameters=()):
        self.connection.calls.append((query, parameters))
        if "/* profile:state */" in query:
            self.rows = [self.connection.state] if self.connection.state else []
        elif "/* profile:eligible */" in query:
            self.rows = self.connection.units
        elif "/* profile:resources */" in query:
            self.rows = self.connection.resources
        elif "/* profile:pending */" in query:
            self.rows = self.connection.pending
        elif "/* profile:insight_support */" in query:
            self.rows = self.connection.insight_links
        elif "/* profile:passages */" in query:
            self.rows = self.connection.passages
        elif "set_config('oam.tenant_id'" in query:
            self.rows = []
        else:
            raise AssertionError(f"unexpected Profile query: {query[:120]}")
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ProfileConnection:
    def __init__(self):
        self.calls = []
        self.state = {"state": "active", "policy_epoch": 4, "index_generation_id": "gen_1"}
        self.units = [
            {
                "unit_id": "fact_unit_1",
                "kind": "fact",
                "origin": "memory",
                "memory_id": "fact_1",
                "insight_id": None,
                "insight_revision": None,
                "content_text": "The user prefers Python examples.",
            },
            {
                "unit_id": "insight_unit_1",
                "kind": "insight",
                "origin": "memory",
                "memory_id": None,
                "insight_id": "insight_1",
                "insight_revision": 2,
                "content_text": "The user often works on backend services.",
            },
        ]
        self.resources = [
            {
                "source_id": "user-preference-examples",
                "source_version": 2,
                "media_type": "application/json",
                "original_bytes": json.dumps(
                    {
                        "resource_id": "user-preference-examples",
                        "resource_type": "profile",
                        "subtype": "preferences/v1",
                        "source_revision": "2",
                        "as_of": "2026-07-11T09:00:00+08:00",
                        "content": {"example_language": "Python", "domain": "backend"},
                    }
                ).encode(),
            }
        ]
        self.pending = []
        self.insight_links = [
            {"insight_id": "insight_1", "insight_revision": 2, "memory_id": "fact_1"}
        ]
        self.passages = [
            {
                "memory_id": "fact_1",
                "passage_id": "passage_1",
                "source_id": "chat_1",
                "source_version": 1,
                "normalized_text": "Please show Python examples.",
                "locator": {"message": 1},
            }
        ]

    def transaction(self):
        return nullcontext()

    def cursor(self, **_kwargs):
        return ProfileCursor(self)


def authority(capabilities=frozenset({"memory:read"})):
    return AuthorizedUser("cred_1", "tenant_1", "person_1", "user_1", capabilities)


def load(connection, user=None):
    from oam.profiles.inputs import load_profile_inputs

    return load_profile_inputs(connection, user or authority(), "gen_1")


def test_loader_returns_supported_memory_exact_fields_and_cited_prompt_inputs() -> None:
    connection = ProfileConnection()

    result = load(connection)

    assert set(result.supported) == {"fact_unit_1", "insight_unit_1"}
    assert result.supported["fact_unit_1"] == {
        "kind": "fact",
        "id": "fact_unit_1",
        "memory_id": "fact_1",
    }
    assert result.supported["insight_unit_1"] == {
        "kind": "insight",
        "id": "insight_unit_1",
        "insight_id": "insight_1",
        "revision": 2,
    }
    assert result.exact_fields == (
        {
            "section": "preferences",
            "key": "example_language",
            "value": "Python",
            "qualifier": "backend",
            "assertion_kind": "stated",
            "supports": [
                {
                    "kind": "profile_resource",
                    "resource_id": "user-preference-examples",
                    "resource_version": 2,
                    "subtype": "preferences/v1",
                    "source_revision": "2",
                    "as_of": "2026-07-11T09:00:00+08:00",
                    "content_path": "content.example_language",
                }
            ],
        },
    )
    assert result.eligible_support_ids == {
        "fact_unit_1",
        "insight_unit_1",
        "profile_resource:user-preference-examples:2",
    }
    assert result.prompt_inputs[0]["source_passages"] == [
        {
            "passage_id": "passage_1",
            "source_id": "chat_1",
            "source_version": 1,
            "text": "Please show Python examples.",
            "locator": {"message": 1},
        }
    ]
    assert result.prompt_inputs[1]["support_memory_ids"] == ["fact_1"]
    assert result.prompt_inputs[1]["source_passages"] == result.prompt_inputs[0]["source_passages"]
    assert result.pending_inputs == 0
    assert result.policy_epoch == 4
    assert len(result.identity) == 32
    eligible_sql = next(query for query, _ in connection.calls if "/* profile:eligible */" in query)
    assert "s.input_role='interaction'" in eligible_sql
    assert "u.origin='memory'" in eligible_sql
    assert all("accepted_seq" not in query for query, _ in connection.calls)


def test_evidence_only_change_does_not_dirty_profile_identity_but_coverage_does() -> None:
    connection = ProfileConnection()
    first = load(connection)

    connection.evidence_only_revision = 8
    assert load(connection).identity == first.identity

    connection.pending = [{"source_id": "chat_2"}]
    pending = load(connection)
    assert pending.pending_inputs == 1
    assert pending.identity != first.identity


def test_bounded_prompt_keeps_insights_when_many_facts_exist() -> None:
    connection = ProfileConnection()
    insight = connection.units[-1]
    connection.units = [
        {
            **connection.units[0],
            "unit_id": f"fact_unit_{index:03d}",
            "memory_id": f"fact_{index:03d}",
        }
        for index in range(100)
    ] + [insight]
    connection.passages = [
        {
            **connection.passages[0],
            "memory_id": f"fact_{index:03d}",
            "passage_id": f"passage_{index:03d}",
        }
        for index in range(100)
    ] + connection.passages

    inputs = load(connection)

    assert len(inputs.prompt_inputs) <= 64
    assert {item["kind"] for item in inputs.prompt_inputs} == {"fact", "insight"}
    assert len(inputs.supported) == 101


def test_prompt_limits_cited_passages_per_input() -> None:
    connection = ProfileConnection()
    connection.insight_links = [
        {"insight_id": "insight_1", "insight_revision": 2, "memory_id": f"fact_{index}"}
        for index in range(20)
    ]
    connection.passages = [
        {
            **connection.passages[0],
            "memory_id": f"fact_{index}",
            "passage_id": f"passage_{index}",
        }
        for index in range(20)
    ]

    inputs = load(connection)

    insight = next(item for item in inputs.prompt_inputs if item["kind"] == "insight")
    assert len(insight["source_passages"]) <= 8
    assert len(insight["support_memory_ids"]) == 20


def test_invalid_active_profile_resource_fails_instead_of_silently_dropping_fields() -> None:
    connection = ProfileConnection()
    connection.resources[0]["original_bytes"] = b'{"resource_type":"evidence"}'

    with pytest.raises(ValueError):
        load(connection)


def test_loader_requires_read_capability_and_current_generation() -> None:
    connection = ProfileConnection()
    with pytest.raises(PermissionError):
        load(connection, authority(frozenset()))
    assert connection.calls == []

    connection.state["index_generation_id"] = "gen_old"
    with pytest.raises(ValueError, match="generation"):
        load(connection)
