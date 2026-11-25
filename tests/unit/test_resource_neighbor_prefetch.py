"""Only ready, unchanged Resource recall may reuse ID reads overlapping the reranker."""

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import PoolTimeout
from test_query_time_recall import CONFIG
from test_recall_repository_setup import RecordingConnection
from test_resource_guided_recall import ResourceService, recall

from oam.auth.credentials import AuthorizedUser
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallUnavailable


class NeighborService(ResourceService):
    def __init__(
        self,
        *,
        initial=None,
        final=None,
        mixed=False,
        early_error=None,
        checkout_error=None,
        provider_error=None,
        expect_early=True,
        stale=False,
        hold_sql=False,
    ):
        super().__init__(guidance=True, mixed=mixed)
        self.pool = object()
        ready = {**self.repo.user_state(), "accepted_seq": 7}
        states = iter(({**ready, **(initial or {})}, {**ready, **(final or {})}))
        self.repo.user_state = lambda: next(states)
        self.worker_entered = threading.Event()
        self.scorer_started = threading.Event()
        self.scorer_finished = threading.Event()
        self.release_worker = threading.Event()
        if not hold_sql:
            self.release_worker.set()
        self.worker_released = threading.Event()
        self.early_calls = []
        self.late_calls = []
        self.neighbor_body_reads = []
        self.checkout_error = checkout_error
        self.request_thread = None
        embed = self.models.embed
        rerank = self.models.rerank
        hydrate = self.repo.hydrate
        neighbors = self.repo.resource_neighbors

        def embedding(*args, **kwargs):
            self.request_thread = threading.get_ident()
            return embed(*args, **kwargs)

        def scoring(*args, **kwargs):
            self.scorer_started.set()
            try:
                if expect_early:
                    assert self.worker_entered.wait(2), "neighbor IDs did not overlap the reranker"
                if provider_error:
                    raise provider_error
                return rerank(*args, **kwargs)
            finally:
                self.scorer_finished.set()

        def early(ids, generation):
            self.early_calls.append((tuple(ids), generation))
            self.worker_entered.set()
            assert self.scorer_started.wait(2) and self.scorer_finished.wait(2)
            assert self.release_worker.wait(2)
            if early_error:
                raise early_error
            if stale:
                return {uid: [uid] for uid in ids}
            return neighbors(ids, generation)

        def late(ids, generation):
            assert self.scorer_finished.is_set() and threading.get_ident() == self.request_thread
            self.late_calls.append((tuple(ids), generation))
            return neighbors(ids, generation)

        def body(ids):
            if "neighbor" in ids:
                assert (
                    self.scorer_finished.is_set() and threading.get_ident() == self.request_thread
                )
                self.neighbor_body_reads.append(tuple(ids))
            return hydrate(ids)

        self.models.embed = embedding
        self.models.rerank = scoring
        self.repo.resource_neighbors = late
        self.repo.hydrate = body
        self.worker_repo = SimpleNamespace(resource_neighbors=early)

    @contextmanager
    def _repository(self, authority, request, deadline, *, checkout_timeout=None):
        if checkout_timeout is None:
            yield self.repo
        elif threading.get_ident() == self.request_thread:
            # The independent FTS-stage optimization can decline its own preflight.
            yield SimpleNamespace(resource_only_plain_text=lambda _: False)
        else:
            assert 0 < checkout_timeout <= 0.01 < deadline - time.monotonic()
            try:
                if self.checkout_error:
                    self.worker_entered.set()
                    raise self.checkout_error
                yield self.worker_repo
            finally:
                self.worker_released.set()


def test_neighbor_ids_overlap_rerank_with_native_late_hydration_and_exact_context():
    service = NeighborService()
    native = recall(service)
    serial = ResourceService(guidance=True)
    expected = recall(serial)
    assert service.early_calls == [(("global", "rescue"), "generation")]
    assert service.worker_released.is_set() and service.late_calls == []
    assert len(service.neighbor_body_reads) == 1
    assert service.repo.trace["resource_rerank_ids"] == serial.repo.trace["resource_rerank_ids"]
    assert (native["items"], native["context"], native["context_tokens"]) == (
        expected["items"],
        expected["context"],
        expected["context_tokens"],
    )


@pytest.mark.parametrize(
    ("initial", "final", "expect_early"),
    [
        ({"readiness": "pending"}, {}, False),
        ({"readiness": "partial"}, {}, False),
        ({"parse_partial": True}, {}, False),
        ({}, {"readiness": "pending"}, True),
        ({}, {"parse_partial": True}, True),
        ({}, {"accepted_seq": 8}, True),
    ],
)
def test_new_acceptance_or_incomplete_publication_uses_current_neighbors(
    initial, final, expect_early
):
    service = NeighborService(initial=initial, final=final, expect_early=expect_early, stale=True)
    result = recall(service, allow_partial=True)
    assert bool(service.early_calls) is expect_early
    assert len(service.late_calls) == 1
    assert result["items"][0]["citations"][0]["block"] == "text:5"


@pytest.mark.parametrize(
    "options", [{"early_error": RuntimeError("lookup")}, {"checkout_error": PoolTimeout()}]
)
def test_optional_id_lookup_failure_retries_only_at_normal_consumption(options):
    service = NeighborService(**options)
    result = recall(service)
    assert len(service.late_calls) == len(service.neighbor_body_reads) == 1
    assert service.worker_released.is_set() and result["diagnostics"]["degraded"] == []


def test_mixed_slate_stays_on_native_path_without_neighbor_work():
    service = NeighborService(mixed=True, expect_early=False)
    recall(service)
    assert not service.worker_entered.is_set() and service.late_calls == []


@pytest.mark.parametrize("partial", [False, True])
def test_failed_rerank_discards_active_neighbor_error_and_drains_owned_worker(partial, monkeypatch):
    draining = threading.Event()

    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.observed = kwargs["thread_name_prefix"] == "oam-recall-neighbors"

        def shutdown(self, wait=True, *, cancel_futures=False):
            if self.observed and wait:
                draining.set()
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr("oam.retrieval.service.ThreadPoolExecutor", ObservedExecutor)
    service = NeighborService(
        provider_error=ValueError("scorer failed"), early_error=PermissionError(), hold_sql=True
    )
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(recall, service, allow_partial=partial)
        try:
            assert service.scorer_finished.wait(2) and service.worker_entered.is_set()
            assert draining.wait(2), "request did not drain its active SQL worker"
            assert not service.worker_released.is_set() and not future.done()
        finally:
            service.release_worker.set()
        if partial:
            result = future.result(2)
            assert result["diagnostics"]["degraded"] == ["rerank"]
        else:
            with pytest.raises(RecallUnavailable, match="rerank"):
                future.result(2)
    assert service.worker_released.is_set()
    assert not service.late_calls and not service.neighbor_body_reads


def test_failed_rerank_cancels_a_neighbor_read_that_has_not_started(monkeypatch):
    queued = Future()

    def make_executor(**kwargs):
        if kwargs["thread_name_prefix"] == "oam-recall-neighbors":
            return nullcontext(SimpleNamespace(submit=lambda *_: queued))
        return ThreadPoolExecutor(**kwargs)

    monkeypatch.setattr("oam.retrieval.service.ThreadPoolExecutor", make_executor)
    service = NeighborService(provider_error=ValueError("scorer failed"), expect_early=False)
    with pytest.raises(RecallUnavailable, match="rerank"):
        recall(service)
    assert queued.cancelled() and not service.late_calls and not service.neighbor_body_reads


def test_policy_change_discards_earlier_ids_and_keeps_trace_publication_fenced():
    service = NeighborService(final={"policy_epoch": 1}, stale=True)
    service.repo.record_trace = lambda *_: False
    with pytest.raises(RecallUnavailable, match="source policy changed"):
        recall(service)
    assert len(service.late_calls) == 1 and service.worker_released.is_set()


@pytest.mark.parametrize("error", [PermissionError(), psycopg.errors.QueryCanceled()])
def test_fatal_errors_on_a_consumed_neighbor_read_keep_existing_semantics(error):
    service = NeighborService(early_error=error)
    with pytest.raises((PermissionError, TimeoutError)):
        recall(service, allow_partial=True)
    assert service.worker_released.is_set() and not service.late_calls


def test_existing_user_state_query_projects_the_acceptance_sequence():
    connection = RecordingConnection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))
    repository = RecallRepository(connection, authority, CONFIG, deadline=time.monotonic() + 5)
    repository.user_state()
    query = connection.statements[-1]
    assert "SELECT sc.index_generation_id,sc.policy_epoch,sc.accepted_seq," in query
