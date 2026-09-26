from __future__ import annotations

import re

from bs4 import BeautifulSoup

from oam.config import ResourceConfig
from oam.contracts.resources import ResourceMediaType
from oam.resources.limits import enforce_byte_limit
from oam.resources.originals import OriginalDescriptor
from oam.resources.types import HtmlParseResult, ParsedBlock, ResourceParseError

_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "caption", "tr")


def parse_html(original: OriginalDescriptor, limits: ResourceConfig) -> HtmlParseResult:
    if original.media_type is not ResourceMediaType.HTML:
        raise ResourceParseError("HTML parser requires text/html original bytes")
    enforce_byte_limit(original, limits)
    soup = BeautifulSoup(original.content, "html.parser")
    for hidden in soup.find_all(("script", "style", "noscript", "template")):
        hidden.decompose()
    title = _normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    heading = None
    blocks = []
    for element in soup.find_all(_BLOCK_TAGS):
        text = _normalize_text(element.get_text(" ", strip=True))
        if text:
            if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                heading = text
            table = element.find_parent("table")
            caption_element = table.find("caption") if table else None
            caption = (
                _normalize_text(caption_element.get_text(" ", strip=True))
                if caption_element
                else None
            )
            blocks.append(
                ParsedBlock(
                    locator=f"html:block:{len(blocks) + 1:04d}",
                    text=text,
                    title=title,
                    heading=heading,
                    caption=caption,
                )
            )
    if not blocks:
        fallback = _normalize_text(soup.get_text(" ", strip=True))
        if fallback:
            blocks.append(ParsedBlock(locator="html:block:0001", text=fallback, title=title))
    if not blocks:
        raise ResourceParseError("HTML Resource contains no extractable text")
    return HtmlParseResult(blocks=tuple(blocks))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
