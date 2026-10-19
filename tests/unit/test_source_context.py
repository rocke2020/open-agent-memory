"""Source evidence compaction through the recall service's actual packing path."""

import json
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy

import psycopg
import pytest
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.contracts.search import SearchRequest
from oam.retrieval.service import RecallService
from oam.tokens import estimate_units, estimated_tokens


@pytest.fixture
def evidence_rows():
    text = "On June 3, I missed the workshop while covering an extra shift."
    locator = {
        "source_id": "conversation",
        "source_version": 1,
        "message_id": "0",
        "message_index": 0,
        "role": "user",
        "passage_index": 0,
    }
    common = {"origin": "memory", "time_evidence": [], "locator": {}}
    return [
        {
            **common,
            "unit_id": "fact",
            "kind": "fact",
            "text": "The user missed the workshop on June 3.",
            "citations": [
                {
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": "original",
                    "evidence_id": "evidence",
                    "span": {**locator, "quote": text},
                }
            ],
        },
        {**common, "unit_id": "other", "kind": "fact", "text": "Another topic.", "citations": []},
        {
            **common,
            "unit_id": "original",
            "kind": "passage",
            "text": text,
            "citations": [locator],
        },
        {
            **common,
            "unit_id": "second_event",
            "kind": "passage",
            "text": "On June 10, I missed another workshop while working.",
            "citations": [{**locator, "message_id": "2", "message_index": 2}],
        },
    ]


class SourceRepository(DatedRepository):
    def __init__(self, request, rows, graph_paths):
        self.request = request
        self.rows = {row["unit_id"]: row for row in rows}
        self.graph_paths = graph_paths

    def dense(self, vector, limit):
        return []

    def ordinary(self, seeds, vector):
        return tuple(self.graph_paths), self.graph_paths

    def hydrate(self, unit_ids):
        return [deepcopy(self.rows[unit_id]) for unit_id in unit_ids]

    def source_passage_locators(self, source_ids):
        return [
            {
                "unit_id": row["unit_id"],
                "passage_id": row.get("passage_id", row["unit_id"]),
                "source_id": row["citations"][0]["source_id"],
                "source_version": row["citations"][0]["source_version"],
                "locator": row["citations"][0],
            }
            for row in self.rows.values()
            if row["kind"] == "passage"
            and row["citations"]
            and row["citations"][0].get("source_id") in source_ids
        ]

    def supported_fact_ids(self, passage_ids, vector, limit, excluded_ids):
        return []

    def source_user_passage_ids(self, source_keys, vector, limit, excluded_ids):
        return []


class SourceService(RecallService):
    def __init__(self, rows, *, config=CONFIG, graph_paths=None):
        super().__init__(config, None, EmbeddingOnly(), generation_id="generation")
        self.rows = rows
        self.graph_paths = graph_paths or {}

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield SourceRepository(request, self.rows, self.graph_paths)


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_assistant_original_does_not_consume_the_only_protected_fact_slot(evidence_rows, mode):
    assistant = deepcopy(evidence_rows[2])
    assistant.update(
        unit_id="assistant_guide",
        text="Here are several ways to organize a workshop. " * 50,
        citations=[
            {
                "source_id": "guide",
                "source_version": 1,
                "message_id": "1",
                "message_index": 1,
                "role": "assistant",
            }
        ],
    )

    class RankedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "assistant_guide"}, {"unit_id": "fact"}]

        def source_user_passage_ids(self, source_keys, vector, limit, excluded_ids):
            return []

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, self.graph_paths)

    tier = getattr(CONFIG.search, mode).model_copy(
        update={"source_seed_results": 1, "max_results": 2}
    )
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={mode: tier})})
    result = RankedService([assistant, evidence_rows[0], evidence_rows[2]], config=config).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )

    assert [item["unit_id"] for item in result["items"]] == ["fact", "original"]
    assert "The user missed the workshop on June 3." in result["context"]
    assert evidence_rows[2]["text"] in result["context"]
    assert result["items"][1]["citations"] == evidence_rows[2]["citations"]
    assert result["diagnostics"]["generative_calls"] == 0


def test_trace_statement_timeout_preserves_recall_deadline_error(evidence_rows, monkeypatch):
    def timed_out(*args):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(SourceRepository, "record_trace", timed_out)
    with pytest.raises(TimeoutError, match="deadline"):
        SourceService(evidence_rows).search(object(), SearchRequest(query="missed workshops"))


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_passage_seed_recovers_query_relevant_user_original_without_a_fact(evidence_rows, mode):
    seed = deepcopy(evidence_rows[0])
    seed.update(
        unit_id="assistant_seed",
        kind="passage",
        text="Here is a checklist for tracking missed workshops.",
        citations=[
            {
                "source_id": "conversation",
                "source_version": 1,
                "message_id": "3",
                "message_index": 3,
                "role": "assistant",
            }
        ],
    )

    class PassageRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "assistant_seed"}]

        def source_user_passage_ids(self, source_keys, vector, limit, excluded_ids):
            assert source_keys == (("conversation", 1),)
            assert "assistant_seed" in excluded_ids
            return [{"unit_id": "original"}]

    class PassageService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield PassageRepository(request, self.rows, self.graph_paths)

    result = PassageService([seed, evidence_rows[2]]).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )

    assert "On June 3, I missed the workshop while covering an extra shift." in result["context"]
    original = next(item for item in result["items"] if item["unit_id"] == "original")
    assert original["citations"][0]["role"] == "user"
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["context_tokens"] <= CONFIG.context.default_tokens


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("max_results", [5, 6])
def test_supplemental_original_does_not_displace_ranked_evidence(evidence_rows, mode, max_results):
    guide = deepcopy(evidence_rows[2])
    guide.update(
        unit_id="guide",
        text="Workshop troubleshooting guide.",
        citations=[{"source_id": "guide", "source_version": 1, "role": "assistant"}],
    )
    recovered = deepcopy(guide)
    recovered.update(
        unit_id="recovered",
        text="I requested a workshop troubleshooting guide.",
        citations=[{"source_id": "guide", "source_version": 1, "role": "user"}],
    )
    ranked_fact = {**evidence_rows[1], "unit_id": "ranked_fact", "text": "The workshop moved."}

    class RankedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": uid} for uid in ("fact", "guide", "other", "ranked_fact")]

        def source_user_passage_ids(self, source_keys, vector, limit, excluded_ids):
            assert source_keys == (("guide", 1),)
            return [{"unit_id": "recovered"}]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, self.graph_paths)

    tier = getattr(CONFIG.search, mode).model_copy(
        update={"source_seed_results": 2, "max_results": max_results}
    )
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={mode: tier})})
    rows = [*evidence_rows[:3], guide, ranked_fact, recovered]
    result = RankedService(rows, config=config).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )
    ids = {item["unit_id"] for item in result["items"]}
    assert {"fact", "original", "ranked_fact"} <= ids
    assert ("recovered" in ids) == (max_results == 6)
    assert len(ids) <= max_results


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_grouped_context_preserves_facts_and_shows_source_once(evidence_rows, mode):
    result = SourceService(evidence_rows).search(
        object(), SearchRequest(query="missed workshops", recall_level=mode)
    )

    assert [row["unit_id"] for row in result["items"]] == [
        "fact",
        "other",
        "original",
        "second_event",
    ]
    assert evidence_rows[0]["text"] in result["context"]
    assert "source=[3]" in result["context"]
    assert result["context"].index("[3] passage") < result["context"].index("[1] fact")
    assert result["context"].index("[1] fact") < result["context"].index("[2] fact")
    assert result["context"].count(evidence_rows[2]["text"]) == 1
    assert "June 10" in result["context"]
    assert result["items"][0]["citations"] == evidence_rows[0]["citations"]
    assert result["items"][2]["citations"] == evidence_rows[2]["citations"]
    assert result["context_tokens"] == estimated_tokens(estimate_units(result["context"]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_version", 2),
        ("message_id", "other"),
        ("message_index", 2),
        ("role", "assistant"),
        ("quote", "unsupported text"),
        ("passage_index", 1),
    ],
)
def test_inconsistent_support_keeps_fact(evidence_rows, field, value):
    evidence_rows[0]["citations"][0]["span"][field] = value
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))
    assert {row["unit_id"] for row in result["items"]} == {
        "fact",
        "other",
        "original",
        "second_event",
    }
    assert "source=[3]" not in result["context"]


@pytest.mark.parametrize("unique_evidence", ["time", "path"])
def test_unique_time_and_graph_evidence_remain_on_the_fact(evidence_rows, unique_evidence):
    event = {"role": "event", "range": "2025-06-03"}
    evidence_rows[0]["time_evidence"] = [event] if unique_evidence == "time" else []
    path = {"arm": "graph", "nodes": ["seed", "fact"]}
    paths = {"fact": [path]} if unique_evidence == "path" else {}
    result = SourceService(evidence_rows, graph_paths=paths).search(
        object(), SearchRequest(query="workshops")
    )
    fact = next(row for row in result["items"] if row["unit_id"] == "fact")
    original = next(row for row in result["items"] if row["unit_id"] == "original")
    assert fact["paths"] == paths.get("fact", [])
    assert fact["time_evidence"] == evidence_rows[0]["time_evidence"]
    assert original["paths"] == []
    assert original["time_evidence"] == []
    assert "source=[3]" in result["context"]
    assert result["context"].count(evidence_rows[2]["text"]) == 1


@pytest.mark.parametrize(
    ("cap", "expected"), [(2, ["fact", "other"]), (3, ["fact", "other", "original"])]
)
def test_grouping_neither_fetches_nor_backfills_beyond_item_cap(evidence_rows, cap, expected):
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(
                update={"normal": CONFIG.search.normal.model_copy(update={"max_results": cap})}
            )
        }
    )
    result = SourceService(evidence_rows, config=config).search(
        object(), SearchRequest(query="workshops")
    )
    assert [row["unit_id"] for row in result["items"]] == expected
    assert ("source=[3]" in result["context"]) == (cap == 3)


@pytest.mark.parametrize("correct_quote", [True, False])
def test_coverage_checks_exact_unicode_quote(evidence_rows, correct_quote):
    passage = evidence_rows[2]
    passage["text"] = "开始。检修完成。"
    evidence_rows[0]["citations"][0]["span"]["quote"] = (
        "检修完成。" if correct_quote else "检修完成!"
    )
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="检修"))
    assert "fact" in {row["unit_id"] for row in result["items"]}
    assert ("source=[3]" in result["context"]) is correct_quote


def test_one_uncovered_support_prevents_grouping(evidence_rows):
    other = deepcopy(evidence_rows[0]["citations"][0])
    other["span"]["source_version"] = 2
    evidence_rows[0]["citations"].append(other)
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))
    assert "fact" in {row["unit_id"] for row in result["items"]}
    assert "source=[3]" not in result["context"]


def test_resolved_subject_and_date_survive_an_ambiguous_source(evidence_rows):
    source = "She missed it yesterday."
    evidence_rows[2]["text"] = source
    fact = evidence_rows[0]
    fact["text"] = "Maya missed the workshop on June 3."
    fact["citations"][0]["span"].update(quote=source)
    result = SourceService(evidence_rows).search(
        object(), SearchRequest(query="Who missed the workshop?")
    )

    assert fact["text"] in result["context"]
    assert source in result["context"]
    assert result["items"][0]["text"] == fact["text"]


def test_multiple_facts_share_one_source_without_losing_metadata(evidence_rows):
    second_fact = deepcopy(evidence_rows[0])
    second_fact.update(unit_id="shift", text="The user was covering an extra shift.")
    second_fact["time_evidence"] = [{"role": "event", "range": "2025-06-03"}]
    evidence_rows.insert(2, second_fact)
    before = deepcopy(evidence_rows)
    path = {"arm": "graph", "relation": "causal", "nodes": ["fact", "shift"]}
    result = SourceService(evidence_rows, graph_paths={"shift": [path]}).search(
        object(), SearchRequest(query="What happened at the workshop?")
    )

    assert evidence_rows == before
    assert {row["unit_id"] for row in result["items"]} == {row["unit_id"] for row in before}
    assert result["context"].count("source=[4]") == 2
    assert result["context"].count(before[3]["text"]) == 1
    shift = next(row for row in result["items"] if row["unit_id"] == "shift")
    assert shift["paths"] == [path]
    assert shift["time_evidence"] == second_fact["time_evidence"]


def test_recall_recovers_adjacent_user_source_before_tail_candidates(evidence_rows):
    class WindowRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def source_passage_locators(self, source_ids):
            assert source_ids == ("conversation",)
            return [
                {
                    "unit_id": row["unit_id"],
                    "passage_id": row["unit_id"],
                    "source_id": "conversation",
                    "source_version": 1,
                    "locator": row["citations"][0],
                }
                for row in (self.rows["original"], self.rows["second_event"])
            ]

    class WindowService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield WindowRepository(request, self.rows, self.graph_paths)

    result = WindowService(evidence_rows).search(
        object(), SearchRequest(query="missed workshops", max_context_tokens=2048)
    )

    assert "The user missed the workshop on June 3." in result["context"]
    assert "On June 10, I missed another workshop" in result["context"]
    assert result["context_tokens"] <= 2048


@pytest.mark.parametrize("mixed", [False, True])
def test_source_windows_reuse_hydrated_passages_and_fetch_only_missing_rows(evidence_rows, mixed):
    hydrations = []

    class TrackingRepository(SourceRepository):
        def lexical(self, query, limit):
            ids = ("fact", "original") if mixed else tuple(self.rows)
            return [{"unit_id": unit_id} for unit_id in ids]

        def hydrate(self, unit_ids):
            hydrations.append(list(unit_ids))
            return super().hydrate(unit_ids)

    class TrackingService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield TrackingRepository(request, self.rows, self.graph_paths)

    result = TrackingService(evidence_rows).search(object(), SearchRequest(query="workshops"))

    assert "June 3" in result["context"] and "June 10" in result["context"]
    if mixed:
        assert hydrations == [["fact", "original"], ["second_event"]]
        assert [item["unit_id"] for item in result["items"]] == [
            "fact",
            "original",
            "second_event",
        ]
    else:
        assert hydrations == [["fact", "other", "original", "second_event"]]
        assert [item["unit_id"] for item in result["items"]] == list(
            ("fact", "other", "original", "second_event")
        )


def test_source_policy_change_during_passage_reuse_rejects_result(evidence_rows):
    from oam.retrieval.service import RecallUnavailable

    class RevisedRepository(SourceRepository):
        epoch = 1

        def user_state(self):
            return {**super().user_state(), "policy_epoch": self.epoch}

        def source_passage_locators(self, source_ids):
            rows = super().source_passage_locators(source_ids)
            RevisedRepository.epoch = 2
            return rows

        def record_trace(self, trace_id, policy_epoch, decisions, result):
            return policy_epoch == self.epoch

    class RevisedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RevisedRepository(request, self.rows, self.graph_paths)

    with pytest.raises(RecallUnavailable, match="source policy changed before trace publication"):
        RevisedService(evidence_rows).search(object(), SearchRequest(query="workshops"))


def test_recall_keeps_related_user_fact_with_its_source_dialogue(evidence_rows):
    request_text = "Please recommend medical imaging research."
    related = deepcopy(evidence_rows[0])
    related.update(unit_id="related", text="The user requested research on medical imaging.")
    related["citations"][0]["source_id"] = "research"
    related["citations"][0]["passage_id"] = "research_request"
    related["citations"][0]["span"].update(
        source_id="research", quote=request_text, end=len(request_text)
    )
    request = deepcopy(evidence_rows[2])
    request.update(
        unit_id="research_request_unit", passage_id="research_request", text=request_text
    )
    request["citations"][0].update(source_id="research", end=len(request_text))
    response = deepcopy(evidence_rows[2])
    response.update(
        unit_id="research_response_unit",
        passage_id="research_response",
        text="A medical imaging symposium published relevant work.",
    )
    response["citations"][0].update(
        source_id="research",
        role="assistant",
        message_id="1",
        message_index=1,
        end=len(response["text"]),
    )

    class RelatedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            assert unit_ids == ("fact",)
            return {
                "unit_id": "related",
                "source_id": "research",
                "source_version": 1,
            }

    class RelatedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RelatedRepository(request, self.rows, self.graph_paths)

    result = RelatedService([*evidence_rows, related, request, response]).search(
        object(), SearchRequest(query="medical research", max_context_tokens=2048)
    )
    selected = {item["unit_id"] for item in result["items"]}
    assert {"related", "research_request_unit", "research_response_unit"} <= selected
    assert result["context_tokens"] <= 2048


def test_related_user_fact_does_not_displace_the_only_query_ranked_result(evidence_rows):
    unrelated = deepcopy(evidence_rows[0])
    unrelated.update(unit_id="unrelated", text="The user owns a garden rake.")
    unrelated["citations"][0].update(source_id="garden", passage_id="garden_passage")
    unrelated["citations"][0]["span"]["source_id"] = "garden"

    class RelatedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            assert unit_ids == ("fact",)
            return {"unit_id": "unrelated", "source_id": "garden", "source_version": 1}

    class RelatedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RelatedRepository(request, self.rows, self.graph_paths)

    fast = CONFIG.search.fast.model_copy(update={"source_seed_results": 1, "max_results": 1})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = RelatedService([evidence_rows[0], unrelated], config=config).search(
        object(), SearchRequest(query="Which workshop did I miss?", recall_level="fast")
    )

    assert [item["unit_id"] for item in result["items"]] == ["fact"]


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_related_fact_does_not_displace_a_seed_exceeding_its_primary_budget(evidence_rows, mode):
    ranked = deepcopy(evidence_rows[0])
    ranked["text"] = "Query ranked detail. " * 110
    related = deepcopy(evidence_rows[0])
    related.update(unit_id="related", text="The user owns a garden rake.")
    related["citations"][0].update(source_id="garden", passage_id="garden_passage")
    related["citations"][0]["span"]["source_id"] = "garden"

    class RelatedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            return {"unit_id": "related", "source_id": "garden", "source_version": 1}

    class RelatedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RelatedRepository(request, self.rows, self.graph_paths)

    tier = getattr(CONFIG.search, mode).model_copy(
        update={"source_seed_results": 2, "max_results": 2}
    )
    config = CONFIG.model_copy(
        update={
            "search": CONFIG.search.model_copy(update={mode: tier}),
            "context": CONFIG.context.model_copy(update={"source_detail_budget_fraction": 0.5}),
        }
    )
    result = RelatedService([ranked, evidence_rows[2], related], config=config).search(
        object(),
        SearchRequest(
            query="Which workshop did I miss?", recall_level=mode, max_context_tokens=1000
        ),
    )

    assert {item["unit_id"] for item in result["items"]} == {"fact", "original"}
    assert result["context_tokens"] <= 1000


def test_related_source_detail_does_not_displace_ranked_facts_original(evidence_rows):
    related = deepcopy(evidence_rows[0])
    related.update(unit_id="related", text="The user asked about a garden tool.")
    related["citations"][0].update(source_id="garden", passage_id="garden_passage")
    related["citations"][0]["span"].update(source_id="garden", quote="I asked about a garden tool.")
    related_passage = deepcopy(evidence_rows[2])
    related_passage.update(unit_id="garden_passage", text="I asked about a garden tool.")
    related_passage["citations"][0].update(source_id="garden", end=len(related_passage["text"]))

    class RelatedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            return {"unit_id": "related", "source_id": "garden", "source_version": 1}

    class RelatedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RelatedRepository(request, self.rows, self.graph_paths)

    fast = CONFIG.search.fast.model_copy(update={"source_seed_results": 2, "max_results": 3})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = RelatedService(
        [evidence_rows[0], evidence_rows[2], related, related_passage], config=config
    ).search(object(), SearchRequest(query="Which workshop did I miss?", recall_level="fast"))

    assert {"fact", "original"} <= {item["unit_id"] for item in result["items"]}


def test_supported_fact_does_not_displace_the_query_matched_passage(evidence_rows):
    companion = deepcopy(evidence_rows[0])
    companion.update(unit_id="companion", text="The user missed a workshop.")

    class CompanionRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "original"}]

        def supported_fact_ids(self, passage_ids, vector, limit, excluded_ids):
            assert passage_ids == ("original",)
            return [{"passage_id": "original", "unit_id": "companion"}]

    class CompanionService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield CompanionRepository(request, self.rows, self.graph_paths)

    fast = CONFIG.search.fast.model_copy(update={"source_seed_results": 1, "max_results": 1})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = CompanionService([evidence_rows[2], companion], config=config).search(
        object(), SearchRequest(query="What date did I miss the workshop?", recall_level="fast")
    )

    assert [item["unit_id"] for item in result["items"]] == ["original"]
    assert "June 3" in result["context"]


@pytest.mark.parametrize("context_budget", [2048, 500])
def test_supported_fact_does_not_displace_a_later_query_ranked_seed(evidence_rows, context_budget):
    companion = deepcopy(evidence_rows[0])
    companion.update(unit_id="companion", text="The user missed a workshop.")
    later_seed = deepcopy(evidence_rows[1])
    later_seed["text"] = "Another query-ranked topic. " * 40

    class CompanionRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "original"}, {"unit_id": "other"}]

        def supported_fact_ids(self, passage_ids, vector, limit, excluded_ids):
            return [{"passage_id": "original", "unit_id": "companion"}]

    class CompanionService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield CompanionRepository(request, self.rows, self.graph_paths)

    fast = CONFIG.search.fast.model_copy(update={"source_seed_results": 2, "max_results": 2})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = CompanionService([evidence_rows[2], later_seed, companion], config=config).search(
        object(),
        SearchRequest(
            query="Which workshop and other topic?",
            recall_level="fast",
            max_context_tokens=context_budget,
        ),
    )

    assert [item["unit_id"] for item in result["items"]] == ["original", "other"]


def test_related_source_reads_a_bounded_window_around_a_late_request(evidence_rows):
    request_text = "I asked about joining the astronomy club."
    response_text = "The assistant suggested the next local astronomy meeting."
    related = deepcopy(evidence_rows[0])
    related.update(unit_id="related", text="The user asked about the astronomy club.")
    related["citations"][0].update(source_id="long_source", passage_id="late_request")
    related["citations"][0]["span"].update(
        source_id="long_source",
        message_id="50",
        message_index=50,
        quote=request_text,
        end=len(request_text),
    )
    passages = []
    for index in range(50):
        passage = deepcopy(evidence_rows[2])
        passage.update(
            unit_id=f"earlier_{index}",
            text=("A previous unrelated observation. " * 8) + str(index),
        )
        passage["citations"][0].update(
            source_id="long_source",
            message_id=str(index),
            message_index=index,
            end=len(passage["text"]),
        )
        passages.append(passage)
    for unit_id, index, role, text in (
        ("late_request", 50, "user", request_text),
        ("late_response", 51, "assistant", response_text),
    ):
        passage = deepcopy(evidence_rows[2])
        passage.update(unit_id=unit_id, text=text)
        passage["citations"][0].update(
            source_id="long_source",
            message_id=str(index),
            message_index=index,
            role=role,
            end=len(text),
        )
        passages.append(passage)

    class TrackingRepository(SourceRepository):
        hydrated_ids = []

        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            return {"unit_id": "related", "source_id": "long_source", "source_version": 1}

        def hydrate(self, unit_ids):
            self.hydrated_ids.extend(unit_ids)
            return super().hydrate(unit_ids)

    class TrackingService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield TrackingRepository(request, self.rows, self.graph_paths)

    TrackingRepository.hydrated_ids = []
    result = TrackingService([*evidence_rows, related, *passages]).search(
        object(), SearchRequest(query="astronomy club", max_context_tokens=2048)
    )

    assert response_text in result["context"]
    assert sum(unit_id.startswith("earlier_") for unit_id in TrackingRepository.hydrated_ids) <= 6


def test_related_source_keeps_companion_from_original_source_window(evidence_rows):
    related = deepcopy(evidence_rows[0])
    related.update(unit_id="related", text="The user is learning another topic.")
    related["citations"][0]["source_id"] = "related_source"
    related["citations"][0]["passage_id"] = "related_0"
    related["citations"][0]["span"].update(
        source_id="related_source", quote="The user asked about another topic.", end=35
    )
    expanded = []
    for index in range(17):
        passage = deepcopy(evidence_rows[2])
        passage.update(unit_id=f"related_{index}", text="The user asked about another topic.")
        passage["citations"][0].update(
            source_id="related_source",
            message_id=str(index),
            message_index=index,
            end=len(passage["text"]),
        )
        expanded.append(passage)
    companion = deepcopy(evidence_rows[0])
    companion.update(unit_id="companion", text="The user also missed the second workshop.")
    companion["citations"][0]["passage_id"] = "second_event"
    companion["citations"][0]["span"].update(
        message_id="2",
        message_index=2,
        quote=evidence_rows[3]["text"],
        end=len(evidence_rows[3]["text"]),
    )

    class CompanionRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

        def related_user_fact(self, unit_ids):
            return {
                "unit_id": "related",
                "source_id": "related_source",
                "source_version": 1,
            }

        def supported_fact_ids(self, passage_ids, vector, limit, excluded_ids):
            return (
                [{"passage_id": "second_event", "unit_id": "companion"}]
                if "second_event" in passage_ids
                else []
            )

    class CompanionService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield CompanionRepository(request, self.rows, self.graph_paths)

    result = CompanionService([*evidence_rows, related, *expanded, companion]).search(
        object(), SearchRequest(query="missed workshops", max_context_tokens=5000)
    )
    assert "companion" in {item["unit_id"] for item in result["items"]}


def test_incremental_context_cost_bounds_compact_rendering(evidence_rows):
    from oam.retrieval.context import ContextUnits, render_context

    evidence_rows[0]["text"] = "用户在 June 3 错过 workshop 👋。"
    for order in (
        evidence_rows,
        [evidence_rows[2], evidence_rows[0], evidence_rows[3], evidence_rows[1]],
    ):
        counter = ContextUnits([], order)
        selected = []
        for item in order:
            selected.append(item)
            assert counter.candidate_units(item) >= estimate_units(render_context(selected))
            counter.add(item)
            assert counter.units >= estimate_units(render_context(selected))


def test_context_assembly_estimates_each_item_variant_once(evidence_rows, monkeypatch):
    from oam.retrieval import context

    render_item = context.render_item
    render_counts = Counter()

    def counting_render(item, source_id=None):
        render_counts[(id(item), source_id)] += 1
        return render_item(item, source_id)

    monkeypatch.setattr(context, "render_item", counting_render)
    chosen, omissions = context.assemble_context(
        evidence_rows,
        evidence_rows[:2],
        evidence_rows[2:],
        budget=2048,
        seed_budget=1024,
        detail_fraction=0.5,
        max_results=4,
    )

    assert [item["unit_id"] for item in chosen] == [
        "fact",
        "other",
        "original",
        "second_event",
    ]
    assert omissions == []
    assert render_counts[(id(evidence_rows[0]), "original")] == 1
    assert render_counts and max(render_counts.values()) == 1


def test_recall_reuses_seed_estimates_during_assembly(evidence_rows, monkeypatch):
    from oam.retrieval import context

    render_item = context.render_item
    render_counts = Counter()

    def counting_render(item, source_id=None):
        render_counts[(id(item), source_id)] += 1
        return render_item(item, source_id)

    monkeypatch.setattr(context, "render_item", counting_render)
    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))

    assert "The user missed the workshop on June 3." in result["context"]
    assert render_counts and max(render_counts.values()) <= 2


def test_recorded_replay_reassembles_the_same_grouped_context(evidence_rows):
    from oam.debug.traces import TraceRepository

    result = SourceService(evidence_rows).search(object(), SearchRequest(query="workshops"))

    class RecordedTrace(TraceRepository):
        def get(self, connection, authority, trace_id):
            return {"status": "recorded", "result": json.loads(json.dumps(result, sort_keys=True))}

    replayed = RecordedTrace().replay(None, None, "trace", "reassemble_recorded")
    assert replayed["status"] == "exact"
    assert replayed["result"] == result


def test_seed_packing_represents_other_sources_before_repeating_one_source():
    from oam.retrieval import context

    def fact(unit_id, source_id, source_version=1):
        return {
            "unit_id": unit_id,
            "kind": "fact",
            "origin": "memory",
            "text": f"Evidence {unit_id}",
            "citations": [{"source_id": source_id, "source_version": source_version}],
            "time_evidence": [],
        }

    ranked = [
        fact("a1", "source_a"),
        fact("a2", "source_a"),
        fact("a3", "source_a"),
        fact("a4", "source_a"),
        fact("b1", "source_b"),
        fact("a_new", "source_a", 2),
    ]
    seed_order = context.source_diverse_seed_order

    assert [item["unit_id"] for item in seed_order(ranked)] == [
        "a1",
        "b1",
        "a_new",
        "a2",
        "a3",
        "a4",
    ]
    selected = context.pack_ranked(seed_order(ranked), 2048, 3)
    assert "[2] fact memory\nEvidence b1" in context.render_context(selected)
    assert [item["unit_id"] for item in seed_order(ranked[:4])] == [
        "a1",
        "a2",
        "a3",
        "a4",
    ]


def test_seed_packing_prefers_the_highest_ranked_user_fact_per_source():
    from oam.retrieval.context import source_diverse_seed_order

    def item(unit_id, source_id, kind, role, text):
        citation = {"source_id": source_id, "source_version": 1}
        citation["span" if kind == "fact" else "role"] = {"role": role} if kind == "fact" else role
        return {
            "unit_id": unit_id,
            "kind": kind,
            "text": text,
            "citations": [citation],
        }

    ranked = [
        item("long_assistant_passage", "source_a", "passage", "assistant", "x" * 2000),
        item("other_user_fact", "source_b", "fact", "user", "Other event"),
        item("short_assistant_fact", "source_a", "fact", "assistant", "A"),
        item("long_user_fact", "source_a", "fact", "user", "User attended an event"),
        item("short_user_fact", "source_a", "fact", "user", "User event"),
    ]

    assert [item["unit_id"] for item in source_diverse_seed_order(ranked)] == [
        "long_user_fact",
        "other_user_fact",
        "long_assistant_passage",
        "short_assistant_fact",
        "short_user_fact",
    ]
    assert source_diverse_seed_order(ranked[:1]) == ranked[:1]
    assistant_only = [
        item("long_assistant_fact", "source_c", "fact", "assistant", "Detailed event with a date"),
        item("another_source_fact", "source_d", "fact", "user", "Another event"),
        item("short_assistant_fact", "source_c", "fact", "assistant", "Event"),
    ]
    assert [item["unit_id"] for item in source_diverse_seed_order(assistant_only)] == [
        "long_assistant_fact",
        "another_source_fact",
        "short_assistant_fact",
    ]
    passages_only = [
        item("long_passage", "source_e", "passage", "assistant", "x" * 2000),
        item("another_source", "source_f", "passage", "user", "Elsewhere"),
        item("short_passage", "source_e", "passage", "user", "User source"),
    ]
    assert [item["unit_id"] for item in source_diverse_seed_order(passages_only)] == [
        "long_passage",
        "another_source",
        "short_passage",
    ]


def test_recall_restores_facts_supported_by_direct_and_windowed_user_passages(evidence_rows):
    direct = deepcopy(evidence_rows[0])
    direct.update(unit_id="direct_detail", text="The user covered an extra shift.")
    window = deepcopy(evidence_rows[0])
    window.update(unit_id="window_detail", text="The user missed another workshop on June 10.")
    window["citations"][0]["passage_id"] = "second_event"
    window["citations"][0]["span"].update(
        message_id="2",
        message_index=2,
        quote=evidence_rows[3]["text"],
        end=len(evidence_rows[3]["text"]),
    )
    unrelated = deepcopy(window)
    unrelated.update(unit_id="unrelated", text="An unrelated later event.")
    later = deepcopy(evidence_rows[3])
    later.update(unit_id="later")
    later["citations"][0].update(message_id="4", message_index=4)
    unrelated["citations"][0]["passage_id"] = "later"
    rows = [*evidence_rows, direct, window, unrelated, later]

    class CompanionRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "original"}]

        def supported_fact_ids(self, passage_ids, vector, limit, excluded_ids):
            mapping = {
                "original": ("fact", "direct_detail"),
                "second_event": ("window_detail",),
                "later": ("unrelated",),
            }
            return [
                {"passage_id": passage_id, "unit_id": unit_id}
                for passage_id in passage_ids
                if passage_id in mapping
                for unit_id in mapping[passage_id]
                if unit_id not in excluded_ids
            ][:limit]

    class CompanionService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield CompanionRepository(request, self.rows, self.graph_paths)

    for mode in ("fast", "normal"):
        result = CompanionService(rows).search(
            object(), SearchRequest(query="Which workshop was missed?", recall_level=mode)
        )
        selected = {item["unit_id"] for item in result["items"]}
        assert {"fact", "original", "second_event", "direct_detail", "window_detail"} <= selected
        assert "unrelated" not in selected
        assert result["diagnostics"]["generative_calls"] == 0
        assert result["context_tokens"] <= CONFIG.context.default_tokens
