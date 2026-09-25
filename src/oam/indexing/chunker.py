"""Structural chunks with exclusive source spans and bounded context-only tails."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from oam.config import ChunkConfig
from oam.tokens import UNITS_PER_TOKEN, cumulative_units, estimated_tokens


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    start: int
    end: int
    previous_context: str
    estimated_tokens: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start or len(self.text) != self.end - self.start:
            raise ValueError("chunk text must exactly occupy its source character span")


def chunk_text(
    text: str, config: ChunkConfig, *, previous_context: str = ""
) -> tuple[TextChunk, ...]:
    """Chunk one already-eligible message; the caller owns cross-message eligibility."""
    if config.extractable_overlap_estimated_tokens:
        raise ValueError("owned source overlap must remain zero")
    if not text.strip():
        return ()
    weights = cumulative_units(text)
    if any(
        weights[index + 1] - weights[index] > config.max_estimated_tokens * UNITS_PER_TOKEN
        for index in range(len(text))
    ):
        raise ValueError("single source character exceeds chunk hard maximum")
    boundaries = [match.end() for match in re.finditer(r"\n", text)]
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))
    chunks = []
    start = 0
    context = _tail(previous_context, config.previous_context_estimated_tokens)
    while start < len(text):
        target = max(start + 1, _bounded_end(weights, start, config.target_estimated_tokens))
        maximum = _bounded_end(weights, start, config.max_estimated_tokens)
        boundary_index = bisect_right(boundaries, target) - 1
        if boundary_index >= 0 and boundaries[boundary_index] > start:
            end = boundaries[boundary_index]
        else:
            next_boundary = boundaries[bisect_right(boundaries, start)]
            if next_boundary <= maximum:
                end = next_boundary
            else:
                spaces = tuple(re.finditer(r"\s+", text[start:target]))
                end = start + spaces[-1].end() if spaces else target
        content = text[start:end]
        chunks.append(
            TextChunk(
                text=content,
                start=start,
                end=end,
                previous_context=context,
                estimated_tokens=estimated_tokens(weights[end] - weights[start]),
            )
        )
        context = _tail(context + content, config.previous_context_estimated_tokens)
        start = end
    return tuple(chunks)


def _bounded_end(weights: list[int], start: int, tokens: int) -> int:
    return bisect_right(weights, weights[start] + tokens * UNITS_PER_TOKEN) - 1


def _tail(text: str, tokens: int) -> str:
    weights = cumulative_units(text)
    start = bisect_left(weights, weights[-1] - tokens * UNITS_PER_TOKEN)
    return text[start:]
