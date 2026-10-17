"""Learned-memory browsing parameters."""

from typing import Literal

MemoryType = Literal["basic", "insight"]
MemoryKind = Literal["fact", "insight"]
MEMORY_KIND_BY_TYPE: dict[MemoryType, MemoryKind] = {"basic": "fact", "insight": "insight"}
MEMORY_TYPE_BY_KIND = {kind: memory_type for memory_type, kind in MEMORY_KIND_BY_TYPE.items()}
MAX_MEMORY_PAGE_SIZE = 200
MAX_MEMORY_OFFSET = (2**63) - 1
