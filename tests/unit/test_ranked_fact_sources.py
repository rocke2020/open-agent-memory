"""Opt-in fact citations do not force otherwise unselected source bodies."""

from contextlib import contextmanager

import pytest
import test_source_context
from test_local_evidence import fact, original
from test_source_context import CONFIG, SourceRepository, SourceService

from oam.config import ConfigurationError, load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.context import assemble_context, context_token_count, render_context


@pytest.fixture
def evidence_rows():
    return test_source_context.evidence_rows.__wrapped__()


@pytest.mark.parametrize("independent", [False, True])
def test_ranked_fact_bodies_require_a_standalone_original_root(evidence_rows, independent):
    class Repository(SourceRepository):
        def lexical(self, query, limit):
            nodes = ["fact", "original"] if independent else ["fact"]
            return [{"unit_id": node} for node in nodes]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    fast = CONFIG.search.fast.model_copy(update={"fact_source_bodies": "ranked"})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = Service(evidence_rows, config=config).search(
        object(), SearchRequest(query="Which workshop did I miss?", recall_level="fast")
    )
    assert [row["unit_id"] for row in result["items"]] == (
        ["fact", "original"] if independent else ["fact"]
    )
    assert result["items"][0]["citations"] == evidence_rows[0]["citations"]
    assert result["context_tokens"] == context_token_count(result["context"])


@pytest.mark.parametrize("invalid", ["quote", "locator"])
def test_ranked_fact_sources_still_validate_complete_persisted_support(evidence_rows, invalid):
    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    span = evidence_rows[0]["citations"][0]["span"]
    if invalid == "quote":
        span["quote"] = "This text never occurred."
    else:
        span["message_id"] = "wrong-message"
    fast = CONFIG.search.fast.model_copy(update={"fact_source_bodies": "ranked"})
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={"fast": fast})})
    result = Service(evidence_rows, config=config).search(
        object(), SearchRequest(query="Which workshop did I miss?", recall_level="fast")
    )
    assert result["items"] == []
    assert result["diagnostics"]["context_budget"]["source_omitted_ids"] == ["fact"]


@pytest.mark.parametrize("degraded", [False, True])
def test_ranked_source_projection_matches_fast_scoring_and_degraded_output(evidence_rows, degraded):
    from test_fast_rerank import Scorer, scoring_config

    class Repository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "fact"}]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    evidence_rows[2]["text"] += " Unrelated background retained only in the complete original."
    values = scoring_config().model_dump(mode="json")
    values["search"]["fast"]["fact_source_bodies"] = "ranked"
    service = Service(evidence_rows, config=type(CONFIG).model_validate(values))
    scorer = Scorer([0.8], error=RuntimeError("unavailable") if degraded else None)
    service.models = scorer
    result = service.search(
        object(),
        SearchRequest(query="Which workshop did I miss?", recall_level="fast", allow_partial=True),
    )
    assert len(scorer.calls[0][1]) == 1
    assert "Unrelated background" not in scorer.calls[0][1][0]
    assert [row["unit_id"] for row in result["items"]] == ["fact"]
    assert "Unrelated background" not in result["context"]
    assert result["diagnostics"]["rerank"]["status"] == ("degraded" if degraded else "applied")


def pack(candidates, groups, *, sources=(), optional=None, chunks=True, limit=18000):
    return assemble_context(
        candidates,
        list(sources),
        context_max_tokens=limit,
        include_chunks=chunks,
        max_results=10,
        evidence_groups=groups,
        optional_context=optional,
        fact_source_bodies="ranked",
    )


def test_ranked_bodies_preserve_complete_late_original_and_omit_expansion():
    owner, neighbor, detail = original("owner", 0), original("neighbor", 2), original("detail", 4)
    owner["text"] = "The order was placed. It has not arrived yet."
    claim = fact("claim", owner)
    claim["citations"][0]["span"]["quote"] = "The order was placed."
    rows, _, _, _ = pack(
        [claim, owner], [[claim, owner]], sources=[detail], optional={"claim": [neighbor]}
    )
    assert [row["unit_id"] for row in rows] == ["claim", "owner"]
    assert rows[1] == owner
    assert "It has not arrived yet." in render_context(rows)


@pytest.mark.parametrize("same_source", [True, False])
def test_ranked_projection_keeps_full_support_identity_for_fact_dedup(same_source):
    first, second = original("first", 0), original("second", 0)
    second["text"] = first["text"]
    if not same_source:
        second["citations"][0]["source_id"] = "another-conversation"
    a, b = fact("a", first), fact("b", second)
    b["text"] = a["text"]
    rows, _, _, _ = pack([a, b], [[a, first], [b, second]])
    assert [row["unit_id"] for row in rows] == (["a"] if same_source else ["a", "b"])


def test_ranked_fact_policy_keeps_complete_insight_group():
    owner = original("owner", 0)
    support = fact("support", owner)
    insight = {**support, "unit_id": "insight", "kind": "insight", "text": "A supported insight."}
    rows, _, _, _ = pack([insight], [[insight, support, owner]])
    assert [row["unit_id"] for row in rows] == ["insight", "support", "owner"]


def test_ranked_fact_policy_keeps_resource_and_respects_exact_budget():
    resource = {**original("resource", 0), "kind": "resource_passage", "origin": "resource"}
    owner = original("owner", 2)
    claim = fact("claim", owner)
    limit = context_token_count(render_context([claim]))
    rows, omissions, _, _ = pack([claim, resource], [[claim, owner]], limit=limit)
    assert [row["unit_id"] for row in rows] == ["claim"]
    assert omissions == ["resource"]
    assert context_token_count(render_context(rows)) <= limit
    assert pack([resource], [])[0] == [resource]


def test_ranked_fact_policy_does_not_admit_unvalidated_fact_or_chunks_when_disabled():
    owner = original("owner", 0)
    claim = fact("claim", owner)
    assert pack([claim], [])[0] == []
    rows, _, _, _ = pack([claim, owner], [[claim, owner]], chunks=False)
    assert rows == [claim]


def test_ranked_fact_policy_is_explicit_config_and_rejects_unknown_values(tmp_path):
    override = tmp_path / "ranked.yaml"
    override.write_text("search:\n  fast:\n    fact_source_bodies: ranked\n")
    assert load_config(override).config.search.fast.fact_source_bodies == "ranked"
    override.write_text("search:\n  fast:\n    fact_source_bodies: invalid\n")
    with pytest.raises(ConfigurationError):
        load_config(override)
