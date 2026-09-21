from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol

EmbeddingVector = tuple[float, ...]


class EmbeddingClient(Protocol):
    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicFakeEmbeddingClient:
    """Offline fake for tests; it never represents a production model binding."""

    dimensions: int = 8

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")

    def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> EmbeddingVector:
        digest = hashlib.shake_256(text.encode("utf-8")).digest(self.dimensions * 8)
        maximum = (1 << 64) - 1
        values = tuple(
            (int.from_bytes(digest[offset : offset + 8], "big") / maximum) * 2.0 - 1.0
            for offset in range(0, len(digest), 8)
        )
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            return (1.0,) + (0.0,) * (self.dimensions - 1)
        return tuple(value / norm for value in values)
