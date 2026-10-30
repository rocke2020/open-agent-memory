"""The general L2 operator control requires an explicit migration authority."""

import json

from oam.cli import main


def test_l2_hold_uses_only_the_configured_operator_dsn(tmp_path, monkeypatch, capsys):
    config = tmp_path / "operator.yaml"
    config.write_text("database:\n  migration_dsn_env: SYNTHETIC_OPERATOR_DSN\n")
    monkeypatch.delenv("SYNTHETIC_OPERATOR_DSN", raising=False)
    monkeypatch.setenv("OAM_API_DSN", "postgresql://private-api-secret")
    result = main(
        ["l2-hold", "--config", str(config), "status", "--tenant-id", "t", "--user-id", "u"]
    )
    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": "missing required environment variable SYNTHETIC_OPERATOR_DSN"
    }
    assert "private" not in captured.err
