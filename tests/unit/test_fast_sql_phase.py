"""Fast SQL phase ownership closes before a response and never spans model I/O."""

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import get_ident

import psycopg
import pytest
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.contracts.search import SearchRequest
from oam.retrieval.service import RecallService, RecallUnavailable


class PhaseRepository(DatedRepository):
    def __init__(self, request, service, token):
        super().__init__(request)
        self.service, self.token = service, token
        self.trace = None

    def user_state(self):
        self.service.state_reads += 1
        return super().user_state()

    def dense(self, vector, limit):
        self.service.reads.append(("dense", self.token))
        if self.service.failure == "sql":
            raise psycopg.ProgrammingError("injected SQL failure")
        return [dict(row, kind="fact") for row in super().dense(vector, limit)]

    def fast_graph(self, seeds, vector):
        self.service.reads.append(("graph", self.token))
        return (), {}

    def hydrate(self, ids):
        self.service.reads.append(("hydrate", self.token))
        if self.service.failure == "deadline":
            raise TimeoutError("injected deadline")
        return super().hydrate(ids)

    def record_trace(self, trace_id, policy_epoch, decisions, result):
        self.service.reads.append(("trace", self.token))
        self.trace = super().record_trace(trace_id, policy_epoch, decisions, result)
        return self.trace


class PhaseModels(EmbeddingOnly):
    def __init__(self, service):
        self.service = service

    def embed(self, *args, **kwargs):
        assert get_ident() not in self.service.owners.values()
        return super().embed(*args, **kwargs)


class PhaseService(RecallService):
    def __init__(self, failure=None):
        super().__init__(CONFIG, None, PhaseModels(self), generation_id="generation")
        self.failure = failure
        self.reads, self.closed, self.traces = [], [], []
        self.owners = {}
        self.next_token = 0
        self.state_reads = 0

    @contextmanager
    def _repository(self, authority, request, deadline):
        # A temporal substep cannot borrow another connection while the caller owns one.
        assert get_ident() not in self.owners.values()
        self.next_token += 1
        token = self.next_token
        repository = PhaseRepository(request, self, token)
        self.owners[token] = get_ident()
        try:
            yield repository
            if repository.trace and self.failure == "commit":
                raise RuntimeError("injected commit failure")
        except BaseException:
            self.closed.append((token, "rollback"))
            raise
        else:
            if repository.trace:
                self.traces.append(repository.trace)
            self.closed.append((token, "commit"))
        finally:
            del self.owners[token]


def request(**updates):
    return SearchRequest(query="apple harvest", recall_level="fast", fast_graph=True, **updates)


def test_strict_fast_reuses_one_post_embedding_sql_owner_and_commits_trace():
    service = PhaseService()
    result = service.search(object(), request())

    assert {method for method, _ in service.reads} == {"dense", "graph", "hydrate", "trace"}
    assert len({token for _, token in service.reads}) == 1
    assert not service.owners
    assert service.state_reads == 2
    assert service.traces == [result["trace_id"]]
    assert len(service.closed) == service.next_token


@pytest.mark.parametrize(
    "options",
    [
        {"allow_partial": True},
        {
            "time_basis": "event",
            "time_window": {"start": "2025-09-01T00:00:00Z", "end": "2025-10-01T00:00:00Z"},
        },
    ],
)
def test_partial_and_temporal_fast_keep_independent_transactions(options):
    service = PhaseService()
    result = service.search(object(), request(**options))
    assert len({token for _, token in service.reads}) > 1
    assert not service.owners and service.traces == [result["trace_id"]]


@pytest.mark.parametrize(
    ("failure", "error"),
    [("sql", RecallUnavailable), ("deadline", TimeoutError), ("commit", RuntimeError)],
)
def test_fast_phase_failure_releases_owner_and_never_publishes_trace(failure, error):
    service = PhaseService(failure)
    with pytest.raises(error):
        service.search(object(), request())
    assert not service.owners and not service.traces
    assert len(service.closed) == service.next_token
    assert any(outcome == "rollback" for _, outcome in service.closed)


def test_inferred_temporal_fast_keeps_independent_transactions():
    service = PhaseService()
    result = service.search(
        object(),
        SearchRequest(
            query="apple harvest last year",
            recall_level="fast",
            fast_graph=True,
            reference_time=datetime(2026, 1, 1, tzinfo=UTC),
            reference_timezone="UTC",
        ),
    )
    assert result["diagnostics"]["query_time"]["status"] == "inferred"
    assert len({token for _, token in service.reads}) > 1
    assert not service.owners and service.traces == [result["trace_id"]]
