"""Local evidence selection preserves independent hits and complete support."""

from contextlib import contextmanager
from copy import deepcopy

from test_source_context import CONFIG, SourceRepository, SourceService

from oam.contracts.search import SearchRequest
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
