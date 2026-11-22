"""Assistant FACT redundancy never changes required evidence admission."""

from copy import deepcopy

import pytest
from test_local_evidence import fact, original

from oam.retrieval.context import bundle_items, context_token_count, render_context


def pair(identity="owner", role="assistant"):
    owner = original(identity, 0, role)
    owner["citations"][0]["message_id"] = identity
    return fact("claim-" + identity, owner), owner


def test_redundant_assistant_fact_is_removed_with_all_originals_and_user_fact_intact():
    assistant, owner = pair()
    user, user_owner = pair("user-owner", "user")
    roots = [[assistant, owner], [user, user_owner]]
    before = deepcopy(roots)
    result = bundle_items(roots, fact_source_bodies="assistant_deduplicated")
    assert [r["unit_id"] for r in result] == ["owner", "claim-user-owner", "user-owner"]
    assert result == [owner, user, user_owner]
    assert roots == before
    assert owner["text"] in render_context(result)
    assert assistant["text"] not in render_context(result)
    assert context_token_count(render_context(result)) < context_token_count(
        render_context(bundle_items(roots))
    )


@pytest.mark.parametrize("role", ["user", None, "unknown"])
def test_user_and_unknown_supported_facts_are_never_deduplicated(role):
    claim, owner = pair(role=role)
    assert bundle_items([[claim, owner]], fact_source_bodies="assistant_deduplicated") == [
        claim,
        owner,
    ]


@pytest.mark.parametrize(
    "guard",
    [
        "truncated_fact",
        "truncated_owner",
        "excerpt",
        "time",
        "alias",
        "wrong_quote",
        "ambiguous_quote",
        "locator",
        "no_citations",
        "mixed_roles",
        "multiple_owners",
    ],
)
def test_qualified_assistant_fact_is_not_lost(guard):
    claim, owner = pair()
    roots = [[claim, owner]]
    if guard == "truncated_fact":
        claim["truncated"] = True
    elif guard == "truncated_owner":
        owner["truncated"] = True
    elif guard == "excerpt":
        owner["excerpt"] = {"start": 0, "end": len(owner["text"]), "original_length": 100}
    elif guard == "time":
        claim["time_evidence"] = [{"role": "event", "at": "2025-04-01T00:00:00Z"}]
    elif guard == "alias":
        claim["paths"] = [
            {
                "alias_witnesses": [
                    {
                        "support_id": "alias",
                        "quote": "a nickname",
                        "citation": deepcopy(claim["citations"][0]),
                    }
                ]
            }
        ]
    elif guard == "wrong_quote":
        claim["citations"][0]["span"]["quote"] = "Absent quotation."
    elif guard == "ambiguous_quote":
        owner["text"] *= 2
    elif guard == "locator":
        claim["citations"][0]["span"]["message_id"] = "different"
    elif guard == "no_citations":
        claim["citations"] = []
    elif guard == "mixed_roles":
        other = deepcopy(claim["citations"][0])
        other["span"]["role"] = "user"
        claim["citations"].append(other)
    else:
        other, other_owner = pair("other")
        claim["citations"].extend(other["citations"])
        roots[0].append(other_owner)
    result = bundle_items(roots, fact_source_bodies="assistant_deduplicated")
    assert result == roots[0]


def test_insight_required_fact_remains_but_independent_assistant_fact_can_be_deduplicated():
    claim, owner = pair()
    support, support_owner = pair("support-owner")
    insight = {**support, "unit_id": "insight", "kind": "insight"}
    roots = [[insight, support, support_owner], [claim, owner]]
    result = bundle_items(roots, fact_source_bodies="assistant_deduplicated")
    assert [r["unit_id"] for r in result] == [
        "insight",
        "claim-support-owner",
        "support-owner",
        "owner",
    ]
    assert result == [insight, support, support_owner, owner]


def service_fixture():
    from contextlib import contextmanager

    import test_source_context
    from test_source_context import CONFIG, SourceRepository, SourceService

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "original"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    rows = test_source_context.evidence_rows.__wrapped__()
    rows[0]["citations"][0]["span"]["role"] = "assistant"
    rows[1]["citations"][0]["span"]["role"] = "assistant"
    rows[2]["citations"][0]["role"] = "assistant"
    return Service, rows, CONFIG


@pytest.mark.parametrize(
    "level,chunks,want",
    [
        ("fast", True, ["original", "second_event"]),
        ("fast", False, ["fact"]),
        ("normal", True, ["fact", "original", "second_event"]),
        ("normal", False, ["fact"]),
    ],
)
def test_service_projection_preserves_required_source_tail_and_only_runs_for_fast_chunks(
    level, chunks, want
):
    from oam.contracts.search import SearchRequest

    Service, rows, config = service_fixture()
    values = config.model_dump(mode="json")
    values["search"]["fast"]["fact_source_bodies"] = "assistant_deduplicated"
    result = Service(rows, config=type(config).model_validate(values)).search(
        object(),
        SearchRequest(
            query="Which workshop did I miss?", recall_level=level, include={"chunks": chunks}
        ),
    )
    assert [r["unit_id"] for r in result["items"]] == want
    assert result["context_tokens"] == context_token_count(result["context"])
    if level == "fast" and chunks:
        assert rows[2]["text"] in result["context"]
        assert rows[3]["text"] in result["context"]
        assert result["diagnostics"]["context_budget"]["assistant_deduplicated_fact_ids"] == [
            "fact"
        ]


def test_operator_yaml_enables_assistant_fact_deduplication(tmp_path):
    from oam.config import load_config

    path = tmp_path / "assistant-facts.yaml"
    path.write_text("search:\n  fast:\n    fact_source_bodies: assistant_deduplicated\n")
    assert load_config(path).config.search.fast.fact_source_bodies == "assistant_deduplicated"


def test_projection_never_refills_required_budget_omissions():
    from contextlib import contextmanager

    from test_source_context import SourceRepository, SourceService

    from oam.contracts.search import SearchRequest

    _, rows, config = service_fixture()
    rows[0]["citations"][0]["span"]["quote"] = "Check the reservation."
    rows[2]["text"] = "Check the reservation. " + "Extra catalogue detail. " * 80
    leading = original("leading", 0)
    leading["citations"][0]["source_id"] = "another-source"
    leading["locator"] = deepcopy(leading["citations"][0])
    rows.extend([leading, fact("second-fact", leading)])

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": node} for node in ("leading", "fact", "second-fact")]

        def dense(self, vector, limit):
            return [{**self.rows[node], "score": 0.8} for node in ("fact", "second-fact")]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    for policy in ("required", "assistant_deduplicated"):
        values = config.model_dump(mode="json")
        values["search"]["fast"].update(
            context_max_tokens=300,
            fact_source_bodies=policy,
            source_seed_results=1,
            partial_match_gate="user_fact_exchange",
        )
        result = Service(rows, config=type(config).model_validate(values)).search(
            object(), SearchRequest(query="Which reservation?", recall_level="fast")
        )
        assert {r["unit_id"] for r in result["items"]} == {"leading", "second-fact"}
        assert (
            result["diagnostics"]["context_budget"]["omission_reasons"]["fact"]
            == "context_or_result_limit"
        )
        assert result["context_tokens"] <= 300


def test_final_insight_invalidation_preserves_independent_original_and_cleans_paths(monkeypatch):
    from test_l2_source_context import CONFIG, ORIGINAL, InsightRepository, InsightService, evidence

    from oam.contracts.search import SearchRequest

    old = InsightRepository.hydrate
    calls = 0

    def changed(self, unit_ids):
        nonlocal calls
        rows = old(self, unit_ids)
        if "insight" in unit_ids:
            calls += 1
            if calls > 1:
                return [r for r in rows if r["unit_id"] != "insight"]
        return rows

    monkeypatch.setattr(InsightRepository, "hydrate", changed)
    monkeypatch.setattr(
        InsightRepository, "lexical", lambda *args: [{"unit_id": "fact"}, {"unit_id": "insight"}]
    )
    rows = evidence()
    for r in rows:
        for c in r["citations"]:
            locator = c.get("span", c)
            locator.update(role="assistant", message_index=0)
    values = CONFIG.model_dump(mode="json")
    values["search"]["fast"]["fact_source_bodies"] = "assistant_deduplicated"
    result = InsightService(rows, config=type(CONFIG).model_validate(values)).search(
        object(), SearchRequest(query="batch implementation", recall_level="fast")
    )
    # FACT retains distinct time evidence; the independent complete source must survive.
    assert {r["unit_id"] for r in result["items"]} == {"fact", "code"}
    assert ORIGINAL in result["context"]
    assert all(not any(p["arm"] == "l2_source" for p in r["paths"]) for r in result["items"])
    assert (
        result["diagnostics"]["l2_source_expansion"][0]["reason"] == "insight_changed_before_return"
    )
