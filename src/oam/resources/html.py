from __future__ import annotations

import re

from bs4 import BeautifulSoup

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import enforce_byte_limit
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import HtmlParseResult, ParsedBlock, ResourceParseError

_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr")


def parse_html(original: OriginalDescriptor, limits: ResourceConfig) -> HtmlParseResult:
    if original.media_type is not ResourceMediaType.HTML:
        raise ResourceParseError("HTML parser requires text/html original bytes")
    enforce_byte_limit(original, limits)
    soup = BeautifulSoup(original.content, "html.parser")
    for hidden in soup.find_all(("script", "style", "noscript", "template")):
        hidden.decompose()
    text_blocks = []
    for element in soup.find_all(_BLOCK_TAGS):
        text = _normalize_text(element.get_text(" ", strip=True))
        if text:
            text_blocks.append(text)
    if not text_blocks:
        fallback = _normalize_text(soup.get_text(" ", strip=True))
        if fallback:
            text_blocks.append(fallback)
    if not text_blocks:
        raise ResourceParseError("HTML Resource contains no extractable text")
    return HtmlParseResult(
        blocks=tuple(
            ParsedBlock(locator=f"html:block:{index:04d}", text=text)
            for index, text in enumerate(text_blocks, start=1)
        )
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
