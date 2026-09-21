"""Resolve canonical runtime assets from a checkout or installed distribution."""

from __future__ import annotations

import sysconfig
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_DISTRIBUTION_ROOT = Path(sysconfig.get_path("data")) / "share" / "open-agent-memory"


def default_config_path() -> Path:
    return _first_existing(
        Path.cwd() / "configs" / "oam.yaml",
        _SOURCE_ROOT / "configs" / "oam.yaml",
        _DISTRIBUTION_ROOT / "oam.yaml",
    )


def default_migration_directory() -> Path:
    return _first_existing(
        Path.cwd() / "migrations",
        _SOURCE_ROOT / "migrations",
        _DISTRIBUTION_ROOT / "migrations",
    )


def _first_existing(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[-1]
