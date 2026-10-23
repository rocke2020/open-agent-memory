"""Distinct support keeps its relevance rank regardless of source or speaker."""

import pytest

from oam.retrieval.context import assemble_context


def passage(identity, source, text):
    return dict(
        unit_id=identity,
        kind="passage",
        origin="memory",
        text=text,
        citations=[
            dict(
                source_id=source,
                source_version=1,
                message_id=identity,
                message_index=0,
                role="user",
                passage_index=0,
            )
        ],
        time_evidence=[],
    )


def fact(identity, original, text):
    locator = original["citations"][0]
    return dict(
        unit_id=identity,
        kind="fact",
        origin="memory",
        text=text,
        citations=[
            dict(
                source_id=locator["source_id"],
                source_version=1,
                passage_id=original["unit_id"],
                span={**locator, "quote": original["text"]},
            )
        ],
        time_evidence=[],
    )


@pytest.mark.parametrize("budget,cap", [(560, 96), (10000, 2)])
def test_distinct_events_from_one_source_keep_both_originals(budget, cap):
    first = passage("event1", "A", "I attended the workshop on June 3. " * 30)
    second = passage("event2", "A", "I attended the workshop on June 10. " * 30)
    other = passage("unrelated", "B", "I bought a blue umbrella yesterday. " * 30)
    selected, _, _, _ = assemble_context(
        [first, second, other],
        [],
        context_max_tokens=budget,
        include_chunks=True,
        max_results=cap,
    )
    assert [row["unit_id"] for row in selected] == ["event1", "event2"]


def test_user_role_fact_cannot_displace_a_higher_ranked_original_answer():
    top = passage("top", "X", "A workshop is scheduled this month. " * 30)
    answer = passage("answer", "Y", "The workshop registration code is ZX42. " * 30)
    draft = passage(
        "draft_original",
        "Y",
        "Please edit this fictional draft: I enjoy general AI conferences. " * 20,
    )
    learned = fact("draft_fact", draft, "The fictional narrator enjoys general AI conferences.")
    selected, _, _, _ = assemble_context(
        [top, answer, learned, draft],
        [],
        evidence_groups=[[learned, draft]],
        context_max_tokens=649,
        include_chunks=True,
        max_results=96,
    )
    assert [row["unit_id"] for row in selected] == ["top", "answer"]
    assert "ZX42" in selected[1]["text"]


def test_distinct_event_facts_keep_rank_when_original_bodies_are_hidden():
    first = passage("event1", "A", "I attended the workshop on June 3. " * 30)
    second = passage("event2", "A", "I attended the workshop on June 10. " * 30)
    other = passage("unrelated", "B", "I bought a blue umbrella yesterday. " * 30)
    facts = [
        fact("event_fact1", first, "The user attended a workshop on June 3. " * 10),
        fact("event_fact2", second, "The user attended a workshop on June 10. " * 10),
        fact("unrelated_fact", other, "The user bought a blue umbrella yesterday. " * 10),
    ]
    selected, _, _, _ = assemble_context(
        facts,
        [],
        evidence_groups=[[facts[0], first], [facts[1], second], [facts[2], other]],
        context_max_tokens=771,
        include_chunks=False,
        max_results=96,
    )
    assert [row["unit_id"] for row in selected] == ["event_fact1", "event_fact2"]
