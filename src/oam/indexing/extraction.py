"""Source-grounded indexing outputs, separate from persistence and recall."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_serializer,
    model_validator,
)

from oam.config import OAMConfig
from oam.contracts.evidence import TemporalAnnotation
from oam.indexing.chunker import TextChunk, split_chunk_for_output_retry
from oam.models.runtime import ModelOutputTooLongError, RuntimeModels, Usage
from oam.quotes import unique_quote
from oam.tokens import UNITS_PER_TOKEN, cumulative_units, estimate_units, estimated_tokens

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Offset = Annotated[StrictInt, Field(ge=0)]
REFERENCE_ABBREVIATIONS = frozenset(
    ("dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e")
)

EXTRACTION_SYSTEM = (
    files("oam.indexing").joinpath("extraction-prompt.txt").read_text(encoding="utf-8")
)


INSIGHT_SYSTEM = files("oam.indexing").joinpath("insight-prompt.txt").read_text(encoding="utf-8")

REFERENCE_CONTEXT_INSTRUCTIONS = (
    files("oam.indexing").joinpath("extraction-reference-suffix.txt").read_text(encoding="utf-8")
)
REFERENCE_EXTRACTION_SYSTEM = EXTRACTION_SYSTEM + REFERENCE_CONTEXT_INSTRUCTIONS


class ExtractionError(ValueError):
    """An indexing envelope is invalid rather than a valid no-op."""

    def __init__(self, message: str, *, usage: Usage | None = None, retryable: bool = False):
        super().__init__(message)
        self.usage = usage
        self.retryable = retryable


class FrozenProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fact(FrozenProjection):
    output_index: Offset
    text: NonBlank
    category: Literal["fact", "preference", "experience"] = "fact"
    quote: str = Field(min_length=1)
    entities: tuple[NonBlank, ...] = ()
    caused_by: tuple[Offset, ...] = ()
    updates: tuple[Offset, ...] = ()
    annotations: tuple[TemporalAnnotation, ...] = ()
    related_existing_ids: tuple[NonBlank, ...] = ()
    passage_id: NonBlank | None = None

    @model_serializer(mode="wrap")
    def omit_absent_identifiers(self, serialize):
        output = serialize(self)
        if not self.related_existing_ids:
            output.pop("related_existing_ids", None)
        if self.passage_id is None:
            output.pop("passage_id", None)
        return output


class ExtractionDiagnostic(FrozenProjection):
    output_index: Offset
    field: str
    code: str


class FactExtraction(FrozenProjection):
    facts: tuple[Fact, ...]
    outcome: Literal["facts", "no_facts", "partial"]
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()
    usage: Usage


class InsightInput(FrozenProjection):
    id: NonBlank
    text: NonBlank
    source_refs: tuple[NonBlank, ...] = Field(min_length=1)
    mentioned_at: datetime | None = None
    temporal_annotations: tuple[TemporalAnnotation, ...] = ()


class InsightResult(FrozenProjection):
    text: NonBlank | None
    support_ids: tuple[NonBlank, ...]
    outcome: Literal["insight", "no_insight"]
    subtype: (
        Literal[
            "preferences",
            "patterns",
            "projects",
            "relationships",
            "experiences",
            "general_insights",
        ]
        | None
    )
    usage: Usage

    @model_validator(mode="after")
    def validate_subtype(self) -> InsightResult:
        if (self.outcome == "no_insight") != (self.subtype is None):
            raise ValueError("only a supported Insight has a subtype")
        return self


def model_input_tokens(system: str, envelope: dict) -> int:
    user = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    messages = json.dumps(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimated_tokens(estimate_units(messages))


def pack_insight_facts(
    facts: tuple[InsightInput, ...], config: OAMConfig
) -> tuple[InsightInput, ...]:
    packed = []
    for fact in facts[: config.extraction.max_facts_per_group]:
        candidate = [*packed, fact]
        envelope = {"facts": [item.model_dump(mode="json") for item in candidate]}
        if model_input_tokens(INSIGHT_SYSTEM, envelope) > config.extraction.max_input_tokens:
            break
        packed = candidate
    return tuple(packed)


def synthesize_insight(
    models: RuntimeModels,
    *,
    facts: tuple[InsightInput, ...],
    config: OAMConfig,
    timeout_seconds: float,
) -> InsightResult:
    identities = {fact.id for fact in facts}
    if (
        len(identities) != len(facts)
        or not 2 <= len(facts) <= config.extraction.max_facts_per_group
    ):
        raise ExtractionError("Insight input requires a bounded set of distinct supporting facts")
    body, usage = _generate_json(
        models,
        "insights",
        INSIGHT_SYSTEM,
        {"facts": [fact.model_dump(mode="json") for fact in facts]},
        config,
        timeout_seconds,
    )
    required = {"outcome", "text", "support_ids", "subtype"}
    if not required <= set(body):
        raise ExtractionError(
            "Insight output requires outcome, text, support_ids and subtype",
            usage=usage,
            retryable=True,
        )
    try:
        result = InsightResult.model_validate(
            {**{key: body[key] for key in required}, "usage": usage}
        )
    except ValidationError as exc:
        raise ExtractionError("Insight output is malformed", usage=usage, retryable=True) from exc
    if result.outcome == "no_insight":
        if result.text is not None or result.support_ids:
            raise ExtractionError(
                "no_insight cannot contain a claim or support IDs", usage=usage, retryable=True
            )
        return result
    support_ids = set(result.support_ids)
    if (
        result.text is None
        or len(support_ids) < 2
        or len(support_ids) != len(result.support_ids)
        or not support_ids <= identities
    ):
        raise ExtractionError(
            "Insight requires text and at least two distinct known supports",
            usage=usage,
            retryable=True,
        )
    return result


def extraction_envelope(
    *,
    chunk: TextChunk,
    config: OAMConfig,
    message_role: str,
    mentioned_at: datetime | None = None,
    reference_timezone: str | None = None,
) -> dict:
    if message_role not in {"user", "assistant", "system", "tool"}:
        raise ExtractionError("source role is invalid")
    if mentioned_at is not None and (
        mentioned_at.tzinfo is None or mentioned_at.utcoffset() is None
    ):
        raise ExtractionError("mention time must be timezone-aware")
    return {
        "message_role": message_role,
        "mentioned_at": mentioned_at.isoformat() if mentioned_at else None,
        "reference_timezone": reference_timezone,
        "center": {"text": chunk.text},
        "previous_context_only": chunk.previous_context,
        "max_facts": config.extraction.max_facts_per_group,
    }


def pack_reference_facts(
    references: tuple[dict, ...],
    envelope: dict,
    config: OAMConfig,
    *,
    system: str = REFERENCE_EXTRACTION_SYSTEM,
):
    selected = references[: config.extraction.reference_max_facts]
    metadata = [{**reference, "text": ""} for reference in selected]
    metadata_units = estimate_units(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
    text_units = [estimate_units(reference["text"]) for reference in selected]
    total_text_units = sum(text_units)
    available_units = max(
        0, config.extraction.reference_max_input_tokens * UNITS_PER_TOKEN - metadata_units
    )
    packed = []
    for reference, units in zip(selected, text_units, strict=True):
        preview = dict(reference)
        if total_text_units > available_units:
            target = units * available_units // total_text_units
            preview["text"] = _reference_text_prefix(reference["text"], target)
            if preview["text"] != reference["text"]:
                preview["text_truncated"] = True
        candidate = [*packed, preview]
        if (
            model_input_tokens(system, {**envelope, "reference_facts": candidate})
            > config.extraction.max_input_tokens
        ):
            break
        packed = candidate
    return packed


def _reference_text_prefix(text: str, target_units: int) -> str:
    weights = cumulative_units(text)
    boundaries = []
    for match in re.finditer(r"[。！？]|[.!?](?=\s|$)|(?<!\d)[，,]|[，,](?!\d)", text):
        if match.group() == ".":
            word = re.search(r"[A-Za-z.]+$", text[: match.start()])
            if word and (
                word.group().casefold() in REFERENCE_ABBREVIATIONS
                or len(word.group()) == 1
                or re.fullmatch(r"[A-Za-z](?:\.[A-Za-z])+", word.group())
            ):
                continue
        boundaries.append((match.end(), match.group() not in (",", "，")))
    boundaries.append((len(text), True))
    end, _ = min(
        boundaries,
        key=lambda boundary: (
            abs(weights[boundary[0]] - target_units),
            not boundary[1],
            -boundary[0],
        ),
    )
    return text if end == len(text) else text[:end].rstrip()


def extract_facts(
    models: RuntimeModels,
    *,
    chunk: TextChunk,
    config: OAMConfig,
    message_role: str,
    mentioned_at: datetime | None = None,
    reference_timezone: str | None = None,
    timeout_seconds: float,
    references: tuple[dict, ...] = (),
    check_retry_claim: Callable[[], None] | None = None,
) -> FactExtraction:
    envelope = extraction_envelope(
        chunk=chunk,
        config=config,
        message_role=message_role,
        mentioned_at=mentioned_at,
        reference_timezone=reference_timezone,
    )
    system = EXTRACTION_SYSTEM
    if config.extraction.reference_max_facts:
        system = REFERENCE_EXTRACTION_SYSTEM
        envelope["reference_facts"] = pack_reference_facts(references, envelope, config)
        if (
            not envelope["reference_facts"]
            and model_input_tokens(system, envelope) > config.extraction.max_input_tokens
        ):
            envelope.pop("reference_facts")
            system = EXTRACTION_SYSTEM
    offered_ids = {reference["id"] for reference in envelope.get("reference_facts", [])}
    try:
        body, usage = _generate_json(
            models, "extraction", system, envelope, config, timeout_seconds
        )
    except ModelOutputTooLongError as error:
        halves = split_chunk_for_output_retry(chunk, config.chunk.ingestion)
        if halves is None:
            raise
        result = retry_split_extractions(
            tuple(
                lambda half=half: extract_facts(
                    models,
                    chunk=half,
                    config=config,
                    message_role=message_role,
                    mentioned_at=mentioned_at,
                    reference_timezone=reference_timezone,
                    timeout_seconds=timeout_seconds,
                    references=references,
                    check_retry_claim=check_retry_claim,
                )
                for half in halves
            ),
            error,
            check_retry_claim,
        )
        return validate_passage_support(result, {None: chunk})
    return validate_fact_output(body, usage, config, {None: chunk}, offered_ids)


def retry_split_extractions(
    producers: tuple[Callable[[], FactExtraction], ...],
    overflow: ModelOutputTooLongError,
    check_retry_claim: Callable[[], None] | None = None,
) -> FactExtraction:
    """Retry smaller inputs in source order under the caller's existing checkpoint."""
    logging.getLogger(__name__).warning(
        "Extraction output overflow; retrying smaller source inputs"
    )
    results = []
    for produce in producers:
        if check_retry_claim is not None:
            try:
                check_retry_claim()
            except Exception as error:
                error.usage = _sum_extraction_usage(
                    [overflow.usage, *(result.usage for result in results)]
                )
                raise
        try:
            results.append(produce())
        except Exception as error:
            error.usage = _sum_extraction_usage(
                [
                    overflow.usage,
                    *(result.usage for result in results),
                    getattr(error, "usage", None),
                ]
            )
            raise
    facts, diagnostics, offset = [], [], 0
    for result in results:
        facts.extend(
            fact.model_copy(
                update={
                    "output_index": fact.output_index + offset,
                    "caused_by": tuple(target + offset for target in fact.caused_by),
                    "updates": tuple(target + offset for target in fact.updates),
                }
            )
            for fact in result.facts
        )
        diagnostics.extend(
            item.model_copy(update={"output_index": item.output_index + offset})
            for item in result.diagnostics
        )
        indices = [f.output_index for f in result.facts] + [
            d.output_index for d in result.diagnostics
        ]
        offset += max(indices, default=-1) + 1
    return FactExtraction(
        facts=tuple(facts),
        diagnostics=tuple(diagnostics),
        outcome="partial" if diagnostics else ("facts" if facts else "no_facts"),
        usage=_sum_extraction_usage([overflow.usage, *(result.usage for result in results)]),
    )


def _sum_extraction_usage(usages: list[Usage | None]) -> Usage:
    fields = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
        values = [getattr(usage, field) for usage in usages if usage is not None]
        known = [value for value in values if value is not None]
        fields[field] = sum(known) if known else None
    fields["coverage"] = (
        "complete"
        if all(usage is not None and usage.coverage == "complete" for usage in usages)
        else ("partial" if any(value is not None for value in fields.values()) else "unavailable")
    )
    return Usage.model_validate(fields)


def validate_fact_output(
    body: dict,
    usage: Usage,
    config: OAMConfig,
    chunks: dict[str | None, TextChunk],
    offered_ids: set[str],
) -> FactExtraction:
    if not {"outcome", "facts"} <= set(body) or not isinstance(body["facts"], list):
        raise ExtractionError("fact output requires outcome and facts", usage=usage, retryable=True)
    records = body["facts"]
    if body["outcome"] == "no_facts" and not records:
        return FactExtraction(facts=(), outcome="no_facts", usage=usage)
    if body["outcome"] != "facts" or not records:
        raise ExtractionError(
            "empty facts are valid only with explicit no_facts", usage=usage, retryable=True
        )
    if len(records) > config.extraction.max_facts_per_group:
        raise ExtractionError(
            "fact output exceeds configured record bound", usage=usage, retryable=True
        )
    accepted = []
    diagnostics = []
    accepted_indices: set[int] = set()
    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict) or "output_index" in record:
                raise ValueError
            raw = dict(record)
            owner = raw.get("passage_id")
            if (owner is not None and not isinstance(owner, str)) or owner not in chunks:
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="passage_id", code="invalid_passage"
                    )
                )
                continue
            chunk = chunks[owner]
            supplied_ids = raw.pop("related_existing_ids", [])
            valid_ids = (
                tuple(
                    dict.fromkeys(
                        value
                        for value in supplied_ids
                        if isinstance(value, str) and value in offered_ids
                    )
                )
                if isinstance(supplied_ids, list)
                else ()
            )
            if not isinstance(supplied_ids, list) or any(
                not isinstance(value, str) or value not in offered_ids for value in supplied_ids
            ):
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="related_existing_ids", code="invalid_reference"
                    )
                )
            if valid_ids:
                raw["related_existing_ids"] = valid_ids
            annotations = raw.pop("annotations", [])
            if not isinstance(annotations, list):
                raise ValueError
            fact = Fact.model_validate({**raw, "output_index": index})
            if not unique_quote(chunk.text, fact.quote):
                raise ValueError("support quote must be unique in its owning passage")
        except (ValueError, ValidationError):
            diagnostics.append(
                ExtractionDiagnostic(output_index=index, field="fact", code="invalid_fact")
            )
            continue
        valid_annotations = []
        for raw_annotation in annotations:
            try:
                if not isinstance(raw_annotation, dict):
                    raise ValueError
                annotation = TemporalAnnotation.model_validate(raw_annotation)
                if not unique_quote(fact.quote, annotation.quote):
                    raise ValueError("annotation quote must be unique within fact support")
                valid_annotations.append(annotation)
            except (ValueError, ValidationError):
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="annotations", code="invalid_annotation"
                    )
                )
        relations = {}
        for field in ("caused_by", "updates"):
            supplied = getattr(fact, field)
            valid = tuple(
                dict.fromkeys(target for target in supplied if target in accepted_indices)
            )
            if len(valid) != len(supplied):
                diagnostics.append(
                    ExtractionDiagnostic(output_index=index, field=field, code="invalid_relation")
                )
            relations[field] = valid
        accepted.append(
            fact.model_copy(update={**relations, "annotations": tuple(valid_annotations)})
        )
        accepted_indices.add(index)
    return FactExtraction(
        facts=tuple(accepted),
        outcome="partial" if diagnostics else "facts",
        diagnostics=tuple(diagnostics),
        usage=usage,
    )


def validate_passage_support(
    result: FactExtraction, chunks: dict[str | None, TextChunk]
) -> FactExtraction:
    """Recheck split results against their canonical passage rather than a retry child."""
    accepted = []
    indices = set()
    diagnostics = list(result.diagnostics)
    for fact in result.facts:
        chunk = chunks[fact.passage_id]
        if not unique_quote(chunk.text, fact.quote):
            diagnostics.append(
                ExtractionDiagnostic(
                    output_index=fact.output_index, field="fact", code="invalid_fact"
                )
            )
            continue
        relations = {}
        for field in ("caused_by", "updates"):
            supplied = getattr(fact, field)
            relations[field] = tuple(target for target in supplied if target in indices)
            if relations[field] != supplied:
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=fact.output_index, field=field, code="invalid_relation"
                    )
                )
        accepted.append(fact.model_copy(update=relations))
        indices.add(fact.output_index)
    return result.model_copy(
        update={
            "facts": tuple(accepted),
            "diagnostics": tuple(diagnostics),
            "outcome": "partial" if diagnostics else result.outcome,
        }
    )


def _generate_json(
    models: RuntimeModels,
    role: str,
    system: str,
    envelope: dict,
    config: OAMConfig,
    timeout_seconds: float,
) -> tuple[dict, Usage]:
    user = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if model_input_tokens(system, envelope) > config.extraction.max_input_tokens:
        raise ExtractionError("complete extraction input exceeds configured token bound")
    result = models.generate(
        role,
        system=system,
        user=user,
        max_output_tokens=config.extraction.max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    try:
        body = json.loads(result.content)
    except ValueError as exc:
        raise ExtractionError(
            "extraction output is not valid JSON", usage=result.usage, retryable=True
        ) from exc
    if not isinstance(body, dict):
        raise ExtractionError(
            "extraction output must be a JSON object", usage=result.usage, retryable=True
        )
    return body, result.usage
