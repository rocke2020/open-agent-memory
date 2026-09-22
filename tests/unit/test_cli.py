import json
from pathlib import Path

import oam.cli as cli
from oam.cli import _preflight, main
from oam.config import load_config
from oam.models.preflight import (
    ChatProbeOutput,
    EmbeddingProbeOutput,
    VisionProbeOutput,
)

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_cli_entrypoint_loads_dotenv_without_overriding_process_environment(
    tmp_path, monkeypatch, capsys
) -> None:
    dotenv_config = tmp_path / "dotenv.yaml"
    dotenv_config.write_text("resource:\n  max_bytes: 12345\n", encoding="utf-8")
    system_config = tmp_path / "system.yaml"
    system_config.write_text("resource:\n  max_bytes: 54321\n", encoding="utf-8")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        f"OAM_CONFIG={dotenv_config}\nOAM_DOTENV_ONLY=loaded\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OAM_CONFIG", str(system_config))
    monkeypatch.delenv("OAM_DOTENV_ONLY", raising=False)

    exit_code = cli.entrypoint(["preflight", "--json"], dotenv_path=dotenv_path)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["config_sha256"] == load_config(system_config).sha256
    assert cli.os.environ["OAM_DOTENV_ONLY"] == "loaded"


def test_version_command_reports_the_product_release(capsys) -> None:
    exit_code = main(["--version"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "open-agent-memory 0.0.1"


def test_preflight_fails_closed_and_names_every_missing_required_role(capsys) -> None:
    exit_code = main(
        ["preflight", "--config", str(REPOSITORY_ROOT / "configs" / "oam.yaml"), "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert {key: value for key, value in payload.items() if key != "roles"} == {
        "config_sha256": payload["config_sha256"],
        "contract_version": "0.0.1",
        "missing_required_roles": [
            "abstract",
            "document_vision",
            "embedding",
            "extraction",
            "insights",
            "profile",
        ],
        "ready": False,
    }
    assert {role["status"] for role in payload["roles"]} == {"disabled"}
    assert len(payload["config_sha256"]) == 64


def test_command_help_exposes_required_step_two_entrypoints(capsys) -> None:
    exit_code = main(["--help"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert all(
        command in output
        for command in ["preflight", "migrate", "api", "worker", "recover-bootstrap"]
    )


class PassingProbeClient:
    def __init__(self) -> None:
        self.roles_probed = 0

    def probe_chat(self, binding, *, max_output_tokens):
        self.roles_probed += 1
        return ChatProbeOutput(content='{"ok": true}', finish_reason="stop")

    def probe_embeddings(self, binding, texts):
        self.roles_probed += 1
        return EmbeddingProbeOutput(vectors=((0.1, 0.2), (0.2, 0.3)))

    def probe_vision(self, binding, *, max_output_tokens):
        self.roles_probed += 1
        return VisionProbeOutput(
            content='{"left":"red","right":"blue"}',
            finish_reason="stop",
        )

    def probe_rerank(self, binding, documents):
        raise AssertionError("the optional reranker is not configured")


def test_preflight_command_runs_enabled_role_capability_probes(tmp_path, capsys) -> None:
    config_path = tmp_path / "oam.yaml"
    config_path.write_text(
        """
models:
  roles:
    extraction: shared
    insights: shared
    embedding: shared
    document_vision: shared
    abstract: shared
    profile: shared
  bindings:
    shared:
      protocol: openai_compatible
      model: synthetic-model
      endpoint_env: SYNTHETIC_MODEL_URL
      capabilities: [chat, vision, embeddings]
""",
        encoding="utf-8",
    )
    client = PassingProbeClient()

    exit_code = _preflight(
        config_path,
        as_json=True,
        environment={"SYNTHETIC_MODEL_URL": "https://models.example.test/v1"},
        probe_client=client,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["missing_required_roles"] == []
    assert {role["status"] for role in payload["roles"]} == {"passed", "disabled"}
    assert client.roles_probed == 6


def test_environment_may_select_one_alternate_config_path(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "alternate.yaml"
    config_path.write_text("resource:\n  max_bytes: 12345\n", encoding="utf-8")
    monkeypatch.setenv("OAM_CONFIG", str(config_path))

    exit_code = main(["preflight", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["config_sha256"] == load_config(config_path).sha256
