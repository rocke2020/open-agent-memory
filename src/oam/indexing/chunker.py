"""Structural chunks with exclusive source spans and bounded context-only tails."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from oam.config import IngestionChunkConfig
from oam.tokens import UNITS_PER_TOKEN, cumulative_units, estimated_tokens

# Portions Copyright (c) 2025 Vectorize AI, Inc.
# MIT license: third_party_licenses/extraction-split-MIT.txt.
MIN_OUTPUT_SPLIT_CHARS = 500


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
    text: str, config: IngestionChunkConfig, *, previous_context: str = ""
) -> tuple[TextChunk, ...]:
    """Chunk eligible text at original offsets, omitting whitespace-only source spans."""
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
        if content.strip():
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


def split_chunk_for_output_retry(
    chunk: TextChunk, config: IngestionChunkConfig
) -> tuple[TextChunk, TextChunk] | None:
    """Halve an overflowing source span, preferring nearby sentence boundaries."""
    text = chunk.text
    if len(text.strip()) <= MIN_OUTPUT_SPLIT_CHARS:
        return None
    mid_point = len(text) // 2
    search_range = int(len(text) * 0.2)
    search_start = max(0, mid_point - search_range)
    search_end = min(len(text), mid_point + search_range)
    best_split = mid_point
    for ending in [". ", "! ", "? ", "\n\n"]:
        pos = text.rfind(ending, search_start, search_end)
        if pos != -1:
            best_split = pos + len(ending)
            break
    first_half, second_half = text[:best_split], text[best_split:]
    if not first_half.strip() or not second_half.strip():
        return None
    return (
        TextChunk(
            first_half,
            chunk.start,
            chunk.start + best_split,
            chunk.previous_context,
            estimated_tokens(cumulative_units(first_half)[-1]),
        ),
        TextChunk(
            second_half,
            chunk.start + best_split,
            chunk.end,
            _tail(chunk.previous_context + first_half, config.previous_context_estimated_tokens),
            estimated_tokens(cumulative_units(second_half)[-1]),
        ),
    )


def _tail(text: str, tokens: int) -> str:
    weights = cumulative_units(text)
    start = bisect_left(weights, weights[-1] - tokens * UNITS_PER_TOKEN)
    return text[start:]
