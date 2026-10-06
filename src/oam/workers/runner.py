"""Shared lifecycle for embedded and dedicated durable workers."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

import httpx

from oam.capacity import PostgresCapacity
from oam.config import ConfigurationError, OAMConfig, load_config
from oam.db.readiness import postgres_connection_ready
from oam.models.runtime import RuntimeModels
from oam.workers.index_source import process_one

IDLE_POLL_SECONDS = 0.5
FAILED_CLAIM_POLL_SECONDS = 1


class WorkerRunner:
    """Own task threads and keep model clients alive until their last task exits."""

    def __init__(
        self, config: OAMConfig, queue_dsn: str, content_dsn: str, environment: Mapping[str, str]
    ) -> None:
        self.config = config
        self.queue_dsn = queue_dsn
        self.content_dsn = content_dsn
        self.worker_id = "worker_" + uuid4().hex
        self.stop_event = Event()
        self._lock = Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list[Future[int]] = []
        self._remaining = 0
        self._starting = False
        self._closed = False
        self._failed = False
        self._http = httpx.Client(trust_env=False)
        self._capacity = PostgresCapacity(config, queue_dsn)
        try:
            self._capacity.configure()
            self.models = RuntimeModels(
                config,
                environment,
                self._http,
                background=True,
                admission=self._capacity,
                stop_event=self.stop_event,
            )
        except BaseException:
            self._close_resources()
            raise

    @classmethod
    def from_environment(
        cls, config: OAMConfig, environment: Mapping[str, str] | None = None
    ) -> WorkerRunner:
        environment = os.environ if environment is None else environment
        names = (
            config.database.worker_queue_dsn_env,
            config.database.worker_content_dsn_env,
        )
        for name in names:
            if not environment.get(name):
                raise ConfigurationError(f"missing required environment variable {name}")
        return cls(config, environment[names[0]], environment[names[1]], environment)

    @property
    def healthy(self) -> bool:
        return (
            bool(self._futures)
            and not self.stop_event.is_set()
            and all(not future.done() for future in self._futures)
        )

    def databases_ready(self) -> bool:
        return all(postgres_connection_ready(dsn) for dsn in (self.queue_dsn, self.content_dsn))

    @property
    def failed(self) -> bool:
        return self._failed

    def start(self) -> None:
        if self._executor is not None or self._closed:
            raise RuntimeError("worker runner has already started or closed")
        concurrency = min(self.config.worker.max_tasks, self.config.database.worker_pool_max)
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="oam-worker"
        )
        self._starting = True
        try:
            for _ in range(concurrency):
                with self._lock:
                    self._remaining += 1
                try:
                    future = self._executor.submit(self._process_claims)
                except BaseException:
                    with self._lock:
                        self._remaining -= 1
                    raise
                self._futures.append(future)
                future.add_done_callback(self._task_done)
        except BaseException:
            self._failed = True
            self.request_stop()
            self._executor.shutdown(wait=False)
            raise
        finally:
            self._starting = False
            with self._lock:
                finished = self._remaining == 0
            if finished:
                self._close_resources()

    def request_stop(self) -> None:
        self.stop_event.set()

    def shutdown(self) -> bool:
        """Stop claims; bounded drain leaves final cleanup with any surviving task."""
        self.request_stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False)
        _, unfinished = wait(self._futures, timeout=self.config.lease.shutdown_seconds)
        if unfinished:
            print("worker shutdown incomplete: active tasks are still draining", file=sys.stderr)
            return False
        self._close_resources()
        return True

    def run_once(self) -> int:
        try:
            return self._process_claims(once=True)
        finally:
            self.request_stop()
            self._close_resources()

    def _process_claims(self, *, once: bool = False) -> int:
        while not self.stop_event.is_set():
            try:
                processed = process_one(
                    self.config,
                    self.queue_dsn,
                    self.content_dsn,
                    models=self.models,
                    worker_id=self.worker_id,
                )
            except Exception as error:
                cause = (
                    f" (cause: {type(error.__cause__).__name__})"
                    if error.__cause__ is not None
                    else ""
                )
                print(
                    f"indexing claim failed: {type(error).__name__}{cause}",
                    file=sys.stderr,
                    flush=True,
                )
                if once:
                    return 1
                self.stop_event.wait(FAILED_CLAIM_POLL_SECONDS)
                continue
            if once:
                return 0
            if not processed:
                self.stop_event.wait(IDLE_POLL_SECONDS)
        return 0

    def _task_done(self, future: Future[int]) -> None:
        if not future.cancelled() and future.exception() is not None:
            self._failed = True
            self.request_stop()
        with self._lock:
            self._remaining -= 1
            finished = self._remaining == 0 and not self._starting
        if finished:
            self._close_resources()

    def _close_resources(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._http.close()
            finally:
                self._capacity.close()


def run_worker_from_environment(config_path: str | Path, *, once: bool = False) -> int:
    """Resolve worker resources; install process signals only in this entrypoint."""
    from oam.startup import validate_startup_environment

    try:
        config = load_config(config_path).config
    except ConfigurationError:
        print("invalid OAM configuration", file=sys.stderr)
        return 2
    try:
        validate_startup_environment(config, os.environ, command="worker")
        worker = WorkerRunner.from_environment(config)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    if once:
        return worker.run_once()
    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: worker.request_stop())
        worker.start()
        worker.stop_event.wait()
    finally:
        drained = worker.shutdown()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0 if drained and not worker.failed else 1
