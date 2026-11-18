import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.models.runtime import EmbeddingResult, ModelResult, Usage
from oam.tokens import resolve_counter


class Database:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.epoch = 1
        self.generation = "gen"
        self.text = "The user teaches mathematics."
        self.pending = []
        self.resources = []
        self.empty = False
        self.rows = []

    @contextmanager
    def connection(self, **kwargs):
        yield self

    @contextmanager
    def transaction(self):
        self.active += 1
        try:
            yield self
        finally:
            self.active -= 1

    def cursor(self, **kwargs):
        return self

    def execute(self, sql, params=()):
        assert not any(word in sql.upper().split() for word in ("INSERT", "UPDATE", "DELETE"))
        self.calls.append(sql)
        if "set_config" in sql:
            self.rows = []
        elif "SELECT index_generation_id" in sql:
            self.rows = [(self.generation,)]
        elif "SELECT policy_epoch" in sql:
            self.rows = [(self.epoch,)]
        elif "profile:state" in sql:
            self.rows = [
                dict(state="active", policy_epoch=self.epoch, index_generation_id=self.generation)
            ]
        elif "profile:eligible" in sql:
            self.rows = (
                []
                if self.empty
                else [
                    dict(
                        unit_id="fact",
                        kind="fact",
                        origin="memory",
                        memory_id="memory",
                        insight_id=None,
                        insight_revision=None,
                        insight_subtype=None,
                        content_text=self.text,
                    )
                ]
            )
        elif "profile:resources" in sql:
            self.rows = self.resources
        elif "profile:insight_support" in sql:
            self.rows = []
        elif "profile:source_scope" in sql:
            self.rows = (
                []
                if self.empty
                else [dict(input_role="interaction", source_id="chat", active_version=1)]
            )
        elif "profile:pending" in sql:
            self.rows = self.pending
        elif "profile:passages" in sql or "profile:manifest_support" in sql:
            self.rows = [
                dict(
                    memory_id="memory",
                    passage_id="p",
                    source_id="chat",
                    source_version=1,
                    event_time=None,
                    normalized_text=self.text,
                    locator={},
                )
            ]
        elif "ORDER BY embedding" in sql:
            self.rows = [("fact",)]
        else:
            raise AssertionError(sql)
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return next(iter(self.rows), None)


@pytest.fixture
def scenario():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    db = Database()
    authority = AuthorizedUser("key", "tenant", "user:alice", "alice", frozenset({"memory:read"}))

    class Models:
        ingestion_counter = resolve_counter(config.tokenizer)
        after = None
        body = "The user teaches mathematics [1]."
        support_ids = ["fact"]

        def embed(self, texts, *, timeout_seconds):
            assert db.active == 0
            self.query = texts[0]
            return EmbeddingResult(vectors=((0.1,),), usage=Usage(input_tokens=5, total_tokens=5))

        def generate(self, role, **kwargs):
            assert db.active == 0
            self.request = json.loads(kwargs["user"])
            if self.after:
                self.after()
            return ModelResult(
                content=json.dumps(dict(content=self.body, support_ids=self.support_ids)),
                usage=Usage(input_tokens=20, output_tokens=12, total_tokens=32, reasoning_tokens=3),
            )

    return SimpleNamespace(config=config, db=db, authority=authority, models=Models())


def run(s):
    from oam.profiles.generation import ProfileGenerator

    return ProfileGenerator(s.config, s.db, s.models).generate(
        s.authority, "Summarize teaching work"
    )


def test_generates_transient_supported_content_without_writes_or_content_logs(
    scenario, caplog, capsys
):
    s = scenario
    result = run(s)
    assert result["content"] == "The user teaches mathematics [1]."
    assert result["references"][0]["passage_id"] == "p"
    assert result["usage"]["total_tokens"] == 32
    assert result["usage"]["reasoning_tokens"] == 3
    assert result["embedding_usage"]["total_tokens"] == 5
    assert result["generation_calls"] == 1
    assert s.models.query.endswith("Summarize teaching work")
    assert "Current information about" not in s.models.query
    assert result["selection_coverage"]["selected"] == 1
    assert "teaches mathematics" not in caplog.text + capsys.readouterr().out
    assert not any(
        "FROM profiles" in sql or "operations" in sql and "SELECT DISTINCT" not in sql
        for sql in s.db.calls
    )


@pytest.mark.parametrize("state", ["pending", "failed", "blocked"])
def test_refuses_unready_evidence(scenario, state):
    from oam.profiles.service import ProfileInputsNotReady

    scenario.db.pending = [dict(source_id="chat", source_version=1, coverage_state=state)]
    with pytest.raises(ProfileInputsNotReady):
        run(scenario)


def test_refuses_empty_evidence(scenario):
    scenario.db.empty = True
    with pytest.raises(ValueError, match="evidence"):
        run(scenario)


@pytest.mark.parametrize("change", ["text", "epoch", "generation", "empty"])
def test_rejects_support_or_authority_change_during_generation(scenario, change):
    values = dict(text="Different evidence", epoch=2, generation="changed", empty=True)
    scenario.models.after = lambda: setattr(scenario.db, change, values[change])
    with pytest.raises((ValueError, RuntimeError)):
        run(scenario)


@pytest.mark.parametrize(
    "body,supports",
    [
        ("Claim [2]", ["fact"]),
        ("Claim [0]", ["fact"]),
        ("Claim [1]", ["unknown"]),
        ("Claim [1]", ["fact", "fact"]),
    ],
)
def test_rejects_invalid_references(scenario, body, supports):
    from oam.profiles.generation import ProfileGenerationUnavailable

    scenario.models.body, scenario.models.support_ids = body, supports
    with pytest.raises(ProfileGenerationUnavailable):
        run(scenario)


def test_accepts_json_format_without_numeric_markers(scenario):
    scenario.models.body = '{"teaching":"mathematics"}'
    assert run(scenario)["content"] == '{"teaching":"mathematics"}'


def test_one_deadline_rejects_late_generation(scenario, monkeypatch):
    import oam.profiles.generation as generation

    now = [0.0]
    monkeypatch.setattr(generation.time, "monotonic", lambda: now[0])
    scenario.models.after = lambda: now.__setitem__(0, 10000.0)
    with pytest.raises(TimeoutError):
        run(scenario)


def test_sets_database_timeout_from_remaining_total_budget(scenario):
    run(scenario)
    assert sum("statement_timeout" in sql for sql in scenario.db.calls) == 3


def test_embedding_consumes_the_shared_generation_deadline(scenario, monkeypatch):
    import oam.profiles.generation as generation

    now = [0.0]
    monkeypatch.setattr(generation.time, "monotonic", lambda: now[0])
    embed = scenario.models.embed

    def delayed_embed(*args, **kwargs):
        result = embed(*args, **kwargs)
        now[0] = 10000.0
        return result

    scenario.models.embed = delayed_embed
    with pytest.raises(TimeoutError):
        run(scenario)
    assert not hasattr(scenario.models, "request")


def test_exact_details_are_evidence_without_unrelated_auto_append(scenario):
    scenario.db.resources = [
        dict(
            source_id="preferences",
            source_version=1,
            media_type="application/json",
            original_bytes=json.dumps(
                {
                    "resource_id": "preferences",
                    "resource_type": "profile",
                    "subtype": "preferences/v1",
                    "source_revision": "1",
                    "content": {"example_language": "Python", "domain": "backend"},
                }
            ).encode(),
        )
    ]
    result = run(scenario)
    assert scenario.models.request["exact_fields"][0]["value"] == "Python"
    assert "Python" not in result["content"]
    assert len(result["references"]) == 1


def test_exact_only_inputs_can_generate_without_embedding(scenario):
    scenario.db.empty = True
    scenario.db.resources = [
        dict(
            source_id="preferences",
            source_version=1,
            media_type="application/json",
            original_bytes=json.dumps(
                {
                    "resource_id": "preferences",
                    "resource_type": "profile",
                    "subtype": "preferences/v1",
                    "source_revision": "1",
                    "content": {"example_language": "Python", "domain": "backend"},
                }
            ).encode(),
        )
    ]
    scenario.models.body = "The user prefers Python examples [1]."
    scenario.models.support_ids = ["profile_resource:preferences:1"]
    result = run(scenario)
    assert result["references"] == [
        {
            "support_id": "profile_resource:preferences:1",
            "kind": "profile_data",
            "profile_data_id": "preferences",
            "profile_data_version": 1,
        }
    ]
    assert result["embedding_usage"] == {}
    assert not hasattr(scenario.models, "query")


def test_rejects_generation_becoming_unset_during_generation(scenario):
    from oam.profiles.generation import ProfileGenerationChanged

    scenario.models.after = lambda: setattr(scenario.db, "generation", None)
    with pytest.raises(ProfileGenerationChanged, match="generation"):
        run(scenario)


@pytest.mark.parametrize("stage", ["embed", "generate"])
@pytest.mark.parametrize("timeout", [True, False])
def test_provider_failure_has_typed_sanitized_error(scenario, stage, timeout):
    import httpx

    from oam.profiles.generation import (
        ProfileGenerationDeadlineExceeded,
        ProfileGenerationUnavailable,
    )

    def fail(*args, **kwargs):
        if timeout:
            raise httpx.ReadTimeout("private provider payload")
        raise httpx.ConnectError("private provider payload")

    setattr(scenario.models, stage, fail)
    expected = ProfileGenerationDeadlineExceeded if timeout else ProfileGenerationUnavailable
    with pytest.raises(expected) as caught:
        run(scenario)
    assert "private" not in str(caught.value)
    assert caught.value.generation_calls == (1 if stage == "generate" else 0)
    assert caught.value.embedding_usage.get("total_tokens") == (5 if stage == "generate" else None)


def test_invalid_generated_document_retains_usage_without_body(scenario):
    from oam.profiles.generation import ProfileGenerationUnavailable

    scenario.models.body = "private generated body [999]"
    with pytest.raises(ProfileGenerationUnavailable) as caught:
        run(scenario)
    assert caught.value.usage["reasoning_tokens"] == 3
    assert caught.value.usage["total_tokens"] == 32
    assert caught.value.generation_calls == 1
    assert "private" not in str(caught.value)
    assert "private" not in repr(vars(caught.value))


def test_model_output_error_retains_failed_call_usage(scenario):
    from oam.models.runtime import ModelOutputError
    from oam.profiles.generation import ProfileGenerationUnavailable

    def fail(*args, **kwargs):
        raise ModelOutputError(
            "private malformed body", usage=Usage(output_tokens=40, reasoning_tokens=30)
        )

    scenario.models.generate = fail
    with pytest.raises(ProfileGenerationUnavailable) as caught:
        run(scenario)
    assert caught.value.usage["output_tokens"] == 40
    assert caught.value.usage["reasoning_tokens"] == 30
    assert "private" not in str(caught.value)


def test_evidence_conflict_is_distinct_and_retains_generation_usage(scenario):
    from oam.profiles.generation import ProfileGenerationChanged

    scenario.models.after = lambda: setattr(scenario.db, "text", "changed")
    with pytest.raises(ProfileGenerationChanged) as caught:
        run(scenario)
    assert caught.value.usage["total_tokens"] == 32


@pytest.mark.parametrize("denied", [False, True])
def test_final_policy_failure_retains_usage(scenario, monkeypatch, denied):
    from oam.db.inspection import SourceInspector, SourcePolicyChanged

    error = PermissionError("user unavailable") if denied else SourcePolicyChanged("policy changed")

    def reject(*args):
        raise error

    monkeypatch.setattr(SourceInspector, "_check_policy", reject)
    with pytest.raises(type(error)) as caught:
        run(scenario)
    assert caught.value.usage["total_tokens"] == 32
    assert caught.value.embedding_usage["total_tokens"] == 5
    assert caught.value.generation_calls == 1


@pytest.mark.parametrize("final_validation", [False, True])
def test_database_statement_timeout_is_typed_and_retains_usage(scenario, final_validation):
    from psycopg.errors import QueryCanceled

    from oam.profiles.generation import ProfileGenerationDeadlineExceeded

    execute = scenario.db.execute
    armed = [not final_validation]

    def timed_out(sql, params=()):
        if armed[0] and "profile:state" in sql:
            raise QueryCanceled("canceling statement due to statement timeout")
        return execute(sql, params)

    scenario.db.execute = timed_out
    if final_validation:
        scenario.models.after = lambda: armed.__setitem__(0, True)
    with pytest.raises(ProfileGenerationDeadlineExceeded) as caught:
        run(scenario)
    assert caught.value.generation_calls == (1 if final_validation else 0)
    assert caught.value.usage.get("total_tokens") == (32 if final_validation else None)
    assert caught.value.embedding_usage.get("total_tokens") == (5 if final_validation else None)
    if not final_validation:
        assert not hasattr(scenario.models, "query")
        assert not hasattr(scenario.models, "request")


def test_unrelated_database_errors_are_not_reclassified(scenario):
    from psycopg import OperationalError

    execute = scenario.db.execute
    error = OperationalError("database connection failed")

    def failed(sql, params=()):
        if "profile:state" in sql:
            raise error
        return execute(sql, params)

    scenario.db.execute = failed
    with pytest.raises(OperationalError) as caught:
        run(scenario)
    assert caught.value is error


@pytest.mark.parametrize("after_generation", [False, True])
def test_connection_pool_timeout_retains_usage_and_deadline_contract(scenario, after_generation):
    from psycopg_pool import PoolTimeout

    from oam.profiles.generation import ProfileGenerationDeadlineExceeded

    original = scenario.db.connection
    armed = [not after_generation]

    @contextmanager
    def exhausted_pool(**kwargs):
        if armed[0]:
            raise PoolTimeout("pool acquisition exceeded its timeout")
        with original(**kwargs) as connection:
            yield connection

    scenario.db.connection = exhausted_pool
    if after_generation:
        scenario.models.after = lambda: armed.__setitem__(0, True)
    with pytest.raises(ProfileGenerationDeadlineExceeded) as caught:
        run(scenario)
    assert caught.value.generation_calls == (1 if after_generation else 0)
    assert caught.value.usage.get("total_tokens") == (32 if after_generation else None)
    assert caught.value.embedding_usage.get("total_tokens") == (5 if after_generation else None)


def test_final_reauthorization_shares_generation_deadline(scenario, monkeypatch):
    import oam.profiles.generation as generation

    now = [0.0]
    monkeypatch.setattr(generation.time, "monotonic", lambda: now[0])

    def reauthorize(connection):
        assert connection is scenario.db
        assert scenario.db.active == 1
        now[0] = scenario.config.profile.attempt_timeout_seconds + 1

    with pytest.raises(generation.ProfileGenerationDeadlineExceeded) as caught:
        generation.ProfileGenerator(scenario.config, scenario.db, scenario.models).generate(
            scenario.authority, "Summarize teaching work", reauthorize=reauthorize
        )
    assert caught.value.usage["total_tokens"] == 32
