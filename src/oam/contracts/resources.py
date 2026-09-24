from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import Field, PositiveInt

from oam.config import OAMConfig
from oam.contracts.evidence import CallerTemporalAnnotation, NonBlank, SourceInputMetadata


class ResourceMediaType(StrEnum):
    TEXT = "text/plain"
    HTML = "text/html"
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"


class ResourceTemporalAnnotation(CallerTemporalAnnotation):
    locator: NonBlank


class EvidenceResourceCreate(SourceInputMetadata):
    resource_id: str = Field(min_length=1)
    media_type: ResourceMediaType
    filename: str = Field(min_length=1)
    byte_length: PositiveInt
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_type: Literal["evidence"] = "evidence"
    temporal_annotations: list[ResourceTemporalAnnotation] = Field(default_factory=list)

    def validate_source_spans(self, blocks: Mapping[str, str]) -> None:
        for annotation in self.temporal_annotations:
            if annotation.locator not in blocks:
                raise ValueError("annotation locator does not identify a parsed source block")
            annotation.validate_quote(blocks[annotation.locator])

    def validate_limits(self, config: OAMConfig) -> None:
        if self.byte_length > config.resource.max_bytes:
            raise ValueError(f"resource exceeds {config.resource.max_bytes} bytes")
