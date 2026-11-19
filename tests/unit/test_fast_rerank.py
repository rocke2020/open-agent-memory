"""Optional fast scoring preserves complete evidence and read boundaries."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_l2_source_context import ORIGINAL as INSIGHT_ORIGINAL
from test_l2_source_context import InsightService
from test_local_evidence import fact, original
from test_query_time_recall import CONFIG, DatedRepository, DatedService, EmbeddingOnly
from test_source_context import SourceRepository, SourceService, evidence_rows  # noqa: F401, F811

from oam.contracts.search import SearchRequest
from oam.models.runtime import Usage
from oam.retrieval.service import RecallUnavailable


def scoring_config(*, cap=2, floor=None, adjacent=False):
    values = CONFIG.model_dump(mode="json")
    values["models"]["reranker"] = {
        "protocol": "openai_compatible",
        "model": "opaque/operator-choice",
        "endpoint_env": "TEST_RERANK_ENDPOINT",
        "capabilities": ["rerank"],
    }
    values["search"]["fast"].update(
        rerank_candidates=cap, rerank_min_score=floor, adjacent_user_context=adjacent
    )
    return type(CONFIG).model_validate(values)


class DistinctRepository(DatedRepository):
    def __init__(self, request):
        super().__init__(request)
        self.rows["new"]["text"] = "The newer apple harvest was excellent."


class DistinctService(DatedService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield DistinctRepository(request)


class Scorer(EmbeddingOnly):
    def __init__(self, scores, error=None):
        self.scores, self.error, self.calls = scores, error, []

    def rerank(self, query, documents, **kwargs):
        self.calls.append((query, documents))
        if self.error:
            raise self.error
        assert len(documents) == len(self.scores)
        return SimpleNamespace(scores=tuple(self.scores), usage=Usage(coverage="complete"))


@pytest.mark.parametrize(
    ("source_seeds", "channel_limit", "keep_reply"),
    [(2, 2, True), (1, 2, False), (2, 1, False)],
)
def test_output_cap_does_not_reduce_the_configured_reply_witness_budget(
    source_seeds, channel_limit, keep_reply
):
    parent = original("parent", 4)
    witness = fact("witness", parent)
    reply = original("reply_body", 5, "assistant")
    reply["text"] = "Try a short trail away from busy parks."
    reply_fact = fact("reply_fact", reply)
    leading = original("leading_original", 20)
    rows = [parent, witness, reply, reply_fact, leading]
    for row in rows:
        if row["kind"] == "passage":
            row["locator"] = row["citations"][0]

    class Repository(SourceRepository):
        def dense(self, vector, limit):
            return [
                {
                    "unit_id": "parent",
                    "kind": "passage",
                    "score": 0.9,
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": "parent",
                },
                {"unit_id": "witness", "kind": "fact", "score": 0.8},
            ][:limit]

        def lexical(self, query, limit):
            return [
                {"unit_id": node, "strict_match": False}
                for node in ("reply_fact", "leading_original")
            ][:limit]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    class ReplyScorer(EmbeddingOnly):
        def rerank(self, query, documents, **kwargs):
            scores = tuple(0.9 if reply["text"] in document else 0.1 for document in documents)
            return SimpleNamespace(scores=scores, usage=Usage(coverage="complete"))

    values = scoring_config(cap=8, floor=0.5).model_dump(mode="json")
    values["search"]["fast"].update(
        max_results=1,
        source_seed_results=source_seeds,
        channel_candidates=channel_limit,
        partial_match_gate="user_fact_exchange",
    )
    service = Service(rows, config=type(CONFIG).model_validate(values))
    service.models = ReplyScorer()
    result = service.search(
        object(), SearchRequest(query="Which activity suits my preference?", recall_level="fast")
    )
    assert (reply["text"] in result["context"]) is keep_reply
    if keep_reply:
        selected = {item["unit_id"]: item for item in result["items"]}
        assert selected["reply_body"]["text"] == reply["text"]
        assert selected["reply_body"]["citations"] == reply["citations"]
    assert sum(item["kind"] == "fact" for item in result["items"]) <= 1
    assert sum(item["kind"] == "passage" for item in result["items"]) <= 1
    assert result["diagnostics"]["generative_calls"] == 0


@pytest.mark.parametrize("floor", [float("nan"), float("inf"), -float("inf")])
def test_fast_floor_requires_a_finite_operator_score(floor):
    with pytest.raises(ValidationError):
        scoring_config(floor=floor)


@pytest.mark.parametrize("missing", ["binding", "cap"])
def test_fast_floor_requires_explicit_scoring_inputs(missing):
    values = scoring_config(floor=-2.5).model_dump(mode="json")
    if missing == "binding":
        values["models"]["reranker"] = None
    else:
        values["search"]["fast"]["rerank_candidates"] = None
    with pytest.raises(ValidationError, match="rerank_min_score"):
        type(CONFIG).model_validate(values)


@pytest.mark.parametrize(
    ("scores", "floor", "want"),
    [
        ([0.1, 0.8], 0.2, ["new"]),
        ([0.4, 0.4], 0.2, ["old", "new"]),
        ([-10.0, -3.0], None, ["new", "old"]),
        ([-10.0, -3.0], 0.0, []),
    ],
)
@pytest.mark.parametrize("source", ["config", "request"])
def test_fast_scores_global_roots_with_stable_ties_and_optional_floor(scores, floor, want, source):
    scorer = Scorer(scores)
    config = scoring_config(cap=None) if source == "request" else scoring_config(floor=floor)
    result = DistinctService(config, None, scorer, generation_id="generation").search(
        object(),
        SearchRequest(
            query="the complete original query",
            recall_level="fast",
            overrides={"rerank_candidates": 2, "rerank_min_score": floor}
            if source == "request"
            else {},
        ),
    )
    assert [item["unit_id"] for item in result["items"]] == want
    assert scorer.calls[0][0] == "the complete original query"
    assert result["diagnostics"]["rerank"]["applied"] is True
    assert result["diagnostics"]["rerank"]["scored"] == 2
    assert result["diagnostics"]["rerank"]["accepted"] == len(want)
    assert result["diagnostics"]["generative_calls"] == 0


def test_fast_cap_omits_unscored_tail_explicitly():
    result = DistinctService(
        scoring_config(cap=1), None, Scorer([0.4]), generation_id="generation"
    ).search(object(), SearchRequest(query="apple", recall_level="fast"))
    assert [item["unit_id"] for item in result["items"]] == ["old"]
    assert result["diagnostics"]["context_budget"]["omission_reasons"]["new"] == "rerank_root_cap"


@pytest.mark.parametrize("partial", [False, True])
def test_fast_error_preserves_original_selection_only_for_partial(partial):
    scorer = Scorer([], RuntimeError("scorer unavailable"))
    service = DistinctService(scoring_config(), None, scorer, generation_id="generation")
    request = SearchRequest(query="apple", recall_level="fast", allow_partial=partial)
    if not partial:
        with pytest.raises(RecallUnavailable, match="rerank"):
            service.search(object(), request)
    else:
        result = service.search(object(), request)
        assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
        assert result["diagnostics"]["rerank"]["status"] == "degraded"
        assert result["diagnostics"]["degraded"] == ["rerank"]


def test_fast_timeout_never_degrades_to_success():
    service = DistinctService(
        scoring_config(), None, Scorer([], TimeoutError("deadline")), generation_id="generation"
    )
    with pytest.raises(TimeoutError):
        service.search(
            object(), SearchRequest(query="apple", recall_level="fast", allow_partial=True)
        )


@pytest.mark.parametrize("changed", [None, "generation", "parse_partial", "policy"])
def test_fast_closes_transaction_before_scoring_and_reads_fresh_state(changed):
    class Repository(DistinctRepository):
        def user_state(self):
            state = super().user_state()
            if service.scored:
                service.fresh_reads += 1
                if changed == "generation":
                    state["index_generation_id"] = "new-generation"
                if changed == "parse_partial":
                    state["parse_partial"] = True
                if changed == "policy":
                    state["policy_epoch"] = 1
            return state

    class Service(DatedService):
        active, scored, fresh_reads = 0, False, 0

        @contextmanager
        def _repository(self, authority, request, deadline):
            self.active += 1
            try:
                yield Repository(request)
            finally:
                self.active -= 1

    class BoundaryScorer(Scorer):
        def rerank(self, *args, **kwargs):
            assert service.active == 0, "database transaction held during external scoring"
            service.scored = True
            return super().rerank(*args, **kwargs)

    service = Service(
        scoring_config(), None, BoundaryScorer([0.1, 0.8]), generation_id="generation"
    )
    if changed:
        with pytest.raises(RecallUnavailable):
            service.search(object(), SearchRequest(query="apple", recall_level="fast"))
    else:
        result = service.search(object(), SearchRequest(query="apple", recall_level="fast"))
        assert [item["unit_id"] for item in result["items"]] == ["new", "old"]
    assert service.fresh_reads > 0
    assert service.active == 0


@pytest.mark.parametrize(
    ("group_score", "unit_score", "expected_ids"),
    [
        (0.9, 0.1, ["original"]),
        (0.1, 0.9, ["original"]),
        (0.9, 0.8, ["fact", "original"]),
    ],
)
def test_fast_fact_relevance_requires_both_claim_and_source(
    evidence_rows,  # noqa: F811
    group_score,
    unit_score,
    expected_ids,
):
    rows = deepcopy(evidence_rows)
    rows[0]["text"] = "The user can meet on Tuesdays."
    rows[0]["citations"][0]["span"]["quote"] = "I can meet on Tuesdays."
    rows[0]["time_evidence"] = [{"role": "mentioned", "at": "2024-06-03T12:00:00Z"}]
    rows[2]["text"] = "I can meet on Tuesdays. My conference budget is $400. Complete budget tail."
    rows[2]["time_evidence"] = deepcopy(rows[0]["time_evidence"])

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "original"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, self.graph_paths)

    class RelevanceScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            self.scores = [
                (
                    group_score
                    if "The user can meet" in document and "$400" in document
                    else unit_score
                    if "The user can meet" in document
                    else 0.8
                )
                for document in documents
            ]
            return super().rerank(query, documents, **kwargs)

    service = Service(rows, config=scoring_config(floor=0.5))
    service.models = RelevanceScorer([])
    result = service.search(
        object(),
        SearchRequest(
            query="What is my conference budget?", recall_level="fast", include={"chunks": True}
        ),
    )

    assert [item["unit_id"] for item in result["items"]] == expected_ids
    assert result["items"][-1]["text"] == rows[2]["text"]
    assert result["items"][-1]["citations"] == rows[2]["citations"]
    query, documents = service.models.calls[0]
    assert query == "What is my conference budget?"
    units = [doc for doc in documents if "The user can meet" in doc and "$400" not in doc]
    assert len(units) == 1
    assert "user message:0" in units[0]
    assert "quote:'I can meet on Tuesdays.'" in units[0]
    assert "2024-06-03T12:00:00Z" in units[0]
    assert len(documents) == 3
    assert result["diagnostics"]["generative_calls"] == 0


@pytest.mark.parametrize("identical_claims", [False, True])
def test_fast_scores_claims_sharing_a_source_without_collapsing_root_ids(
    evidence_rows,  # noqa: F811
    identical_claims,
):
    rows = deepcopy(evidence_rows)
    rows[2]["text"] = "My conference budget is $400. I can meet on Tuesdays."
    rows[0]["text"] = "The user's conference budget is $400."
    rows[0]["citations"][0]["span"]["quote"] = "My conference budget is $400."
    rows[1]["text"] = "The user can meet on Tuesdays."
    rows[1]["citations"][0]["span"]["quote"] = "I can meet on Tuesdays."
    if identical_claims:
        rows[0]["text"] = rows[1]["text"]
        rows[0]["citations"][0]["span"]["quote"] = "I can meet on Tuesdays."

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "other"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, self.graph_paths)

    class ClaimScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            # Both complete groups contain the relevant budget; the scheduling
            # singleton does not. Scores follow document meaning, not array slots.
            self.scores = [0.9 if "$400" in doc else 0.1 for doc in documents]
            return super().rerank(query, documents, **kwargs)

    service = Service(rows, config=scoring_config(floor=0.5))
    service.models = ClaimScorer([])
    result = service.search(object(), SearchRequest(query="conference budget", recall_level="fast"))

    assert result["diagnostics"]["rerank"]["scored"] == 2
    assert result["diagnostics"]["rerank"]["accepted"] == (0 if identical_claims else 1)
    assert [item["unit_id"] for item in result["items"]] == (
        [] if identical_claims else ["fact", "original"]
    )
    assert len(service.models.calls[0][1]) == (2 if identical_claims else 4)


def test_fast_keeps_insight_scoring_on_its_complete_support_group():
    class InsightScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            self.scores = [
                0.9 if "Batch optimization" in doc and INSIGHT_ORIGINAL in doc else -1.0
                for doc in documents
            ]
            return super().rerank(query, documents, **kwargs)

    service = InsightService(config=scoring_config(floor=0.5))
    service.models = InsightScorer([])
    result = service.search(
        object(), SearchRequest(query="batch optimization", recall_level="fast")
    )

    assert {item["unit_id"] for item in result["items"]} == {"insight", "fact", "fact2", "code"}
    insight_documents = [doc for doc in service.models.calls[0][1] if "Batch optimization" in doc]
    assert len(insight_documents) == 1
    assert INSIGHT_ORIGINAL in insight_documents[0]
    assert "The implementation was submitted for review." in insight_documents[0]
    assert result["context"].count("from joblib import Parallel, delayed") == 1


def test_fast_scores_complete_required_body_and_filters_optional_same_id(evidence_rows):  # noqa: F811
    rows = deepcopy(evidence_rows)
    rows[2]["text"] += " Complete tail provenance. " * 200
    for row in rows[:2]:
        row["citations"][0]["span"]["quote"] = rows[2]["text"]
    rows[2]["time_evidence"] = [{"role": "mentioned", "at": "2024-06-03T12:00:00Z"}]

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "original"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, self.graph_paths)

    class FullScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            assert len(documents) >= 2
            assert rows[2]["text"] in documents[0]
            assert "user" in documents[0]
            assert "2024-06-03" in documents[0]
            assert "The user missed" in documents[0]
            # Original is also a standalone root: reject its independent admission,
            # but preserve it as the accepted fact's mandatory source evidence.
            self.scores = [0.9 if "The user missed" in doc else -1.0 for doc in documents]
            return super().rerank(query, documents, **kwargs)

    service = Service(rows, config=scoring_config(floor=0.2, adjacent=True))
    service.models = FullScorer([])
    result = service.search(
        object(),
        SearchRequest(query="missed workshops", recall_level="fast", include={"chunks": True}),
    )
    assert [item["unit_id"] for item in result["items"]] == ["fact", "original"]
    assert rows[2]["text"] in result["context"]
    assert "second_event" not in {item["unit_id"] for item in result["items"]}


def test_fast_deduplicates_identical_rendered_inputs_without_deduplicating_roots():
    scorer = Scorer([0.5])
    result = DatedService(scoring_config(), None, scorer, generation_id="generation").search(
        object(), SearchRequest(query="apple", recall_level="fast")
    )
    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["rerank"]["documents"] == 1
    assert result["diagnostics"]["rerank"]["scored"] == 2


@pytest.mark.parametrize(
    ("previous_score", "next_score", "want"),
    [
        (0.4, 0.8, ["fact", "original", "second_event", "previous_event"]),
        (0.4, 0.4, ["fact", "original", "previous_event", "second_event"]),
    ],
)
def test_fast_orders_optional_originals_by_score_with_stable_ties(
    evidence_rows,  # noqa: F811
    previous_score,
    next_score,
    want,
):
    rows = deepcopy(evidence_rows)
    for row in rows[:2]:
        row["citations"][0]["span"].update(message_id="2", message_index=2)
    rows[2]["citations"][0].update(message_id="2", message_index=2)
    rows[3]["citations"][0].update(message_id="4", message_index=4)
    previous = deepcopy(rows[3])
    previous.update(unit_id="previous_event", text="An earlier workshop was rescheduled.")
    previous["citations"][0].update(message_id="0", message_index=0)
    rows.append(previous)

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, self.graph_paths)

    class OptionalScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            self.scores = [
                0.9
                if "The user missed" in text
                else previous_score
                if previous["text"] in text
                else next_score
                for text in documents
            ]
            return super().rerank(query, documents, **kwargs)

    service = Service(rows, config=scoring_config(cap=1, floor=0.2, adjacent=True))
    service.models = OptionalScorer([])
    result = service.search(
        object(), SearchRequest(query="workshops", recall_level="fast", include={"chunks": True})
    )
    assert [item["unit_id"] for item in result["items"]] == want


def test_partial_wrong_score_count_restores_original_order():
    scorer = Scorer([0.5])
    # A malformed adapter returning too few scores must follow the existing stage policy.
    scorer.rerank = lambda *args, **kwargs: SimpleNamespace(
        scores=(0.5,), usage=Usage(coverage="complete")
    )
    result = DistinctService(scoring_config(), None, scorer, generation_id="generation").search(
        object(), SearchRequest(query="apple", recall_level="fast", allow_partial=True)
    )
    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["rerank"]["applied"] is False
    assert result["diagnostics"]["degraded"] == ["rerank"]


def test_normal_keeps_its_existing_early_plain_text_scoring_path():
    scorer = Scorer([0.1, 0.8])
    result = DistinctService(
        scoring_config(floor=100.0), None, scorer, generation_id="generation"
    ).search(object(), SearchRequest(query="apple", recall_level="normal"))
    assert scorer.calls[0][1] == (
        "Apple harvest was good.",
        "The newer apple harvest was excellent.",
    )
    assert [item["unit_id"] for item in result["items"]] == ["new", "old"]
    assert "rerank" not in result["diagnostics"]


def test_rejected_optional_original_remains_required_for_another_kept_root(evidence_rows):  # noqa: F811
    rows = deepcopy(evidence_rows)
    second_fact = deepcopy(rows[0])
    second_fact.update(unit_id="second_fact", text="A separate supported event.")
    citation = second_fact["citations"][0]
    citation["passage_id"] = "second_event"
    citation["span"] = {**rows[3]["citations"][0], "quote": rows[3]["text"]}
    rows.append(second_fact)

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "second_fact"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, self.graph_paths)

    class GroupScorer(Scorer):
        def rerank(self, query, documents, **kwargs):
            self.scores = [
                0.9
                if "The user missed" in doc
                else 0.8
                if "A separate supported event." in doc
                else -1.0
                for doc in documents
            ]
            return super().rerank(query, documents, **kwargs)

    service = Service(rows, config=scoring_config(floor=0.2, adjacent=True))
    service.models = GroupScorer([])
    result = service.search(
        object(), SearchRequest(query="workshops", recall_level="fast", include={"chunks": True})
    )
    assert {item["unit_id"] for item in result["items"]} == {
        "fact",
        "original",
        "second_fact",
        "second_event",
    }
    optional_omissions = result["diagnostics"]["rerank"]["optional_omissions"]
    assert {entry["unit_id"] for entry in optional_omissions} == {"original", "second_event"}
    assert all(entry["reason"] == "rerank_score_floor" for entry in optional_omissions)


def test_fast_cap_without_binding_preserves_unscored_default():
    values = scoring_config().model_dump(mode="json")
    values["models"]["reranker"] = None
    config = type(CONFIG).model_validate(values)
    result = DistinctService(config, None, EmbeddingOnly(), generation_id="generation").search(
        object(), SearchRequest(query="apple", recall_level="fast")
    )
    assert [item["unit_id"] for item in result["items"]] == ["old", "new"]
    assert result["diagnostics"]["rerank"]["status"] == "disabled"
    assert "rerank" not in result["diagnostics"]["executed_stages"]


def test_normal_rejects_a_silently_ignored_fast_only_floor():
    values = CONFIG.model_dump(mode="json")
    values["search"]["normal"]["rerank_min_score"] = 0.2
    with pytest.raises(ValidationError, match="search.normal.rerank_min_score"):
        type(CONFIG).model_validate(values)
