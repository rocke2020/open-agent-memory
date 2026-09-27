"""Operator permit commands require explicit authority and reconciliation intent."""

import json
from uuid import uuid4

import pytest

from oam.cli import main


def test_permit_list_requires_selected_operator_dsn_without_application_fallback(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "operator.yaml"
    config_path.write_text("database:\n  migration_dsn_env: SYNTHETIC_OPERATOR_DSN\n")
    monkeypatch.delenv("SYNTHETIC_OPERATOR_DSN", raising=False)
    monkeypatch.setenv("OAM_API_DSN", "postgresql://private-api-secret")
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "postgresql://private-worker-secret")

    assert main(["model-permits", "--config", str(config_path), "list"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": "missing required environment variable SYNTHETIC_OPERATOR_DSN"
    }
    assert "private" not in captured.err


@pytest.mark.parametrize("flags", [[], ["--known-completed", "--force-unknown"]])
def test_reconciliation_requires_one_explicit_outcome(flags, capsys):
    assert (
        main(["model-permits", "reconcile", str(uuid4()), "--reason", "provider check", *flags])
        == 2
    )
    assert "--known-completed" in capsys.readouterr().err


def test_reconciliation_requires_operator_reason(capsys):
    assert main(["model-permits", "reconcile", str(uuid4()), "--known-completed"]) == 2
    assert "--reason" in capsys.readouterr().err
