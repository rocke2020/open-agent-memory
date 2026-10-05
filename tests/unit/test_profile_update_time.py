from datetime import UTC, datetime
from pathlib import Path

from oam.config import load_config
from oam.profiles import service as profile_service
from oam.profiles.inputs import ProfileInputs
from oam.profiles.service import _view


def test_profile_recipe_change_invalidates_an_unchanged_refresh(monkeypatch) -> None:
    input_identity = b"x" * 32
    for instruction in (None, "Return a JSON summary"):
        before = profile_service.profile_identity(input_identity, instruction)
        assert before != profile_service.profile_identity(b"y" * 32, instruction)
        assert before != profile_service.profile_identity(input_identity, "Another format")
        monkeypatch.setattr(
            profile_service, "PROFILE_SYSTEM", "Write a corrected current summary", raising=False
        )
        after = profile_service.profile_identity(input_identity, instruction)
        assert after != before
        assert after == profile_service.profile_identity(input_identity, instruction)
        monkeypatch.undo()


def test_stored_field_profile_is_readable_without_regeneration() -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    inputs = ProfileInputs(
        identity=b"x" * 32,
        scope_identity=b"y" * 32,
        supported={"fact_1": {"kind": "fact", "id": "fact_1", "memory_id": "fact_1"}},
        exact_fields=(),
        pending_inputs=0,
        failed_inputs=0,
        blocked_inputs=0,
        waiting_source_ids=(),
        policy_epoch=1,
        prompt_inputs=[
            {
                "kind": "fact",
                "id": "fact_1",
                "source_passages": [
                    {
                        "source_id": "chat_1",
                        "source_version": 1,
                        "passage_id": "passage_1",
                        "event_time": "2026-09-01T09:00:00Z",
                    }
                ],
            }
        ],
        eligible_support_ids={"fact_1"},
        uncovered_inputs=0,
    )
    snapshot = {
        "profile_revision": 4,
        "sections": {
            "main": [
                {
                    "key": "role",
                    "value": "Engineer",
                    "qualifier": "as stated in September",
                    "supports": [{"kind": "fact", "id": "fact_1"}],
                    "state": "active",
                }
            ]
        },
        "content": None,
        "references": [],
        "support_ids": [],
        "instruction": None,
        "input_identity": b"z" * 32,
        "published_at": None,
    }

    view = _view(config, inputs, snapshot, 500)
    assert isinstance(view["content"], str)
    assert "Engineer" in view["content"]
    assert "as stated in September" in view["content"]
    assert view["content"].startswith("## Main content\n")
    assert "## References" in view["content"]
    assert view["references"][0]["source_id"] == "chat_1"
    assert view["support_ids"] == ["fact_1"]
    assert view["freshness"] == "dirty"
    assert view["updated_at"] is None
    assert view["generation_calls"] == 0
    assert snapshot["content"] is None

    omitted = _view(config, inputs, snapshot, 0)
    assert omitted["content"] is None
    assert omitted["omitted"] == [{"reason": "budget"}]

    inputs.eligible_support_ids.clear()
    withdrawn = _view(config, inputs, snapshot, 500)
    assert withdrawn["content"] is None
    assert withdrawn["freshness"] == "stale"
    assert withdrawn["references"] == []

    inputs.eligible_support_ids.add("fact_1")
    inputs.prompt_inputs.clear()
    unhydrated = _view(config, inputs, snapshot, 500)
    assert unhydrated["content"] is None
    assert unhydrated["freshness"] == "stale"


def test_profile_view_reports_only_a_recorded_publication_time() -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    inputs = ProfileInputs(
        identity=b"x" * 32,
        scope_identity=b"y" * 32,
        supported={},
        exact_fields=(),
        pending_inputs=0,
        failed_inputs=0,
        blocked_inputs=0,
        waiting_source_ids=(),
        policy_epoch=1,
        prompt_inputs=[],
        eligible_support_ids=set(),
        uncovered_inputs=0,
    )
    snapshot = {
        "profile_revision": 2,
        "content": "## Main content\n\nThe user prefers concise replies."
        "\n\n## References\n\n1. Chat",
        "references": [{"support_id": "fact_1", "source_id": "chat_1"}],
        "support_ids": ["fact_1"],
        "instruction": None,
        "input_identity": inputs.identity,
        "published_at": datetime(2026, 9, 29, 12, 30, tzinfo=UTC),
    }
    inputs.eligible_support_ids.add("fact_1")

    assert _view(config, inputs, snapshot, 500)["freshness"] == "dirty"
    snapshot["input_identity"] = profile_service.profile_identity(inputs.identity, None)
    assert _view(config, inputs, snapshot, 500)["freshness"] == "current"

    assert _view(config, inputs, snapshot, 500)["updated_at"] == "2026-09-29T12:30:00+00:00"
    assert _view(config, inputs, {**snapshot, "published_at": None}, 500)["updated_at"] is None
    assert "The user prefers concise replies." in _view(config, inputs, snapshot, 500)["content"]
    assert _view(config, inputs, snapshot, 1)["content"] is None
    inputs.eligible_support_ids.clear()
    assert _view(config, inputs, snapshot, 500)["content"] is None
