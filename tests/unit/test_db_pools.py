from __future__ import annotations

from types import SimpleNamespace

import pytest

from oam.db.pools import create_worker_pools


class FakePool:
    created: list[FakePool] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.open_timeout = None
        self.closed = False
        self.__class__.created.append(self)

    def open(self, *, wait, timeout):
        self.opened = wait
        self.open_timeout = timeout

    def close(self):
        self.closed = True


def test_worker_pools_are_process_local_and_bounded(monkeypatch):
    monkeypatch.setattr("oam.db.pools.ConnectionPool", FakePool)
    config = SimpleNamespace(
        database=SimpleNamespace(
            api_pool_max=10, worker_pool_max=10, pool_min_size=1, pool_acquire_timeout_seconds=5
        )
    )

    pools = create_worker_pools(config, "queue-dsn", "content-dsn")

    assert len(FakePool.created) == 2
    assert [pool.kwargs["conninfo"] for pool in FakePool.created] == [
        "queue-dsn",
        "content-dsn",
    ]
    assert all(pool.kwargs["min_size"] == 1 for pool in FakePool.created)
    assert all(pool.kwargs["max_size"] == 10 for pool in FakePool.created)
    assert all(pool.opened is True for pool in FakePool.created)
    assert all(pool.open_timeout == 5 for pool in FakePool.created)

    pools.close()

    assert all(pool.closed for pool in FakePool.created)


def test_worker_pool_checkout_timeout_is_configured(monkeypatch):
    monkeypatch.setattr("oam.db.pools.ConnectionPool", FakePool)
    config = SimpleNamespace(
        database=SimpleNamespace(
            api_pool_max=7,
            worker_pool_max=7,
            pool_min_size=2,
            pool_acquire_timeout_seconds=2.5,
        )
    )

    create_worker_pools(config, "queue-dsn", "content-dsn")

    assert [pool.kwargs["min_size"] for pool in FakePool.created[-2:]] == [2, 2]
    assert [pool.kwargs["timeout"] for pool in FakePool.created[-2:]] == [2.5, 2.5]
    assert [pool.open_timeout for pool in FakePool.created[-2:]] == [2.5, 2.5]


def test_worker_pool_content_constructor_failure_closes_queue(monkeypatch):
    class FailingPool(FakePool):
        def __init__(self, **kwargs):
            if kwargs["name"] == "oam-worker-content":
                raise RuntimeError("content pool construction failed")
            super().__init__(**kwargs)

    monkeypatch.setattr("oam.db.pools.ConnectionPool", FailingPool)
    config = SimpleNamespace(
        database=SimpleNamespace(
            api_pool_max=10, worker_pool_max=10, pool_min_size=1, pool_acquire_timeout_seconds=5
        )
    )
    with pytest.raises(RuntimeError, match="content pool construction failed"):
        create_worker_pools(config, "queue-dsn", "content-dsn")
    assert FailingPool.created[-1].closed
