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
    clock.now += timedelta(seconds=4)
    second_admission = second.admit(RecallLevel.DEEP, trusted_turn_key="turn-1")

    assert first_admission.deadline_at == started + timedelta(seconds=3)
    assert second_admission.turn_deadline_at == started + timedelta(seconds=10)
    assert second_admission.remaining_seconds == 6


def test_expired_turn_dispatches_nothing() -> None:
    started = datetime(2026, 9, 21, tzinfo=UTC)
    clock = MutableClock(started)
    coordinator = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, SharedDeadlines(), clock
    )
    coordinator.admit(RecallLevel.FAST, trusted_turn_key="turn-1")
    clock.now += timedelta(seconds=10)

    with pytest.raises(TurnDeadlineExhausted):
        coordinator.admit(RecallLevel.DEEP, trusted_turn_key="turn-1")


def test_trusted_turn_requires_shared_repository() -> None:
    clock = MutableClock(datetime(2026, 9, 21, tzinfo=UTC))
    coordinator = RecallDeadlineCoordinator(
        load_config(default_config_path()).config.recall, None, clock
    )

    with pytest.raises(TurnDeadlineConfigurationError):
        coordinator.admit(RecallLevel.FAST, trusted_turn_key="turn-1")
