from __future__ import annotations

import re
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import enforce_byte_limit
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import (
    PageRenderingMetadata,
    ParsedPdfPage,
    PdfParseResult,
    ResourceLimitError,
    ResourceParseError,
)

_RENDER_DPI = 72


def parse_pdf(
    original: OriginalDescriptor,
    limits: ResourceConfig,
    *,
    include_render_metadata: bool = False,
) -> PdfParseResult:
    if original.media_type is not ResourceMediaType.PDF:
        raise ResourceParseError("PDF parser requires application/pdf original bytes")
    enforce_byte_limit(original, limits)
    try:
        reader = PdfReader(BytesIO(original.content), strict=False)
    except (PdfReadError, OSError, ValueError) as exc:
        raise ResourceParseError("PDF Resource is malformed or unreadable") from exc
    if reader.is_encrypted:
        raise ResourceParseError("PDF Resource is encrypted or password-required")
    page_count = len(reader.pages)
    if page_count > limits.max_pdf_pages:
        raise ResourceLimitError(f"PDF page limit is {limits.max_pdf_pages}; received {page_count}")
    if page_count == 0:
        raise ResourceParseError("PDF Resource contains no pages")

    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = _normalize_text(page.extract_text() or "")
        rendering = None
        if include_render_metadata:
            rendering = PageRenderingMetadata(
                width_pixels=round(float(page.mediabox.width) * _RENDER_DPI / 72),
                height_pixels=round(float(page.mediabox.height) * _RENDER_DPI / 72),
                dpi=_RENDER_DPI,
            )
        pages.append(
            ParsedPdfPage(
                locator=f"pdf:page:{page_number:04d}",
                page_number=page_number,
                text=text,
                rendering=rendering,
            )
        )
    return PdfParseResult(page_count=page_count, pages=tuple(pages))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
