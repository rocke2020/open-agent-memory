from __future__ import annotations

from oam.config import ResourceConfig
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import ResourceLimitError


def enforce_byte_limit(original: OriginalDescriptor, limits: ResourceConfig) -> None:
    if original.byte_length > limits.max_bytes:
        raise ResourceLimitError(
            f"Resource byte limit is {limits.max_bytes}; received {original.byte_length}"
        )
