import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from oam.config import load_config
from oam.models.runtime import ModelOutputTooLongError, Usage
from oam.profiles import worker
from oam.profiles.inputs import ProfileInputs
from oam.profiles.service import profile_identity
from oam.workers.claims import ClaimedWork


@pytest.mark.parametrize(
    "mode", ["success", "generation_fails", "cancel_after_embed", "embedding_timeout"]
)
def test_slow_embedding_renews_lease_and_preserves_usage(monkeypatch, mode) -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    claim = ClaimedWork("t", "u", "w", "op", "refresh_profile", "a", 1, datetime.now(UTC))
    item = {
        "id": "f",
        "kind": "fact",
        "text": "Prefers English",
        "source_passages": [{"source_id": "s", "source_version": 1, "passage_id": "p"}],
    }
    inputs = ProfileInputs(b"i" * 32, b"s" * 32, {"f": item}, (), 0, 0, 0, (), 1, [item], {"f"}, 0)
    clock = {"now": 0, "expires": 0, "cancelled": False}
    target = profile_identity(
        inputs.identity,
        None,
        config.profile.max_input_tokens,
        (config.models.embedding.query_prefix if config.models.embedding else ""),
    )

    class Connection:
        inserted: Any = None
        stage: Any = None
        attempt_usage: Any = None
        attempt_failed = False

        def transaction(self):
            return nullcontext()

        def cursor(self):
            return self

        def execute(self, query, params=()):
            self.row = None
            if "SELECT w.input_digest" in query:
                self.row = (target, [], None, None)
            elif "SET lease_expires_at" in query:
                self.row = (
                    ("w",) if clock["now"] <= clock["expires"] or clock["expires"] == 0 else None
                )
                if self.row:
                    clock["expires"] = clock["now"] + params[0]
            elif "SELECT published_seq" in query:
                self.row = (0,)
            elif "SELECT COALESCE(MAX(profile_revision)" in query:
                self.row = (1,)
            elif query.startswith("INSERT INTO profiles"):
                self.inserted = params
            elif query.startswith("UPDATE operations SET status='succeeded'"):
                self.stage = params[0].obj
            elif query.startswith("UPDATE work_units SET status='failed_terminal'"):
                self.row = None if clock["cancelled"] else ("w",)
            elif query.startswith("UPDATE attempts SET disposition='failed'"):
                self.attempt_failed = True
                self.attempt_usage = params[0].obj if params[0] is not None else None
            elif query.startswith("UPDATE operations SET status='partial_failed'"):
                self.stage = params[0].obj
            return self

        def fetchone(self):
            return self.row

    connection = Connection()
    pool = SimpleNamespace(connection=lambda **kwargs: nullcontext(connection))
    monkeypatch.setattr(worker, "bind_user", lambda *_: None)
    monkeypatch.setattr(worker, "claim_generation", lambda *_: "gen")
    monkeypatch.setattr(worker, "load_profile_inputs", lambda *a, **k: inputs)
    monkeypatch.setattr(worker, "rank_profile_candidates", lambda *a: ["f"])

    def guard(*args):
        if clock["cancelled"]:
            raise worker.StaleProfileWork("cancelled")
        if clock["expires"] and clock["now"] > clock["expires"]:
            raise worker.StaleProfileWork("expired")

    monkeypatch.setattr(worker, "_guard", guard)

    class Models:
        def embed(self, *args, **kwargs):
            clock["now"] += 150
            if mode == "embedding_timeout":
                raise httpx.ReadTimeout("embedding timed out")
            if mode == "cancel_after_embed":
                clock["cancelled"] = True
            return SimpleNamespace(
                vectors=((1.0,),),
                usage=SimpleNamespace(model_dump=lambda **kwargs: {"input_tokens": 2}),
            )

        def generate(self, *args, **kwargs):
            assert mode != "embedding_timeout"
            clock["now"] += 150
            if mode == "generation_fails":
                raise ModelOutputTooLongError(
                    "truncated", usage=Usage(input_tokens=10, output_tokens=4, total_tokens=14)
                )
            return SimpleNamespace(
                content=json.dumps({"content": "English [1]", "support_ids": ["f"]}),
                usage=SimpleNamespace(
                    model_dump=lambda **kwargs: {"input_tokens": 10, "output_tokens": 4}
                ),
            )

    if mode == "embedding_timeout":
        with pytest.raises(httpx.ReadTimeout):
            worker.process_profile_claim(
                config,
                "unused",
                models=Models(),
                pool=cast(Any, pool),
                claim=claim,
                worker_id="worker",
            )
        assert connection.attempt_failed
        assert connection.stage["error"] == "ReadTimeout"
        return

    if mode == "cancel_after_embed":
        with pytest.raises(worker.StaleProfileWork):
            worker.process_profile_claim(
                config,
                "unused",
                models=Models(),
                pool=cast(Any, pool),
                claim=claim,
                worker_id="worker",
            )
        assert connection.inserted is None
        assert connection.stage is None
        assert connection.attempt_usage["embedding_usage"] == {"input_tokens": 2}
        return

    if mode == "generation_fails":
        with pytest.raises(ModelOutputTooLongError):
            worker.process_profile_claim(
                config,
                "unused",
                models=Models(),
                pool=cast(Any, pool),
                claim=claim,
                worker_id="worker",
            )
        assert connection.inserted is None
        assert connection.stage["embedding_usage"] == {"input_tokens": 2}
        assert connection.stage["usage"]["input_tokens"] == 10
        assert connection.stage["usage"]["output_tokens"] == 4
        return

    worker.process_profile_claim(
        config, "unused", models=Models(), pool=cast(Any, pool), claim=claim, worker_id="worker"
    )
    assert connection.inserted is not None
    assert "English [1]" in connection.inserted[3]
    assert connection.inserted[-1].obj == {"unexamined": 0, "budget_omitted": 0, "selected": 1}
    assert connection.stage["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert connection.stage["embedding_usage"] == {"input_tokens": 2}
