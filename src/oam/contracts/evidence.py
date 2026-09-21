from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from oam.config import OAMConfig

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class EvidenceMessage(FrozenContract):
    message_id: NonBlank
    role: MessageRole
    content: NonBlank
    event_time: datetime
    learning_eligible: bool = False

    @field_validator("event_time")
    @classmethod
    def require_aware_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_time must be timezone-aware")
        return value


class EvidenceAppendRequest(FrozenContract):
    input_role: Literal["interaction"] = "interaction"
    source_id: NonBlank
    messages: list[EvidenceMessage] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        if len(self.messages) > config.ingest.max_messages:
            raise ValueError(f"interaction exceeds {config.ingest.max_messages} messages")
        size = sum(len(message.content.encode("utf-8")) for message in self.messages)
        if size > config.ingest.max_bytes:
            raise ValueError(f"interaction content exceeds {config.ingest.max_bytes} bytes")
