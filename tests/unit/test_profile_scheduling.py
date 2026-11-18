from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


def test_policy_patch_preserves_mode_and_clears_opposite_trigger():
    from oam.profiles.scheduling import merge_policy

    original = {"mode": "incremental", "refresh_after_consolidation": True}
    policy = merge_policy(original, {"refresh_cron": "0 3 * * *"})
    assert policy.mode == "incremental"
    assert policy.refresh_cron == "0 3 * * *"
    assert policy.refresh_after_consolidation is False
    policy = merge_policy(policy.model_dump(), {"refresh_after_consolidation": True})
    assert policy.refresh_cron is None
    assert policy.refresh_after_consolidation is True
    assert merge_policy(policy.model_dump(), {"full_refresh_cron": None}).full_refresh_cron is None


def test_blank_cron_patch_clears_only_that_schedule():
    from oam.contracts.profile import ProfilePolicyPatch
    from oam.profiles.scheduling import merge_policy

    patch = ProfilePolicyPatch(refresh_cron="   ").model_dump(exclude_unset=True)
    assert patch == {"refresh_cron": None}
    policy = merge_policy({"refresh_after_consolidation": True}, patch)
    assert policy.refresh_after_consolidation is True
    assert policy.refresh_cron is None


@pytest.mark.parametrize(
    "patch",
    [
        {"refresh_cron": "0 3 * * *", "refresh_after_consolidation": True},
        {"refresh_cron": "not cron"},
        {"refresh_cron": "0 0 3 * * *"},
        {"mode": "delta"},
        {"min_refresh_interval_seconds": -1},
        {"mode": None},
    ],
)
def test_invalid_policies_are_rejected(patch):
    from oam.profiles.scheduling import merge_policy

    with pytest.raises((ValidationError, ValueError)):
        merge_policy({}, patch)


def test_full_schedule_has_independent_clock_and_drift_gate():
    from oam.profiles.scheduling import schedule_decision

    now = datetime(2026, 11, 15, 4, tzinfo=UTC)  # Sunday, after 03:00 UTC.
    policy = {"full_refresh_cron": "0 3 * * 0", "mode": "incremental"}
    state = {"last_full_at": now - timedelta(days=7), "last_success_at": now - timedelta(hours=2)}
    assert schedule_decision(policy, state, now, changed=False, ready=True) == "full"
    state["last_success_at"] = state["last_full_at"]
    assert schedule_decision(policy, state, now, changed=False, ready=True) is None


def test_normal_cron_skips_unchanged_and_honors_utc_boundary():
    from oam.profiles.scheduling import schedule_decision

    policy = {"refresh_cron": "0 3 * * *", "mode": "incremental"}
    state = {"last_success_at": datetime(2026, 11, 14, 4, tzinfo=UTC)}
    assert (
        schedule_decision(
            policy, state, datetime(2026, 11, 15, 2, tzinfo=UTC), changed=True, ready=True
        )
        is None
    )
    assert (
        schedule_decision(
            policy, state, datetime(2026, 11, 15, 3, tzinfo=UTC), changed=True, ready=True
        )
        == "incremental"
    )
    assert (
        schedule_decision(
            policy, state, datetime(2026, 11, 15, 4, tzinfo=UTC), changed=False, ready=True
        )
        is None
    )


def test_full_pending_survives_interval_and_incomplete_consolidation():
    from oam.profiles.scheduling import schedule_decision

    now = datetime(2026, 11, 15, 4, tzinfo=UTC)
    policy = {"refresh_after_consolidation": True, "min_refresh_interval_seconds": 3600}
    state = {"pending_mode": "full", "last_admitted_at": now - timedelta(minutes=5)}
    # The decision records strongest intent; admission applies the interval separately.
    assert schedule_decision(policy, state, now, changed=True, ready=True) == "full"
    assert schedule_decision(policy, {}, now, changed=True, ready=False) is None


def test_disabled_policy_clears_pending_automatic_work():
    from oam.profiles.scheduling import merge_policy

    policy = merge_policy({}, {})
    assert policy.refresh_cron is None and policy.full_refresh_cron is None
    assert policy.refresh_after_consolidation is False


@pytest.mark.parametrize(
    "active,ready,age,expected",
    [
        (True, True, 7200, False),
        (False, False, 7200, False),
        (False, True, 300, False),
        (False, True, 7200, True),
    ],
)
def test_admission_keeps_full_intent_and_never_replaces_active_work(
    monkeypatch, active, ready, age, expected
):
    from oam.config import load_config
    from oam.profiles import scheduling

    now = datetime(2026, 11, 15, 4, tzinfo=UTC)
    state = {
        "policy": {"refresh_after_consolidation": True, "min_refresh_interval_seconds": 3600},
        "paused": False,
        "pending_mode": "full",
        "last_success_at": None,
        "last_full_at": None,
        "last_admitted_at": now - timedelta(seconds=age),
    }
    writes = []
    calls = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def cursor(self, **kwargs):
            return nullcontext(self)

        def execute(self, query, params=()):
            if "SELECT 1 FROM users" in query or "SELECT oam_authorize_user" in query:
                self.row = (1,)
            elif "SELECT * FROM profile_refresh_policies" in query:
                self.row = state
            elif "SELECT clock_timestamp()" in query:
                self.row = (now,)
            elif "SELECT 1 FROM operations" in query:
                self.row = (1,) if active else None
            else:
                writes.append((query, params))
                self.row = None
            return self

        def fetchone(self):
            return self.row

    class Service:
        def __init__(self, *args):
            pass

        def get(self, *args):
            return {
                "updated_at": None,
                "pending_inputs": 0 if ready else 1,
                "failed_inputs": 0,
                "blocked_inputs": 0,
                "freshness": "dirty",
            }

        def refresh(self, *args, **kwargs):
            calls.append(kwargs)
            return {"status": "accepted", "operation_id": "op"}

    connection = Connection()
    # bind_user needs an execute-capable cursor, whereas dict cursor is a context manager.
    monkeypatch.setattr(scheduling, "bind_user", lambda *args: None)
    monkeypatch.setattr(scheduling, "ProfileService", Service)
    pool = SimpleNamespace(connection=lambda: nullcontext(connection))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    assert scheduling.admit_scheduled_profile(config, pool, "t", "u") is expected
    assert calls == ([{"mode": "full"}] if expected else [])
    assert writes[0][1] == ("full", "t", "u")
    assert any("pending_mode=NULL" in sql for sql, _ in writes) is expected
