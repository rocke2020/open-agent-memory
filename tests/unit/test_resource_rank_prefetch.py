"""Overlap eligible document rank with FTS using independent, bounded SQL owners."""

import re
import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import PoolTimeout
from test_resource_guided_recall import ResourceService, recall

from oam.retrieval import resource_sql
from oam.retrieval.service import RecallService, RecallUnavailable

LITERAL_QUERY = 'Which "binding clause" applies to LAB-204?'


class OverlapService(ResourceService):
    def __init__(
        self, *, pure=True, mixed=False, preflight_error=None, rank_error=None, lexical_hit=True
    ):
        super().__init__(guidance=True, mixed=mixed)
        self.pool = object()
        self.fts_started = threading.Event()
        self.release_fts = threading.Event()
        self.prefetches = 0
        self.preflights = 0
        self.checked_out = 0
        self.prefetch_threads = []
        self.sql_threads = []
        embed = self.models.embed

        def embedding(*args, **kwargs):
            assert self.fts_started.wait(2), "FTS did not start"
            return embed(*args, **kwargs)

        def lexical(*_args):
            self.sql_threads.append(threading.get_ident())
            self.fts_started.set()
            assert self.release_fts.wait(2), "rank did not overlap FTS"
            return [{"unit_id": "global"}] if lexical_hit else []

        def only_text(generation):
            assert generation == "generation"
            self.preflights += 1
            if preflight_error or not pure:
                self.release_fts.set()
            if preflight_error:
                raise preflight_error
            return pure

        def rank(*args):
            self.prefetches += 1
            self.prefetch_threads.append(threading.get_ident())
            assert self.fts_started.is_set() and self.checked_out == 2
            self.release_fts.set()
            if rank_error:
                raise rank_error
            return self.repo.resource_document_rank(*args)

        self.models.embed = embedding
        self.repo.lexical = lexical
        self.prefetch_repo = SimpleNamespace(
            resource_only_plain_text=only_text, resource_document_rank=rank
        )

    @contextmanager
    def _repository(self, authority, request, deadline, *, checkout_timeout=None):
        self.checked_out += 1
        try:
            if checkout_timeout is None:
                yield self.repo
            else:
                assert 0 < checkout_timeout < deadline - time.monotonic()
                yield self.prefetch_repo
        finally:
            self.checked_out -= 1


def test_rank_overlaps_fts_once_and_preserves_native_slate_and_context():
    service = OverlapService()
    result = recall(service)
    serial = ResourceService(guidance=True)
    expected = recall(serial)
    assert service.prefetches == service.repo.document_calls == 1
    assert service.prefetch_threads != service.sql_threads and service.checked_out == 0
    assert service.models.documents == serial.models.documents
    assert service.repo.trace["resource_rerank_ids"] == serial.repo.trace["resource_rerank_ids"]
    assert result["items"] == expected["items"] and result["context"] == expected["context"]
    assert result["diagnostics"]["timing_ms"]["resource_document_rank"] > 0


def literal_recall(service):
    service.models.expected_query = LITERAL_QUERY
    service.repo.expected_query = LITERAL_QUERY
    service.repo.anchors = service.repo.lexical
    service.repo.lexical = lambda *_: []
    return recall(service, query=LITERAL_QUERY)


@pytest.mark.parametrize(
    ("opening_hit", "anchor_hit", "mixed", "expected"),
    [
        (True, True, False, (1, 1, 1)),
        (False, True, False, (1, 0, 1)),  # A later passage matches. Rank at ordinary consumption.
        (False, False, False, (1, 0, 0)),
        (True, True, True, (0, 0, 0)),
    ],
    ids=["opening", "deeper-only", "absent", "mixed"],
)
def test_literal_overlap_requires_a_current_first_passage(opening_hit, anchor_hit, mixed, expected):
    service = OverlapService(pure=not mixed, mixed=mixed, lexical_hit=anchor_hit)
    witness_calls = []

    def witness(query, generation):
        assert (query, generation) == (LITERAL_QUERY, "generation")
        assert service.checked_out == 2 and service.fts_started.is_set()
        witness_calls.append(query)
        if not opening_hit:
            service.release_fts.set()
        return opening_hit

    service.prefetch_repo.resource_first_passage_anchor = witness
    if not anchor_hit:
        service.repo.resource_clause_rank = lambda *_: {}
        service.repo.resource_neighbors = lambda *_: {}
    result = literal_recall(service)
    assert (len(witness_calls), service.prefetches, service.repo.document_calls) == expected
    assert service.checked_out == 0 and result["diagnostics"]["degraded"] == []
    if opening_hit and not mixed:
        serial = ResourceService(guidance=True)
        baseline = literal_recall(serial)
        assert serial.repo.trace["resource_rerank_ids"] == service.repo.trace["resource_rerank_ids"]
        assert serial.models.documents == service.models.documents
        assert (baseline["items"], baseline["context"]) == (result["items"], result["context"])


def test_literal_future_finishing_during_witness_skips_speculative_rank():
    service = OverlapService()
    pending = Future()

    def witness(*_):
        pending.set_result(([], []))
        return True

    service.prefetch_repo.resource_first_passage_anchor = witness
    result = service._prefetch_document_rank(
        object(),
        SimpleNamespace(query=LITERAL_QUERY),
        time.monotonic() + 5,
        "generation",
        pending,
        {},
    )
    assert result is None and pending.done()
    assert service.prefetches == 0 and service.checked_out == 0


def test_mixed_owner_never_prefetches_and_only_ranks_an_eligible_final_slate():
    mixed = OverlapService(pure=False, mixed=True)
    recall(mixed)
    assert mixed.preflights == 1 and mixed.prefetches == mixed.repo.document_calls == 0
    mixed_with_text_slate = OverlapService(pure=False)
    recall(mixed_with_text_slate)
    assert mixed_with_text_slate.prefetches == 0 and mixed_with_text_slate.repo.document_calls == 1


def test_optional_preflight_failure_keeps_original_strict_path():
    service = OverlapService(preflight_error=PoolTimeout("capacity"))
    result = recall(service)
    assert result["diagnostics"]["degraded"] == []
    assert service.prefetches == 0 and service.repo.document_calls == 1
    assert service.checked_out == 0


def test_unused_prefetched_rank_error_does_not_fail_strict_recall():
    service = OverlapService(rank_error=RuntimeError("unused rank"), lexical_hit=False)
    service.repo.resource_clause_rank = lambda *_: {}
    service.repo.resource_neighbors = lambda *_: {}
    result = recall(service)
    assert service.prefetches == 1 and service.checked_out == 0
    assert result["diagnostics"]["degraded"] == []
    assert [item["unit_id"] for item in result["items"]] == ["global"]


@pytest.mark.parametrize("partial", [True, False])
def test_consumed_rank_error_uses_existing_stage_policy(partial):
    service = OverlapService(rank_error=RuntimeError("rank failed"))
    if partial:
        result = recall(service, allow_partial=True)
        assert result["diagnostics"]["degraded"] == ["resource_document_guidance"]
        assert result["items"][0]["unit_id"] == "global"
    else:
        with pytest.raises(RecallUnavailable, match="resource_document_guidance"):
            recall(service)
    assert service.prefetches == 1 and service.repo.document_calls == service.checked_out == 0


@pytest.mark.parametrize("error", [PermissionError("revoked"), psycopg.errors.QueryCanceled()])
def test_permission_and_sql_deadline_failures_remain_fatal_in_partial_mode(error):
    service = OverlapService(rank_error=error)
    with pytest.raises((PermissionError, TimeoutError)):
        recall(service, allow_partial=True)
    assert service.checked_out == 0


def test_optional_checkout_does_not_shorten_the_sql_deadline(monkeypatch):
    service = ResourceService(guidance=True)
    seen = {}

    def checkout(*, timeout):
        seen["checkout"] = timeout
        return nullcontext(SimpleNamespace(transaction=nullcontext))

    def repository(*args, deadline, **kwargs):
        seen["sql"] = deadline

    service.pool = SimpleNamespace(connection=checkout)
    monkeypatch.setattr("oam.retrieval.service.RecallRepository", repository)
    request = SimpleNamespace(recall_level=SimpleNamespace(value="normal"))
    deadline = time.monotonic() + 20
    with RecallService._repository(service, object(), request, deadline, checkout_timeout=0.01):
        pass
    assert seen == {"checkout": 0.01, "sql": deadline}


def test_preflight_requires_a_nonempty_current_plain_text_scope():
    # Statement-local data exercises the actual SQL without using a database service.
    fixture = """
        WITH users(tenant_id,user_id,state,index_generation_id) AS (VALUES
            ('a','pure','active','g'), ('a','mixed','active','g'), ('a','pdf','active','g'),
            ('a','empty','active','g'), ('a','missing','active','g'), ('a','gone','inactive','g'),
            ('b','pure','active','g')),
        sources(tenant_id,user_id,source_id,active_version,status,input_role) AS (VALUES
            ('a','pure','doc',2,'active','evidence_resource'),
            ('a','pure','old',1,'excluded','interaction'),
            ('a','pure','profile',1,'active','profile_resource'),
            ('a','mixed','doc',1,'active','evidence_resource'),
            ('a','mixed','talk',1,'active','interaction'),
            ('a','pdf','doc',1,'active','evidence_resource'),
            ('a','missing','doc',1,'active','evidence_resource'),
            ('a','gone','doc',1,'active','evidence_resource'),
            ('b','pure','foreign',1,'active','interaction')),
        source_versions(tenant_id,user_id,source_id,source_version,status,media_type) AS (VALUES
            ('a','pure','doc',1,'superseded','application/pdf'),
            ('a','pure','doc',2,'active','text/plain'),
            ('a','mixed','doc',1,'active','text/plain'),
            ('a','mixed','talk',1,'active','text/plain'),
            ('a','pdf','doc',1,'active','application/pdf'),
            ('a','gone','doc',1,'active','text/plain'))
    """
    query = fixture + re.sub(r"%\((\w+)\)s", r":\1", resource_sql.ONLY_PLAIN_TEXT)
    connection = sqlite3.connect(":memory:")
    try:

        def permits(user, generation="g"):
            parameters = {"ann_tenant": "a", "ann_user": user, "generation": generation}
            return bool(connection.execute(query, parameters).fetchone()[0])

        assert permits("pure") and not permits("pure", "new-generation")
        assert not any(permits(user) for user in ("mixed", "pdf", "empty", "missing", "gone"))
    finally:
        connection.close()
