"""Source-backed deduplication retains qualifications and the source's relevance slot."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
import test_source_context
from test_local_evidence import fact, original
from test_source_context import CONFIG, SourceRepository, SourceService

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.context import (
    assemble_context,
    bundle_items,
    context_token_count,
    render_context,
)


def supported_pair():
    owner = original("owner", 0)
    owner["citations"][0]["message_id"] = "0"
    return fact("claim", owner), owner


def pack(candidates, groups, *, budget=18000, mode="ranked_deduplicated", max_results=10):
    return assemble_context(
        candidates,
        [],
        context_max_tokens=budget,
        include_chunks=True,
        max_results=max_results,
        evidence_groups=groups,
        fact_source_bodies=mode,
    )


def test_complete_source_replaces_unqualified_fact_at_its_existing_rendered_position():
    owner = original("owner", 0)
    owner["citations"][0]["message_id"] = "0"
    owner["text"] = "I enjoyed those shows, but now want other genres."
    claim = fact("claim", owner)
    claim["text"] = "The user enjoys those shows."
    claim["citations"][0]["span"]["quote"] = "I enjoyed those shows"
    other = original("other", 2)
    rows, _, _, _ = assemble_context(
        [claim, other, owner],
        [],
        context_max_tokens=18000,
        include_chunks=True,
        max_results=10,
        evidence_groups=[[claim, owner]],
        fact_source_bodies="ranked_deduplicated",
    )
    assert [row["unit_id"] for row in rows] == ["owner", "other"]
    assert rows[0] == owner
    assert "but now want other genres" in render_context(rows)
    assert "The user enjoys those shows." not in render_context(rows)


@pytest.mark.parametrize(
    "qualification",
    [
        "event_time",
        "alias",
        "excerpt",
        "native_truncated",
        "wrong_quote",
        "ambiguous_quote",
        "locator",
        "multiple_owners",
    ],
)
def test_dedup_preserves_fact_when_source_cannot_replace_its_information(qualification):
    claim, owner = supported_pair()
    if qualification == "event_time":
        claim["time_evidence"] = [{"role": "event", "at": "2025-04-01T00:00:00Z"}]
    elif qualification == "alias":
        claim["paths"] = [
            {
                "alias_witnesses": [
                    {
                        "support_id": "alias",
                        "quote": "Her nickname",
                        "citation": deepcopy(claim["citations"][0]),
                    }
                ]
            }
        ]
    elif qualification == "excerpt":
        owner["excerpt"] = {"start": 0, "end": len(owner["text"]), "original_length": 100}
    elif qualification == "native_truncated":
        owner["truncated"] = True
    elif qualification == "wrong_quote":
        claim["citations"][0]["span"]["quote"] = "This never occurred."
    elif qualification == "ambiguous_quote":
        owner["text"] *= 2
    elif qualification == "locator":
        claim["citations"][0]["span"]["message_id"] = "other-message"
    else:
        other = original("other", 2)
        claim["citations"].extend(fact("other-claim", other)["citations"])
    rows = pack([claim, owner], [[claim, owner]])[0]
    assert [row["unit_id"] for row in rows] == ["claim", "owner"]
    assert rows[0] == claim
    assert rows[1] == owner


def test_dedup_keeps_fact_without_selected_owner_and_keeps_insight_support():
    claim, owner = supported_pair()
    assert pack([claim], [[claim, owner]])[0] == [claim]
    insight = {**claim, "unit_id": "insight", "kind": "insight"}
    assert pack([insight], [[insight, claim, owner]])[0] == [insight, claim, owner]


def test_dedup_uses_exact_tokens_without_displacing_complete_source():
    claim, owner = supported_pair()
    owner["text"] += " Complete qualifying detail." * 20
    limit = context_token_count(render_context([owner]))
    rows, omissions, _, _ = pack([claim, owner], [[claim, owner]], budget=limit)
    assert rows == [owner]
    assert omissions == []
    assert context_token_count(render_context(rows)) == limit


def test_dedup_does_not_reclaim_pool_slots_reserved_by_fact_roots():
    first, owner = supported_pair()
    second = deepcopy(first)
    second.update(unit_id="second", text="Another supported summary.")
    rows, omissions, _, _ = pack(
        [owner, first, second], [[first, owner], [second, owner]], max_results=1
    )
    assert rows == [owner]
    assert omissions == ["second"]


def test_fact_returns_when_its_source_owning_insight_is_removed_before_return():
    claim, owner = supported_pair()
    support = deepcopy(claim)
    support.update(unit_id="support", text="The Insight's required support.")
    insight = {**claim, "unit_id": "insight", "kind": "insight"}
    roots = [[insight, support, owner], [claim]]
    assert [
        r["unit_id"] for r in bundle_items(roots, fact_source_bodies="ranked_deduplicated")
    ] == ["insight", "support", "owner"]
    assert bundle_items(roots[1:], fact_source_bodies="ranked_deduplicated") == [claim]


@pytest.mark.parametrize("mode", ["required", "ranked"])
def test_existing_fact_policies_keep_the_derived_fact(mode):
    claim, owner = supported_pair()
    assert pack([claim, owner], [[claim, owner]], mode=mode)[0] == [claim, owner]


@pytest.mark.parametrize(
    "recall_level,chunks,expected",
    [
        ("fast", True, ["original"]),
        ("fast", False, ["fact"]),
        ("normal", True, ["fact", "original", "second_event"]),
        ("normal", False, ["fact"]),
    ],
)
def test_source_dedup_survives_final_rebuild_only_for_enabled_fast_chunks(
    recall_level, chunks, expected
):
    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}, {"unit_id": "original"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    rows = test_source_context.evidence_rows.__wrapped__()
    values = CONFIG.model_dump(mode="json")
    values["search"]["fast"]["fact_source_bodies"] = "ranked_deduplicated"
    result = Service(rows, config=type(CONFIG).model_validate(values)).search(
        object(),
        SearchRequest(
            query="Which workshop did I miss?",
            recall_level=recall_level,
            include={"chunks": chunks},
        ),
    )
    assert [r["unit_id"] for r in result["items"]] == expected
    assert result["context_tokens"] == context_token_count(result["context"])
    if recall_level == "fast" and chunks:
        assert result["diagnostics"]["context_budget"]["source_deduplicated_fact_ids"] == ["fact"]


def test_dedup_config_is_resolved_from_operator_yaml(tmp_path):
    path = tmp_path / "source-dedup.yaml"
    path.write_text("search:\n  fast:\n    fact_source_bodies: ranked_deduplicated\n")
    assert load_config(path).config.search.fast.fact_source_bodies == "ranked_deduplicated"
