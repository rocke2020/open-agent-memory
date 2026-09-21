from pathlib import Path

import pytest

from oam.auth import bootstrap

CONFIG_PATH = Path(__file__).parents[2] / "configs" / "oam.yaml"


def test_initial_bootstrap_requires_the_configured_operator_dsn(monkeypatch, capsys):
    monkeypatch.delenv("OAM_MIGRATION_DSN", raising=False)

    assert bootstrap.bootstrap_from_environment(CONFIG_PATH) == 2
    assert "OAM_MIGRATION_DSN" in capsys.readouterr().err


@pytest.mark.parametrize("recovery", [False, True])
def test_operator_bootstrap_errors_do_not_expose_secrets(monkeypatch, capsys, recovery):
    monkeypatch.setenv("OAM_MIGRATION_DSN", "postgresql://operator-secret")
    monkeypatch.setenv("OAM_BOOTSTRAP_OUTPUT", "/operator/first-key")
    monkeypatch.setenv("OAM_BOOTSTRAP_RECOVERY_OUTPUT", "/operator/replacement-key")

    def failed_operation(*args):
        raise OSError("operator-secret")

    function = "_recover_bootstrap" if recovery else "initialize_bootstrap"
    monkeypatch.setattr(bootstrap, function, failed_operation)
    entrypoint = (
        bootstrap.recover_bootstrap_from_environment
        if recovery
        else bootstrap.bootstrap_from_environment
    )

    assert entrypoint(CONFIG_PATH) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "bootstrap" in output.err
    assert "operator-secret" not in output.err
