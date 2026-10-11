from __future__ import annotations

import math

from oam.config import ResourceConfig
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import ResourceLimitError


def enforce_byte_limit(original: OriginalDescriptor, limits: ResourceConfig) -> None:
    if original.byte_length > limits.max_bytes:
        raise ResourceLimitError(
            f"Resource byte limit is {limits.max_bytes}; received {original.byte_length}"
        )


def downscaled_dimensions(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """Fit a pixel budget without upscaling; allow only integer-pixel rounding."""
    scale = min(1.0, math.sqrt(max_pixels / (width * height)))
    target_width = min(max_pixels, max(1, int(width * scale)))
    target_height = min(max_pixels // target_width, max(1, int(height * scale)))
    return target_width, target_height
