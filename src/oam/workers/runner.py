"""Durable worker process entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from oam.config import ConfigurationError, load_config


def run_worker_from_environment(config_path: str | Path) -> int:
    """Resolve distinct queue/content connections before starting worker stages."""
    try:
        resolved = load_config(config_path)
    except ConfigurationError:
        print("invalid OAM configuration", file=sys.stderr)
        return 2
    for environment_name in (
        resolved.config.database.worker_queue_dsn_env,
        resolved.config.database.worker_content_dsn_env,
    ):
        if not os.environ.get(environment_name):
            print(
                f"missing required environment variable {environment_name}",
                file=sys.stderr,
            )
            return 2
    print("worker stage processors are not installed", file=sys.stderr)
    return 2
