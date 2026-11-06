import hashlib
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
from oam.tokens import resolve_counter
from oam.workers.claims import ClaimedWork


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "generation_fails",
        "cancel_after_embed",
        "embedding_timeout",
        "incremental",
        "incremental_no_change",
        "baseline_changed",
        "oversized_baseline",
        "changed",
        "retracted",
        "old_omitted",
        "pending",
        "baseline_scrubbed",
    ],
)
def test_slow_embedding_renews_lease_and_preserves_usage(monkeypatch, mode) -> None:
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    if mode == "oversized_baseline":
        config = config.model_copy(
            update={"profile": config.profile.model_copy(update={"max_input_tokens": 1500})}
        )
    claim = ClaimedWork("t", "u", "w", "op", "refresh_profile", "a", 1, datetime.now(UTC))
    item = {
        "id": "f",
        "kind": "fact",
        "text": "Prefers English",
        "source_passages": [{"source_id": "s", "source_version": 1, "passage_id": "p"}],
    }
    inputs = ProfileInputs(b"i" * 32, b"s" * 32, {"f": item}, (), 0, 0, 0, (), 1, [item], {"f"}, 0)
    baseline: dict[str, Any] | None = None
    if mode in {
        "incremental",
        "incremental_no_change",
        "baseline_changed",
        "oversized_baseline",
        "changed",
        "retracted",
        "old_omitted",
        "pending",
        "baseline_scrubbed",
    }:
        recipe_identity = profile_identity(
            bytes(32),
            None,
            config.profile.max_input_tokens,
            (config.models.embedding.query_prefix if config.models.embedding else ""),
            "o200k_base",
            config.profile.max_memory_tokens,
        )
        recipe = hashlib.sha256(recipe_identity + worker.INCREMENTAL_SYSTEM.encode()).hexdigest()
        old_key = hashlib.sha256(b"old").hexdigest()
        new_key = hashlib.sha256(b"f").hexdigest()
        old_item = {
            "id": "old",
            "kind": "fact",
            "text": "The user likes sailing",
            "source_passages": [
                {"source_id": "old-source", "source_version": 1, "passage_id": "old-passage"}
            ],
        }
        inputs.supported["old"] = old_item
        inputs.stored_support_inputs.extend([old_item, item])
        inputs.eligible_support_ids.add("old")
        inputs.manifest.update(
            {"units": {old_key: "v1", new_key: "v2"}, "sources": {"s": 1}, "exact": "x"}
        )
        baseline = {
            "profile_revision": 1,
            "instruction": None,
            "content": (
                "## Main content\n\n- The user likes sailing [1]."
                "\n\n## References\n\n1. Conversation old"
            ),
            "support_ids": ["old"],
            "references": [
                {
                    "support_id": "old",
                    "kind": "fact",
                    "passages": [
                        {
                            "source_id": "old-source",
                            "source_version": 1,
                            "passage_id": "old-passage",
                        }
                    ],
                }
            ],
            "input_manifest": {
                **inputs.manifest,
                "units": {old_key: "v1"},
                "examined": [old_key],
                "recipe": recipe,
            },
        }
        if mode in {"old_omitted", "pending"}:
            baseline["input_manifest"]["units"][new_key] = "v2"
            if mode == "pending":
                baseline["input_manifest"]["pending"] = [new_key]
        if mode in {"changed", "retracted"}:
            baseline["content"] = baseline["content"].replace(
                "\n\n## References", "\n- The user uses French [2].\n\n## References"
            )
            baseline["support_ids"].append("f")
            baseline["input_manifest"]["units"][new_key] = "v1" if mode == "changed" else "v2"
            baseline["input_manifest"]["examined"].append(new_key)
            if mode == "retracted":
                inputs.manifest["units"].pop(new_key)
                inputs.supported.pop("f")
                inputs.eligible_support_ids.remove("f")
                inputs.stored_support_inputs.remove(item)
    if mode == "oversized_baseline":
        assert baseline is not None
        baseline["content"] = baseline["content"].replace("sailing", "sailing " * 1000)
    rank_scopes = []
    clock = {"now": 0, "expires": 0, "cancelled": False}
    target = profile_identity(
        inputs.identity,
        None,
        config.profile.max_input_tokens,
        (config.models.embedding.query_prefix if config.models.embedding else ""),
        "o200k_base",
        config.profile.max_memory_tokens,
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
                self.row = (target, [], None, None, "auto")
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
    monkeypatch.setattr(worker, "_latest_snapshot", lambda *_: baseline)
    monkeypatch.setattr(worker, "claim_generation", lambda *_: "gen")
    monkeypatch.setattr(worker, "load_profile_inputs", lambda *a, **k: inputs)

    def rank_candidates(*args, **kwargs):
        rank_scopes.append(kwargs.get("candidate_ids"))
        return ["f"]

    monkeypatch.setattr(worker, "rank_profile_candidates", rank_candidates)

    def guard(*args):
        if clock["cancelled"]:
            raise worker.StaleProfileWork("cancelled")
        if clock["expires"] and clock["now"] > clock["expires"]:
            raise worker.StaleProfileWork("expired")

    monkeypatch.setattr(worker, "_guard", guard)

    counter = resolve_counter(config.tokenizer)
    monkeypatch.setattr(
        worker, "resolve_counter", lambda *_: pytest.fail("worker must reuse runtime counter")
    )

    class Models:
        ingestion_counter = counter

        def embed(self, *args, **kwargs):
            assert mode not in {"retracted", "old_omitted"}
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
            assert mode not in {"retracted", "old_omitted"}
            assert mode != "embedding_timeout"
            clock["now"] += 150
            if mode == "generation_fails":
                raise ModelOutputTooLongError(
                    "truncated", usage=Usage(input_tokens=10, output_tokens=4, total_tokens=14)
                )
            content = json.dumps({"content": "English [1]", "support_ids": ["f"]})
            if baseline is not None and mode != "oversized_baseline":
                assert "Existing blocks" in kwargs["system"]
                assert [value["id"] for value in json.loads(kwargs["user"])["inputs"]] == ["f"]
                content = (
                    json.dumps({"no_change": True, "edits": []})
                    if mode == "incremental_no_change"
                    else json.dumps(
                        {
                            "no_change": False,
                            "edits": [
                                {
                                    "action": "add",
                                    "text": "- The user prefers English [1].",
                                    "support_ids": ["f"],
                                }
                            ],
                        }
                    )
                )
                if mode == "baseline_changed":
                    baseline["profile_revision"] = 2
                if mode == "baseline_scrubbed":
                    baseline["content"] = None
            return SimpleNamespace(
                content=content,
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

    if mode in {"baseline_changed", "baseline_scrubbed"}:
        with pytest.raises(worker.StaleProfileWork, match="baseline changed"):
            worker.process_profile_claim(
                config,
                "unused",
                models=Models(),
                pool=cast(Any, pool),
                claim=claim,
                worker_id="worker",
            )
        assert connection.inserted is None
        return

    worker.process_profile_claim(
        config, "unused", models=Models(), pool=cast(Any, pool), claim=claim, worker_id="worker"
    )
    assert connection.inserted is not None
    if mode in {"retracted", "old_omitted"}:
        assert "sailing [1]" in connection.inserted[3]
        assert "French" not in connection.inserted[3]
        assert connection.stage["mode"] == "incremental"
        assert connection.stage["generation_calls"] == 0
        assert connection.stage["usage"] == {}
        assert connection.inserted[-1].obj["unexamined"] == (mode == "old_omitted")
        assert connection.stage["pruned_blocks"] == (mode == "retracted")
        return
    if mode == "oversized_baseline":
        assert connection.stage["mode"] == "full"
        assert connection.stage["fallback_reason"] == "incremental_input_budget"
        assert connection.inserted[-2].obj["fallback_reason"] == "incremental_input_budget"
        assert rank_scopes == [["f"], None]
        assert "English [1]" in connection.inserted[3]
        assert connection.inserted[-2].obj["examined"] == [hashlib.sha256(b"f").hexdigest()]
        assert connection.inserted[-1].obj == {"selected": 1, "unexamined": 1, "budget_omitted": 0}
        return
    if baseline is not None:
        assert "sailing [1]" in connection.inserted[3]
        assert ("English [2]" in connection.inserted[3]) == (
            mode in {"incremental", "changed", "pending"}
        )
        assert "French" not in connection.inserted[3]
        assert connection.stage["mode"] == "incremental"
        assert connection.inserted[-2].obj["examined"] == sorted(inputs.manifest["units"])
    else:
        assert "English [1]" in connection.inserted[3]
    assert connection.inserted[-1].obj == {
        "unexamined": 0,
        "budget_omitted": 0,
        "selected": 2 if baseline else 1,
    }
    assert connection.stage["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert connection.stage["embedding_usage"] == {"input_tokens": 2}
