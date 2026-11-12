"""Learned-memory browsing parameters."""

from typing import Literal

MemoryType = Literal["basic", "insight"]
MemoryKind = Literal["fact", "insight"]
MEMORY_KIND_BY_TYPE: dict[MemoryType, MemoryKind] = {"basic": "fact", "insight": "insight"}
MEMORY_TYPE_BY_KIND = {kind: memory_type for memory_type, kind in MEMORY_KIND_BY_TYPE.items()}
MAX_MEMORY_PAGE_SIZE = 200
MAX_MEMORY_OFFSET = (2**63) - 1
MAX_INSIGHT_TITLE_CHARS = 120


def insight_title(locator: dict, text: str) -> str:
    """Read a saved title, with bounded compatibility for older records."""
    title = locator.get("title")
    if isinstance(title, str) and title.strip() and len(title) <= MAX_INSIGHT_TITLE_CHARS:
        return title
    label = locator.get("retrieval_text")
    value = label if isinstance(label, str) and label.strip() else text
    return " ".join(value.split())[:MAX_INSIGHT_TITLE_CHARS] or "Insight"
