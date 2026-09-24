"""Developer-facing retain inputs; storage identities are assigned by OAM."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, TypeAdapter, model_validator

from oam.config import OAMConfig
from oam.contracts.evidence import (
    CallerTemporalAnnotation,
    FrozenContract,
    MessageRole,
    NonBlank,
    SourceMetadata,
    SourceText,
)
from oam.contracts.resources import ResourceTemporalAnnotation
from oam.contracts.search import SearchRequest


class UserRequest(FrozenContract):
    user_id: NonBlank = Field(max_length=256)


class RetainMetadata(SourceMetadata, UserRequest):
    pass


class RetainMessage(FrozenContract):
    role: MessageRole
    content: SourceText
    event_time: datetime
    learning_eligible: StrictBool
    temporal_annotations: list[CallerTemporalAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence(self):
        if not self.content.strip():
            raise ValueError("message content must not be blank")
        if self.event_time.utcoffset() is None:
            raise ValueError("event_time must be timezone-aware")
        for annotation in self.temporal_annotations:
            annotation.validate_quote(self.content)
        return self


class RetainInteraction(RetainMetadata):
    input_type: Literal["interaction"]
    messages: list[RetainMessage] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        if len(self.messages) > config.ingest.max_messages:
            raise ValueError("interaction exceeds message limit")
        if sum(len(m.content.encode("utf-8")) for m in self.messages) > config.ingest.max_bytes:
            raise ValueError("interaction exceeds byte limit")


class RetainTextDocument(RetainMetadata):
    input_type: Literal["document"]
    document_id: NonBlank = Field(max_length=256)
    update_mode: Literal["replace", "append"]
    text: SourceText
    temporal_annotations: list[ResourceTemporalAnnotation] = Field(default_factory=list)

    def validate_limits(self, config: OAMConfig) -> None:
        if len(self.text.encode("utf-8")) > config.resource.max_bytes:
            raise ValueError("document exceeds byte limit")
        if not self.text.strip():
            raise ValueError("document text must not be blank")


class RetainFileDocument(RetainMetadata):
    input_type: Literal["document"]
    document_id: NonBlank = Field(max_length=256)
    update_mode: Literal["replace"]
    temporal_annotations: list[ResourceTemporalAnnotation] = Field(default_factory=list)


RetainJSON = Annotated[RetainInteraction | RetainTextDocument, Field(discriminator="input_type")]
RETAIN_JSON = TypeAdapter(RetainJSON)


class RecallRequest(SearchRequest):
    user_id: NonBlank = Field(max_length=256)
