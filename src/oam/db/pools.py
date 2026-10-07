"""Process-local PostgreSQL pools for long-lived OAM processes."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from oam.config import OAMConfig


@dataclass
class WorkerPools:
    """The queue and content pools owned by one worker process."""

    queue: ConnectionPool
    content: ConnectionPool

    def close(self) -> None:
        """Return/close both pools, even if closing the first pool fails."""
        try:
            self.queue.close()
        finally:
            self.content.close()


def create_worker_pools(config: OAMConfig, queue_dsn: str, content_dsn: str) -> WorkerPools:
    """Create and open the worker's bounded, process-local queue/content pools."""
    queue = None
    content = None
    try:
        queue = ConnectionPool(
            conninfo=queue_dsn,
            min_size=config.database.pool_min_size,
            max_size=config.database.worker_pool_max,
            open=False,
            timeout=config.database.pool_acquire_timeout_seconds,
            name="oam-worker-queue",
        )
        content = ConnectionPool(
            conninfo=content_dsn,
            min_size=config.database.pool_min_size,
            max_size=config.database.worker_pool_max,
            open=False,
            timeout=config.database.pool_acquire_timeout_seconds,
            name="oam-worker-content",
        )
        queue.open(wait=True, timeout=config.database.pool_acquire_timeout_seconds)
        content.open(wait=True, timeout=config.database.pool_acquire_timeout_seconds)
    except BaseException:
        try:
            if queue is not None:
                queue.close()
        finally:
            if content is not None:
                content.close()
        raise
    assert queue is not None and content is not None
    return WorkerPools(queue=queue, content=content)
