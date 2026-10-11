from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import downscaled_dimensions, enforce_byte_limit
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
                if pixels > limits.max_input_pixels_per_image:
                    raise ResourceLimitError(
                        "Resource original-image pixel limit is "
                        f"{limits.max_input_pixels_per_image}; received {pixels}"
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


def prepare_image_for_vision(original: OriginalDescriptor, limits: ResourceConfig) -> bytes:
    """Validate originals and resize only the model-input copy, retaining its format."""
    metadata = inspect_images((original,), limits).images[0]
    if metadata.pixels <= limits.max_pixels_per_image:
        return original.content
    try:
        with (
            Image.open(BytesIO(original.content)) as image,
            ImageOps.exif_transpose(image) as oriented,
        ):
            dimensions = downscaled_dimensions(*oriented.size, limits.max_pixels_per_image)
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            mode = "RGBA" if metadata.format == "PNG" and has_alpha else "RGB"
            with (
                oriented.convert(mode) as converted,
                converted.resize(dimensions, Image.Resampling.LANCZOS) as resized,
            ):
                output = BytesIO()
                resized.save(output, format=metadata.format)
                return output.getvalue()
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise ResourceParseError("image Resource could not be downscaled") from exc
