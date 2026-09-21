from __future__ import annotations

import hashlib
from dataclasses import dataclass

from oam.contracts.resources import ResourceMediaType


@dataclass(frozen=True, slots=True)
class OriginalDescriptor:
    """Immutable accepted bytes plus their media type and content digest."""

    media_type: ResourceMediaType
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        content = bytes(self.content)
        object.__setattr__(self, "content", content)
        expected = hashlib.sha256(content).hexdigest()
        if self.sha256 != expected:
            raise ValueError("sha256 does not match original bytes")

    @classmethod
    def from_bytes(
        cls,
        media_type: ResourceMediaType | str,
        content: bytes,
    ) -> OriginalDescriptor:
        immutable_content = bytes(content)
        return cls(
            media_type=ResourceMediaType(media_type),
            content=immutable_content,
            sha256=hashlib.sha256(immutable_content).hexdigest(),
        )

    @property
    def byte_length(self) -> int:
        return len(self.content)
