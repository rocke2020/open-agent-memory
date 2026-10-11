"""Process one durable source claim through compatible indexing checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from importlib.resources import files
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from oam.config import OAMConfig
from oam.contracts.evidence import EvidenceAppendRequest
from oam.governance.cleanup import cleanup_claim, cleanup_removal_claim
from oam.indexing.chunker import chunk_text
from oam.indexing.extraction import (
    FactExtraction,
    InsightInput,
    InsightResult,
    extract_facts,
    extraction_envelope,
    model_input_tokens,
    pack_insight_facts,
    synthesize_insight,
)
from oam.indexing.grouping import (
    ExtractionPassage,
    extract_group,
    group_envelope,
    plan_extraction_groups,
)
from oam.indexing.identity import index_generation
from oam.indexing.references import eligible_reference_ids, freeze_extraction_references
from oam.indexing.store import MEMORY_REVISION, IndexStore, stable_id
from oam.workers.claims import claim_one

ABSTRACT_SYSTEM = (
    files("oam.resources").joinpath("abstract-prompt.txt").read_text(encoding="utf-8").rstrip("\n")
)
PDF_VISION_SYSTEM = (
    files("oam.resources")
    .joinpath("pdf-vision-system-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
PDF_VISION_USER_TEMPLATE = (
    files("oam.resources")
    .joinpath("pdf-vision-user-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
IMAGE_VISION_SYSTEM = (
    files("oam.resources")
    .joinpath("image-vision-system-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
IMAGE_VISION_USER = (
    files("oam.resources")
    .joinpath("image-vision-user-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
PDF_NATIVE_PARSE_REVISION = 2
LEGACY_RESOURCE_PARSE_REVISION = 1
PASSAGE_EMBED_BATCH_SIZE = 16
EXTRACTION_WINDOW_BATCHES = 2


def process_one(
    config: OAMConfig,
    queue_dsn: str,
    content_dsn: str,
    *,
    models,
    worker_id: str,
    queue_pool: ConnectionPool | None = None,
    content_pool: ConnectionPool | None = None,
) -> bool:
    queue_connection = (
        queue_pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
        if queue_pool is not None
        else psycopg.connect(queue_dsn)
    )
    with queue_connection as connection:
        claim = claim_one(
            connection.cursor(),
            worker_id=worker_id,
            attempt_id="attempt_" + uuid4().hex,
            lease_seconds=config.lease.seconds,
            user_concurrency=config.user.concurrency,
        )
    if claim is None:
        return False
    if claim.stage in {"cleanup_source", "cleanup_memory", "cleanup_insight"}:
        if content_pool is None:
            if claim.stage == "cleanup_source":
                cleanup_claim(content_dsn, claim, worker_id)
            else:
                cleanup_removal_claim(content_dsn, claim, worker_id)
        else:
            if claim.stage == "cleanup_source":
                cleanup_claim(content_dsn, claim, worker_id, pool=content_pool)
            else:
                cleanup_removal_claim(content_dsn, claim, worker_id, pool=content_pool)
        return True
    if claim.stage == "refresh_profile":
        from oam.profiles.worker import process_profile_claim

        profile_kwargs = {
            "models": models,
            "claim": claim,
            "worker_id": worker_id,
        }
        if content_pool is not None:
            profile_kwargs["pool"] = content_pool
        process_profile_claim(config, content_dsn, **profile_kwargs)
        return True
    store = IndexStore(
        content_dsn,
        config,
        claim,
        worker_id,
        index_generation(config, os.environ),
        pool=content_pool,
    )
    try:
        if claim.stage == "index_semantic_links":
            from oam.workers.semantic_links import process_semantic_claim

            process_semantic_claim(store)
            return True
        if claim.stage == "consolidate":
            from oam.governance.repair import repair_insights

            if repair_insights(store, models):
                with store.transaction() as connection:
                    store.guard(connection)
                    store.readiness(connection, insights_ready=True)
                store.finish()
                return True
            with store.transaction() as connection:
                store.guard(connection)
                rows = connection.execute(
                    "SELECT DISTINCT m.memory_id FROM memories m "
                    "JOIN support s USING(tenant_id,user_id,memory_id) "
                    "JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                    "WHERE m.tenant_id=%s AND m.user_id=%s AND e.source_id=%s "
                    "AND e.source_version=%s AND m.lifecycle_state='active' "
                    "ORDER BY m.memory_id",
                    (*store.key, store.source["source_id"], store.source["source_version"]),
                ).fetchall()
            new_fact_ids = [row[0] for row in rows]
            _insights(store, models, new_fact_ids)
            store.finish()
            return True
        if store.source["input_role"] == "evidence_resource":
            _resource(store, models)
            store.finish()
            return True
        if store.source["input_role"] == "profile_resource":
            store.finish()
            return True
        if store.source["input_role"] != "interaction":
            raise ValueError("this source processor requires eligible interactions")
        request = EvidenceAppendRequest.model_validate_json(bytes(store.source["original_bytes"]))
        with store.model_pipeline():
            fact_ids, extraction_diagnostics = _index_interaction(config, store, models, request)
        with store.transaction() as connection:
            store.guard(connection)
            store.readiness(
                connection,
                memory_ready=True,
                extraction_coverage="partial" if extraction_diagnostics else "complete",
                extraction_diagnostics=extraction_diagnostics,
            )
        from oam.indexing.links import build_temporal_links

        with store.transaction() as connection:
            store.guard(connection)
            links = build_temporal_links(
                connection, store.authority, config, store.generation_id, fact_ids
            )
            store.readiness(connection, temporal_links_ready=True, link_coverage=links)
        store.finish(
            schedule_consolidation=config.index.insights_enabled, semantic_seed_ids=fact_ids
        )
    except BaseException as error:
        store.fail(error)
        raise
    return True


def _index_interaction(config, store, models, request):
    fact_ids = []
    extraction_diagnostics = []
    pending_extractions = []
    extraction_failures = []
    capacity = config.provider.llm_concurrency
    window = EXTRACTION_WINDOW_BATCHES * capacity
    current_stage = "extraction_pipeline"

    def checkpoint(stage, inputs, produce):
        nonlocal current_stage
        current_stage = stage
        return store.checkpoint(stage, inputs, produce)

    def submit_extraction(members):
        passages = tuple(by_passage[item[0]] for item in members)
        group_id = (
            members[0][0]
            if len(members) == 1
            else stable_id("extraction_group", [item[0] for item in members])
        )
        if len(members) == 1:
            passage_id, chunk, message, _, _, extraction_input, vector = members[0]
            extraction_envelope(
                chunk=chunk,
                config=config,
                message_role=message.role.value,
                mentioned_at=message.event_time,
                reference_timezone=request.reference_timezone,
            )
        else:
            envelope = group_envelope(passages, config, request.reference_timezone)
            extraction_input = dict(envelope)
            vector = members[0][-1]
        references = []
        if config.extraction.reference_max_facts:
            options = (
                {}
                if len(members) == 1
                else {
                    "vectors": tuple(item[-1] for item in members),
                }
            )
            references = freeze_extraction_references(
                store, group_id, vector, extraction_input, **options
            )
            extraction_input["reference_facts"] = references
        if len(members) == 1:

            def produce(chunk=chunk, message=message, references=references):
                return extract_facts(
                    models,
                    chunk=chunk,
                    config=config,
                    message_role=message.role.value,
                    mentioned_at=message.event_time,
                    reference_timezone=request.reference_timezone,
                    timeout_seconds=config.model.attempt_timeout_seconds,
                    references=tuple(references),
                    check_retry_claim=store.check_retry_claim,
                )
        else:

            def produce(passages=passages, references=references):
                return extract_group(
                    models,
                    passages=passages,
                    config=config,
                    reference_timezone=request.reference_timezone,
                    timeout_seconds=config.model.attempt_timeout_seconds,
                    references=tuple(references),
                    check_retry_claim=store.check_retry_claim,
                )

        future = extraction_executor.submit(
            store.checkpoint, "extract:" + group_id, extraction_input, produce
        )
        pending_extractions.append((members, group_id, future))
        if len(pending_extractions) >= window:
            publish_extractions(wait_for_one=True)

    def publish_extractions(*, wait_for_one=False):
        if not pending_extractions:
            return
        completed = [item for item in pending_extractions if item[2].done()]
        if not completed and wait_for_one:
            wait(
                [item[2] for item in pending_extractions],
                return_when=FIRST_COMPLETED,
            )
            completed = [item for item in pending_extractions if item[2].done()]
        ready = completed[:capacity]
        if not ready:
            return
        ready_futures = {item[2] for item in ready}
        pending_extractions[:] = [
            item for item in pending_extractions if item[2] not in ready_futures
        ]
        extracted = []
        for _, group_id, future in ready:
            try:
                extracted.append(future.result())
            except BaseException as error:
                extraction_failures.append(("extract:" + group_id, error))
                extracted.append(error)
        failures = []
        publishable = []
        for members, group_id, output in zip(
            [item[0] for item in ready], [item[1] for item in ready], extracted, strict=True
        ):
            if isinstance(output, BaseException):
                failures.append((group_id, output))
                continue
            result = FactExtraction.model_validate(output)
            extraction_diagnostics.extend(
                {"passage_id": group_id, **diagnostic.model_dump(mode="json")}
                for diagnostic in result.diagnostics
            )
            if result.facts:
                publishable.append((group_id, members, result))
        if failures:
            raise failures[0][1]
        for group_id, members, result in publishable:
            texts = tuple(fact.text for fact in result.facts)
            batch_id = stable_id("facts_batch", [group_id])
            embedded = checkpoint(
                "facts_embed:" + batch_id,
                texts,
                lambda texts=texts: models.embed(
                    texts, timeout_seconds=config.model.attempt_timeout_seconds
                ),
            )
            fact_ids.extend(
                _publish_facts(
                    store,
                    {item[0]: (item[2], item[3], item[4], item[1]) for item in members},
                    result,
                    embedded["vectors"],
                    metadata=request,
                    diagnostics=extraction_diagnostics,
                )
            )

    prepared = []
    previous = ""
    for message_index, message in enumerate(request.messages):
        if not message.learning_eligible:
            previous = ""
            continue
        evidence_id = stable_id(
            "evidence",
            store.generation_id,
            store.source["source_id"],
            store.source["source_version"],
            message.message_id,
        )
        chunks = chunk_text(message.content, config.chunk.ingestion, previous_context=previous)
        for passage_index, chunk in enumerate(chunks):
            passage_id = stable_id("passage", evidence_id, passage_index)
            prepared.append((passage_id, chunk, message, evidence_id, message_index, passage_index))
        previous = message.content
    by_passage = {
        item[0]: ExtractionPassage(
            item[0], item[1], item[2].role.value, item[2].event_time, item[4]
        )
        for item in prepared
    }
    groups = plan_extraction_groups(tuple(by_passage.values()), config, request.reference_timezone)
    ending_groups = {group[-1].passage_id: tuple(p.passage_id for p in group) for group in groups}
    published = {}
    extraction_executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="oam-extract")
    try:
        for offset in range(0, len(prepared), PASSAGE_EMBED_BATCH_SIZE):
            batch = prepared[offset : offset + PASSAGE_EMBED_BATCH_SIZE]
            texts = tuple(chunk.text for _, chunk, _, _, _, _ in batch)
            batch_id = stable_id("passage_batch", [item[0] for item in batch])
            embedded = checkpoint(
                "passage:" + batch_id,
                texts,
                lambda texts=texts: models.embed(
                    texts, timeout_seconds=config.model.attempt_timeout_seconds
                ),
            )
            for (
                passage_id,
                chunk,
                message,
                evidence_id,
                message_index,
                passage_index,
            ), vector in zip(batch, embedded["vectors"], strict=True):
                locator = {
                    "source_id": store.source["source_id"],
                    "source_version": store.source["source_version"],
                    "message_id": message.message_id,
                    "message_index": message_index,
                    "role": message.role.value,
                    "passage_index": passage_index,
                }
                with store.transaction() as connection:
                    store.guard(connection)
                    connection.execute(
                        "INSERT INTO evidence(tenant_id,user_id,evidence_id,source_id,"
                        "source_version,"
                        "accepted_seq,content_text,event_time,learning_eligible,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,'active') ON CONFLICT DO NOTHING",
                        (
                            *store.key,
                            evidence_id,
                            store.source["source_id"],
                            store.source["source_version"],
                            store.source["accepted_seq"],
                            message.content,
                            message.event_time,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,"
                        "parse_revision,"
                        "locator,normalized_text,generation_id,visible_from) "
                        "VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (
                            *store.key,
                            passage_id,
                            evidence_id,
                            Jsonb(locator),
                            chunk.text,
                            store.generation_id,
                            store.source["accepted_seq"],
                        ),
                    )
                    store.insert_unit(
                        connection,
                        unit_id=passage_id,
                        kind="passage",
                        text=chunk.text,
                        vector=vector,
                        locator=locator,
                        evidence_id=evidence_id,
                        passage_id=passage_id,
                        mentioned_at=message.event_time,
                        source_created_at=request.source_created_at,
                        source_updated_at=request.source_updated_at,
                        ordinal=passage_index,
                    )
                    for index, annotation in enumerate(message.temporal_annotations):
                        first = message.content.index(annotation.quote)
                        if first < chunk.end and chunk.start < first + len(annotation.quote):
                            store.insert_annotation(
                                connection,
                                passage_id,
                                annotation,
                                origin="caller",
                                ordinal=index,
                                reference_timezone=request.reference_timezone,
                            )
                    store.readiness(connection, source_searchable=True)
                extraction_input = {
                    "text": chunk.text,
                    "context": chunk.previous_context,
                    "role": message.role.value,
                    "mentioned_at": message.event_time.isoformat(),
                }
                if request.reference_timezone is not None:
                    extraction_input["reference_timezone"] = request.reference_timezone
                published[passage_id] = (
                    passage_id,
                    chunk,
                    message,
                    evidence_id,
                    locator,
                    extraction_input,
                    vector,
                )
                if passage_id not in ending_groups:
                    continue
                submit_extraction(
                    [published.pop(identifier) for identifier in ending_groups[passage_id]]
                )
            publish_extractions()
        while pending_extractions:
            publish_extractions(wait_for_one=True)
    except BaseException as error:
        for _, _, future in pending_extractions:
            future.cancel()
        extraction_executor.shutdown(wait=True, cancel_futures=True)
        for _, group_id, future in pending_extractions:
            if not future.cancelled() and future.exception() is not None:
                extraction_failures.append(("extract:" + group_id, future.exception()))
        if not any(failure is error for _, failure in extraction_failures):
            extraction_failures.append((current_stage, error))
        stage, failure = extraction_failures[0]
        store.mark_deferred_failure_stage(stage)
        if len(extraction_failures) > 1:
            raise RuntimeError(
                "multiple indexing stages failed; usage coverage is partial"
            ) from failure
        raise
    else:
        extraction_executor.shutdown(wait=True)
    return fact_ids, extraction_diagnostics


def _resource(store, models):
    from oam.contracts.resources import ResourceMediaType
    from oam.resources.html import parse_html
    from oam.resources.images import prepare_image_for_vision
    from oam.resources.limits import enforce_byte_limit
    from oam.resources.originals import OriginalDescriptor
    from oam.resources.pdf_isolated import inspect_pdf, render_page
    from oam.resources.types import ParsedBlock, ResourceParseError

    content = bytes(store.source["original_bytes"])
    original = OriginalDescriptor.from_bytes(media_type=store.source["media_type"], content=content)
    metadata = store.resource_metadata()
    annotated_locators = {annotation.locator for annotation in metadata.temporal_annotations}
    spans = {}
    processing_error = None
    processing_error_stage = None
    if original.media_type is ResourceMediaType.TEXT:
        enforce_byte_limit(original, store.config.resource)
        source_text = content.decode("utf-8")
        chunks = chunk_text(source_text, store.config.chunk.ingestion)
        if not chunks:
            raise ResourceParseError("text Resource contains no extractable text")
        blocks = tuple(
            ParsedBlock(locator=f"text:{index}", text=chunk.text)
            for index, chunk in enumerate(chunks)
        )
    elif original.media_type is ResourceMediaType.PDF:
        pages = inspect_pdf(original, store.config.resource)
        blocks = []
        rendered_pixels = 0
        for page in pages:
            if page.text:
                page_span = {
                    "page": page.page_number,
                    "extraction_method": "native_text",
                }
                if page.locator in annotated_locators:
                    # Caller quotes address the original flattened whole-page representation.
                    text = re.sub(r"\s+", " ", page.text).strip()
                    blocks.append(ParsedBlock(locator=page.locator, text=text))
                    spans[page.locator] = {
                        **page_span,
                        "parse_revision": LEGACY_RESOURCE_PARSE_REVISION,
                    }
                else:
                    chunks = chunk_text(page.text, store.config.chunk.ingestion)
                    for chunk in chunks:
                        locator = (
                            page.locator
                            if len(chunks) == 1
                            else f"{page.locator}:span:{chunk.start}:{chunk.end}"
                        )
                        blocks.append(ParsedBlock(locator=locator, text=chunk.text))
                        spans[locator] = {
                            **page_span,
                            "parse_revision": PDF_NATIVE_PARSE_REVISION,
                            "start": chunk.start,
                            "end": chunk.end,
                        }
                if not page.needs_vision:
                    continue
            vision_stage = None
            try:
                image, pixels = render_page(
                    original,
                    page.page_number,
                    store.config.resource,
                    remaining_pixels=store.config.resource.max_total_pixels - rendered_pixels,
                )
                rendered_pixels += pixels
                vision_stage = "vision:" + page.locator
                generated = store.checkpoint(
                    vision_stage,
                    {
                        "sha256": original.sha256,
                        "page": page.page_number,
                        "vision_sha256": hashlib.sha256(image).hexdigest(),
                    },
                    lambda image=image, current_page=page: _validated_vision_result(
                        models.generate_vision(
                            image,
                            media_type="image/png",
                            system=PDF_VISION_SYSTEM,
                            user=PDF_VISION_USER_TEMPLATE.format(
                                page_number=current_page.page_number
                            ),
                            max_output_tokens=store.config.resource.max_output_tokens,
                            timeout_seconds=store.config.model.attempt_timeout_seconds,
                        ),
                        current_page.locator,
                        {"page": current_page.page_number},
                    ),
                )
                visual_blocks, visual_spans = _visual_blocks(
                    generated["content"], page.locator, {"page": page.page_number}
                )
            except Exception as error:
                processing_error = error
                processing_error_stage = vision_stage
                break
            for block in visual_blocks:
                if page.text and _native_contains_visual_observation(page.text, block.text):
                    continue
                blocks.append(block)
                spans[block.locator] = visual_spans[block.locator]
        blocks = tuple(blocks)
        source_text = "\n".join(block.text for block in blocks)
    elif original.media_type in {ResourceMediaType.PNG, ResourceMediaType.JPEG}:
        vision_content = prepare_image_for_vision(original, store.config.resource)
        generated = store.checkpoint(
            "vision:image:0001",
            {
                "sha256": original.sha256,
                "vision_sha256": hashlib.sha256(vision_content).hexdigest(),
            },
            lambda: _validated_vision_result(
                models.generate_vision(
                    vision_content,
                    media_type=original.media_type.value,
                    system=IMAGE_VISION_SYSTEM,
                    user=IMAGE_VISION_USER,
                    max_output_tokens=store.config.resource.max_output_tokens,
                    timeout_seconds=store.config.model.attempt_timeout_seconds,
                ),
                "image:0001",
                {"image": 1},
            ),
        )
        blocks, spans = _visual_blocks(generated["content"], "image:0001", {"image": 1})
        source_text = "\n".join(block.text for block in blocks)
    else:
        blocks = parse_html(original, store.config.resource).blocks
        source_text = "\n".join(block.text for block in blocks)
    if not blocks:
        with store.transaction() as connection:
            store.guard(connection)
            store.readiness(connection, source_searchable=False, parse_coverage="none")
        if processing_error is not None:
            if processing_error_stage is not None:
                store.mark_deferred_failure_stage(processing_error_stage)
            raise processing_error
        raise ResourceParseError("Resource contains no extractable detail")
    metadata.validate_source_quotes({block.locator: block.text for block in blocks})
    evidence_id = stable_id(
        "resource_evidence",
        store.generation_id,
        store.source["source_id"],
        store.source["source_version"],
    )
    for ordinal, block in enumerate(blocks):
        passage_id = stable_id("resource_passage", evidence_id, block.locator)
        vector = store.checkpoint(
            "passage:" + passage_id,
            block.text,
            lambda text=block.text: models.embed(
                (text,), timeout_seconds=store.config.model.attempt_timeout_seconds
            ),
        )
        locator = {
            "source_id": store.source["source_id"],
            "source_version": store.source["source_version"],
            "document_id": store.source["source_id"],
            "revision": store.source["source_version"],
            "block": block.locator,
            **spans.get(block.locator, {}),
        }
        with store.transaction() as connection:
            store.guard(connection)
            connection.execute(
                "INSERT INTO evidence(tenant_id,user_id,evidence_id,source_id,source_version,"
                "accepted_seq,content_text,learning_eligible,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,false,'active') "
                "ON CONFLICT (tenant_id,user_id,evidence_id) DO UPDATE "
                "SET content_text=EXCLUDED.content_text",
                (
                    *store.key,
                    evidence_id,
                    store.source["source_id"],
                    store.source["source_version"],
                    store.source["accepted_seq"],
                    source_text,
                ),
            )
            connection.execute(
                "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,"
                "locator,normalized_text,generation_id,visible_from) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    *store.key,
                    passage_id,
                    evidence_id,
                    locator.get("parse_revision", LEGACY_RESOURCE_PARSE_REVISION),
                    Jsonb(locator),
                    block.text,
                    store.generation_id,
                    store.source["accepted_seq"],
                ),
            )
            store.insert_unit(
                connection,
                unit_id=passage_id,
                kind="resource_passage",
                text=block.text,
                vector=vector["vectors"][0],
                locator=locator,
                evidence_id=evidence_id,
                passage_id=passage_id,
                source_created_at=metadata.source_created_at,
                source_updated_at=metadata.source_updated_at,
                ordinal=ordinal,
                title=block.title,
                heading=block.heading,
                caption=block.caption,
            )
            for index, annotation in enumerate(metadata.temporal_annotations):
                if annotation.locator == block.locator:
                    store.insert_annotation(
                        connection,
                        passage_id,
                        annotation,
                        origin="caller",
                        ordinal=index,
                        reference_timezone=metadata.reference_timezone,
                    )
            readiness = {"source_searchable": True, "abstract_state": "pending"}
            if original.media_type is ResourceMediaType.PDF:
                readiness["parse_coverage"] = "partial"
            store.readiness(connection, **readiness)
    if processing_error is not None:
        with store.transaction() as connection:
            store.guard(connection)
            store.readiness(
                connection,
                parse_coverage="partial",
                abstract_state="pending",
                abstract_coverage="none",
            )
        if processing_error_stage is not None:
            store.mark_deferred_failure_stage(processing_error_stage)
        raise processing_error
    with store.transaction() as connection:
        store.guard(connection)
        store.readiness(connection, parse_coverage="complete")
    texts = _select_abstract_passages(
        [{"locator": block.locator, "text": block.text} for block in blocks],
        store.config.extraction.max_input_tokens,
    )
    try:
        if not texts:
            raise ValueError("abstract input has no passages within the complete prompt bound")
        generated = store.checkpoint(
            "abstract",
            texts,
            lambda: models.generate(
                "abstract",
                system=ABSTRACT_SYSTEM,
                user=json.dumps({"passages": texts}, ensure_ascii=False, separators=(",", ":")),
                max_output_tokens=store.config.resource.max_output_tokens,
                timeout_seconds=store.config.model.attempt_timeout_seconds,
            ),
        )
        value = json.loads(generated["content"])
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("abstract"), str)
            or not value["abstract"].strip()
        ):
            raise ValueError("Resource abstract is not valid nonempty text")
    except Exception:
        with store.transaction() as connection:
            store.guard(connection)
            store.readiness(connection, abstract_state="failed", abstract_coverage="none")
        raise
    with store.transaction() as connection:
        store.guard(connection)
        store.readiness(
            connection,
            abstract_state="current",
            abstract=value["abstract"],
            abstract_coverage="full" if len(texts) == len(blocks) else "partial",
        )


def _select_abstract_passages(
    passages: list[dict[str, str]], max_input_tokens: int
) -> list[dict[str, str]]:
    """Keep complete input when possible; otherwise cover the source with complete passages."""
    if model_input_tokens(ABSTRACT_SYSTEM, {"passages": passages}) <= max_input_tokens:
        return passages
    order = [0] if passages else []
    if len(passages) > 1:
        order.append(len(passages) - 1)
    intervals = deque([(1, len(passages) - 2)])
    while intervals:
        start, end = intervals.popleft()
        if start > end:
            continue
        middle = (start + end) // 2
        order.append(middle)
        intervals.extend(((start, middle - 1), (middle + 1, end)))
    selected = []
    chosen = []
    for index in order:
        candidate = [*selected, passages[index]]
        if model_input_tokens(ABSTRACT_SYSTEM, {"passages": candidate}) <= max_input_tokens:
            selected = candidate
            chosen.append(index)
    return [passages[index] for index in sorted(chosen)]


def _validated_vision_result(generated, prefix: str, base_locator: dict):
    from oam.models.runtime import ModelOutputError
    from oam.resources.types import ResourceParseError

    try:
        _visual_blocks(generated.content, prefix, base_locator)
    except ResourceParseError as exc:
        raise ModelOutputError(str(exc), usage=generated.usage) from exc
    return generated


def _native_contains_visual_observation(native_text: str, visual_text: str) -> bool:
    def comparable(text: str) -> str:
        return " ".join(re.sub(r":(?=\s)", "", text.casefold()).split())

    visual = comparable(visual_text)
    if len(visual) < 8:
        return False
    native = comparable(native_text)
    offset = 0
    while (start := native.find(visual, offset)) >= 0:
        end = start + len(visual)
        if (start == 0 or native[start - 1].isspace()) and (
            end == len(native) or native[end].isspace()
        ):
            return True
        offset = start + 1
    return False


def _visual_blocks(content: str, prefix: str, base_locator: dict):
    from oam.resources.types import ParsedBlock, ResourceParseError

    try:
        value = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ResourceParseError("visual extraction must be valid JSON") from exc
    raw = value.get("blocks") if isinstance(value, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ResourceParseError("visual extraction requires detailed blocks")
    blocks, spans = [], {}
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ResourceParseError("visual extraction block requires text")
        extracted = item["text"].strip()
        if not extracted:
            raise ResourceParseError("visual extraction block is empty")
        region = item.get("region")
        locator = f"{prefix}:block:{index:04d}"
        detail = {**base_locator, "parse_revision": 1, "extraction_method": "vision"}
        if region is not None:
            if (
                not isinstance(region, list)
                or len(region) != 4
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in region
                )
                or not (0 <= region[0] < region[2] <= 1)
                or not (0 <= region[1] < region[3] <= 1)
            ):
                raise ResourceParseError("visual extraction region is invalid")
            locator = f"{prefix}:region:{index:04d}"
            detail["region"] = region
        blocks.append(ParsedBlock(locator=locator, text=extracted))
        spans[locator] = detail
    return tuple(blocks), spans


def _publish_facts(store, owners, result, vectors, *, metadata, diagnostics):
    singleton_id = next(iter(owners))
    identifiers = {
        fact.output_index: stable_id(
            "memory", fact.passage_id or singleton_id, fact.output_index, fact.text
        )
        for fact in result.facts
    }
    with store.transaction() as connection:
        store.guard(connection)
        offered_ids = list(
            {target for fact in result.facts for target in fact.related_existing_ids}
        )
        eligible_ids = eligible_reference_ids(connection, store.generation_id, offered_ids)
        for fact, vector in zip(result.facts, vectors, strict=True):
            passage_id = fact.passage_id or singleton_id
            message, evidence_id, locator, chunk = owners[passage_id]
            if any(target not in eligible_ids for target in fact.related_existing_ids):
                diagnostics.append(
                    {
                        "passage_id": passage_id,
                        "output_index": fact.output_index,
                        "field": "related_existing_ids",
                        "code": "ineligible_reference",
                    }
                )
            memory_id = identifiers[fact.output_index]
            excluded = connection.execute(
                "SELECT 1 FROM memories WHERE tenant_id=%s AND user_id=%s AND memory_id=%s "
                "AND lifecycle_state='excluded' UNION ALL SELECT 1 FROM operations "
                "WHERE tenant_id=%s AND user_id=%s AND removal_target_kind='memory' "
                "AND removal_target_id=%s LIMIT 1",
                (*store.key, memory_id, *store.key, memory_id),
            ).fetchone()
            if excluded:
                continue
            support_locator = {**locator, "passage_id": passage_id, "quote": fact.quote}
            connection.execute(
                "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,assertion_kind,"
                "category,generation_id,visible_from) VALUES (%s,%s,%s,%s,'stated',%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    *store.key,
                    memory_id,
                    fact.text,
                    fact.category,
                    store.generation_id,
                    store.source["accepted_seq"],
                ),
            )
            connection.execute(
                "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
                "support_kind,span) VALUES (%s,%s,%s,%s,%s,'source',%s) ON CONFLICT DO NOTHING",
                (*store.key, memory_id, evidence_id, passage_id, Jsonb(support_locator)),
            )
            store.insert_unit(
                connection,
                unit_id=memory_id,
                kind="fact",
                text=fact.text,
                vector=vector,
                locator=support_locator,
                evidence_id=evidence_id,
                memory_id=memory_id,
                mentioned_at=message.event_time,
                source_created_at=metadata.source_created_at,
                source_updated_at=metadata.source_updated_at,
                ordinal=fact.output_index,
            )
            for entity in fact.entities:
                connection.execute(
                    "INSERT INTO unit_entities VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (*store.key, memory_id, entity.casefold().strip()),
                )
            support_first = chunk.start + chunk.text.index(fact.quote)
            support_last = support_first + len(fact.quote)
            for index, annotation in enumerate(message.temporal_annotations):
                first = message.content.index(annotation.quote)
                if support_first <= first and first + len(annotation.quote) <= support_last:
                    store.insert_annotation(
                        connection,
                        memory_id,
                        annotation,
                        origin="caller",
                        ordinal=index,
                        reference_timezone=metadata.reference_timezone,
                    )
            for index, annotation in enumerate(fact.annotations):
                authoritative = [
                    supplied
                    for supplied in message.temporal_annotations
                    if support_first + fact.quote.index(annotation.quote)
                    < message.content.index(supplied.quote) + len(supplied.quote)
                    and message.content.index(supplied.quote)
                    < support_first + fact.quote.index(annotation.quote) + len(annotation.quote)
                ]
                if authoritative:
                    fields = ("role", "start", "end")
                    if any(
                        any(
                            getattr(annotation, field) != getattr(supplied, field)
                            for field in fields
                        )
                        for supplied in authoritative
                    ):
                        diagnostics.append(
                            {
                                "passage_id": passage_id,
                                "output_index": fact.output_index,
                                "field": "annotations",
                                "code": "caller_annotation_precedence",
                            }
                        )
                    continue
                store.insert_annotation(
                    connection,
                    memory_id,
                    annotation,
                    origin="generative_extraction",
                    ordinal=index,
                    reference_timezone=metadata.reference_timezone,
                )
        for fact in result.facts:
            for kind in ("caused_by", "updates"):
                for target in getattr(fact, kind):
                    connection.execute(
                        "INSERT INTO unit_links SELECT %s,%s,%s,%s,%s,1 "
                        "WHERE EXISTS(SELECT 1 FROM search_units WHERE tenant_id=%s "
                        "AND user_id=%s AND unit_id=%s) AND EXISTS(SELECT 1 FROM search_units "
                        "WHERE tenant_id=%s AND user_id=%s AND unit_id=%s) ON CONFLICT DO NOTHING",
                        (
                            *store.key,
                            identifiers[fact.output_index],
                            identifiers[target],
                            kind,
                            *store.key,
                            identifiers[fact.output_index],
                            *store.key,
                            identifiers[target],
                        ),
                    )
    return list(identifiers.values())


def _insights(store, models, new_fact_ids):
    from oam.db.eligibility import ELIGIBLE_UNITS

    if not new_fact_ids:
        with store.transaction() as connection:
            store.guard(connection)
            store.readiness(connection, insights_ready=True, insight_outcome="no_insight")
        return

    while True:
        with store.transaction() as connection:
            store.guard(connection)
            examined, last_outcome = store.l2_progress(connection)
            pending_rows = connection.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}) "
                "SELECT unit_id FROM eligible "
                "WHERE kind='fact' AND source_id=%s AND source_version=%s "
                "AND unit_id=ANY(%s) AND NOT (unit_id=ANY(%s)) "
                "ORDER BY created_at,unit_id LIMIT %s",
                (
                    store.source["source_id"],
                    store.source["source_version"],
                    new_fact_ids,
                    list(examined),
                    store.config.extraction.max_facts_per_group,
                ),
            ).fetchall()
        if not pending_rows:
            with store.transaction() as connection:
                store.guard(connection)
                store.readiness(connection, insights_ready=True, insight_outcome=last_outcome)
            return

        seed_ids = [row[0] for row in pending_rows]
        with store.transaction() as connection:
            store.guard(connection)
            rows = connection.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}), seeds AS ("
                "SELECT unit_id,embedding FROM eligible WHERE kind='fact' AND unit_id=ANY(%s)"
                "), candidate_annotations AS ("
                "SELECT candidate.unit_id,candidate.content_text,candidate.source_id,"
                "candidate.source_version,candidate.mentioned_at,candidate.source_created_at,"
                "candidate.source_updated_at,candidate.embedding,"
                "jsonb_agg(candidate_times.detail ORDER BY candidate_times.annotation_id) "
                "FILTER (WHERE candidate_times.annotation_id IS NOT NULL) AS annotations "
                "FROM eligible candidate "
                "LEFT JOIN unit_times candidate_times ON (candidate_times.tenant_id,"
                "candidate_times.user_id,candidate_times.unit_id)=(candidate.tenant_id,"
                "candidate.user_id,candidate.unit_id) "
                "WHERE candidate.kind='fact' "
                "GROUP BY candidate.unit_id,candidate.content_text,candidate.source_id,"
                "candidate.source_version,candidate.mentioned_at,candidate.source_created_at,"
                "candidate.source_updated_at,candidate.embedding"
                "), ranked AS ("
                "SELECT candidate.unit_id,candidate.content_text,candidate.source_id,"
                "candidate.source_version,candidate.mentioned_at,candidate.source_created_at,"
                "candidate.source_updated_at,candidate.annotations,"
                "min(candidate.embedding <=> seeds.embedding) AS distance "
                "FROM candidate_annotations candidate CROSS JOIN seeds "
                "GROUP BY candidate.unit_id,candidate.content_text,candidate.source_id,"
                "candidate.source_version,candidate.mentioned_at,candidate.source_created_at,"
                "candidate.source_updated_at,candidate.annotations"
                ") SELECT unit_id,content_text,source_id,source_version,mentioned_at,"
                "source_created_at,source_updated_at,annotations FROM ranked "
                "ORDER BY (unit_id=ANY(%s)) DESC,distance,unit_id LIMIT %s",
                (seed_ids, seed_ids, store.config.extraction.max_facts_per_group),
            ).fetchall()

        facts = tuple(
            InsightInput(
                id=row[0],
                text=row[1],
                source_refs=(f"{row[2]}:{row[3]}",),
                mentioned_at=row[4],
                temporal_annotations=tuple(
                    _insight_temporal_annotation(value) for value in (row[7] or [])
                ),
            )
            for row in rows
        )
        facts = pack_insight_facts(facts, store.config)
        if len(rows) >= 2 and len(facts) < 2:
            raise ValueError("Insight input budget cannot fit two supporting facts")
        examined_group = [fact.id for fact in facts if fact.id in seed_ids]
        if not examined_group:
            examined_group = seed_ids[:1]

        if len(facts) < 2:
            with store.transaction() as connection:
                store.guard(connection)
                store.mark_l2_group(connection, examined_group, "no_insight")
            continue

        input_values = [fact.model_dump(mode="json") for fact in facts]
        result = InsightResult.model_validate(
            store.checkpoint(
                "insight",
                input_values,
                lambda facts=facts: synthesize_insight(
                    models,
                    facts=facts,
                    config=store.config,
                    timeout_seconds=store.config.model.attempt_timeout_seconds,
                ),
            )
        )
        if result.text:
            vector = store.checkpoint(
                "insight_embed",
                result.text,
                lambda text=result.text: models.embed(
                    (text,), timeout_seconds=store.config.model.attempt_timeout_seconds
                ),
            )
            digest = hashlib.sha256(json.dumps(input_values, sort_keys=True).encode()).digest()
            identifier = stable_id("insight", store.generation_id, digest.hex())
            with store.transaction() as connection:
                store.guard(connection)
                removed = connection.execute(
                    "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
                    "AND removal_target_kind='insight' AND removal_target_id=%s",
                    (*store.key, identifier),
                ).fetchone()
                if removed:
                    store.mark_l2_group(connection, examined_group, result.outcome)
                    continue
                eligible = connection.execute(
                    f"WITH eligible AS ({ELIGIBLE_UNITS}) "
                    "SELECT unit_id FROM eligible WHERE unit_id=ANY(%s)",
                    (list(result.support_ids),),
                ).fetchall()
                if {row[0] for row in eligible} != set(result.support_ids):
                    raise ValueError("Insight support became ineligible")
                connection.execute(
                    "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,"
                    "subtype,input_digest,recipe_id,generation_id,visible_from) "
                    "VALUES (%s,%s,%s,1,%s,%s,%s,'oam-insight-v2',%s,%s) ON CONFLICT DO NOTHING",
                    (
                        *store.key,
                        identifier,
                        result.text,
                        result.subtype,
                        digest,
                        store.generation_id,
                        store.source["accepted_seq"],
                    ),
                )
                for memory_id in result.support_ids:
                    connection.execute(
                        "INSERT INTO insight_support VALUES "
                        "(%s,%s,%s,1,%s,%s) ON CONFLICT DO NOTHING",
                        (*store.key, identifier, memory_id, MEMORY_REVISION),
                    )
                store.insert_unit(
                    connection,
                    unit_id=identifier,
                    kind="insight",
                    text=result.text,
                    vector=vector["vectors"][0],
                    locator={"support_ids": list(result.support_ids), "seed_ids": examined_group},
                    insight_id=identifier,
                )
        with store.transaction() as connection:
            store.guard(connection)
            store.mark_l2_group(connection, examined_group, result.outcome)
            store.readiness(connection, insights_ready=False, insight_outcome=result.outcome)


def _insight_temporal_annotation(value):
    from oam.contracts.evidence import TemporalAnnotation

    if not isinstance(value, dict):
        raise ValueError("invalid persisted Insight temporal annotation")
    return TemporalAnnotation.model_validate(
        {key: value[key] for key in TemporalAnnotation.model_fields if key in value}
    )
