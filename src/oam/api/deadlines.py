from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from oam.config import RecallConfig
from oam.contracts.search import RecallLevel


class TurnDeadlineConfigurationError(RuntimeError):
    """Trusted turn admission cannot use a shared deadline owner."""


class TurnDeadlineExhausted(TimeoutError):
    """No online-recall time remains, so retrieval must not dispatch."""


class TurnDeadlineRepository(Protocol):
    def get_or_create(self, turn_key: str, proposed_deadline: datetime) -> datetime: ...


@dataclass(frozen=True, slots=True)
class DeadlineAdmission:
    deadline_at: datetime
    turn_deadline_at: datetime | None
    remaining_seconds: float


class RecallDeadlineCoordinator:
    def __init__(
        self,
        config: RecallConfig,
        repository: TurnDeadlineRepository | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._repository = repository
        self._clock = clock

    def admit(
        self,
        recall_level: RecallLevel,
        *,
        trusted_turn_key: str | None = None,
        caller_deadline: datetime | None = None,
    ) -> DeadlineAdmission:
        now = self._aware(self._clock(), "clock")
        level_seconds = (
            self._config.fast_deadline_seconds
            if recall_level is RecallLevel.FAST
            else self._config.deep_deadline_seconds
        )
        candidates = [now + timedelta(seconds=level_seconds)]
        if caller_deadline is not None:
            candidates.append(self._aware(caller_deadline, "caller_deadline"))

        turn_deadline: datetime | None = None
        if trusted_turn_key is not None:
            if not trusted_turn_key.strip():
                raise TurnDeadlineConfigurationError("trusted turn key cannot be empty")
            if self._repository is None:
                raise TurnDeadlineConfigurationError(
                    "trusted turn admission requires a shared deadline repository"
                )
            proposed = now + timedelta(seconds=self._config.turn_deadline_seconds)
            stored = self._aware(
                self._repository.get_or_create(trusted_turn_key, proposed),
                "stored turn deadline",
            )
            turn_deadline = min(stored, proposed)
            candidates.append(turn_deadline)

        deadline = min(candidates)
        admitted_at = self._aware(self._clock(), "clock")
        if deadline <= admitted_at:
            raise TurnDeadlineExhausted("online recall deadline is exhausted")
        return DeadlineAdmission(
            deadline_at=deadline,
            turn_deadline_at=turn_deadline,
            remaining_seconds=(deadline - admitted_at).total_seconds(),
        )

    @staticmethod
    def _aware(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise TurnDeadlineConfigurationError(f"{name} must be timezone-aware")
        return value
