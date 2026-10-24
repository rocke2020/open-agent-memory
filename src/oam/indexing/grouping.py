"""Source-ordered bounded message groups with explicit passage support ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from importlib.resources import files
from typing import cast

from oam.config import OAMConfig
from oam.indexing.chunker import TextChunk, split_chunk_for_output_retry
from oam.indexing.extraction import (
    ExtractionInputTooLongError,
    _generate_json,
    extraction_envelope,
    extraction_system,
    model_input_tokens,
    pack_reference_facts,
    retry_split_extractions,
    validate_fact_output,
    validate_passage_support,
)
from oam.models.runtime import ModelOutputTooLongError

# The output repeats source claims as text and quote and adds ownership/qualification fields.
GROUP_OUTPUT_TEXT_MULTIPLIER = 3
GROUP_OUTPUT_PASSAGE_TOKENS = 128
GROUP_CONTEXT_INSTRUCTIONS = (
    files("oam.indexing").joinpath("extraction-group-prefix.txt").read_text(encoding="utf-8")
)


@dataclass(frozen=True)
class ExtractionPassage:
    passage_id: str
    chunk: TextChunk
    role: str
    mentioned_at: datetime
    message_index: int


def group_system(config: OAMConfig) -> str:
    return GROUP_CONTEXT_INSTRUCTIONS + extraction_system(
        config, with_references=bool(config.extraction.reference_max_facts)
    )


def group_envelope(
    passages: tuple[ExtractionPassage, ...],
    config: OAMConfig,
    reference_timezone: str | None = None,
) -> dict:
    centers = []
    for index, passage in enumerate(passages):
        envelope = extraction_envelope(
            chunk=passage.chunk,
            config=config,
            message_role=passage.role,
            mentioned_at=passage.mentioned_at,
            reference_timezone=reference_timezone,
        )
        centers.append(
            {
                "passage_id": f"p{index}",
                "message_index": passage.message_index,
                "message_role": passage.role,
                "mentioned_at": envelope["mentioned_at"],
                "center": envelope["center"],
            }
        )
    return {
        "passages": centers,
        "previous_context_only": passages[0].chunk.previous_context,
        "reference_timezone": reference_timezone,
        "max_facts": config.extraction.max_facts_per_group,
    }


def plan_extraction_groups(
    passages: tuple[ExtractionPassage, ...],
    config: OAMConfig,
    reference_timezone: str | None = None,
) -> tuple[tuple[ExtractionPassage, ...], ...]:
    groups = []
    current: list[ExtractionPassage] = []
    for passage in passages:
        proposed = tuple([*current, passage])
        expected_output = (
            sum(p.chunk.estimated_tokens for p in proposed) * GROUP_OUTPUT_TEXT_MULTIPLIER
        )
        expected_output += len(proposed) * GROUP_OUTPUT_PASSAGE_TOKENS
        envelope = group_envelope(proposed, config, reference_timezone)
        if config.extraction.reference_max_facts:
            envelope["reference_facts"] = []
        if current and (
            passage.message_index > current[-1].message_index + 1
            or len(proposed)
            > min(config.extraction.group_max_passages, config.extraction.max_facts_per_group)
            or model_input_tokens(group_system(config), envelope)
            > config.extraction.max_input_tokens
            or expected_output > config.extraction.group_max_estimated_output_tokens
        ):
            groups.append(tuple(current))
            current = []
        current.append(passage)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def extract_group(
    models,
    *,
    passages: tuple[ExtractionPassage, ...],
    config: OAMConfig,
    timeout_seconds: float,
    reference_timezone: str | None = None,
    references: tuple[dict, ...] = (),
    check_retry_claim: Callable[[], None] | None = None,
):
    envelope = group_envelope(passages, config, reference_timezone)
    system = group_system(config)
    if config.extraction.reference_max_facts:
        envelope["reference_facts"] = pack_reference_facts(
            references, envelope, config, system=system
        )
    try:
        body, usage = _generate_json(
            models, "extraction", system, envelope, config, timeout_seconds
        )
    except (ModelOutputTooLongError, ExtractionInputTooLongError) as error:
        halves: tuple[tuple[ExtractionPassage, ...], ...]
        if len(passages) > 1:
            midpoint = len(passages) // 2
            halves = (passages[:midpoint], passages[midpoint:])
        else:
            chunks = split_chunk_for_output_retry(passages[0].chunk, config.chunk.ingestion)
            if chunks is None:
                raise
            halves = tuple((replace(passages[0], chunk=chunk),) for chunk in chunks)
        result = retry_split_extractions(
            tuple(
                partial(
                    extract_group,
                    models,
                    passages=half,
                    config=config,
                    timeout_seconds=timeout_seconds,
                    reference_timezone=reference_timezone,
                    references=references,
                    check_retry_claim=check_retry_claim,
                )
                for half in halves
            ),
            error,
            check_retry_claim,
        )
        return validate_passage_support(result, {p.passage_id: p.chunk for p in passages})
    passages_by_alias = {f"p{index}": passage for index, passage in enumerate(passages)}
    result = validate_fact_output(
        body,
        usage,
        config,
        {alias: passage.chunk for alias, passage in passages_by_alias.items()},
        {ref["id"] for ref in envelope.get("reference_facts", [])},
    )
    return result.model_copy(
        update={
            "facts": tuple(
                fact.model_copy(
                    update={"passage_id": passages_by_alias[cast(str, fact.passage_id)].passage_id}
                )
                for fact in result.facts
            )
        }
    )
