"""Durable worker process entrypoint."""

from __future__ import annotations

import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx

from oam.config import ConfigurationError, load_config
from oam.models.runtime import RuntimeModels
from oam.workers.index_source import MAX_SOURCE_EXTRACTIONS, process_one

IDLE_POLL_SECONDS = 0.5


def run_worker_from_environment(config_path: str | Path, *, once: bool = False) -> int:
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
    config = resolved.config
    stopped = Event()
    previous = {}
    if not once:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stopped.set())
    worker_id = "worker_" + uuid4().hex
    try:
        with httpx.Client(trust_env=False) as http:
            models = RuntimeModels(config, os.environ, http)
            model_cap = min(config.worker.max_model_calls, config.provider.global_concurrency)
            concurrency = min(
                config.worker.max_tasks,
                model_cap,
                config.database.worker_pool_max,
                config.user.concurrency,
            )
            # Shared runtime slots bound actual calls while one active source can use the full cap.
            source_parallelism = min(MAX_SOURCE_EXTRACTIONS, model_cap)
            source_config = config.model_copy(
                update={
                    "worker": config.worker.model_copy(
                        update={"max_model_calls": source_parallelism}
                    )
                }
            )

            def process_claims() -> int:
                while not stopped.is_set():
                    try:
                        processed = process_one(
                            source_config,
                            os.environ[config.database.worker_queue_dsn_env],
                            os.environ[config.database.worker_content_dsn_env],
                            models=models,
                            worker_id=worker_id,
                        )
                    except Exception as error:
                        print(
                            f"indexing claim failed: {type(error).__name__}",
                            file=sys.stderr,
                            flush=True,
                        )
                        if once:
                            return 1
                        stopped.wait(1)
                        continue
                    if once:
                        return 0
                    if not processed:
                        stopped.wait(IDLE_POLL_SECONDS)
                return 0

            if once:
                return process_claims()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                try:
                    pending = [executor.submit(process_claims) for _ in range(concurrency)]
                    for future in pending:
                        future.result()
                finally:
                    # Drain complete sources before closing the shared model client.
                    stopped.set()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0
