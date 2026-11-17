"""A refresh queues one Profile behind the input preparation it requested."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.profiles import service
from oam.profiles.inputs import ProfileInputs


@pytest.mark.parametrize("existing_profile", [False, True])
def test_refresh_enqueues_profile_after_preparing_failed_and_pending_inputs(
    monkeypatch, existing_profile
):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    inputs = ProfileInputs(
        b"i" * 32,
        b"s" * 32,
        {},
        (),
        2,
        1,
        0,
        ("pending",),
        1,
        [],
        set(),
        0,
        failed_source_ids=("failed",),
    )
    snapshot = None
    if existing_profile:
        inputs = replace(
            inputs,
            pending_inputs=1,
            waiting_source_ids=(),
            manifest={"units": {}, "sources": {}, "exact": "x"},
        )
        snapshot = {
            "profile_revision": 1,
            "published_at": None,
            "content": "Existing supported profile",
            "support_ids": [],
            "references": [],
            "instruction": None,
            "input_manifest": inputs.manifest,
            "input_identity": service.profile_identity(
                inputs.identity,
                None,
                config.profile.max_input_tokens,
                service.profile_query_prefix(config),
                service.resolve_counter(config.tokenizer).identity,
                config.profile.max_memory_tokens,
            ),
        }
    calls = []
    prepared = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def cursor(self):
            return nullcontext(self)

        def execute(self, query, params=()):
            calls.append((query, params))
            self.row = ("gen",) if "SELECT index_generation_id" in query else None
            return self

        def fetchone(self):
            return self.row

        def fetchall(self):
            return []

    connection = Connection()
    authority = AuthorizedUser(
        "key", "t", "principal", "u", frozenset({"memory:read", "memory:append"})
    )
    pool = SimpleNamespace(connection=lambda: nullcontext(connection))
    monkeypatch.setattr(service, "load_profile_inputs", lambda *a, **k: inputs)
    monkeypatch.setattr(service, "_current_snapshot", lambda *a: snapshot)
    monkeypatch.setattr(service, "_latest_profile_instruction", lambda *a: None)
    monkeypatch.setattr(service, "require_queue_capacity", lambda *a: None)

    def prepare(connection_arg, authority_arg, config_arg, source_ids):
        assert connection_arg is connection and authority_arg is authority
        prepared.append(source_ids)
        return ["index-retry", "insight-work"]

    monkeypatch.setattr(service, "prepare_profile_dependencies", prepare)
    result = service.ProfileService(config, pool).refresh(authority)
    assert result["status"] == "accepted"
    assert prepared == [("failed",) if existing_profile else ("pending", "failed")]
    profile_work = next(
        params for query, params in calls if query.startswith("INSERT INTO work_units")
    )
    assert profile_work[-2] == ["index-retry", "insight-work"]
    assert profile_work[3] == result["operation_id"]
