"""Pending L2 seeds stay in the operation generation across worker retries."""

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import load_config
from oam.indexing import consolidation


@pytest.mark.parametrize("policy", ["related", "source"])
def test_pending_seeds_exclude_other_generations_before_group_limit(monkeypatch, policy):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"insight_seed_max": 1})}
    )
    store = SimpleNamespace(
        config=config,
        generation_id="current",
        source={"source_id": "source", "source_version": 1},
    )
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE facts(unit_id,memory_id,source_id,source_version,"
            "generation_id,kind,embedding,created_at)"
        )
        db.executemany(
            "INSERT INTO facts VALUES(?,?,'source',1,?,'fact',1,?)",
            [("old", "old", "previous", 0), ("new", "new", "current", 1)],
        )

        def rows(connection, query, parameters):
            # Only adapt PostgreSQL placeholder/array/vector syntax; execute filtering
            # and ordering in a real in-memory database, not a simulated selector.
            query = re.sub(r"=ANY\(%s\)", " IN (SELECT value FROM json_each(?))", query)
            query = query.replace("%s", "?").replace("::text", "").replace("<=>", "-")
            return [
                dict(row)
                for row in connection.execute(
                    query,
                    tuple(
                        json.dumps(value) if isinstance(value, list) else value
                        for value in parameters
                    ),
                )
            ]

        monkeypatch.setattr(consolidation, "ELIGIBLE_UNITS", "SELECT * FROM facts")
        monkeypatch.setattr(consolidation, "_rows", rows)
        selector = (
            consolidation._pending_group
            if policy == "related"
            else consolidation._pending_source_batch
        )
        # Match the worker: it supplies active IDs from every stored generation.
        selected = selector(store, db, ["old", "new"], set())
        assert [row["memory_id"] for row in selected] == ["new"]
        assert selector(store, db, ["old", "new"], {"new"}) == []
