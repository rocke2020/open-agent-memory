from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import enforce_byte_limit
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import (
    ImageInspection,
    ImageMetadata,
    ResourceLimitError,
    ResourceParseError,
)

_EXPECTED_FORMAT = {
    ResourceMediaType.PNG: "PNG",
    ResourceMediaType.JPEG: "JPEG",
}


def inspect_images(
    originals: tuple[OriginalDescriptor, ...],
    limits: ResourceConfig,
) -> ImageInspection:
    if len(originals) > limits.max_images:
        raise ResourceLimitError(
            f"Resource image count limit is {limits.max_images}; received {len(originals)}"
        )
    aggregate_bytes = sum(original.byte_length for original in originals)
    if aggregate_bytes > limits.max_bytes:
        raise ResourceLimitError(
            f"Resource aggregate byte limit is {limits.max_bytes}; received {aggregate_bytes}"
        )
    for original in originals:
        enforce_byte_limit(original, limits)

    results = []
    total_pixels = 0
    for index, original in enumerate(originals, start=1):
        expected_format = _EXPECTED_FORMAT.get(original.media_type)
        if expected_format is None:
            raise ResourceParseError("image parser accepts only image/png and image/jpeg")
        try:
            with Image.open(BytesIO(original.content)) as image:
                actual_format = image.format
                width, height = image.size
                pixels = width * height
                if pixels > limits.max_pixels_per_image:
                    raise ResourceLimitError(
                        "Resource per-image pixel limit is "
                        f"{limits.max_pixels_per_image}; received {pixels}"
                    )
                total_pixels += pixels
                if total_pixels > limits.max_total_pixels:
                    raise ResourceLimitError(
                        "Resource total pixel limit is "
                        f"{limits.max_total_pixels}; received {total_pixels}"
                    )
                if actual_format != expected_format:
                    raise ResourceParseError(
                        f"declared {original.media_type.value} does not match {actual_format} bytes"
                    )
                image.verify()
        except ResourceParseError:
            raise
        except (
            Image.DecompressionBombError,
            OSError,
            SyntaxError,
            UnidentifiedImageError,
        ) as exc:
            raise ResourceParseError("image Resource is malformed or unreadable") from exc
        results.append(
            ImageMetadata(
                locator=f"image:{index:04d}",
                format=expected_format,
                width=width,
                height=height,
                pixels=pixels,
            )
        )
    return ImageInspection(images=tuple(results), total_pixels=total_pixels)
