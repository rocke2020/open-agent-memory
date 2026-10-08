from __future__ import annotations

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
PDF_VISION_SCALE = 2
MAJOR_IMAGE_PAGE_FRACTION = 0.25
TABLE_LINE_LENGTH_FRACTION = 0.08
CORRUPT_TEXT_FRACTION = 0.1


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
    return value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def native_text_usable(text: str) -> bool:
    """Reject empty or visibly damaged text layers before selecting native extraction."""
    return bool(text.strip()) and (
        sum(
            character == "\ufffd" or (ord(character) < 32 and character not in "\n\r\t")
            for character in text
        )
        / len(text)
        < CORRUPT_TEXT_FRACTION
    )


def render_pdf_page(
    original: OriginalDescriptor,
    page_number: int,
    limits: ResourceConfig,
    *,
    remaining_pixels: int,
) -> tuple[bytes, int]:
    """Render one bounded physical page for document vision."""
    import pypdfium2 as pdfium

    if original.media_type is not ResourceMediaType.PDF:
        raise ResourceParseError("PDF renderer requires application/pdf original bytes")
    try:
        with pdfium.PdfDocument(original.content) as document:
            if not 1 <= page_number <= len(document):
                raise ResourceParseError("PDF page number is out of range")
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                pixels = round(width * PDF_VISION_SCALE) * round(height * PDF_VISION_SCALE)
                if pixels > limits.max_pixels_per_image or pixels > remaining_pixels:
                    raise ResourceLimitError("PDF rendered page exceeds Resource pixel limit")
                bitmap = page.render(scale=PDF_VISION_SCALE)
                try:
                    output = BytesIO()
                    bitmap.to_pil().save(output, format="PNG")
                    return output.getvalue(), pixels
                finally:
                    bitmap.close()
            finally:
                page.close()
    except (ResourceParseError, ResourceLimitError):
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise ResourceParseError("PDF page rendering failed") from exc


def page_has_visual_structure(original: OriginalDescriptor, page_number: int) -> bool:
    """Select text pages with a large image or a vector-drawn table grid."""
    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(original.content) as document:
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                page_area = width * height
                for item in page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,)):
                    left, bottom, right, top = item.get_bounds()
                    image_area = abs((right - left) * (top - bottom))
                    if page_area and image_area / page_area >= MAJOR_IMAGE_PAGE_FRACTION:
                        return True
                horizontal_lines = 0
                vertical_lines = 0
                for item in page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_PATH,)):
                    left, bottom, right, top = item.get_bounds()
                    line_width = abs(right - left)
                    line_height = abs(top - bottom)
                    if line_width >= width * TABLE_LINE_LENGTH_FRACTION and line_height <= 3:
                        horizontal_lines += 1
                    if line_height >= height * TABLE_LINE_LENGTH_FRACTION and line_width <= 3:
                        vertical_lines += 1
                return horizontal_lines >= 3 and vertical_lines >= 2
            finally:
                page.close()
    except (RuntimeError, ValueError, OSError) as exc:
        raise ResourceParseError("PDF page inspection failed") from exc
