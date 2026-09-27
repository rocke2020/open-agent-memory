"""Explicit dedicated-worker mode for integration tests of API behavior."""

from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from oam.api.app import create_app
from oam.config import load_config


def create_api_only_app(config_path: str | Path, **kwargs):
    config = load_config(config_path).config.model_dump(mode="json")
    config["worker"]["embedded_enabled"] = False
    with TemporaryDirectory(prefix="oam-api-test-") as directory:
        selected = Path(directory) / "oam.yaml"
        selected.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return create_app(selected, **kwargs)
