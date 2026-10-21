from datetime import UTC, datetime, timedelta

import pytest

from oam.api.deadlines import (
    RecallDeadlineCoordinator,
    TurnDeadlineConfigurationError,
    TurnDeadlineExhausted,
)
from oam.config import load_config
from oam.contracts.search import RecallLevel
from oam.paths import default_config_path


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SharedDeadlines:
    def __init__(self) -> None:
        self.values: dict[str, datetime] = {}

    def get_or_create(self, key: str, proposed: datetime) -> datetime:
        return self.values.setdefault(key, proposed)


def test_two_processes_share_the_first_turn_deadline() -> None:
    started = datetime(2026, 9, 21, tzinfo=UTC)
    clock = MutableClock(started)
    repository = SharedDeadlines()
    first = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, repository, clock
    )
    second = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, repository, clock
    )

    first_admission = first.admit(RecallLevel.FAST, trusted_turn_key="turn-1")
    clock.now += timedelta(seconds=7)
    second_admission = second.admit(RecallLevel.NORMAL, trusted_turn_key="turn-1")

    assert first_admission.deadline_at == started + timedelta(seconds=3)
    assert second_admission.turn_deadline_at == started + timedelta(seconds=10)
    assert second_admission.remaining_seconds == 3


def test_expired_turn_dispatches_nothing() -> None:
    started = datetime(2026, 9, 21, tzinfo=UTC)
    clock = MutableClock(started)
    coordinator = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, SharedDeadlines(), clock
    )
    coordinator.admit(RecallLevel.FAST, trusted_turn_key="turn-1")
    clock.now += timedelta(seconds=10)

    with pytest.raises(TurnDeadlineExhausted):
        coordinator.admit(RecallLevel.NORMAL, trusted_turn_key="turn-1")


def test_trusted_turn_requires_shared_repository() -> None:
    clock = MutableClock(datetime(2026, 9, 21, tzinfo=UTC))
    coordinator = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, None, clock
    )

    with pytest.raises(TurnDeadlineConfigurationError):
        coordinator.admit(RecallLevel.FAST, trusted_turn_key="turn-1")


def test_normal_uses_its_selected_cap_and_later_react_call_has_fresh_budget(tmp_path) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text("recall:\n  normal_deadline_seconds: 2\n")
    config = load_config(path).config.recall
    started = datetime(2026, 9, 22, tzinfo=UTC)
    clock = MutableClock(started)
    coordinator = RecallDeadlineCoordinator(config, None, clock)
    coordinator.admit(RecallLevel.FAST)
    clock.now += timedelta(seconds=30)

    admitted = coordinator.admit(RecallLevel.NORMAL)

    assert admitted.deadline_at == started + timedelta(seconds=32)
    assert admitted.remaining_seconds == 2
    assert admitted.turn_deadline_at is None
