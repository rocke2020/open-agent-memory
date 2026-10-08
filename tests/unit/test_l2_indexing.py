from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.contracts.evidence import TemporalAnnotation
from oam.indexing.extraction import InsightInput
from oam.indexing.store import IndexStore
from oam.workers.index_source import _insights


def test_insight_input_preserves_evidence_dates_and_temporal_annotations():
    mentioned_at = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    annotation = TemporalAnnotation(
        role="event",
        start="2019-01-01T00:00:00+00:00",
        end="2020-01-01T00:00:00+00:00",
        quote="in 2019",
    )

    fact = InsightInput(
        id="fact-1",
        text="The user moved in 2019.",
        source_refs=("source-1:1",),
        mentioned_at=mentioned_at,
        temporal_annotations=(annotation,),
    )

    assert fact.mentioned_at == mentioned_at
    assert fact.temporal_annotations == (annotation,)
    assert fact.model_dump(mode="json")["mentioned_at"] == "2026-10-05T09:00:00Z"


def test_l2_claim_migration_serializes_users_and_orders_by_fact_storage_time():
    migration = (
        Path(__file__).parents[2] / "migrations" / "0022_l2_consolidation_order.sql"
    ).read_text()
    normalized = " ".join(migration.lower().split())

    assert "w.stage='consolidate'" in normalized
    assert "u.created_at" in normalized
    assert "advisory_xact_lock" in normalized
    assert "active_l2" in normalized


def test_l2_candidate_query_does_not_duplicate_temporal_annotations_per_seed():
    source = Path(__file__).parents[2] / "src/oam/workers/index_source.py"
    text = source.read_text()

    assert "candidate_annotations AS" in text
    assert "FROM candidate_annotations candidate CROSS JOIN seeds" in text


class L2Connection:
    def __init__(self, state, facts):
        self.state = state
        self.facts = facts

    def execute(self, query, params):
        if query.startswith("SELECT stage_state FROM operations"):
            return SimpleNamespace(fetchone=lambda: (dict(self.state),))
        if query.startswith("UPDATE operations SET stage_state="):
            self.state.update(params[0].obj)
            return None
        if "SELECT unit_id FROM eligible" in query:
            excluded = set(params[3])
            rows = [(fact[0],) for fact in self.facts if fact[0] not in excluded]
        elif "candidate_annotations AS" in query:
            rows = self.facts
        else:
            raise AssertionError(f"Unexpected database query: {query}")
        return SimpleNamespace(fetchall=lambda: rows)


@pytest.fixture
def l2_store(monkeypatch):
    store = object.__new__(IndexStore)
    store.config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    store.key = ("synthetic-tenant", "synthetic-user")
    store.claim = SimpleNamespace(operation_id="synthetic-operation")
    store.source = {"source_id": "source-1", "source_version": 1}
    state = {}
    facts = [
        ("fact-1", "The user moved in 2019.", "source-1", 1, None, None, None, []),
        ("fact-2", "The user moved in 2020.", "source-1", 1, None, None, None, []),
    ]
    connection = L2Connection(state, facts)

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(store, "transaction", transaction)
    monkeypatch.setattr(store, "guard", lambda connection: None)
    return store, state, facts


def test_l2_budget_failure_keeps_facts_unexamined(l2_store):
    store, state, _ = l2_store
    store.config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"max_input_tokens": 453})}
    )

    with pytest.raises(ValueError, match="input budget cannot fit two supporting facts"):
        _insights(store, None, ["fact-1", "fact-2"])

    assert not state.get("l2_examined_ids")
    assert not state.get("insights_ready")


def test_l2_resume_preserves_completed_group_outcome(l2_store):
    store, state, _ = l2_store
    state.update(
        l2_examined_ids=["fact-1", "fact-2"],
        last_insight_outcome="insight",
        insight_outcome="insight",
        insights_ready=False,
    )

    _insights(store, None, ["fact-1", "fact-2"])

    assert state["insight_outcome"] == "insight"
    assert state["insights_ready"] is True


def test_l2_single_eligible_fact_remains_a_valid_no_insight(l2_store):
    store, state, facts = l2_store
    facts.pop()

    _insights(store, None, ["fact-1"])

    assert state["l2_examined_ids"] == ["fact-1"]
    assert state["insight_outcome"] == "no_insight"
    assert state["insights_ready"] is True
