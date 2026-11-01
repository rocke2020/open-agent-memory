import json
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oam.auth.credentials import AuthorizedUser
from oam.profiles.inputs import PENDING_PROFILE_INPUTS_SQL


@pytest.mark.parametrize(
    ("status", "memory_ready", "expected"),
    [
        ("succeeded", "true", []),
        ("running", "false", [("chat", 1, "pending")]),
        ("partial_failed", "false", [("chat", 1, "blocked")]),
    ],
)
def test_profile_coverage_without_insights_tracks_indexing_only(status, memory_ready, expected):
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE sources(tenant_id,user_id,source_id,active_version,input_role,status);"
            "CREATE TABLE source_versions(tenant_id,user_id,source_id,source_version,status);"
            "CREATE TABLE evidence(tenant_id,user_id,source_id,source_version,status,"
            "learning_eligible,evidence_id);"
            "CREATE TABLE operations(tenant_id,user_id,operation_id,source_id,source_version,"
            "kind,status,stage_state);"
            "CREATE TABLE work_units(tenant_id,user_id,operation_id,status);"
            "INSERT INTO sources VALUES('t','u','chat',1,'interaction','active');"
            "INSERT INTO source_versions VALUES('t','u','chat',1,'active');"
            "INSERT INTO evidence VALUES('t','u','chat',1,'active',1,'e');"
        )
        connection.execute(
            "INSERT INTO operations VALUES('t','u','index','chat',1,'index_interaction',?,?)",
            (status, json.dumps({"memory_ready": memory_ready})),
        )
        work_status = "failed_terminal" if status == "partial_failed" else status
        connection.execute("INSERT INTO work_units VALUES('t','u','index',?)", (work_status,))
        query = PENDING_PROFILE_INPUTS_SQL.replace("%s", "?")
        assert connection.execute(query, ("t", "u", False, False)).fetchall() == expected


@pytest.mark.parametrize(
    ("retry_version", "retry_generation", "ready", "expected"),
    [
        (1, "gen", "true", []),
        (2, "gen", "true", [("chat", 1, "failed")]),
        (1, "old", "true", []),
        (1, "gen", "false", [("chat", 1, "failed")]),
    ],
)
def test_profile_coverage_recognizes_only_current_source_insight_retry(
    retry_version, retry_generation, ready, expected
) -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE sources(tenant_id,user_id,source_id,active_version,input_role,status);"
            "CREATE TABLE source_versions(tenant_id,user_id,source_id,source_version,status);"
            "CREATE TABLE evidence(tenant_id,user_id,source_id,source_version,status,"
            "learning_eligible,evidence_id);"
            "CREATE TABLE operations(tenant_id,user_id,operation_id,source_id,source_version,"
            "kind,status,stage_state,created_at);"
            "CREATE TABLE work_units(tenant_id,user_id,operation_id,generation_id,status);"
            "INSERT INTO sources VALUES('tenant','alice','chat',1,'interaction','active');"
            "INSERT INTO source_versions VALUES('tenant','alice','chat',1,'active');"
            "INSERT INTO evidence VALUES('tenant','alice','chat',1,'active',1,'evidence');"
            "INSERT INTO operations VALUES('tenant','alice','index','chat',1,"
            "'index_interaction','succeeded','{}',0);"
            "INSERT INTO work_units VALUES('tenant','alice','index','gen','succeeded');"
            "INSERT INTO operations VALUES('tenant','alice','failed','chat',1,"
            "'consolidate','partial_failed','{}',0);"
            "INSERT INTO work_units VALUES('tenant','alice','failed','gen','failed_terminal');"
        )
        query = PENDING_PROFILE_INPUTS_SQL.replace("%s", "?")
        parameters = ("tenant", "alice", True, True)
        assert connection.execute(query, parameters).fetchall() == [("chat", 1, "failed")]
        # PostgreSQL's ->> yields text; use its literal text value in this SQL boundary fixture.
        connection.execute(
            "INSERT INTO operations VALUES('tenant','alice','retry','chat',?,"
            "'consolidate','succeeded',?,1)",
            (retry_version, json.dumps({"insights_ready": ready})),
        )
        connection.execute(
            "INSERT INTO work_units VALUES('tenant','alice','retry',?,'succeeded')",
            (retry_generation,),
        )
        assert connection.execute(query, parameters).fetchall() == expected


@pytest.mark.parametrize(
    ("repair", "expected"),
    [
        ({}, [("chat", 1, "pending")]),
        ({"operation_status": "running", "work_status": "running"}, [("chat", 1, "pending")]),
        ({"work_status": "retry_wait"}, [("chat", 1, "pending")]),
        (
            {"operation_status": "partial_failed", "work_status": "failed_terminal"},
            [("chat", 1, "failed")],
        ),
        (
            {"operation_status": "cancelled", "work_status": "cancelled"},
            [("chat", 1, "failed")],
        ),
        ({"operation_status": "succeeded", "work_status": "succeeded"}, []),
        ({"created_at": 0}, [("chat", 1, "pending")]),
        ({"generation": "old"}, [("chat", 1, "pending")]),
        ({"version": 2}, []),
        ({"tenant": "other"}, []),
        ({"user": "other"}, []),
        ({"source": "other"}, []),
        ({"independent_success": True}, [("chat", 1, "pending")]),
        (
            {
                "independent_success": True,
                "operation_status": "partial_failed",
                "work_status": "failed_terminal",
            },
            [("chat", 1, "failed")],
        ),
    ],
)
def test_profile_coverage_preserves_each_unresolved_repair(repair, expected) -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE sources(tenant_id,user_id,source_id,active_version,input_role,status);"
            "CREATE TABLE source_versions(tenant_id,user_id,source_id,source_version,status);"
            "CREATE TABLE evidence(tenant_id,user_id,source_id,source_version,status,"
            "learning_eligible,evidence_id);"
            "CREATE TABLE operations(tenant_id,user_id,operation_id,source_id,source_version,"
            "kind,status,stage_state,created_at);"
            "CREATE TABLE work_units(tenant_id,user_id,operation_id,generation_id,status);"
            "INSERT INTO sources VALUES('tenant','alice','chat',1,'interaction','active');"
            "INSERT INTO source_versions VALUES('tenant','alice','chat',1,'active');"
            "INSERT INTO evidence VALUES('tenant','alice','chat',1,'active',1,'evidence');"
            "INSERT INTO operations VALUES('tenant','alice','historical','chat',1,"
            "'consolidate','succeeded','{\"insights_ready\":\"true\"}',1);"
            "INSERT INTO work_units VALUES('tenant','alice','historical','gen','succeeded');"
        )
        tenant, user = repair.get("tenant", "tenant"), repair.get("user", "alice")
        connection.execute(
            "INSERT INTO operations VALUES(?,?,'repair',?,?,'consolidate',?,?,?)",
            (
                tenant,
                user,
                repair.get("source", "chat"),
                repair.get("version", 1),
                repair.get("operation_status", "accepted"),
                json.dumps({"insight_repairs": [{"insight_id": "insight-a"}]}),
                repair.get("created_at", 2),
            ),
        )
        connection.execute(
            "INSERT INTO work_units VALUES(?,?,'repair',?,?)",
            (tenant, user, repair.get("generation", "gen"), repair.get("work_status", "queued")),
        )
        if repair.get("independent_success"):
            connection.execute(
                "INSERT INTO operations VALUES('tenant','alice','repair-b','chat',1,"
                "'consolidate','succeeded',?,3)",
                (
                    json.dumps(
                        {"insights_ready": "true", "insight_repairs": [{"insight_id": "insight-b"}]}
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO work_units VALUES('tenant','alice','repair-b','gen','succeeded')"
            )
        query = PENDING_PROFILE_INPUTS_SQL.replace("%s", "?")
        parameters = ("tenant", "alice", True, True)
        assert connection.execute(query, parameters).fetchall() == expected
        if repair.get("independent_success"):
            connection.execute(
                "UPDATE operations SET status='succeeded',stage_state=? "
                "WHERE operation_id='repair'",
                (
                    json.dumps(
                        {"insights_ready": "true", "insight_repairs": [{"insight_id": "insight-a"}]}
                    ),
                ),
            )
            connection.execute(
                "UPDATE work_units SET status='succeeded' WHERE operation_id='repair'"
            )
            assert connection.execute(query, parameters).fetchall() == []


class ProfileCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, parameters=()):
        self.connection.calls.append((query, parameters))
        if "SELECT index_generation_id FROM users" in query:
            self.rows = [(self.connection.state["index_generation_id"],)]
        elif "SELECT 1 FROM users" in query:
            self.rows = [(1,)]
        elif "/* profile:state */" in query:
            self.rows = [self.connection.state] if self.connection.state else []
        elif "/* profile:eligible */" in query:
            self.rows = self.connection.units
        elif "/* profile:resources */" in query:
            self.rows = self.connection.resources
        elif "/* profile:source_scope */" in query:
            self.rows = self.connection.source_scope
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
                "insight_subtype": None,
                "content_text": "The user prefers Python examples.",
            },
            {
                "unit_id": "insight_unit_1",
                "kind": "insight",
                "origin": "memory",
                "memory_id": None,
                "insight_id": "insight_1",
                "insight_revision": 2,
                "insight_subtype": "projects",
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
        self.source_scope = [
            {"input_role": "interaction", "source_id": "chat_1", "active_version": 1},
            {
                "input_role": "profile_resource",
                "source_id": "user-preference-examples",
                "active_version": 2,
            },
        ]
        self.insight_links = [
            {"insight_id": "insight_1", "insight_revision": 2, "memory_id": "fact_1"}
        ]
        self.passages = [
            {
                "memory_id": "fact_1",
                "passage_id": "passage_1",
                "source_id": "chat_1",
                "source_version": 1,
                "event_time": datetime(2026, 9, 29, 12, 3, tzinfo=UTC),
                "normalized_text": "Please show Python examples.",
                "locator": {"message": 1},
            }
        ]

    def transaction(self):
        return nullcontext()

    def cursor(self, **_kwargs):
        return ProfileCursor(self)

    def execute(self, query, parameters=()):
        return self.cursor().execute(query, parameters)


def authority(capabilities=frozenset({"memory:read"})):
    return AuthorizedUser("cred_1", "tenant_1", "person_1", "user_1", capabilities)


def load(connection, user=None):
    from oam.profiles.inputs import load_profile_inputs

    return load_profile_inputs(connection, user or authority(), "gen_1", insights_enabled=True)


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
            "event_time": "2026-09-29T12:03:00+00:00",
            "text": "Please show Python examples.",
            "locator": {"message": 1},
        }
    ]
    assert [item["kind"] for item in result.prompt_inputs] == ["fact", "insight"]
    assert result.prompt_inputs[1]["support_memory_ids"] == ["fact_1"]
    assert result.prompt_inputs[1]["subtype"] == "projects"
    assert result.prompt_inputs[1]["revision"] == 2
    assert result.pending_inputs == 0
    assert result.policy_epoch == 4
    assert len(result.identity) == 32
    eligible_sql = next(query for query, _ in connection.calls if "/* profile:eligible */" in query)
    assert "s.input_role='interaction'" in eligible_sql
    assert "u.origin='memory'" in eligible_sql
    assert "JOIN insights i" in eligible_sql
    assert all("accepted_seq" not in query for query, _ in connection.calls)


def test_evidence_only_change_does_not_dirty_profile_identity_but_coverage_does() -> None:
    connection = ProfileConnection()
    first = load(connection)

    connection.evidence_only_revision = 8
    assert load(connection).identity == first.identity
    assert load(connection).scope_identity == first.scope_identity

    connection.source_scope.append(
        {"input_role": "interaction", "source_id": "chat_2", "active_version": 1}
    )
    connection.pending = [{"source_id": "chat_2", "source_version": 1, "coverage_state": "pending"}]
    pending = load(connection)
    assert pending.pending_inputs == 1
    assert pending.failed_inputs == 0
    assert pending.waiting_source_ids == ("chat_2",)
    assert pending.identity != first.identity
    assert pending.scope_identity != first.scope_identity

    connection.pending = [{"source_id": "chat_2", "source_version": 1, "coverage_state": "failed"}]
    failed = load(connection)
    assert failed.pending_inputs == 1
    assert failed.failed_inputs == 1
    assert failed.blocked_inputs == 0
    assert failed.waiting_source_ids == ()
    assert failed.identity != pending.identity
    assert failed.scope_identity == pending.scope_identity

    connection.units[1]["insight_subtype"] = "relationships"
    assert load(connection).identity != failed.identity

    connection.pending = [{"source_id": "chat_2", "source_version": 1, "coverage_state": "blocked"}]
    blocked = load(connection)
    assert blocked.failed_inputs == 0
    assert blocked.blocked_inputs == 1
    assert blocked.waiting_source_ids == ("chat_2",)


def test_l1_facts_change_refresh_identity_even_when_not_selected() -> None:
    connection = ProfileConnection()
    first = load(connection)
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

    assert len(inputs.supported) == 101
    assert inputs.identity != first.identity


def test_new_l1_fact_changes_profile_refresh_identity() -> None:
    connection = ProfileConnection()
    before = load(connection)
    connection.units.append(
        {
            **connection.units[0],
            "unit_id": "fact_unit_new",
            "memory_id": "fact_new",
            "content_text": "The user prefers English replies.",
        }
    )
    connection.passages.append(
        {**connection.passages[0], "memory_id": "fact_new", "passage_id": "passage_new"}
    )

    assert load(connection).identity != before.identity


def test_unpaired_l1_can_be_used_as_profile_input() -> None:
    connection = ProfileConnection()
    connection.units = connection.units[:1]

    inputs = load(connection)

    assert [(item["kind"], item["id"]) for item in inputs.prompt_inputs] == [
        ("fact", "fact_unit_1")
    ]
    assert inputs.prompt_inputs[0]["source_passages"][0]["source_id"] == "chat_1"


def test_ranked_candidates_are_hydrated_in_selection_order() -> None:
    from oam.profiles.inputs import load_profile_inputs

    connection = ProfileConnection()
    result = load_profile_inputs(
        connection,
        authority(),
        "gen_1",
        insights_enabled=True,
        selected_unit_ids=["insight_unit_1", "fact_unit_1"],
    )

    assert [item["id"] for item in result.prompt_inputs] == ["insight_unit_1", "fact_unit_1"]


def test_profile_input_limit_counts_all_eligible_units() -> None:
    connection = ProfileConnection()
    insight = connection.units[-1]
    connection.units += [
        {
            **insight,
            "unit_id": f"extra_insight_unit_{index}",
            "insight_id": f"extra_insight_{index}",
        }
        for index in range(64)
    ]
    connection.insight_links += [
        {"insight_id": f"extra_insight_{index}", "insight_revision": 2, "memory_id": "fact_1"}
        for index in range(64)
    ]

    inputs = load(connection)

    assert len(inputs.prompt_inputs) == 64
    assert inputs.uncovered_inputs == 2


def test_profile_data_remains_available_without_insights() -> None:
    connection = ProfileConnection()
    connection.units = connection.units[:1]

    inputs = load(connection)

    assert inputs.prompt_inputs[0]["kind"] == "fact"
    assert inputs.exact_fields[0]["value"] == "Python"
    assert inputs.uncovered_inputs == 0


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


@pytest.mark.parametrize("remove_support", [False, True])
def test_profile_read_uses_stored_collection_generation_and_keeps_support_checks(
    monkeypatch, remove_support
) -> None:
    from oam.config import load_config
    from oam.profiles import service

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection = ProfileConnection()
    inputs = load(connection)
    snapshot = {
        "profile_revision": 7,
        "generation_id": "gen_1",
        "content": "The user prefers Python examples [1].",
        "references": [{"source_id": "chat_1"}],
        "support_ids": ["fact_unit_1"],
        "instruction": None,
        "input_identity": service.profile_identity(
            inputs.identity,
            None,
            config.profile.max_input_tokens,
            (config.models.embedding.query_prefix if config.models.embedding else ""),
        ),
        "published_at": None,
    }
    if remove_support:
        connection.units.clear()
    pool = type("Pool", (), {"connection": lambda _self: nullcontext(connection)})()
    connection.calls.clear()
    monkeypatch.setattr(service, "index_generation", lambda *_args: "gen_new")
    monkeypatch.setattr(service, "_current_snapshot", lambda *_args: snapshot)
    policies = []
    monkeypatch.setattr(
        service.SourceInspector,
        "_check_policy",
        lambda _connection, _authority, epoch: policies.append(epoch),
    )

    result = service.ProfileService(config, pool).get(authority(), 500)

    assert any("FOR SHARE" in query for query, _ in connection.calls)
    assert connection.calls[0][0].startswith("SELECT set_config('oam.tenant_id'")
    assert result["profile_revision"] == 7
    assert result["generation_calls"] == 0
    assert policies == [4]
    if remove_support:
        assert result["content"] is None
        assert result["references"] == []
        assert result["support_ids"] == []
    else:
        assert result["content"] == snapshot["content"]
        assert result["references"] == snapshot["references"]
        assert result["freshness"] == "current"


def test_profile_read_reports_published_selection_coverage(monkeypatch) -> None:
    from oam.config import load_config
    from oam.profiles import service

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection = ProfileConnection()
    inputs = load(connection)
    snapshot = {
        "profile_revision": 7,
        "generation_id": "gen_1",
        "content": "The user prefers Python [1].",
        "references": [{"source_id": "chat_1"}],
        "support_ids": ["fact_unit_1"],
        "instruction": None,
        "input_identity": service.profile_identity(
            inputs.identity,
            None,
            config.profile.max_input_tokens,
            (config.models.embedding.query_prefix if config.models.embedding else ""),
        ),
        "published_at": None,
        "input_coverage": {"unexamined": 3, "budget_omitted": 2},
    }
    result = service._view(config, inputs, snapshot, 500)
    assert result["uncovered_inputs"] == 5
    assert result["freshness"] == "uncovered"


@pytest.mark.parametrize("profile_resources", [False, True])
def test_unindexed_stored_profile_keeps_its_generation_and_current_identity(
    monkeypatch, profile_resources
) -> None:
    from oam.config import load_config
    from oam.profiles import service

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection = ProfileConnection()
    connection.state["index_generation_id"] = None
    connection.units.clear()
    if not profile_resources:
        connection.resources.clear()
    inputs = load(connection)
    support_ids = sorted(inputs.eligible_support_ids)
    snapshot = {
        "profile_revision": 1,
        "generation_id": "gen_1",
        "content": "Python examples" if profile_resources else "",
        "references": [],
        "support_ids": support_ids,
        "instruction": None,
        "input_identity": service.profile_identity(
            inputs.identity,
            None,
            config.profile.max_input_tokens,
            (config.models.embedding.query_prefix if config.models.embedding else ""),
        ),
        "published_at": None,
    }
    pool = type("Pool", (), {"connection": lambda _self: nullcontext(connection)})()
    monkeypatch.setattr(service, "index_generation", lambda *_args: "gen_new")
    monkeypatch.setattr(service, "_current_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(service.SourceInspector, "_check_policy", lambda *_args: None)

    result = service.ProfileService(config, pool).get(authority(), 500)

    assert result["freshness"] == "current"
    assert result["content"] == snapshot["content"]
    assert result["generation_calls"] == 0
    monkeypatch.setattr(service, "_current_snapshot", lambda *_args: None)
    missing = service.ProfileService(config, pool).get(authority(), 500)
    assert missing["freshness"] == "missing"
    assert missing["content"] is None
    assert missing["generation_calls"] == 0
