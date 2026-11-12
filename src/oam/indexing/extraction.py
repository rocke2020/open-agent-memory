"""Source-grounded indexing outputs, separate from persistence and recall."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Literal

import jieba
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
from oam.contracts.evidence import MessageRole, TemporalAnnotation
from oam.contracts.memory import MAX_INSIGHT_TITLE_CHARS, insight_title
from oam.indexing.chunker import TextChunk, split_chunk_for_output_retry
from oam.indexing.entity_names import entity_name_key
from oam.models.runtime import ModelOutputTooLongError, RuntimeModels, Usage
from oam.quotes import unique_quote
from oam.tokens import UNITS_PER_TOKEN, cumulative_units, estimate_units, estimated_tokens

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
InsightTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_INSIGHT_TITLE_CHARS)
]
Offset = Annotated[StrictInt, Field(ge=0)]
InsightSubtype = Literal[
    "preferences", "patterns", "projects", "relationships", "experiences", "general_insights"
]
REFERENCE_ABBREVIATIONS = frozenset(
    ("dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e")
)

EXTRACTION_SYSTEM = (
    files("oam.indexing").joinpath("extraction-prompt.txt").read_text(encoding="utf-8")
)


INSIGHT_SYSTEM = files("oam.indexing").joinpath("insight-prompt.txt").read_text(encoding="utf-8")
INCREMENTAL_INSIGHT_SYSTEM = (
    files("oam.indexing").joinpath("incremental-insight-prompt.txt").read_text(encoding="utf-8")
)

REFERENCE_CONTEXT_INSTRUCTIONS = (
    files("oam.indexing").joinpath("extraction-reference-suffix.txt").read_text(encoding="utf-8")
)
REFERENCE_EXTRACTION_SYSTEM = EXTRACTION_SYSTEM + REFERENCE_CONTEXT_INSTRUCTIONS
CAUSAL_EXTRACTION_INSTRUCTIONS = (
    files("oam.indexing").joinpath("extraction-causal-suffix.txt").read_text(encoding="utf-8")
)


def extraction_system(config: OAMConfig, *, with_references: bool = False) -> str:
    system = EXTRACTION_SYSTEM
    if config.index.enable_causal_graph:
        system += CAUSAL_EXTRACTION_INSTRUCTIONS
    if with_references:
        system += REFERENCE_CONTEXT_INSTRUCTIONS
    return system


class ExtractionError(ValueError):
    """An indexing envelope is invalid rather than a valid no-op."""

    def __init__(self, message: str, *, usage: Usage | None = None, retryable: bool = False):
        super().__init__(message)
        self.usage = usage
        self.retryable = retryable


class ExtractionInputTooLongError(ExtractionError):
    """The serialized input exceeds the local bound before model dispatch."""


class FrozenProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityAlias(FrozenProjection):
    canonical_name: str = Field(strict=True, min_length=1)
    alias: str = Field(strict=True, min_length=1)
    quote: str = Field(strict=True, min_length=1)
    object: NonBlank | None = None
    canonical_occurrence: Annotated[StrictInt, Field(ge=1)] = 1
    alias_occurrence: Annotated[StrictInt, Field(ge=1)] = 1

    @model_serializer(mode="wrap")
    def omit_absent_object(self, serialize):
        result = serialize(self)
        if self.object is None:
            for key in ("object", "canonical_occurrence", "alias_occurrence"):
                result.pop(key, None)
        return result


class EntityMention(FrozenProjection):
    name: NonBlank
    quote: NonBlank
    object: NonBlank
    decision: Literal["same", "different", "new", "unresolved"]
    reference_id: NonBlank | None = None
    reference_name_id: NonBlank | None = None
    different_from: tuple[NonBlank, ...] = ()
    occurrence: Annotated[StrictInt, Field(ge=1)] = 1
    span: tuple[Offset, Offset] | None = None


class EntityReference(FrozenProjection):
    name: str = Field(strict=True, min_length=1)
    reference_id: str = Field(strict=True, min_length=1)
    quote: str = Field(strict=True, min_length=1)


class Fact(FrozenProjection):
    output_index: Offset
    text: NonBlank
    category: Literal["fact", "preference", "experience"] = "fact"
    quote: str = Field(min_length=1)
    entities: tuple[NonBlank, ...] = ()
    entity_aliases: tuple[EntityAlias, ...] = ()
    entity_references: tuple[EntityReference, ...] = ()
    entity_mentions: tuple[EntityMention, ...] | None = None
    caused_by: tuple[Offset, ...] = ()
    updates: tuple[Offset, ...] = ()
    annotations: tuple[TemporalAnnotation, ...] = ()
    related_existing_ids: tuple[NonBlank, ...] = ()
    passage_id: NonBlank | None = None

    @model_serializer(mode="wrap")
    def omit_absent_identifiers(self, serialize):
        output = serialize(self)
        if not self.entity_aliases:
            output.pop("entity_aliases", None)
        if not self.entity_references:
            output.pop("entity_references", None)
        if self.entity_mentions is None:
            output.pop("entity_mentions", None)
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
    entity_candidates: tuple[dict[str, str], ...] = ()

    @model_serializer(mode="wrap")
    def omit_absent_candidates(self, serialize):
        result = serialize(self)
        if not self.entity_candidates:
            result.pop("entity_candidates", None)
        return result


class InsightInput(FrozenProjection):
    id: NonBlank
    text: NonBlank
    source_refs: tuple[NonBlank, ...] = Field(min_length=1)
    mentioned_at: datetime | None = None
    temporal_annotations: tuple[TemporalAnnotation, ...] = ()
    message_role: MessageRole | None = None


class InsightResult(FrozenProjection):
    text: NonBlank | None
    title: InsightTitle | None = None
    retrieval_text: NonBlank | None = None
    support_ids: tuple[NonBlank, ...]
    outcome: Literal["insight", "no_insight"]
    subtype: InsightSubtype | None
    usage: Usage

    @property
    def embedding_text(self) -> str | None:
        return self.retrieval_text if self.retrieval_text is not None else self.text

    @model_validator(mode="after")
    def validate_subtype(self) -> InsightResult:
        if (self.outcome == "no_insight") != (self.subtype is None):
            raise ValueError("only a supported Insight has a subtype")
        if self.outcome == "no_insight" and self.retrieval_text is not None:
            raise ValueError("only a supported Insight has retrieval text")
        if (self.outcome == "insight") != (self.title is not None):
            raise ValueError("every supported Insight requires a title")
        return self


class InsightDecision(FrozenProjection):
    """Model decisions contain semantic handles, never publication revisions."""

    action: Literal["UPDATE", "CREATE", "KEEP_TEXT", "NO_INSIGHT"]
    target_id: NonBlank | None = None
    text: NonBlank | None = None
    title: InsightTitle | None = None
    retrieval_text: NonBlank | None = None
    subtype: InsightSubtype | None = None
    support_ids: tuple[NonBlank, ...] = ()
    seed_ids: tuple[NonBlank, ...] = Field(min_length=1)


class InsightAction(FrozenProjection):
    """Validated backend outcome, bound to captured state before checkpointing."""

    action: Literal["UPDATE", "CREATE", "NO_CHANGE", "NO_INSIGHT"]
    target_id: NonBlank | None = None
    expected_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    # Captured text and labels must survive support-only updates byte for byte.
    text: str | None = None
    title: str | None = None
    retrieval_text: str | None = None
    subtype: InsightSubtype | None = None
    support_ids: tuple[NonBlank, ...] = ()
    seed_ids: tuple[NonBlank, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_title(self) -> InsightAction:
        if self.action in {"CREATE", "UPDATE"} and (
            not isinstance(self.title, str)
            or not self.title.strip()
            or len(self.title) > MAX_INSIGHT_TITLE_CHARS
        ):
            raise ValueError("every published Insight requires a concise title")
        return self


class InsightDecisions(FrozenProjection):
    actions: tuple[InsightDecision, ...] = Field(min_length=1)


class ConsolidationResult(FrozenProjection):
    actions: tuple[InsightAction, ...] = Field(min_length=1)
    model_actions: tuple[Literal["UPDATE", "CREATE", "KEEP_TEXT", "NO_INSIGHT"], ...] = ()
    usage: Usage


def insight_fact_projection(fact: InsightInput) -> dict:
    return fact.model_dump(
        mode="json", exclude={"source_refs"}, exclude_none=True, exclude_defaults=True
    )


def insight_annotations(values) -> tuple[TemporalAnnotation, ...]:
    return tuple(
        TemporalAnnotation.model_validate(
            {key: value[key] for key in TemporalAnnotation.model_fields if key in value}
        )
        for value in values or ()
    )


def consolidation_envelope(facts, seeds, targets) -> dict:
    return {
        "facts": [insight_fact_projection(fact) for fact in facts],
        "seed_ids": list(seeds),
        "targets": [
            {
                "id": target["id"],
                "text": target["text"],
                "title": insight_title(target, target["text"]),
                **(
                    {"retrieval_text": target["retrieval_text"]}
                    if target.get("retrieval_text")
                    else {}
                ),
            }
            for target in targets
        ],
    }


def consolidate_insights(
    models: RuntimeModels,
    *,
    facts: tuple[InsightInput, ...],
    seeds: tuple[str, ...],
    targets: tuple[dict, ...],
    config: OAMConfig,
    timeout_seconds: float,
) -> ConsolidationResult:
    identities = {fact.id for fact in facts}
    target_by_id = {target["id"]: target for target in targets}
    if (
        len(identities) != len(facts)
        or not 1 <= len(facts) <= config.extraction.max_facts_per_group
        or not 1 <= len(seeds) <= config.index.insight_seed_max
        or len(set(seeds)) != len(seeds)
        or not set(seeds) <= identities
        or len(target_by_id) != len(targets)
        or len(targets) > config.index.insight_candidate_max
    ):
        raise ExtractionError("incremental Insight input is not a bounded distinct set")
    body, usage = _generate_json(
        models,
        "insights",
        INCREMENTAL_INSIGHT_SYSTEM,
        consolidation_envelope(facts, seeds, targets),
        config,
        timeout_seconds,
        max_output_tokens=config.index.insight_max_output_tokens,
    )
    try:
        result = InsightDecisions.model_validate(body)
    except ValidationError as exc:
        raise ExtractionError("Insight actions are malformed", usage=usage, retryable=True) from exc

    def invalid(message):
        raise ExtractionError(message, usage=usage, retryable=True)

    covered: set[str] = set()
    used_targets: set[str] = set()
    normalized = []
    for action in result.actions:
        addressed, support = set(action.seed_ids), set(action.support_ids)
        if (
            len(addressed) != len(action.seed_ids)
            or not addressed <= set(seeds)
            or covered & addressed
            or len(support) != len(action.support_ids)
            or not support <= identities
        ):
            invalid("Insight action has duplicate, unknown or already addressed inputs")
        covered.update(addressed)
        target = target_by_id.get(action.target_id)
        if action.action in {"UPDATE", "KEEP_TEXT"}:
            if target is None or action.target_id in used_targets:
                invalid("Insight action has an unknown, duplicate or stale target")
            used_targets.add(action.target_id)
        elif action.target_id is not None:
            invalid("only UPDATE or KEEP_TEXT may address an existing target")
        retained = {item["id"] for item in target["supports"]} if target else set()
        if action.action in {"CREATE", "UPDATE", "KEEP_TEXT"} and (
            not addressed <= support or len(support | retained) < 2
        ):
            invalid("Insight requires a complete claim and at least two known supports")
        if action.action in {"CREATE", "UPDATE"}:
            if not action.text or not action.title or action.subtype is None:
                invalid("Insight requires a complete claim, title and descriptive subtype")
        else:
            if (
                action.text is not None
                or action.title is not None
                or action.retrieval_text is not None
                or action.subtype is not None
                or (action.action == "NO_INSIGHT" and support)
            ):
                invalid("KEEP_TEXT/NO_INSIGHT cannot contain replacement claims")
        values = action.model_dump()
        values["expected_revision"] = target["revision"] if target else None
        if action.action == "KEEP_TEXT":
            values["action"] = "UPDATE" if support - retained else "NO_CHANGE"
            if values["action"] == "UPDATE":
                values.update(
                    text=target["text"],
                    title=insight_title(target, target["text"]),
                    retrieval_text=target.get("retrieval_text"),
                    subtype=target["subtype"],
                )
        normalized.append(InsightAction.model_validate(values))
    if covered != set(seeds):
        invalid("every presented seed requires exactly one disposition")
    return ConsolidationResult(
        actions=tuple(normalized),
        model_actions=tuple(action.action for action in result.actions),
        usage=usage,
    )


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
        envelope = {"facts": [insight_fact_projection(item) for item in candidate]}
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
        {"facts": [insight_fact_projection(fact) for fact in facts]},
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
            {
                **{key: body[key] for key in required},
                "retrieval_text": body.get("retrieval_text"),
                "title": body.get("title"),
                "usage": usage,
            }
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
    system: str | None = None,
):
    if system is None:
        system = extraction_system(config, with_references=True)
    selected = references[: config.extraction.reference_max_facts]
    metadata = [{**reference, "text": ""} for reference in selected]
    metadata_units = estimate_units(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
    while (
        selected and metadata_units > config.extraction.reference_max_input_tokens * UNITS_PER_TOKEN
    ):
        selected = selected[:-1]
        metadata = metadata[:-1]
        metadata_units = estimate_units(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        )
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
    system = extraction_system(config)
    if config.extraction.reference_max_facts:
        system = extraction_system(config, with_references=True)
        envelope["reference_facts"] = pack_reference_facts(
            references, envelope, config, system=system
        )
        if (
            not envelope["reference_facts"]
            and model_input_tokens(system, envelope) > config.extraction.max_input_tokens
        ):
            envelope.pop("reference_facts")
            system = extraction_system(config)
    offered_ids = {reference["id"] for reference in envelope.get("reference_facts", [])}
    try:
        body, usage = _generate_json(
            models, "extraction", system, envelope, config, timeout_seconds
        )
    except (ModelOutputTooLongError, ExtractionInputTooLongError) as error:
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
    return validate_fact_output(
        body,
        usage,
        config,
        {None: chunk},
        offered_ids,
        reference_facts=envelope.get("reference_facts", ()),
        require_entity_mentions=True,
    )


def retry_split_extractions(
    producers: tuple[Callable[[], FactExtraction], ...],
    overflow: ModelOutputTooLongError | ExtractionInputTooLongError,
    check_retry_claim: Callable[[], None] | None = None,
) -> FactExtraction:
    """Retry smaller inputs in source order under the caller's existing checkpoint."""
    input_overflow = isinstance(overflow, ExtractionInputTooLongError)
    logging.getLogger(__name__).warning(
        "Extraction %s overflow; splitting source inputs",
        "input" if input_overflow else "output",
    )
    initial_usage = [] if input_overflow else [overflow.usage]
    results = []
    for produce in producers:
        if check_retry_claim is not None:
            try:
                check_retry_claim()
            except Exception as error:
                usages = [*initial_usage, *(result.usage for result in results)]
                if usages:
                    error.usage = _sum_extraction_usage(usages)
                raise
        try:
            results.append(produce())
        except Exception as error:
            usages = [
                *initial_usage,
                *(result.usage for result in results),
                getattr(error, "usage", None),
            ]
            if any(usage is not None for usage in usages):
                error.usage = _sum_extraction_usage(usages)
            raise
    facts, diagnostics, candidates, offset = [], [], [], 0
    for split_index, result in enumerate(results):
        prefix = f"split-{split_index}:"
        candidates.extend(
            {**candidate, "output_index": str(int(candidate["output_index"]) + offset)}
            for candidate in result.entity_candidates
        )
        facts.extend(
            fact.model_copy(
                update={
                    "output_index": fact.output_index + offset,
                    "caused_by": tuple(target + offset for target in fact.caused_by),
                    "updates": tuple(target + offset for target in fact.updates),
                    "entity_mentions": tuple(
                        mention.model_copy(
                            update={
                                "object": prefix + mention.object,
                                "different_from": tuple(
                                    prefix + key for key in mention.different_from
                                ),
                            }
                        )
                        for mention in fact.entity_mentions
                    )
                    if fact.entity_mentions is not None
                    else None,
                    "entity_aliases": tuple(
                        alias.model_copy(update={"object": prefix + alias.object})
                        if alias.object
                        else alias
                        for alias in fact.entity_aliases
                    ),
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
        usage=_sum_extraction_usage([*initial_usage, *(result.usage for result in results)]),
        entity_candidates=tuple(candidates),
    )


def _sum_extraction_usage(usages: list[Usage | None]) -> Usage:
    fields = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
        values = [getattr(usage, field) for usage in usages if usage is not None]
        known = [value for value in values if value is not None]
        fields[field] = sum(known) if known else None
    for field in ("cached_input_tokens", "uncached_input_tokens", "cache_creation_input_tokens"):
        values = [getattr(usage, field) if usage is not None else None for usage in usages]
        fields[field] = (
            sum(values) if values and all(value is not None for value in values) else None
        )
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
    *,
    reference_facts=(),
    require_entity_mentions=False,
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
                quote = raw.get("quote")
                matches = (
                    [key for key, candidate in chunks.items() if quote in candidate.text]
                    if None not in chunks and isinstance(quote, str) and quote.strip()
                    else []
                )
                if len(matches) == 1 and unique_quote(chunks[matches[0]].text, quote):
                    owner = matches[0]
                    raw["passage_id"] = owner
                    diagnostics.append(
                        ExtractionDiagnostic(
                            output_index=index, field="passage_id", code="recovered_passage"
                        )
                    )
                else:
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
            aliases = raw.pop("entity_aliases", [])
            entity_references = raw.pop("entity_references", [])
            structured_mentions = "entity_mentions" in raw
            entity_mentions = raw.pop("entity_mentions", [])
            if not config.index.enable_causal_graph:
                discarded = raw.pop("caused_by", ())
                if discarded not in ((), []):
                    diagnostics.append(
                        ExtractionDiagnostic(
                            output_index=index, field="caused_by", code="causal_graph_disabled"
                        )
                    )
            if not all(
                isinstance(value, list)
                for value in (annotations, aliases, entity_references, entity_mentions)
            ):
                raise ValueError
            fact = Fact.model_validate({**raw, "output_index": index})
            if not unique_quote(chunk.text, fact.quote):
                raise ValueError("support quote must be unique in its owning passage")
        except (ValueError, ValidationError):
            diagnostics.append(
                ExtractionDiagnostic(output_index=index, field="fact", code="invalid_fact")
            )
            continue
        valid_entities = []
        for name in fact.entities:
            try:
                entity_name_key(name, max_bytes=config.index.entity_name_max_bytes)
                if not _grounded_source_names(chunk.text, fact, fact.quote, (name,)):
                    raise ValueError("entity name requires a whole mention in its own support")
                valid_entities.append(name)
            except ValueError:
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="entities", code="invalid_entity_name"
                    )
                )
        fact = fact.model_copy(update={"entities": tuple(valid_entities)})
        valid_mentions = []
        if require_entity_mentions and not structured_mentions:
            structured_mentions = True
            if fact.entities or aliases:
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index,
                        field="entity_mentions",
                        code="missing_entity_decision",
                    )
                )
        for raw_mention in entity_mentions:
            try:
                if not isinstance(raw_mention, dict) or "span" in raw_mention:
                    raise ValueError("mention offsets are derived by the server")
                mention = EntityMention.model_validate(raw_mention)
                entity_name_key(mention.name, max_bytes=config.index.entity_name_max_bytes)
                if mention.reference_id is not None and mention.reference_id not in offered_ids:
                    raise ValueError("identity reference was not offered")
                if mention.reference_name_id is not None and not any(
                    ref.get("id") == mention.reference_id
                    and any(
                        n.get("name_id") == mention.reference_name_id
                        and entity_name_key(n["name"], max_bytes=config.index.entity_name_max_bytes)
                        == entity_name_key(
                            mention.name, max_bytes=config.index.entity_name_max_bytes
                        )
                        for n in ref.get("entities", ())
                    )
                    for ref in reference_facts
                ):
                    raise ValueError("identity name handle was not offered")
                span = entity_mention_span(
                    chunk.text, fact, mention.name, mention.quote, mention.occurrence
                )
                valid_mentions.append(mention.model_copy(update={"span": span}))
            except (ValueError, ValidationError):
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="entity_mentions", code="invalid_entity_mention"
                    )
                )
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
        valid_aliases = []
        for raw_alias in aliases:
            try:
                if not isinstance(raw_alias, dict):
                    raise ValueError
                alias = EntityAlias.model_validate(raw_alias)
                owners = {m.object for m in valid_mentions if m.name == alias.canonical_name}
                if alias.object is not None:
                    span = entity_mention_span(
                        chunk.text,
                        fact,
                        alias.canonical_name,
                        alias.quote,
                        alias.canonical_occurrence,
                    )
                    entity_mention_span(
                        chunk.text, fact, alias.alias, alias.quote, alias.alias_occurrence
                    )
                    if not any(
                        m.object == alias.object
                        and m.name == alias.canonical_name
                        and m.span == span
                        for m in valid_mentions
                    ):
                        raise ValueError("alias owner must select its original occurrence")
                elif len(owners) > 1:
                    raise ValueError("alias requires an unambiguous object owner")
                canonical_key = entity_name_key(
                    alias.canonical_name, max_bytes=config.index.entity_name_max_bytes
                )
                alias_key = entity_name_key(
                    alias.alias, max_bytes=config.index.entity_name_max_bytes
                )
                if not _grounded_source_names(
                    chunk.text, fact, alias.quote, (alias.canonical_name, alias.alias)
                ):
                    raise ValueError("alias requires whole names within exact fact support")
                if canonical_key != alias_key:
                    valid_aliases.append(alias)
            except (ValueError, ValidationError):
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index, field="entity_aliases", code="invalid_alias"
                    )
                )
        valid_entity_references = []
        for raw_reference in entity_references:
            try:
                if not isinstance(raw_reference, dict):
                    raise ValueError
                reference = EntityReference.model_validate(raw_reference)
                entity_name_key(reference.name, max_bytes=config.index.entity_name_max_bytes)
                if reference.reference_id not in offered_ids:
                    raise ValueError("entity reference requires an offered fact handle")
                if not _grounded_source_names(chunk.text, fact, reference.quote, (reference.name,)):
                    raise ValueError("entity reference requires a whole name within fact support")
                valid_entity_references.append(reference)
            except (ValueError, ValidationError):
                diagnostics.append(
                    ExtractionDiagnostic(
                        output_index=index,
                        field="entity_references",
                        code="invalid_entity_reference",
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
            fact.model_copy(
                update={
                    **relations,
                    "annotations": tuple(valid_annotations),
                    "entity_aliases": tuple(valid_aliases),
                    "entity_references": tuple(valid_entity_references),
                    "entity_mentions": tuple(valid_mentions) if structured_mentions else None,
                }
            )
        )
        accepted_indices.add(index)
    return FactExtraction(
        facts=tuple(accepted),
        outcome="partial" if diagnostics else "facts",
        diagnostics=tuple(diagnostics),
        usage=usage,
        entity_candidates=tuple(
            {
                "reference_id": reference["id"],
                "name": name["name"],
                "name_id": name["name_id"],
                "output_index": str(fact.output_index),
            }
            for fact in accepted
            for reference in reference_facts
            for name in reference.get("entities", ())
            if name.get("name_id")
        ),
    )


def entity_mention_span(
    content: str, fact: Fact, name: str, quote: str, occurrence: int
) -> tuple[int, int]:
    """Locate a whole original name; identity judgment remains the extractor's job."""
    if not unique_quote(fact.quote, quote):
        raise ValueError("mention quote must be unique within fact support")
    start = content.index(fact.quote) + fact.quote.index(quote)
    spans = [
        (start + match.start(), start + match.end())
        for match in re.finditer(re.escape(name), quote)
        if _whole_source_mention(
            content, name, start + match.start(), start + match.end(), fact.entities
        )
    ]
    if occurrence > len(spans):
        raise ValueError("mention occurrence is unavailable")
    return spans[occurrence - 1]


def _grounded_source_names(content: str, fact: Fact, quote: str, names: tuple[str, ...]) -> bool:
    if not unique_quote(fact.quote, quote):
        return False
    quote_start = content.index(fact.quote) + fact.quote.index(quote)
    return all(
        _whole_source_mention(content, name, quote_start, quote_start + len(quote), fact.entities)
        for name in names
    )


def _whole_source_mention(
    content: str,
    name: str,
    quote_start: int,
    quote_end: int,
    observed_names: tuple[str, ...],
) -> bool:
    """Check literal mentions in original context; this does not prove identity."""
    chinese_boundaries = None
    for match in re.finditer(re.escape(name), content[quote_start:quote_end]):
        start, end = quote_start + match.start(), quote_start + match.end()
        if any(
            longer != name
            and name in longer
            and any(
                occurrence.start() <= start and end <= occurrence.end()
                for occurrence in re.finditer(re.escape(longer), content)
            )
            for longer in observed_names
        ):
            continue
        valid = True
        for edge, adjacent in ((start, start - 1), (end, end)):
            if not 0 <= adjacent < len(content):
                continue
            inside = content[start] if edge == start else content[end - 1]
            outside = content[adjacent]
            inside_han = "CJK" in unicodedata.name(inside, "")
            outside_han = "CJK" in unicodedata.name(outside, "")
            if inside_han and outside_han:
                if chinese_boundaries is None:
                    chinese_boundaries = {
                        boundary
                        for _, token_start, token_end in jieba.tokenize(content, HMM=False)
                        for boundary in (token_start, token_end)
                    }
                if edge not in chinese_boundaries:
                    valid = False
                    break
            elif (
                not inside_han
                and not outside_han
                and (outside.isalnum() or outside == "_" or unicodedata.category(outside)[0] == "M")
            ):
                valid = False
                break
            if outside in "-." and not inside_han:
                beyond = adjacent - 1 if edge == start else adjacent + 1
                if 0 <= beyond < len(content) and content[beyond].isalnum():
                    valid = False
                    break
        if valid:
            return True
    return False


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
        if fact.entity_mentions is not None:
            relations["entity_mentions"] = tuple(
                mention.model_copy(
                    update={
                        "span": entity_mention_span(
                            chunk.text, fact, mention.name, mention.quote, mention.occurrence
                        ),
                    }
                )
                for mention in fact.entity_mentions
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
    *,
    max_output_tokens: int | None = None,
) -> tuple[dict, Usage]:
    user = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if model_input_tokens(system, envelope) > config.extraction.max_input_tokens:
        raise ExtractionInputTooLongError(
            "complete extraction input exceeds configured token bound"
        )
    result = models.generate(
        role,
        system=system,
        user=user,
        max_output_tokens=max_output_tokens or config.extraction.max_output_tokens,
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
