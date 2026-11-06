"""Cleanup redacts only traces influenced by a withdrawn source."""

import hashlib
import json
from types import SimpleNamespace

from oam.governance.cleanup import affected_trace_ids


def test_independent_recorded_trace_survives_source_cleanup():
    traces = [
        ("withdrawn", {"arms": {"dense": ["removed_unit"]}, "result_ids": []}),
        (
            "independent",
            {"arms": {"dense": ["other_unit"]}, "result_ids": ["other_unit"]},
        ),
    ]

    assert affected_trace_ids(traces, {"removed_unit"}) == ["withdrawn"]


def _profile_fixture():
    def key(value):
        return hashlib.sha256(value.encode()).hexdigest()

    snapshot = {
        "content": (
            "## Main content\n\n- The user likes SECRET sailing [1].\n"
            "- The user prefers English [2].\n\n## References\n\n"
            "1. SECRET source\n2. Safe source"
        ),
        "instruction": None,
        "support_ids": ["withdrawn", "safe"],
        "references": [],
        "input_manifest": {
            "units": {key("withdrawn"): "secret-digest", key("safe"): "safe-digest"},
            "sources": {"withdrawn-source-hash": 1, "safe-source-hash": 1},
            "exact": "old",
            "examined": [key("withdrawn"), key("safe")],
            "recipe": "recipe",
        },
    }
    inputs = SimpleNamespace(
        exact_fields=(),
        manifest={
            "units": {key("safe"): "safe-digest", key("new"): "new-digest"},
            "sources": {"safe-source-hash": 1, "new-source-hash": 1},
            "exact": "current",
        },
        stored_support_inputs=[
            {
                "id": "safe",
                "kind": "fact",
                "source_passages": [
                    {"source_id": "safe-source", "source_version": 1, "passage_id": "safe-passage"}
                ],
            }
        ],
    )
    return key, snapshot, inputs


def test_closed_profile_sanitizer_removes_withdrawn_claim_and_all_old_provenance():
    from oam.governance.cleanup import sanitize_closed_profile

    key, snapshot, inputs = _profile_fixture()
    result = sanitize_closed_profile(snapshot, inputs)
    assert result is not None
    assert "prefers English [1]" in result["content"]
    assert result["support_ids"] == ["safe"]
    assert result["references"][0]["passages"][0]["source_id"] == "safe-source"
    assert result["input_manifest"]["examined"] == [key("safe")]
    assert result["input_manifest"]["pending"] == [key("new")]
    serialized = json.dumps(result)
    assert "SECRET" not in serialized
    assert "withdrawn" not in serialized
    assert "secret-digest" not in serialized
    assert key("withdrawn") not in serialized


def test_closed_profile_sanitizer_fails_closed_for_custom_or_exact_profiles():
    from oam.governance.cleanup import sanitize_closed_profile

    _, snapshot, inputs = _profile_fixture()
    assert sanitize_closed_profile({**snapshot, "instruction": "JSON"}, inputs) is None
    inputs.exact_fields = ({"value": "exact"},)
    assert sanitize_closed_profile(snapshot, inputs) is None


def test_closed_profile_sanitizer_prunes_changed_support_and_unknown_baselines():
    from oam.governance.cleanup import sanitize_closed_profile

    key, snapshot, inputs = _profile_fixture()
    inputs.manifest["units"][key("safe")] = "changed-digest"
    result = sanitize_closed_profile(snapshot, inputs)
    assert result is not None
    assert result["support_ids"] == []
    assert "prefers English" not in result["content"]
    assert result["input_manifest"]["examined"] == []
    assert key("safe") in result["input_manifest"]["pending"]
    assert (
        sanitize_closed_profile({**snapshot, "content": "Unstructured SECRET prose"}, inputs)
        is None
    )


def test_closed_profile_sanitizer_retains_canonical_empty_baseline_after_last_short_claim_removed():
    from oam.governance.cleanup import sanitize_closed_profile

    _, snapshot, inputs = _profile_fixture()
    snapshot["content"] = "## Main content\n\n- X [1].\n\n## References\n\n1. S"
    snapshot["support_ids"] = ["withdrawn"]
    inputs.manifest = {"units": {}, "sources": {}, "exact": "current"}
    inputs.stored_support_inputs = []
    result = sanitize_closed_profile(snapshot, inputs)
    assert result is not None
    assert result["content"] == (
        "## Main content\n\nNo supported details yet.\n\n## References\n\nNo references yet."
    )
    assert result["support_ids"] == []
    assert result["references"] == []


def test_closed_profile_sanitizer_still_rejects_nonempty_content_growth():
    from oam.governance.cleanup import sanitize_closed_profile

    _, snapshot, inputs = _profile_fixture()
    snapshot["content"] = "## Main content\n\n- Safe [1].\n\n## References\n\n1. S"
    snapshot["support_ids"] = ["safe"]
    for source_id in ("s", "source" * 100):
        inputs.stored_support_inputs[0]["source_passages"][0]["source_id"] = source_id
        assert sanitize_closed_profile(snapshot, inputs) is None


def test_cleanup_scrubs_all_affected_history_but_never_restores_visible_profile(monkeypatch):
    from oam.auth.credentials import AuthorizedUser
    from oam.governance import cleanup

    _, snapshot, _ = _profile_fixture()
    snapshot.update(profile_revision=8, generation_id="generation", visible_to=None)
    statements = []

    class Connection:
        def cursor(self, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params):
            statements.append((statement, params))

        def fetchone(self):
            return snapshot

    def no_hydration(*args, **kwargs):
        raise AssertionError("A visible revision must never be restored by cleanup")

    monkeypatch.setattr(cleanup, "load_profile_inputs", no_hydration)
    authority = AuthorizedUser("worker", "tenant", "worker", "user", frozenset({"memory:read"}))
    cleanup._scrub_removed_profile_copies(Connection(), authority, ["withdrawn"])
    updates = [
        (statement, params) for statement, params in statements if statement.startswith("UPDATE")
    ]
    assert len(updates) == 2
    assert "input_manifest=NULL" in updates[0][0]
    assert "visible_to IS NOT NULL" in updates[0][0]
    assert updates[0][1] == ("tenant", "user")
    assert "content=NULL" in updates[1][0]
    assert updates[1][1] == ("tenant", "user", ["withdrawn"], ["withdrawn"])


def test_cleanup_restores_only_current_sanitized_metadata_in_latest_closed_revision(monkeypatch):
    from oam.auth.credentials import AuthorizedUser
    from oam.governance import cleanup

    _, snapshot, inputs = _profile_fixture()
    snapshot.update(profile_revision=9, generation_id="generation", visible_to=10)
    updates = []

    class Connection:
        def cursor(self, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params):
            if statement.startswith("UPDATE"):
                updates.append((statement, params))

        def fetchone(self):
            return snapshot

    monkeypatch.setattr(cleanup, "load_profile_inputs", lambda *args, **kwargs: inputs)
    authority = AuthorizedUser("worker", "tenant", "worker", "user", frozenset({"memory:read"}))
    cleanup._scrub_removed_profile_copies(Connection(), authority, ["withdrawn"])
    assert len(updates) == 3
    statement, params = updates[-1]
    assert "visible_to IS NOT NULL" in statement
    assert "visible_to=" not in statement
    assert "sections='{}'::jsonb" in statement
    assert params[-3:] == ("tenant", "user", 9)
    assert "prefers English" in params[0] and "SECRET" not in params[0]
    assert params[1].obj == ["safe"]
    assert "withdrawn" not in json.dumps(params[3].obj)
