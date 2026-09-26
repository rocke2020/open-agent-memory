from __future__ import annotations

from dataclasses import dataclass


class ResourceParseError(ValueError):
    """A Resource cannot produce a valid local parse."""


class ResourceLimitError(ResourceParseError):
    """A Resource exceeds an admitted v0.0.1 processing limit."""


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    locator: str
    text: str
    title: str | None = None
    heading: str | None = None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlParseResult:
    blocks: tuple[ParsedBlock, ...]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks)


@dataclass(frozen=True, slots=True)
class PageRenderingMetadata:
    width_pixels: int
    height_pixels: int
    dpi: int


@dataclass(frozen=True, slots=True)
class ParsedPdfPage:
    locator: str
    page_number: int
    text: str
    rendering: PageRenderingMetadata | None


@dataclass(frozen=True, slots=True)
class PdfParseResult:
    page_count: int
    pages: tuple[ParsedPdfPage, ...]


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    locator: str
    format: str
    width: int
    height: int
    pixels: int


@dataclass(frozen=True, slots=True)
class ImageInspection:
    images: tuple[ImageMetadata, ...]
    total_pixels: int
