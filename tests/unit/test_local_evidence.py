"""Local evidence selection preserves independent hits and complete support."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from test_source_context import CONFIG, SourceRepository, SourceService

from oam.contracts.search import SearchRequest
from oam.retrieval import context
from oam.retrieval.context import assemble_context, context_token_count, render_context
from oam.retrieval.local_context import local_evidence_ids


def original(identity, index, role="user", *, version=1, origin="memory"):
    return {
        "unit_id": identity,
        "kind": "passage",
        "origin": origin,
        "text": f"Original {identity}.",
        "time_evidence": [],
        "citations": [
            {
                "source_id": "conversation",
                "source_version": version,
                "message_index": index,
                "role": role,
                "passage_index": 0,
            }
        ],
    }


def fact(identity, passage):
    return {
        "unit_id": identity,
        "kind": "fact",
        "origin": "memory",
        "text": f"Claim {identity}.",
        "time_evidence": [],
        "citations": [
            {
                "source_id": passage["citations"][0]["source_id"],
                "source_version": passage["citations"][0]["source_version"],
                "passage_id": passage["unit_id"],
                "span": {**passage["citations"][0], "quote": passage["text"]},
            }
        ],
    }


@pytest.mark.parametrize(
    ("before_text", "after_text", "cap", "expected_before", "expected_after"),
    [
        (
            "Discard this longer sentence. Tail.",
            "Head. Another longer sentence.",
            8,
            "Tail.",
            "Head.",
        ),
        ("很长的前面内容。尾句。", "首句。后面内容很长。", 4, "尾句。", "首句。"),
        ("discard discard. 🙂🙂。", "🙂🙂。 discard discard.", 3, "🙂🙂。", "🙂🙂。"),
    ],
    ids=["ascii", "cjk", "emoji"],
)
def test_adjacent_excerpts_keep_the_boundary_facing_text_within_the_cap(
    before_text, after_text, cap, expected_before, expected_after
):
    support = original("support", 4)
    root = fact("root", support)
    before, after = original("before", 2), original("after", 6)
    before["text"], after["text"] = before_text, after_text
    originals = {row["unit_id"]: row for row in (before, support, after)}
    locators = [
        {
            "unit_id": row["unit_id"],
            "passage_id": row["unit_id"],
            "source_id": "conversation",
            "source_version": 1,
            "locator": row["citations"][0],
        }
        for row in originals.values()
    ]
    excerpts = context.source_window_excerpts(root, locators, originals, max_chars=cap)
    assert [row["unit_id"] for row in excerpts] == ["before", "after"]
    assert [row["text"] for row in excerpts] == [expected_before, expected_after]
    for row in excerpts:
        full = originals[row["unit_id"]]
        span = row["excerpt"]
        assert row["text"] == full["text"][span["start"] : span["end"]]
        assert span["original_length"] == len(full["text"])
        assert row["citations"] == full["citations"]
        assert len(row["text"]) <= cap
        assert "excerpt" not in full


@pytest.mark.parametrize(
    ("text", "cap", "previous", "starts_message", "expected"),
    [
        ("First. More, details continue without ending", 16, False, True, "First."),
        ("Long opening, more words. Last.", 15, True, True, "Last."),
        ("Alpha, beta gamma without a terminator", 8, False, True, "Alpha,"),
        ("An oversized opening, Tail; unfinished", 8, True, True, "Tail;"),
        ("第一句。第二部分，后面尚未结束", 9, False, True, "第一句。"),
        ("Keep; More, trailing without an ending", 16, False, True, "Keep;"),
        ("第一部分，后面的内容很长且没有结束", 6, False, True, "第一部分，"),
        ("مرحبا؟ this continues without ending", 12, False, True, "مرحبا؟"),
        ("नमस्ते। this continues without ending", 12, False, True, "नमस्ते।"),
        ("“你好！” 后面的内容尚未结束", 5, False, True, "“你好！”"),
        ('Really?!" this continues without ending', 9, False, True, 'Really?!"'),
        ("fragment. Complete. unfinished", 100, True, False, "Complete."),
        ("fragment. Complete. unfinished", 100, False, False, "Complete."),
        ("fragment. unfinished", 100, True, False, None),
        ("No punctuation anywhere", 100, False, True, None),
        ("Dr. Lee left. trailing unfinished", 5, False, True, None),
        ("The U.S. team left. trailing unfinished", 10, False, True, None),
        ("A. Lee left. trailing unfinished", 3, False, True, None),
        ("Cost 3.14 units. trailing unfinished", 9, False, True, None),
        ("价格３．１４元。 其他内容尚未结束", 4, False, True, None),
        ("时间１２：３０，其他内容尚未结束", 5, False, True, None),
        ("Ｄｒ． Lee left. trailing unfinished", 4, False, True, None),
        ("See https://example.com for details", 10, False, True, None),
        ("Cost 1,000 units without an ending", 10, False, True, None),
    ],
)
def test_excerpt_span_uses_sentence_priority_then_clauses_without_raw_cuts(
    text, cap, previous, starts_message, expected
):
    span = context.excerpt_span(
        text, max_chars=cap, previous=previous, starts_message=starts_message
    )
    assert (text[span[0] : span[1]] if span else None) == expected
    if span:
        assert span[1] - span[0] <= cap


@pytest.mark.parametrize("mark", list(".!?。！？؟।॥"))
def test_unicode_sentence_terminators_are_kept(mark):
    text = f"Keep{mark} trailing text without an ending"
    span = context.excerpt_span(text, max_chars=6, previous=False, starts_message=True)
    assert text[span[0] : span[1]] == f"Keep{mark}"


@pytest.mark.parametrize("mark", list(",，;；:：、،؛"))
def test_unicode_clause_punctuation_is_used_when_no_sentence_fits(mark):
    text = f"Lead{mark} trailing text without an ending"
    span = context.excerpt_span(text, max_chars=6, previous=False, starts_message=True)
    assert text[span[0] : span[1]] == f"Lead{mark}"


def test_exact_thousand_character_sentence_is_not_cut():
    text = "a" * 999 + "." + " trailing unfinished"
    assert context.excerpt_span(text, max_chars=1000, previous=False, starts_message=True) == (
        0,
        1000,
    )


def test_short_neighbor_remains_complete_and_is_not_labelled_as_an_excerpt():
    support, neighbor = original("support", 4), original("neighbor", 2)
    rows = [support, neighbor]
    locators = [
        {
            "unit_id": row["unit_id"],
            "passage_id": row["unit_id"],
            "source_id": "conversation",
            "source_version": 1,
            "locator": row["citations"][0],
        }
        for row in rows
    ]
    assert context.source_window_excerpts(
        fact("root", support), locators, {row["unit_id"]: row for row in rows}, max_chars=1000
    ) == [neighbor]


def test_source_window_cannot_bypass_omission_when_no_boundary_fits():
    support = original("support", 4)
    root = fact("root", support)
    neighbor = original("neighbor", 2)
    neighbor["text"] = "unfinished words without punctuation " * 100

    class RankedRepository(SourceRepository):
        def lexical(self, query, count):
            return [{"unit_id": "root"}]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, {})

    values = CONFIG.model_dump(mode="json")
    values["search"]["fast"].update(
        adjacent_user_context=True, adjacent_user_context_max_chars=1000
    )
    result = RankedService(
        [root, support, neighbor], config=type(CONFIG).model_validate(values)
    ).search(object(), SearchRequest(query="history", recall_level="fast"))
    assert {row["unit_id"] for row in result["items"]} == {"root", "support"}


def test_omitted_fact_does_not_make_a_rejected_neighbor_an_independent_original():
    support, neighbor = original("support", 4), original("neighbor", 2)
    neighbor["text"] = "unfinished words without punctuation " * 100
    root, oversized = fact("root", support), fact("oversized", neighbor)
    oversized["text"] = "A large learned record. " * 1000

    class RankedRepository(SourceRepository):
        def lexical(self, query, count):
            return [{"unit_id": "root"}, {"unit_id": "oversized"}]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, {})

    values = CONFIG.model_dump(mode="json")
    values["search"]["fast"].update(
        adjacent_user_context=True, adjacent_user_context_max_chars=1000, context_max_tokens=1500
    )
    result = RankedService(
        [root, oversized, support, neighbor], config=type(CONFIG).model_validate(values)
    ).search(object(), SearchRequest(query="history", recall_level="fast"))
    assert {row["unit_id"] for row in result["items"]} == {"root", "support"}


def test_source_window_does_not_treat_a_cited_passage_as_optional_when_ids_differ():
    support = original("support-unit", 4)
    root = fact("root", support)
    root["citations"][0]["passage_id"] = "source-passage"
    locators = [
        {
            "unit_id": "support-unit",
            "passage_id": "source-passage",
            "source_id": "conversation",
            "source_version": 1,
            "locator": support["citations"][0],
        }
    ]
    assert (
        context.source_window_excerpts(root, locators, {"support-unit": support}, max_chars=1) == []
    )


@pytest.mark.parametrize("required_support", [False, True])
def test_later_admitted_original_promotes_optional_excerpt_to_complete_text(required_support):
    support, neighbor = original("support", 4), original("neighbor", 2)
    neighbor["text"] = "Complete neighboring source. " * 50
    root, later = fact("root", support), fact("later", neighbor)
    excerpt = {
        **neighbor,
        "text": neighbor["text"][-32:],
        "excerpt": {
            "start": len(neighbor["text"]) - 32,
            "end": len(neighbor["text"]),
            "original_length": len(neighbor["text"]),
        },
    }
    items, _, admitted, _ = assemble_context(
        [root, later if required_support else neighbor],
        [],
        context_max_tokens=10000,
        include_chunks=True,
        max_results=10,
        evidence_groups=[[root, support], [later, neighbor]],
        optional_context={"root": [excerpt]},
    )
    selected = {row["unit_id"]: row for row in items}
    assert selected["neighbor"]["text"] == neighbor["text"]
    assert "excerpt" not in selected["neighbor"]
    assert context.bundle_items([*admitted, [excerpt]]) == items


def test_optional_excerpt_cannot_stand_in_for_required_support_that_does_not_fit():
    support, neighbor = original("support", 4), original("neighbor", 2)
    neighbor["text"] = "Large source. " * 10000
    root, later = fact("root", support), fact("later", neighbor)
    excerpt = {
        **neighbor,
        "text": "Large source.",
        "excerpt": {"start": 0, "end": 13, "original_length": len(neighbor["text"])},
    }
    items, omissions, _, _ = assemble_context(
        [root, later],
        [],
        context_max_tokens=1000,
        include_chunks=True,
        max_results=10,
        evidence_groups=[[root, support], [later, neighbor]],
        optional_context={"root": [excerpt]},
    )
    assert {row["unit_id"] for row in items} == {"root", "support", "neighbor"}
    assert "later" in omissions
    assert context_token_count(render_context(items)) <= 1000


def test_excerpt_metadata_is_visible_in_both_renderings_and_recorded_replay(monkeypatch):
    import json

    from oam.debug.traces import TraceRepository

    row = {
        **original("neighbor", 2),
        "text": "Boundary text.",
        "excerpt": {"start": 40, "end": 54, "original_length": 100},
    }
    assert "excerpt=40:54/100" in context.render_item(row)
    assert "excerpt=40:54/100" in context._render_compact_item(row, {"neighbor": "1"}, {}, None)
    assert "native_truncated" not in render_context([row])
    result = {
        "items": [row],
        "context": render_context([row]),
        "context_tokens": context_token_count(render_context([row])),
        "tokenizer": context.CONTEXT_TOKENIZER,
    }
    recorded = json.loads(json.dumps({"status": "recorded", "result": result}))
    monkeypatch.setattr(TraceRepository, "get", lambda *args: recorded)
    assert TraceRepository().replay(None, None, "trace", "reassemble_recorded")["status"] == "exact"


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize(
    ("limit", "before_text", "after_text"),
    [
        (10, "Previous.", "Next."),
        (20, "Previous. Previous.", "Next. Next. Next."),
    ],
)
def test_configured_neighbor_cap_reaches_the_actual_recall_context(
    mode, limit, before_text, after_text
):
    support = original("support", 4)
    support["text"] = "Required source stays complete. " * 100
    root = fact("root", support)
    before, after = original("before", 2), original("after", 6)
    before["text"], after["text"] = "Previous. " * 200, "Next. " * 200

    class RankedRepository(SourceRepository):
        def lexical(self, query, count):
            return [{"unit_id": "root"}]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, {})

    values = CONFIG.model_dump(mode="json")
    values["search"][mode].update(
        partial_match_gate="off",
        adjacent_user_context=True,
        adjacent_user_context_max_chars=limit,
    )
    config = type(CONFIG).model_validate(values)
    result = RankedService([root, support, before, after], config=config).search(
        object(), SearchRequest(query="history", recall_level=mode)
    )
    selected = {row["unit_id"]: row for row in result["items"]}
    assert selected["support"]["text"] == support["text"]
    assert "excerpt" not in selected["support"]
    assert selected["before"]["text"] == before_text
    assert selected["after"]["text"] == after_text
    for identity in ("before", "after"):
        assert len(selected[identity]["text"]) <= limit
        assert "excerpt" in selected[identity]
    assert result["diagnostics"]["local_context"]["adjacent_user_context_max_chars"] == limit


def test_partial_matches_require_the_actual_user_fact_exchange():
    user = original("request", 4)
    witness = fact("seed", user)
    answer = original("answer", 5, "assistant")
    candidates = [
        user,
        answer,
        fact("answer_claim", answer),
        original("later_user", 6),
        original("later_answer", 7, "assistant"),
        original("old_answer", 5, "assistant", version=2),
    ]
    assert local_evidence_ids(
        candidates,
        [witness],
        set(),
        reply_fact_ids={"seed"},
        reply_parent_keys={("conversation", 1, "request")},
    ) == {
        "request",
        "answer",
        "answer_claim",
    }


def test_direct_hits_resources_and_insights_do_not_require_a_local_witness():
    candidates = [
        original("semantic", 1, "assistant"),
        original("strict_lexical", 2),
        original("resource", 4, origin="evidence"),
        {"unit_id": "insight", "kind": "insight", "origin": "memory", "citations": []},
        original("partial", 6),
    ]
    assert local_evidence_ids(
        candidates,
        [],
        {"semantic", "strict_lexical"},
        reply_fact_ids=set(),
        reply_parent_keys=set(),
    ) == {
        "semantic",
        "strict_lexical",
        "resource",
        "insight",
    }


def test_originals_and_assistant_facts_cannot_propagate_a_user_fact_witness():
    user = original("request", 4)
    answer = original("answer", 5, "assistant")
    assert (
        local_evidence_ids(
            [user, answer],
            [user, fact("assistant_claim", answer)],
            set(),
            reply_fact_ids={"assistant_claim"},
            reply_parent_keys=set(),
        )
        == set()
    )


def test_invalid_message_coordinates_do_not_create_a_local_witness():
    user = original("request", 4)
    witness = fact("seed", user)
    malformed = deepcopy(witness)
    malformed["citations"][0]["span"]["message_index"] = True
    assert (
        local_evidence_ids(
            [user], [malformed], set(), reply_fact_ids={"seed"}, reply_parent_keys=set()
        )
        == set()
    )


def test_weak_dense_facts_do_not_authorize_indirect_assistant_replies():
    strong_user = original("strong_user", 4)
    weak_user = original("weak_user", 8)
    strong, weak = fact("strong", strong_user), fact("weak", weak_user)
    candidates = [
        original("strong_reply", 5, "assistant"),
        original("weak_reply", 9, "assistant"),
        weak_user,
    ]
    assert local_evidence_ids(
        candidates,
        [strong, weak],
        set(),
        reply_fact_ids={"strong"},
        reply_parent_keys={("conversation", 1, "strong_user"), ("conversation", 1, "weak_user")},
    ) == {
        "strong_reply",
        "weak_user",
    }


def test_each_partial_reply_needs_its_own_retrieved_parent_original():
    first, second = original("first", 4), original("second", 8)
    witness = fact("seed", first)
    witness["citations"].extend(fact("other", second)["citations"])
    candidates = [
        first,
        second,
        original("first_reply", 5, "assistant"),
        original("second_reply", 9, "assistant"),
    ]
    assert local_evidence_ids(
        candidates,
        [witness],
        set(),
        reply_fact_ids={"seed"},
        reply_parent_keys={("conversation", 1, "first")},
    ) == {"first", "second", "first_reply"}


def test_local_context_follows_its_fact_without_displacing_a_higher_ranked_original():
    higher = original("higher", 0)
    support = original("support", 4)
    root = fact("root", support)
    neighbor = original("neighbor", 2)
    lower = original("lower", 8)
    items, _, _, _ = assemble_context(
        [higher, root, lower],
        [neighbor],
        context_max_tokens=10000,
        include_chunks=True,
        max_results=3,
        evidence_groups=[[root, support]],
        optional_context={"root": [neighbor]},
    )
    assert [item["unit_id"] for item in items] == ["higher", "root", "support", "neighbor"]


def test_oversized_optional_context_never_rejects_or_truncates_required_support():
    support = original("support", 4)
    root = fact("root", support)
    neighbor = original("neighbor", 2)
    neighbor["text"] = "Long unrelated neighbor. " * 10000
    cap = context_token_count(render_context([root, support])) + 10
    items, _, _, oversized = assemble_context(
        [root],
        [neighbor],
        context_max_tokens=cap,
        include_chunks=True,
        max_results=3,
        evidence_groups=[[root, support]],
        optional_context={"root": [neighbor]},
    )
    assert [item["unit_id"] for item in items] == ["root", "support"]
    assert support["text"] in render_context(items)
    assert context_token_count(render_context(items)) <= cap
    assert oversized is False


def test_configured_gate_reaches_the_actual_fast_context():
    support = original("support", 4)
    root = fact("root", support)
    answer = original("answer", 5, "assistant")
    later = original("later", 6)
    strict = original("strict", 20)
    independent = original("independent", 10)

    class LocalRepository(SourceRepository):
        def dense(self, vector, limit):
            return [
                {"unit_id": "root", "kind": "fact", "score": 0.8},
                {
                    "unit_id": "support",
                    "kind": "passage",
                    "score": 0.75,
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": "support",
                },
                {
                    "unit_id": "independent",
                    "kind": "passage",
                    "score": 0.7,
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": "independent",
                },
            ]

        def lexical(self, query, limit):
            return [
                {"unit_id": "answer", "strict_match": False},
                {"unit_id": "later", "strict_match": False},
                {"unit_id": "strict", "strict_match": True},
            ]

    class LocalService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield LocalRepository(request, self.rows, self.graph_paths)

    values = CONFIG.model_dump(mode="json")
    values["search"]["fast"].update(
        partial_match_gate="user_fact_exchange", adjacent_user_context=False
    )
    config = type(CONFIG).model_validate(values)
    result = LocalService(
        [support, root, answer, later, strict, independent], config=config
    ).search(object(), SearchRequest(query="history", recall_level="fast"))
    ids = {item["unit_id"] for item in result["items"]}
    assert {"root", "support", "answer", "strict", "independent"} <= ids
    assert "later" not in ids
    assert result["diagnostics"]["generative_calls"] == 0


def test_unavailable_dense_retrieval_preserves_degraded_lexical_recall():
    item = original("partial", 4)

    class PartialRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "partial", "strict_match": False}]

    class PartialService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield PartialRepository(request, self.rows, self.graph_paths)

    class FailedEmbedding:
        def embed(self, *args, **kwargs):
            raise RuntimeError("embedding unavailable")

    tier = CONFIG.search.fast.model_copy(update={"partial_match_gate": "user_fact_exchange"})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": tier})})
    service = PartialService([item], config=config)
    service.models = FailedEmbedding()
    result = service.search(
        object(), SearchRequest(query="history", recall_level="fast", allow_partial=True)
    )
    assert [row["unit_id"] for row in result["items"]] == ["partial"]
    assert result["diagnostics"]["degraded"] == ["embedding"]
    assert result["diagnostics"]["local_context"]["gate_status"] == "dense_unavailable"


@pytest.mark.parametrize(
    ("mode", "query", "earlier_text"),
    [
        ("fast", "Which workshops interest me?", "I want a workshop on wood carving."),
        ("normal", "Suggest activities for my trip.", "I would like a guided kayak trip."),
    ],
)
def test_bounded_user_recovery_uses_independent_fact_sources_without_propagating(
    mode, query, earlier_text
):
    support = original("support", 8)
    root = fact("root", support)
    earlier = original("earlier", 0)
    earlier["text"] = earlier_text
    bridge = original("bridge", 6)
    bulky = original("bulky", 2, "assistant")
    bulky["citations"][0]["source_id"] = "separate_source"
    bulky["text"] = "An independently retrieved long assistant discussion. " * 100
    reply = original("recovered_reply", 1, "assistant")
    weak_user = original("weak_user", 0)
    weak_user["citations"][0]["source_id"] = "weak_source"
    weak = fact("weak", weak_user)
    assistant = original("assistant", 1, "assistant")
    assistant["citations"][0]["source_id"] = "assistant_source"
    assistant_fact = fact("assistant_fact", assistant)
    stale_user = original("stale_user", 0, version=2)
    stale = fact("stale", stale_user)

    class RecoveryRepository(SourceRepository):
        def dense(self, vector, limit):
            return [
                {"unit_id": "root", "kind": "fact", "score": 0.8},
                {
                    "unit_id": "bulky",
                    "kind": "passage",
                    "score": 0.7,
                    "source_id": "separate_source",
                    "source_version": 1,
                    "passage_id": "bulky",
                },
            ]

        def lexical(self, query, limit):
            return [
                {"unit_id": node, "strict_match": False}
                for node in ("weak", "assistant_fact", "stale", "recovered_reply")
            ]

        def source_user_passage_ids(self, source_keys, vector, limit, excluded_ids):
            assert source_keys == (("conversation", 1),)
            assert limit == getattr(CONFIG.search, mode).source_seed_results
            assert "earlier" not in excluded_ids
            return [{"unit_id": "earlier"}]

    class RecoveryService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RecoveryRepository(request, self.rows, {})

    values = CONFIG.model_dump(mode="json")
    values["search"][mode].update(
        partial_match_gate="user_fact_exchange",
        adjacent_user_context=False,
        context_max_tokens=context_token_count(render_context([root, support, bulky])),
    )
    result = RecoveryService(
        [
            root,
            support,
            earlier,
            bridge,
            bulky,
            reply,
            weak,
            weak_user,
            assistant_fact,
            assistant,
            stale,
            stale_user,
        ],
        config=type(CONFIG).model_validate(values),
    ).search(object(), SearchRequest(query=query, recall_level=mode))
    selected = {row["unit_id"] for row in result["items"]}
    assert {"root", "support", "earlier"} <= selected
    assert earlier_text in result["context"]
    assert "bulky" not in selected
    assert (
        not {
            "recovered_reply",
            "weak",
            "weak_user",
            "assistant_fact",
            "assistant",
            "stale",
            "stale_user",
        }
        & selected
    )
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["context_tokens"] <= getattr(CONFIG.search, mode).context_max_tokens
    assert result["diagnostics"]["local_context"]["window_originals"] == 0
    assert result["diagnostics"]["local_context"]["selected_recovered_user_originals"] == 1
