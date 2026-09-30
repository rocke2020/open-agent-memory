from datetime import UTC, datetime
from pathlib import Path

from oam.config import load_config
from oam.profiles.inputs import ProfileInputs
from oam.profiles.service import _view


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

    assert _view(config, inputs, snapshot, 500)["updated_at"] == "2026-09-29T12:30:00+00:00"
    assert _view(config, inputs, {**snapshot, "published_at": None}, 500)["updated_at"] is None
    assert "The user prefers concise replies." in _view(config, inputs, snapshot, 500)["content"]
    assert _view(config, inputs, snapshot, 1)["content"] is None
    inputs.eligible_support_ids.clear()
    assert _view(config, inputs, snapshot, 500)["content"] is None
