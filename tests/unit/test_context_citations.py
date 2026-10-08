"""Answer-visible citations retain chat provenance without verbose JSON."""

from oam.retrieval.context import render_item


def test_chat_fact_citations_keep_distinct_messages_and_exact_quotes():
    item = {
        "unit_id": "fact",
        "kind": "fact",
        "origin": "memory",
        "text": "The user stores their shoes under their bed.",
        "citations": [
            {
                "source_id": "source",
                "source_version": 2,
                "passage_id": f"passage-{message}",
                "evidence_id": f"evidence-{message}",
                "span": {
                    "message_id": message,
                    "message_index": index,
                    "role": "user",
                    "passage_index": 0,
                    "quote": "under my bed\n床下",
                },
            }
            for index, message in enumerate(("first", "second"))
        ],
        "time_evidence": [
            {"role": "mentioned", "at": "2023-08-11T02:38:00+00:00"},
            {"role": "event", "range": "[2023-08-11,2023-08-12)", "detail": "overnight"},
        ],
    }

    rendered = render_item(item)

    assert "source@2 user message:first" in rendered
    assert "source@2 user message:second" in rendered
    assert rendered.count(repr("under my bed\n床下")) == 2
    assert "mentioned at=2023-08-11T02:38:00+00:00" in rendered
    assert "event range=[2023-08-11,2023-08-12) detail=overnight" in rendered
    assert "evidence_id" not in rendered


def test_chat_passage_and_unknown_citation_formats():
    item = {
        "unit_id": "passage",
        "kind": "passage",
        "origin": "evidence",
        "text": "Original user words.",
        "citations": [
            {
                "source_id": "source",
                "source_version": 3,
                "message_id": "turn-5",
                "message_index": 5,
                "role": "user",
                "passage_index": 0,
            }
        ],
        "time_evidence": [],
    }
    rendered = render_item(item)
    assert "Source: source@3 user message:turn-5" in rendered
    assert "Time:" not in rendered

    item["citations"] = [{"resource_id": "document", "page": 4, "region": [1, 2]}]
    rendered = render_item(item)
    assert 'Citations: [{"page":4,"region":[1,2],"resource_id":"document"}]' in rendered
