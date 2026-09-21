from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from oam.config import OAMConfig


class ResourceMediaType(StrEnum):
    HTML = "text/html"
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"


class EvidenceResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(min_length=1)
    media_type: ResourceMediaType
    filename: str = Field(min_length=1)
    byte_length: PositiveInt
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_type: Literal["evidence"] = "evidence"

    def validate_limits(self, config: OAMConfig) -> None:
        if self.byte_length > config.resource.max_bytes:
            raise ValueError(f"resource exceeds {config.resource.max_bytes} bytes")
