from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.governance.repair import repair_insights
from oam.indexing.extraction import InsightResult
from oam.models.runtime import EmbeddingResult, ModelResult, Usage


class MemoryConnection:
    def __init__(self, state=None, eligible=()):
        self.state = state or {}
        self.eligible = list(eligible)
        self.published = []
        self.direct_removed = set()
        self.history_revision = 0
        self.guarded = False

    def execute(self, query, parameters=()):
        assert self.guarded
        if "SELECT stage_state FROM operations" in query:
            return SimpleNamespace(fetchone=lambda: (self.state,))
        if "WITH eligible AS" in query:
            assert "kind='fact'" in query and "memory_id=ANY(%s)" in query
            assert "owner.state='active'" in query
            ids = parameters[0]
            rows = [row for row in self.eligible if row[1] in ids]
            return SimpleNamespace(
                fetchall=lambda: (
                    [(r[1],) for r in rows] if "SELECT memory_id FROM eligible" in query else rows
                )
            )
        if "removal_target_kind='insight'" in query:
            return SimpleNamespace(
                fetchone=lambda: (True,) if parameters[-1] in self.direct_removed else None
            )
        if "SELECT unit_id FROM eligible" in query:
            return SimpleNamespace(fetchall=lambda: [(row[0],) for row in self.eligible])
        if "SELECT COALESCE(MAX(revision)" in query:
            return SimpleNamespace(fetchone=lambda: (self.history_revision,))
        if query.startswith("UPDATE operations SET stage_state=stage_state||"):
            self.state.update(parameters[0].obj)
        if query.startswith("INSERT INTO insights") or query.startswith(
            "INSERT INTO insight_support"
        ):
            self.published.append((query, parameters))
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


class RepairStore:
    def __init__(self, state, eligible=(), *, direct_removed=()):
        self.key = ("tenant", "user")
        self.claim = SimpleNamespace(operation_id="repair-op")
        self.config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
        self.source = {"accepted_seq": 50, "policy_epoch": 8}
        self.generation_id = "generation"
        self.connection = MemoryConnection(
            state,
            eligible,
        )
        self.connection.direct_removed = set(direct_removed)
        self.units = []
        self.cache = {}

    class _transaction:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.before = copy.deepcopy((self.connection.state, self.connection.published))
            self.connection.guarded = False
            return self.connection

        def __exit__(self, *args):
            if args[0] is not None:
                self.connection.state, self.connection.published = self.before
            self.connection.guarded = False
            return False

    def transaction(self):
        return self._transaction(self.connection)

    def guard(self, connection):
        connection.guarded = True

    def checkpoint(self, stage, inputs, produce):
        assert not self.connection.guarded
        key = (stage, json.dumps(inputs, sort_keys=True))
        if key not in self.cache:
            self.cache[key] = produce().model_dump(mode="json")
        return self.cache[key]

    def mark_l2_group(self, connection, unit_ids, outcome):
        connection.state["l2_examined_ids"] = sorted(
            set(connection.state.get("l2_examined_ids", [])) | set(unit_ids)
        )
        connection.state["last_insight_outcome"] = outcome

    def readiness(self, connection, **values):
        connection.state.update(values)

    def insert_unit(self, connection, **unit):
        self.units.append(unit)


def repair_state(support_ids):
    return {
        "insight_repairs": [
            {
                "insight_id": "insight",
                "next_revision": 3,
                "support_ids": list(support_ids),
                "removal_operation_id": "remove-op",
            }
        ]
    }


@pytest.mark.parametrize("overflow", [False, True])
def test_repair_hydrates_existing_event_annotations_and_budgets_complete_support(overflow):
    annotation = {
        "role": "event",
        "start": "2026-07-03T00:00:00Z",
        "end": "2026-07-04T00:00:00Z",
        "quote": "on July 3",
    }
    if overflow:
        annotation["quote"] = "Long recorded event wording. " * 5000
    store = RepairStore(
        repair_state(["fact-a", "fact-b"]),
        [
            (
                "unit-a",
                "fact-a",
                "Deployment occurred on July 3.",
                "source",
                1,
                datetime(2026, 10, 22, tzinfo=UTC),
                "tool",
                [annotation],
            ),
            ("unit-b", "fact-b", "The rollout succeeded.", "source", 1, None, None, None),
        ],
    )
    requests = []

    class Models:
        def generate(self, role, **kwargs):
            requests.append(json.loads(kwargs["user"]))
            return ModelResult(
                content=json.dumps(
                    {"outcome": "no_insight", "text": None, "subtype": None, "support_ids": []}
                ),
                usage=Usage(),
            )

    if overflow:
        with pytest.raises(ValueError, match="complete eligible support set"):
            repair_insights(store, Models())
        assert requests == []
        assert not store.connection.state.get("repair_outcomes")
    else:
        repair_insights(store, Models())
        assert requests == [
            {
                "facts": [
                    {
                        "id": "fact-a",
                        "text": "Deployment occurred on July 3.",
                        "mentioned_at": "2026-10-22T00:00:00Z",
                        "message_role": "tool",
                        "temporal_annotations": [annotation],
                    },
                    {"id": "fact-b", "text": "The rollout succeeded."},
                ]
            }
        ]
        assert store.connection.state["repair_outcomes"] == {"insight": "deleted"}
        private = next(iter(store.cache))[1]
        assert "source:1" in private and "2026-07-03" in private


def test_repair_publishes_next_revision_under_same_insight_id():
    store = RepairStore(
        repair_state(["fact-a", "fact-b"]),
        [
            ("unit-a", "fact-a", "A supported fact.", "source-a", 1, None, None, None),
            ("unit-b", "fact-b", "Another supported fact.", "source-b", 1, None, None, None),
        ],
    )

    class Models:
        def generate(self, role, **kwargs):
            assert role == "insights"
            return ModelResult(
                content='{"outcome":"insight","title":"Supported subject",'
                '"text":"Updated synthesis.",'
                '"support_ids":["fact-a","fact-b"],"subtype":"general_insights"}',
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2, coverage="complete"),
            )

        def embed(self, texts, **kwargs):
            return EmbeddingResult(vectors=((0.0,) * 1024,), usage=Usage())

    repair_insights(store, Models())

    assert store.connection.state["repair_outcomes"]["insight"] == "updated"
    assert any(
        "'insight',3" in query or "%s,%s,%s,%s" in query for query, _ in store.connection.published
    )
    assert store.units[0]["insight_id"] == "insight"
    assert store.units[0]["insight_revision"] == 3
    assert store.connection.state["l2_examined_ids"] == ["unit-a", "unit-b"]
    assert all(":8:" in stage for stage, _ in store.cache)


@pytest.mark.parametrize("retrieval_text", [None, "Violin restoration"])
def test_repair_embeds_current_purpose_but_publishes_full_supported_text(retrieval_text, capsys):
    store = RepairStore(
        repair_state(["fact-a", "fact-b"]),
        [
            ("unit-a", "fact-a", "The user is restoring a violin.", "source", 1, None, None, None),
            (
                "unit-b",
                "fact-b",
                "The user requested a bridge inspection.",
                "source",
                1,
                None,
                None,
                None,
            ),
        ],
    )
    text = "The user is restoring a violin and requested a bridge inspection."
    embedded = []

    class Models:
        def generate(self, role, **kwargs):
            assert role == "insights"
            return ModelResult(
                content=json.dumps(
                    {
                        "outcome": "insight",
                        "title": "Violin restoration",
                        "text": text,
                        "retrieval_text": retrieval_text,
                        "support_ids": ["fact-a", "fact-b"],
                        "subtype": "projects",
                    }
                ),
                usage=Usage(),
            )

        def embed(self, texts, **kwargs):
            embedded.extend(texts)
            return EmbeddingResult(vectors=((0.25,) * 1024,), usage=Usage())

    assert repair_insights(store, Models())

    assert embedded == [retrieval_text or text]
    assert store.units[0]["text"] == text
    assert store.units[0]["locator"]["support_ids"] == ["fact-a", "fact-b"]
    assert store.units[0]["locator"]["retrieval_text"] == retrieval_text
    assert store.units[0]["locator"]["title"] == "Violin restoration"
    assert store.units[0]["vector"] == [0.25] * 1024
    emitted = capsys.readouterr().out
    assert emitted
    event = json.loads(emitted)
    assert event["stage"] == "insight_indexed"
    assert event["operation_id"] == "repair-op"
    assert event["embedding_input"] == ("purpose" if retrieval_text else "full_text")


def test_insufficient_support_deletes_without_model_call():
    store = RepairStore(
        repair_state(["fact-a"]),
        [("unit-a", "fact-a", "A supported fact.", "source-a", 1, None, None, None)],
    )

    class NoCalls:
        def generate(self, *args, **kwargs):
            raise AssertionError("insufficient support must not call a model")

        def embed(self, *args, **kwargs):
            raise AssertionError("insufficient support must not embed")

    repair_insights(store, NoCalls())
    assert store.connection.state["repair_outcomes"]["insight"] == "deleted"
    assert store.connection.published == []


def test_direct_insight_removal_wins_over_pending_repair():
    store = RepairStore(
        repair_state(["fact-a", "fact-b"]),
        [
            ("unit-a", "fact-a", "A supported fact.", "source-a", 1, None, None, None),
            ("unit-b", "fact-b", "Another supported fact.", "source-b", 1, None, None, None),
        ],
        direct_removed={"insight"},
    )

    class NoCalls:
        def generate(self, *args, **kwargs):
            raise AssertionError("direct removal must block synthesis")

        def embed(self, *args, **kwargs):
            raise AssertionError("direct removal must block embedding")

    repair_insights(store, NoCalls())
    assert store.connection.state["repair_outcomes"]["insight"] == "directly_removed"
    assert store.connection.published == []


def test_repair_refuses_partial_support_after_input_packing(monkeypatch):
    store = RepairStore(
        repair_state(["fact-a", "fact-b", "fact-c"]),
        [
            ("unit-a", "fact-a", "A.", "source", 1, None, None, None),
            ("unit-b", "fact-b", "B.", "source", 1, None, None, None),
            ("unit-c", "fact-c", "C.", "source", 1, None, None, None),
        ],
    )
    monkeypatch.setattr("oam.governance.repair.pack_insight_facts", lambda facts, config: facts[:2])

    class Models:
        def generate(self, role, **kwargs):
            return InsightResult(
                outcome="insight",
                title="Partial subject",
                text="partial",
                usage=Usage(),
                support_ids=["fact-a", "fact-b"],
                subtype="general_insights",
            )

        def embed(self, texts, **kwargs):
            return EmbeddingResult(vectors=((0.0,) * 1024,), usage=Usage())

    monkeypatch.setattr(
        "oam.governance.repair.synthesize_insight",
        lambda *args, **kwargs: Models().generate("insights"),
    )
    with pytest.raises(ValueError, match="complete eligible support"):
        repair_insights(store, Models())
    assert store.connection.published == []


@pytest.mark.parametrize("outcome", ["insight", "no_insight"])
def test_superseded_repair_continues_with_other_insights(monkeypatch, outcome):
    state = repair_state(["fact-a", "fact-b"])
    state["insight_repairs"].append({"insight_id": "later", "next_revision": 4, "support_ids": []})
    store = RepairStore(
        state,
        [
            ("unit-a", "fact-a", "A.", "source", 1, None, None, None),
            ("unit-b", "fact-b", "B.", "source", 1, None, None, None),
        ],
    )
    store.connection.history_revision = 3
    monkeypatch.setattr(
        "oam.governance.repair.synthesize_insight",
        lambda *a, **kw: InsightResult(
            outcome=outcome,
            title="Updated subject" if outcome == "insight" else None,
            text="Updated." if outcome == "insight" else None,
            support_ids=["fact-a", "fact-b"] if outcome == "insight" else [],
            subtype="general_insights" if outcome == "insight" else None,
            usage=Usage(),
        ),
    )

    class Models:
        def embed(self, *a, **kw):
            return EmbeddingResult(vectors=((0.0,) * 1024,), usage=Usage())

    repair_insights(store, Models())
    assert store.connection.state["repair_outcomes"] == {
        "insight": "superseded",
        "later": "deleted",
    }
    assert store.connection.published == []
