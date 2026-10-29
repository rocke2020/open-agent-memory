"""Structural chunks with exclusive source spans and bounded context-only tails."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass

from oam.config import IngestionChunkConfig
from oam.text_boundaries import punctuation_ends
from oam.tokens import UNITS_PER_TOKEN, cumulative_units, estimated_tokens

# Portions Copyright (c) 2025 Vectorize AI, Inc.
# MIT license: third_party_licenses/extraction-split-MIT.txt.
MIN_OUTPUT_SPLIT_CHARS = 500
_PARAGRAPH_END = re.compile(r"\n[ \t\r]*\n(?:[ \t\r]*\n)*")


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
    """Pack lossless structural units at original offsets under a hard token bound."""
    if config.extractable_overlap_estimated_tokens:
        raise ValueError("owned source overlap must remain zero")
    if not text:
        return ()
    weights = cumulative_units(text)
    if any(
        weights[index + 1] - weights[index] > config.max_estimated_tokens * UNITS_PER_TOKEN
        for index in range(len(text))
    ):
        raise ValueError("single source character exceeds chunk hard maximum")
    paragraphs = [match.end() for match in _PARAGRAPH_END.finditer(text)]
    if not paragraphs or paragraphs[-1] != len(text):
        paragraphs.append(len(text))
    sentences, clauses = punctuation_ends(text)
    levels = (sentences, clauses, [match.end() for match in re.finditer(r"\s+", text)])
    hard_units = config.max_estimated_tokens * UNITS_PER_TOKEN

    def bounded_units(start: int, end: int, level: int) -> Iterator[tuple[int, int]]:
        if start == end:
            return
        if weights[end] - weights[start] <= hard_units:
            yield start, end
        elif level < len(levels):
            boundaries = levels[level]
            for stop in boundaries[bisect_right(boundaries, start) : bisect_left(boundaries, end)]:
                yield from bounded_units(start, stop, level + 1)
                start = stop
            yield from bounded_units(start, end, level + 1)
        else:
            while weights[end] - weights[start] > hard_units:
                stop = max(start + 1, _bounded_end(weights, start, config.target_estimated_tokens))
                yield start, stop
                start = stop
            if start < end:
                yield start, end

    chunks = []
    context = _tail(previous_context, config.previous_context_estimated_tokens)

    def emit(start: int, end: int) -> None:
        nonlocal context
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

    paragraph_start = chunk_start = chunk_end = 0
    for paragraph_end in paragraphs:
        for start, end in bounded_units(paragraph_start, paragraph_end, 0):
            if chunk_end > chunk_start and weights[end] - weights[chunk_start] > hard_units:
                emit(chunk_start, chunk_end)
                chunk_start = start
            chunk_end = end
            if (
                weights[chunk_end] - weights[chunk_start]
                >= config.target_estimated_tokens * UNITS_PER_TOKEN
            ):
                emit(chunk_start, chunk_end)
                chunk_start = chunk_end
        paragraph_start = paragraph_end
    if chunk_end > chunk_start:
        emit(chunk_start, chunk_end)
    return tuple(chunks)


def resume_chunks(
    text: str,
    published: list[str],
    config: IngestionChunkConfig,
    *,
    previous_context: str = "",
) -> tuple[TextChunk, ...]:
    """Keep published passage boundaries and chunk only the unprocessed suffix."""
    chunks = []
    offset = 0
    for content in published:
        start = text.find(content, offset)
        if not content or start < 0 or text[offset:start].strip():
            raise ValueError("published passage does not match the source prefix")
        end = start + len(content)
        chunks.append(
            TextChunk(
                text=content,
                start=start,
                end=end,
                previous_context=_tail(
                    previous_context + text[:start], config.previous_context_estimated_tokens
                ),
                estimated_tokens=estimated_tokens(cumulative_units(content)[-1]),
            )
        )
        offset = end
    remaining = chunk_text(text[offset:], config, previous_context=previous_context + text[:offset])
    chunks.extend(
        TextChunk(
            text=chunk.text,
            start=offset + chunk.start,
            end=offset + chunk.end,
            previous_context=chunk.previous_context,
            estimated_tokens=chunk.estimated_tokens,
        )
        for chunk in remaining
    )
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
    sentences, clauses = punctuation_ends(text)
    levels = (
        [match.end() for match in _PARAGRAPH_END.finditer(text)],
        sentences,
        clauses,
        [match.end() for match in re.finditer(r"\s+", text)],
    )
    first_nonblank = len(text) - len(text.lstrip())
    last_nonblank = len(text.rstrip())
    best_split = mid_point
    for boundaries in levels:
        candidates = []
        for point in boundaries:
            while point < len(text) and text[point].isspace():
                point += 1
            if search_start <= point <= search_end and first_nonblank < point < last_nonblank:
                candidates.append(point)
        if candidates:
            best_split = min(candidates, key=lambda point: (abs(point - mid_point), point))
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
