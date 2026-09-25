"""Killable child boundary for local PDF parsing, inspection and rendering."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import enforce_byte_limit
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import ResourceLimitError, ResourceParseError

PDF_CHILD_OUTPUT_MULTIPLIER = 4
PDF_CHILD_ADDRESS_SPACE_BYTES = 1_073_741_824


@dataclass(frozen=True, slots=True)
class InspectedPdfPage:
    locator: str
    page_number: int
    text: str
    needs_vision: bool


def _run_child(
    mode: str,
    original: OriginalDescriptor,
    limits: ResourceConfig,
    *,
    page_number: int = 0,
    remaining_pixels: int = 0,
) -> bytes:
    if original.media_type is not ResourceMediaType.PDF:
        raise ResourceParseError("PDF child requires application/pdf original bytes")
    enforce_byte_limit(original, limits)
    command = [
        sys.executable,
        "-m",
        "oam.resources.pdf_isolated",
        mode,
        limits.model_dump_json(),
        str(page_number),
        str(remaining_pixels),
    ]
    try:
        result = subprocess.run(
            command,
            input=original.content,
            capture_output=True,
            check=False,
            timeout=limits.pdf_child_timeout_seconds,
            env={},
            cwd="/",
        )
    except subprocess.TimeoutExpired as exc:
        raise ResourceParseError("PDF child timed out") from exc
    if result.returncode == 2:
        raise ResourceLimitError("PDF child exceeded a Resource limit")
    if result.returncode != 0:
        raise ResourceParseError("PDF child failed")
    if not result.stdout or len(result.stdout) > limits.max_bytes * PDF_CHILD_OUTPUT_MULTIPLIER:
        raise ResourceLimitError("PDF child result is empty or exceeds transfer limit")
    return result.stdout


def inspect_pdf(
    original: OriginalDescriptor, limits: ResourceConfig
) -> tuple[InspectedPdfPage, ...]:
    try:
        response = json.loads(_run_child("inspect", original, limits))
        pages = tuple(InspectedPdfPage(**item) for item in response["pages"])
    except ResourceParseError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ResourceParseError("PDF child returned invalid inspection data") from exc
    if (
        not pages
        or len(pages) > limits.max_pdf_pages
        or [page.page_number for page in pages] != list(range(1, len(pages) + 1))
    ):
        raise ResourceParseError("PDF child returned invalid page order")
    return pages


def render_page(
    original: OriginalDescriptor,
    page_number: int,
    limits: ResourceConfig,
    *,
    remaining_pixels: int,
) -> tuple[bytes, int]:
    response = _run_child(
        "render",
        original,
        limits,
        page_number=page_number,
        remaining_pixels=remaining_pixels,
    )
    if len(response) < 16:
        raise ResourceParseError("PDF child returned invalid rendered page")
    pixels = int.from_bytes(response[:8], "big")
    image = response[8:]
    if (
        not image.startswith(b"\x89PNG\r\n\x1a\n")
        or pixels <= 0
        or pixels > limits.max_pixels_per_image
        or pixels > remaining_pixels
    ):
        raise ResourceParseError("PDF child returned invalid rendered page")
    return image, pixels


def _apply_linux_limits() -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    for kind, desired in (
        (resource.RLIMIT_AS, PDF_CHILD_ADDRESS_SPACE_BYTES),
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_CORE, 0),
    ):
        _, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY:
            desired = min(desired, hard)
        resource.setrlimit(kind, (desired, desired))


def _main() -> int:
    _apply_linux_limits()
    from oam.resources.pdf import (
        native_text_usable,
        page_has_visual_structure,
        parse_pdf,
        render_pdf_page,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "render"))
    parser.add_argument("limits")
    parser.add_argument("page_number", type=int)
    parser.add_argument("remaining_pixels", type=int)
    arguments = parser.parse_args()
    limits = ResourceConfig.model_validate_json(arguments.limits)
    content = sys.stdin.buffer.read(limits.max_bytes + 1)
    if len(content) > limits.max_bytes:
        return 2
    original = OriginalDescriptor.from_bytes(ResourceMediaType.PDF, content)
    try:
        if arguments.mode == "inspect":
            parsed = parse_pdf(original, limits)
            pages = []
            for page in parsed.pages:
                usable = native_text_usable(page.text)
                pages.append(
                    {
                        "locator": page.locator,
                        "page_number": page.page_number,
                        "text": page.text if usable else "",
                        "needs_vision": not usable
                        or page_has_visual_structure(original, page.page_number),
                    }
                )
            output = json.dumps({"pages": pages}, ensure_ascii=False).encode()
        else:
            image, pixels = render_pdf_page(
                original,
                arguments.page_number,
                limits,
                remaining_pixels=arguments.remaining_pixels,
            )
            output = pixels.to_bytes(8, "big") + image
        if len(output) > limits.max_bytes * PDF_CHILD_OUTPUT_MULTIPLIER:
            return 2
        sys.stdout.buffer.write(output)
        return 0
    except ResourceLimitError:
        return 2
    except Exception:
        return 3


if __name__ == "__main__":
    raise SystemExit(_main())
