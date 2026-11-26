import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter

from oam.config import ModelsConfig, load_config
from oam.contracts.resources import EvidenceResourceCreate
from oam.indexing.identity import index_generation
from oam.indexing.store import input_digest, stable_id
from oam.models.embedding_inputs import EmbeddingAdmission
from oam.models.runtime import EmbeddingResult, ModelOutputError, ModelResult, Usage
from oam.tokens import TokenCounter
from oam.workers import index_source

MEDICAL_FIXTURES = Path(__file__).parents[2] / "conformance/fixtures/medical"


class MemoryStore:
    def require_source_recipe(self):
        pass

    def __init__(self, *, content, media_type="text/plain", input_role="evidence_resource"):
        self.config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config.model_copy(
            update={"models": ModelsConfig()}
        )
        self.key = ("tenant", "scope")
        self.claim = SimpleNamespace(operation_id="operation")
        self.authority = None
        self.generation_id = "generation"
        self.source = {
            "source_id": "document",
            "source_version": 3,
            "accepted_seq": 1,
            "input_role": input_role,
            "media_type": media_type,
            "original_bytes": content,
        }
        self.units = []
        self.statements = []
        self.stages = {}
        self.completed_stages = []
        self.finished = False
        self.embedding_receipts = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, parameters):
        self.statements.append((query, parameters))
        if "SELECT o.operation_id,o.stage_state FROM operations o JOIN work_units w" in query:
            return SimpleNamespace(fetchone=lambda: (self.claim.operation_id, self.stages))
        if "SELECT stage_state FROM operations" in query:
            return SimpleNamespace(fetchone=lambda: (self.stages,))
        if "lifecycle_state='excluded'" in query:
            return SimpleNamespace(fetchone=lambda: None)

    def guard(self, connection):
        pass

    def resume_source(self):
        return {}, {}, []

    def resource_passages(self):
        return {}

    def resource_embedding_checkpoints(self):
        return list(self.embedding_receipts)

    def check_retry_claim(self):
        raise AssertionError("document fixture does not expect overflow retries")

    def checkpoint(self, stage, inputs, produce):
        result = produce()
        self.completed_stages.append(stage)
        output = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if stage.startswith("passage:resource_"):
            self.embedding_receipts.append((stage, input_digest(inputs), output))
        return output

    def insert_unit(self, connection, **unit):
        self.units.append(unit)

    def readiness(self, connection, **stages):
        self.stages.update(stages)

    def mark_deferred_failure_stage(self, stage):
        self.stages["failed_stage"] = stage

    @contextmanager
    def model_pipeline(self):
        yield

    def resource_metadata(self):
        content = self.source["original_bytes"]
        return EvidenceResourceCreate(
            resource_id=self.source["source_id"],
            media_type=self.source["media_type"],
            filename="document.txt",
            byte_length=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def summarize_usage(self, connection):
        pass

    def finish(
        self,
        *,
        schedule_consolidation=False,
        inline_semantic=False,
        enqueue_semantic=False,
        connection=None,
    ):
        if enqueue_semantic:
            assert connection is self
            assert self.stages["memory_ready"] and self.stages["temporal_links_ready"]
            assert self.stages["semantic_links_status"] == "pending"
        self.finished = True

    def fail(self, error):
        raise error


def reject_inline_linking(monkeypatch):
    # Extraction tests stop at queued handoff; PostgreSQL tests own linking.
    from oam.workers import semantic_links

    def complete(store, *, inline):
        pytest.fail("new source unexpectedly ran semantic linking inline")

    monkeypatch.setattr(semantic_links, "process_semantic_claim", complete)


class LocalModels:
    def __init__(self, *, vision_blocks=None, fail_abstract=False, fail_vision=False):
        self.embedded = []
        self.generated_roles = []
        self.abstract_inputs = []
        self.fail_abstract = fail_abstract
        self.fail_vision = fail_vision
        self.vision_blocks = vision_blocks or [
            {"text": "Subject SYN-001"},
            {"text": "Glucose 92 mg/dL", "region": [0.1, 0.2, 0.9, 0.8]},
        ]

    def embed(self, texts, **kwargs):
        self.embedded.extend(texts)
        return EmbeddingResult(vectors=tuple((1.0, 0.0) for _ in texts), usage=Usage())

    def generate(self, role, **kwargs):
        self.generated_roles.append(role)
        if role == "abstract":
            self.abstract_inputs.append(json.loads(kwargs["user"])["passages"])
        if role == "abstract" and self.fail_abstract:
            raise RuntimeError("abstract model unavailable")
        content = (
            {"abstract": "A document summary."}
            if role == "abstract"
            else {"outcome": "no_facts", "facts": []}
        )
        return ModelResult(content=json.dumps(content), usage=Usage())

    def generate_vision(self, image, **kwargs):
        self.generated_roles.append("extraction")
        if self.fail_vision:
            raise RuntimeError("vision model unavailable")
        return ModelResult(
            content=json.dumps({"blocks": self.vision_blocks}),
            usage=Usage(),
        )


def test_document_abstract_admission_counts_system_and_user_before_model_call():
    store = MemoryStore(content=b"One cited sentence.")
    store.config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"max_input_tokens": 10})}
    )
    models = LocalModels()
    try:
        index_source._resource(store, models)
    except ValueError as error:
        assert "abstract input" in str(error)
    else:
        raise AssertionError("oversize complete abstract request was dispatched")
    assert models.generated_roles == []


@pytest.mark.parametrize("blank_prefix", ("", "\n" * 4096))
def test_plain_text_resource_preserves_exact_chunks_and_document_revision(blank_prefix):
    text = "  Dose <10 mg.\n\n" + "Read literally <tag> & keep spaces.\n" * 200
    original = blank_prefix + text
    store = MemoryStore(content=original.encode())
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={"target_estimated_tokens": 512, "max_estimated_tokens": 768}
                    )
                }
            )
        }
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert len(store.units) > 1
    assert "".join(unit["text"] for unit in store.units).lstrip("\n") == text
    assert models.embedded == [unit["text"] for unit in store.units]
    assert all(value.strip() for value in models.embedded)
    assert models.generated_roles == ["abstract"]
    for index, unit in enumerate(store.units):
        assert unit["kind"] == "resource_passage"
        locator = unit["locator"]
        assert locator["document_id"] == "document"
        assert locator["revision"] == 3
        assert locator["block"] == f"text:{index}"
        assert {"start", "end"}.isdisjoint(locator)
        from oam.tokens import resolve_counter

        assert (
            resolve_counter(store.config.tokenizer).count(unit["text"])
            <= store.config.chunk.ingestion.max_estimated_tokens
        )
    evidence_values = [
        parameters
        for query, parameters in store.statements
        if query.startswith("INSERT INTO evidence")
    ]
    assert evidence_values
    assert all(parameters[-1] == original for parameters in evidence_values)
    assert store.stages["abstract_state"] == "current"


@pytest.fixture
def text_embedding_batches():
    passages = ("alpha.\n\n", "beta.\n\n", "gamma.\n\n", "delta.")
    store = MemoryStore(content="".join(passages).encode())
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={"target_estimated_tokens": 5, "max_estimated_tokens": 9}
                    )
                }
            ),
            "resource": store.config.resource.model_copy(update={"generate_abstract": False}),
        }
    )

    class BatchModels(LocalModels):
        def __init__(self, max_items, max_tokens=15):
            super().__init__()
            self.calls = []
            self.embedding_admission = EmbeddingAdmission(
                TokenCounter("character-fixture", len), 9, max_tokens, max_items, False
            )

        def embed(self, texts, **kwargs):
            self.embedding_admission.validate(texts)
            self.calls.append(texts)
            return EmbeddingResult(
                vectors=tuple((float(ord(text[0])), 1.0) for text in texts), usage=Usage()
            )

    return store, passages, BatchModels


@pytest.mark.parametrize("max_items,expected_sizes", [(3, [2, 2]), (1, [1, 1, 1, 1])])
def test_text_resource_embeddings_batch_under_provider_limits(
    text_embedding_batches, max_items, expected_sizes
):
    store, passages, models_type = text_embedding_batches
    models = models_type(max_items)

    index_source._resource(store, models)

    assert [len(call) for call in models.calls] == expected_sizes
    assert [unit["text"] for unit in store.units] == list(passages)
    assert [unit["vector"] for unit in store.units] == [[97, 1], [98, 1], [103, 1], [100, 1]]
    assert store.stages["abstract_state"] == "skipped"


def test_text_resource_batch_reuses_recipe_qualified_singleton(text_embedding_batches):
    store, passages, models_type = text_embedding_batches
    evidence_id = stable_id("resource_evidence", "generation", "document", 3)
    first_id = stable_id("resource_passage", evidence_id, "text:0")
    store.embedding_receipts.append(
        (
            f"passage:{first_id}:recipe=legacy",
            input_digest(passages[0]),
            {"vectors": [[10, 1]], "usage": {}},
        )
    )
    models = models_type(1)

    index_source._resource(store, models)

    assert models.calls == [(text,) for text in passages[1:]]
    assert store.units[0]["vector"] == [10, 1]


@pytest.mark.parametrize(
    "item_limit,request_limit,retained_body",
    [(86, 200, "  tai"), (200, 86, "  tai"), (81, 200, None), (200, 82, None)],
)
def test_plain_text_opening_bounds_provider_input_and_reuses_its_digest(
    item_limit, request_limit, retained_body
):
    first = "\t".join(["x"] * 40) + "\n\n"
    body = "  tail punctuation remains evidence."
    store = MemoryStore(content=(first + body).encode())
    passages = {"text:0": first[:40], "text:1": first[40:], "text:2": body}
    store.resource_passages = lambda: passages
    store.config = store.config.model_copy(
        update={
            "resource": store.config.resource.model_copy(
                update={"plain_text_embedding_opening": True}
            )
        }
    )
    store.generation_id = index_generation(store.config, {})
    admission = EmbeddingAdmission(
        TokenCounter("character-fixture", len, lambda text, limit: text[:limit]),
        item_limit,
        request_limit,
        2,
        False,
    )
    models = LocalModels()
    models.embedding_admission = admission
    index_source._resource(store, models)

    expected = body if retained_body is None else " ".join(["x"] * 40) + "\n\n" + retained_body
    assert models.embedded[-1] == expected
    assert [unit["text"] for unit in store.units] == list(passages.values())
    assert [unit["locator"]["block"] for unit in store.units] == list(passages)
    digests = dict(
        pair for _, _, receipt in store.embedding_receipts for pair in receipt["resource_passages"]
    )
    assert digests[store.units[-1]["unit_id"]] == input_digest(expected).hex()
    resumed = LocalModels()
    resumed.embedding_admission = admission
    index_source._resource(store, resumed)
    assert resumed.embedded == []


def test_plain_text_opening_changes_only_the_enabled_index_generation(tmp_path, monkeypatch):
    monkeypatch.setattr("oam.indexing.identity.INDEX_RECIPE", "oam-source-fact-insight-v39")
    config = MemoryStore(content=b"original").config
    legacy = "gen_021fa876afbd8157b2d0e07b28ce303bd00df04437fe72976bbc63d0f45b37dd"
    assert index_generation(config, {}) == legacy
    enabled = config.model_copy(
        update={
            "resource": config.resource.model_copy(update={"plain_text_embedding_opening": True})
        }
    )
    assert index_generation(enabled, {}) != legacy
    for value, expected in [("false", legacy), ("true", index_generation(enabled, {}))]:
        path = tmp_path / "recipe.yaml"
        path.write_text(f"resource:\n  plain_text_embedding_opening: {value}\n")
        loaded = load_config(path).config.model_copy(update={"models": ModelsConfig()})
        assert index_generation(loaded, {}) == expected


@pytest.mark.parametrize("admitted_opening", [False, True])
def test_text_resource_embedding_recipe_follows_admitted_generation(admitted_opening):
    body = "A clause names the party."
    store = MemoryStore(content=body.encode())
    admitted_config = store.config.model_copy(
        update={
            "resource": store.config.resource.model_copy(
                update={"plain_text_embedding_opening": admitted_opening}
            )
        }
    )
    store.generation_id = index_generation(admitted_config, {})
    store.config = store.config.model_copy(
        update={
            "resource": store.config.resource.model_copy(
                update={"plain_text_embedding_opening": not admitted_opening}
            )
        }
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert models.embedded == [f"{body}\n\n{body}" if admitted_opening else body]
    assert [unit["text"] for unit in store.units] == [body]


def test_plain_text_opening_keeps_complete_small_inputs_and_html_body():
    for media_type, content, expected in [
        ("text/plain", "\tTitle\n  And body. ", "Title And body.\n\n\tTitle\n  And body. "),
        ("text/html", "<p>A body.</p>", "A body."),
    ]:
        store = MemoryStore(content=content.encode(), media_type=media_type)
        store.config = store.config.model_copy(
            update={
                "resource": store.config.resource.model_copy(
                    update={"plain_text_embedding_opening": True}
                )
            }
        )
        store.generation_id = index_generation(store.config, {})
        models = LocalModels()
        index_source._resource(store, models)
        assert models.embedded == [expected]


@pytest.mark.parametrize("published", [0, 1])
def test_text_resource_retry_reuses_legacy_and_batch_embeddings_after_regrouping(
    text_embedding_batches, published, monkeypatch
):
    store, passages, models_type = text_embedding_batches
    evidence_id = stable_id("resource_evidence", "generation", "document", 3)
    first_id = stable_id("resource_passage", evidence_id, "text:0")
    if published:
        store.embedding_receipts.append(
            (f"passage:{first_id}", input_digest(passages[0]), {"vectors": [[10, 1]], "usage": {}})
        )

    insert = store.insert_unit

    def interrupt_publication(connection, **unit):
        if len(store.units) == published:
            raise RuntimeError("simulated interruption after embedding checkpoint")
        return insert(connection, **unit)

    with monkeypatch.context() as failed:
        failed.setattr(store, "insert_unit", interrupt_publication)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            index_source._resource(store, models_type(2))

    saved_passages = {unit["locator"]["block"]: unit["text"] for unit in store.units}
    monkeypatch.setattr(store, "resource_passages", lambda: saved_passages)
    store.units.clear()
    resumed = models_type(1)
    index_source._resource(store, resumed)

    assert resumed.calls == ([(passages[3],)] if published else [(passages[2],), (passages[3],)])
    assert [unit["text"] for unit in store.units] == list(passages)
    assert store.units[0]["vector"] == ([10, 1] if published else [97, 1])


def test_partial_text_resource_preserves_published_chunks_after_limit_change():
    from oam.config import IngestionChunkConfig

    content = "甲乙丙丁戊己庚辛壬癸子丑"
    store = MemoryStore(content=content.encode())
    limits = IngestionChunkConfig(
        target_estimated_tokens=4,
        max_estimated_tokens=6,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )
    store.config = store.config.model_copy(
        update={"chunk": store.config.chunk.model_copy(update={"ingestion": limits})}
    )
    store.resource_passages = lambda: {"text:0": "甲乙", "text:1": "丙丁戊己"}
    index_source._resource(store, LocalModels())
    assert [item["text"] for item in store.units[:2]] == ["甲乙", "丙丁戊己"]
    assert "".join(item["text"] for item in store.units) == content


def test_resource_retry_rejects_conflicting_published_block_without_writing():
    from oam.resources.types import ResourceParseError

    store = MemoryStore(content=b"<p>New parsed text.</p>", media_type="text/html")
    store.resource_passages = lambda: {"removed-block": "previous text"}
    with pytest.raises(ResourceParseError, match="published Resource passages"):
        index_source._resource(store, LocalModels())
    assert store.statements == []
    assert store.units == []


def test_html_resource_also_publishes_document_identity_and_revision():
    store = MemoryStore(content=b"<p>Dose 10 mg.</p>", media_type="text/html")

    index_source._resource(store, LocalModels())

    assert store.units[0]["text"] == "Dose 10 mg."
    assert store.units[0]["locator"]["document_id"] == "document"
    assert store.units[0]["locator"]["revision"] == 3
    assert store.units[0]["locator"]["block"] == "html:block:0001"


def test_native_pdf_resource_indexes_page_text_with_page_citation():
    fixture = MEDICAL_FIXTURES / "synthetic-lab-report.pdf.b64"
    original = base64.b64decode(fixture.read_text(encoding="ascii"))
    store = MemoryStore(content=original, media_type="application/pdf")
    models = LocalModels()

    index_source._resource(store, models)

    assert len(store.units) == 1
    assert "Sodium: 140 mmol/L" in store.units[0]["text"]
    assert "Glucose: 92 mg/dL" in store.units[0]["text"]
    assert store.units[0]["locator"]["block"] == "pdf:page:0001"
    assert store.units[0]["locator"]["page"] == 1
    assert store.units[0]["locator"]["extraction_method"] == "native_text"
    assert models.generated_roles == ["abstract"]


def test_native_pdf_splits_large_pages_into_exact_bounded_page_spans(monkeypatch):
    from oam.resources import pdf_isolated
    from oam.resources.pdf_isolated import InspectedPdfPage
    from oam.tokens import estimate_units, estimated_tokens

    text = "Manual heading\n" + "    run --safe; preserve 5 mg and do not restart.\n" * 100
    original = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={"target_estimated_tokens": 256, "max_estimated_tokens": 512}
                    )
                }
            )
        }
    )
    monkeypatch.setattr(
        pdf_isolated,
        "inspect_pdf",
        lambda *_: (InspectedPdfPage("pdf:page:0001", 1, text, False),),
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert len(store.units) > 1
    assert "".join(unit["text"] for unit in store.units) == text
    for unit in store.units:
        locator = unit["locator"]
        assert locator["page"] == 1 and locator["parse_revision"] == 2
        assert locator["extraction_method"] == "native_text"
        assert text[locator["start"] : locator["end"]] == unit["text"]
        assert estimated_tokens(estimate_units(unit["text"])) <= 512
    passage_rows = [
        parameters
        for query, parameters in store.statements
        if query.startswith("INSERT INTO passages")
    ]
    assert all(parameters[4] == 2 for parameters in passage_rows)
    assert models.generated_roles == ["abstract"]


def test_partial_native_pdf_preserves_published_spans_after_limit_change(monkeypatch):
    from oam.config import IngestionChunkConfig
    from oam.resources import pdf_isolated
    from oam.resources.pdf_isolated import InspectedPdfPage

    text = "甲乙丙丁戊己庚辛壬癸子丑"
    original = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    limits = IngestionChunkConfig(
        target_estimated_tokens=4,
        max_estimated_tokens=6,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )
    store.config = store.config.model_copy(
        update={"chunk": store.config.chunk.model_copy(update={"ingestion": limits})}
    )
    store.resource_passages = lambda: {"pdf:page:0001:span:0:2": "甲乙"}
    monkeypatch.setattr(
        pdf_isolated,
        "inspect_pdf",
        lambda *_: (InspectedPdfPage("pdf:page:0001", 1, text, False),),
    )
    index_source._resource(store, LocalModels())
    assert store.units[0]["locator"]["block"] == "pdf:page:0001:span:0:2"
    assert "".join(item["text"] for item in store.units) == text


def test_caller_annotated_pdf_keeps_legacy_page_text_locator_and_annotation(monkeypatch):
    from oam.contracts.resources import ResourceTemporalAnnotation
    from oam.resources import pdf_isolated
    from oam.resources.pdf_isolated import InspectedPdfPage

    text = "Dose\n    5 mg\n" + "Retain the sealed backup.\n" * 100
    legacy_text = "Dose 5 mg " + " ".join(["Retain the sealed backup."] * 100)
    original = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    annotation = ResourceTemporalAnnotation(
        locator="pdf:page:0001",
        role="date_reference",
        start="2026-10-07T00:00:00Z",
        end="2026-10-08T00:00:00Z",
        quote="Dose 5 mg",
    )
    metadata = store.resource_metadata().model_copy(update={"temporal_annotations": [annotation]})
    monkeypatch.setattr(store, "resource_metadata", lambda: metadata)
    inserted = []
    monkeypatch.setattr(
        store, "insert_annotation", lambda *args, **kwargs: inserted.append(args), raising=False
    )
    monkeypatch.setattr(
        pdf_isolated,
        "inspect_pdf",
        lambda *_: (InspectedPdfPage("pdf:page:0001", 1, text, False),),
    )
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={"target_estimated_tokens": 64, "max_estimated_tokens": 128}
                    )
                }
            )
        }
    )

    index_source._resource(store, LocalModels())

    assert len(store.units) == 1
    assert store.units[0]["text"] == legacy_text
    assert store.units[0]["locator"]["block"] == annotation.locator
    assert store.units[0]["locator"]["parse_revision"] == 1
    assert inserted[0][2] == annotation
    passage_rows = [
        parameters
        for query, parameters in store.statements
        if query.startswith("INSERT INTO passages")
    ]
    assert passage_rows[0][4] == 1


@pytest.mark.parametrize("max_input_tokens", (180, 8192))
def test_resource_abstract_preserves_complete_input_or_covers_later_passages(max_input_tokens):
    from oam.indexing.extraction import model_input_tokens

    content = "".join(
        f"<p>Section {index}: retain precise labels, commands and exceptions.</p>"
        for index in range(10)
    )
    store = MemoryStore(content=content.encode(), media_type="text/html")
    store.config = store.config.model_copy(
        update={
            "extraction": store.config.extraction.model_copy(
                update={"max_input_tokens": max_input_tokens}
            )
        }
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert models.generated_roles == ["abstract"]
    passages = models.abstract_inputs[0]
    assert model_input_tokens(index_source.ABSTRACT_SYSTEM, {"passages": passages}) <= (
        max_input_tokens
    )
    assert passages[0]["text"].startswith("Section 0:")
    assert passages[-1]["text"].startswith("Section 9:")
    assert [item["locator"] for item in passages] == sorted(item["locator"] for item in passages)
    if max_input_tokens == 8192:
        assert [item["text"] for item in passages] == [unit["text"] for unit in store.units]
        assert store.stages["abstract_coverage"] == "full"
    else:
        assert len(passages) < len(store.units)
        assert store.stages["abstract_coverage"] == "partial"


def test_resource_abstract_skips_oversized_passage_and_uses_later_detail():
    content = "<p>" + "introductory prose " * 400 + "</p><p>Retain the sealed backup.</p>"
    store = MemoryStore(content=content.encode(), media_type="text/html")
    store.config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"max_input_tokens": 160})}
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert models.abstract_inputs == [
        [{"locator": "html:block:0002", "text": "Retain the sealed backup."}]
    ]
    assert store.stages["abstract_coverage"] == "partial"


def test_image_resource_indexes_vision_text_with_region_citation():
    fixture = MEDICAL_FIXTURES / "synthetic-lab-report.png.b64"
    original = base64.b64decode(fixture.read_text(encoding="ascii"))
    store = MemoryStore(content=original, media_type="image/png")
    models = LocalModels()

    index_source._resource(store, models)

    assert [unit["text"] for unit in store.units] == ["Subject SYN-001", "Glucose 92 mg/dL"]
    assert store.units[1]["locator"]["block"] == "image:0001:region:0002"
    assert store.units[1]["locator"]["image"] == 1
    assert store.units[1]["locator"]["region"] == [0.1, 0.2, 0.9, 0.8]
    assert store.units[1]["locator"]["extraction_method"] == "vision"
    assert models.embedded == ["Subject SYN-001", "Glucose 92 mg/dL"]


@pytest.mark.parametrize("image_format,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_image_resource_downscales_vision_input_and_preserves_original_and_regions(
    image_format, media_type
):
    output = BytesIO()
    Image.new("RGB", (20, 10), color=(17, 91, 140)).save(output, format=image_format)
    original = output.getvalue()
    store = MemoryStore(content=original, media_type=media_type)
    store.config = store.config.model_copy(
        update={"resource": store.config.resource.model_copy(update={"max_pixels_per_image": 100})}
    )
    received = []

    class InspectingModels(LocalModels):
        def generate_vision(self, image, **kwargs):
            with Image.open(BytesIO(image)) as decoded:
                received.append((decoded.size, decoded.format, kwargs["media_type"]))
            return super().generate_vision(image, **kwargs)

    index_source._resource(store, InspectingModels())

    assert received == [((14, 7), image_format, media_type)]
    assert store.source["original_bytes"] == original
    assert store.units[1]["locator"]["region"] == [0.1, 0.2, 0.9, 0.8]
    assert store.stages["source_searchable"] is True


def test_fact_completion_cap_does_not_raise_resource_vision_or_abstract_budgets():
    original = base64.b64decode(
        (MEDICAL_FIXTURES / "synthetic-lab-report.png.b64").read_text(encoding="ascii")
    )
    store = MemoryStore(content=original, media_type="image/png")
    budgets = []

    class BudgetModels(LocalModels):
        def generate(self, role, **kwargs):
            budgets.append((role, kwargs["max_output_tokens"]))
            return super().generate(role, **kwargs)

        def generate_vision(self, image, **kwargs):
            budgets.append(("extraction", kwargs["max_output_tokens"]))
            return super().generate_vision(image, **kwargs)

    index_source._resource(store, BudgetModels())
    assert budgets == [("extraction", 4096), ("abstract", 4096)]


def test_invalid_vision_output_does_not_complete_vision_checkpoint():
    original = base64.b64decode(
        (MEDICAL_FIXTURES / "synthetic-lab-report.png.b64").read_text(encoding="ascii")
    )
    store = MemoryStore(content=original, media_type="image/png")

    with pytest.raises(ModelOutputError, match="visual extraction") as failure:
        index_source._resource(store, LocalModels(vision_blocks=[{"text": ""}]))

    assert failure.value.usage is not None
    assert "vision:image:0001" not in store.completed_stages


def test_scanned_pdf_resource_renders_page_for_vision_and_cites_page():
    fixture = MEDICAL_FIXTURES / "synthetic-lab-report.png.b64"
    image_bytes = base64.b64decode(fixture.read_text(encoding="ascii"))
    pdf_bytes = BytesIO()
    with Image.open(BytesIO(image_bytes)) as image:
        image.convert("RGB").save(pdf_bytes, format="PDF")
    store = MemoryStore(content=pdf_bytes.getvalue(), media_type="application/pdf")
    models = LocalModels()

    index_source._resource(store, models)

    assert "Glucose 92 mg/dL" in [unit["text"] for unit in store.units]
    assert store.units[1]["locator"]["block"] == "pdf:page:0001:region:0002"
    assert store.units[1]["locator"]["page"] == 1
    assert store.units[1]["locator"]["extraction_method"] == "vision"
    assert models.generated_roles == ["extraction", "abstract"]


def test_pdf_inspection_marks_unusable_text_for_vision(monkeypatch):
    from oam.resources import pdf_isolated
    from oam.resources.pdf_isolated import InspectedPdfPage

    original = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    monkeypatch.setattr(
        pdf_isolated,
        "inspect_pdf",
        lambda original, limits: (
            InspectedPdfPage(locator="pdf:page:0001", page_number=1, text="", needs_vision=True),
        ),
    )
    store = MemoryStore(content=original, media_type="application/pdf")
    models = LocalModels()

    index_source._resource(store, models)

    assert models.generated_roles == ["extraction", "abstract"]
    assert all("�" not in unit["text"] for unit in store.units)
    assert all(unit["locator"]["extraction_method"] == "vision" for unit in store.units)


def test_pdf_page_with_native_text_and_large_image_indexes_both_details():
    native_bytes = base64.b64decode(
        (MEDICAL_FIXTURES / "synthetic-lab-report.pdf.b64").read_text(encoding="ascii")
    )
    image_bytes = base64.b64decode(
        (MEDICAL_FIXTURES / "synthetic-lab-report.png.b64").read_text(encoding="ascii")
    )
    image_pdf = BytesIO()
    with Image.open(BytesIO(image_bytes)) as image:
        image.convert("RGB").save(image_pdf, format="PDF")
    image_writer = PdfWriter()
    image_writer.append(BytesIO(image_pdf.getvalue()))
    image_page = image_writer.pages[0]
    image_page.scale_to(612, 792)
    writer = PdfWriter()
    writer.append(BytesIO(native_bytes))
    writer.pages[0].merge_page(image_page)
    mixed_pdf = BytesIO()
    writer.write(mixed_pdf)
    store = MemoryStore(content=mixed_pdf.getvalue(), media_type="application/pdf")
    models = LocalModels(vision_blocks=[{"text": "Potassium 4.2 mmol/L"}])

    index_source._resource(store, models)

    assert any("Sodium: 140 mmol/L" in unit["text"] for unit in store.units)
    assert "Potassium 4.2 mmol/L" in [unit["text"] for unit in store.units]
    assert models.generated_roles == ["extraction", "abstract"]


def test_mixed_pdf_deduplicates_native_and_vision_wording_on_same_page():
    original = (MEDICAL_FIXTURES / "bilingual/en-mixed-native-image.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    models = LocalModels(
        vision_blocks=[{"text": "Sodium 140 mmol/L"}, {"text": "Glucose 92 mg/dL"}]
    )

    index_source._resource(store, models)

    native = [unit for unit in store.units if unit["locator"]["extraction_method"] == "native_text"]
    visual = [
        unit["text"] for unit in store.units if unit["locator"]["extraction_method"] == "vision"
    ]
    assert len(native) == 1
    assert "Sodium: 140 mmol/L" in native[0]["text"]
    assert visual == ["Glucose 92 mg/dL"]
    assert models.generated_roles == ["extraction", "abstract"]


def test_pdf_visual_dedup_preserves_distinct_numeric_meanings():
    matches = index_source._native_contains_visual_observation

    assert matches("Sodium: 140 mmol/L", "Sodium 140 mmol/L")
    assert not matches("Glucose: 9.2 mg/dL", "Glucose 92 mg/dL")
    assert not matches("Potassium: <2.0 mmol/L", "Potassium 2.0 mmol/L")
    assert not matches("Temperature: -2 C", "Temperature 2 C")
    assert not matches("Creatinine: 1.25 mg/dL", "Creatinine 1.2")
    assert not matches("Dose 5 mg/kg", "Dose 5 mg")


def test_small_logo_on_native_pdf_does_not_request_vision():
    native = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    image = Image.new("RGB", (100, 100), "#2563eb")
    image_pdf = BytesIO()
    image.save(image_pdf, format="PDF")
    logo = PdfWriter()
    logo.append(BytesIO(image_pdf.getvalue()))
    logo.pages[0].scale_to(100, 100)
    writer = PdfWriter()
    writer.append(BytesIO(native))
    writer.pages[0].merge_page(logo.pages[0])
    output = BytesIO()
    writer.write(output)
    store = MemoryStore(content=output.getvalue(), media_type="application/pdf")
    models = LocalModels()

    index_source._resource(store, models)

    assert models.generated_roles == ["abstract"]
    assert store.units[0]["locator"]["extraction_method"] == "native_text"


def test_pdf_vector_table_keeps_native_text_and_requests_visual_rows():
    original = (MEDICAL_FIXTURES / "bilingual/en-vector-table.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    models = LocalModels(
        vision_blocks=[
            {"text": "Glucose 92 mg/dL"},
            {"text": "Glucose row has a reference interval of 70-99 mg/dL"},
        ]
    )

    index_source._resource(store, models)

    assert any("Glucose 92 mg/dL" in unit["text"] for unit in store.units)
    visual = [unit for unit in store.units if unit["locator"]["extraction_method"] == "vision"]
    assert [unit["text"] for unit in visual] == [
        "Glucose row has a reference interval of 70-99 mg/dL"
    ]
    assert models.generated_roles == ["extraction", "abstract"]


@pytest.mark.parametrize("language", ("en", "zh"))
def test_long_allergy_pdf_indexes_every_native_page_without_vision(language):
    original = (MEDICAL_FIXTURES / f"bilingual/{language}-allergy-native.pdf").read_bytes()
    store = MemoryStore(content=original, media_type="application/pdf")
    models = LocalModels()

    index_source._resource(store, models)

    assert [unit["locator"]["page"] for unit in store.units] == [1, 2, 3]
    assert [unit["locator"]["block"] for unit in store.units] == [
        "pdf:page:0001",
        "pdf:page:0002",
        "pdf:page:0003",
    ]
    assert all(unit["locator"]["extraction_method"] == "native_text" for unit in store.units)
    assert models.generated_roles == ["abstract"]


def test_abstract_failure_keeps_passages_searchable_with_failed_abstract_state():
    store = MemoryStore(content=b"<p>Glucose 92 mg/dL</p>", media_type="text/html")

    with pytest.raises(RuntimeError, match="abstract model unavailable"):
        index_source._resource(store, LocalModels(fail_abstract=True))

    assert store.stages["source_searchable"] is True
    assert store.stages["abstract_state"] == "failed"
    assert store.stages["abstract_coverage"] == "none"
    assert [unit["text"] for unit in store.units] == ["Glucose 92 mg/dL"]


@pytest.mark.parametrize("native_page", [1, 2])
def test_scanned_pdf_page_failure_preserves_native_page_as_partial(native_page):
    native = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    scanned = (MEDICAL_FIXTURES / "bilingual/en-scanned.pdf").read_bytes()
    writer = PdfWriter()
    for original in (native, scanned) if native_page == 1 else (scanned, native):
        writer.append(BytesIO(original))
    combined = BytesIO()
    writer.write(combined)
    store = MemoryStore(content=combined.getvalue(), media_type="application/pdf")

    with pytest.raises(RuntimeError, match="vision model unavailable"):
        index_source._resource(store, LocalModels(fail_vision=True))

    assert any("Glucose 92 mg/dL" in unit["text"] for unit in store.units)
    assert {unit["locator"]["page"] for unit in store.units} == {native_page}
    assert store.stages["source_searchable"] is True
    assert store.stages["parse_coverage"] == "partial"
    assert store.stages["abstract_state"] == "pending"


def test_pdf_vision_failure_keeps_error_when_annotated_block_is_unavailable(monkeypatch):
    from oam.contracts.resources import ResourceTemporalAnnotation

    writer = PdfWriter()
    for filename in ("en-scanned.pdf", "en-native.pdf"):
        writer.append(BytesIO((MEDICAL_FIXTURES / "bilingual" / filename).read_bytes()))
    combined = BytesIO()
    writer.write(combined)
    store = MemoryStore(content=combined.getvalue(), media_type="application/pdf")
    annotation = ResourceTemporalAnnotation(
        locator="pdf:page:0001:block:0001",
        role="date_reference",
        start="2026-10-07T00:00:00Z",
        end="2026-10-08T00:00:00Z",
        quote="Subject SYN-001",
    )
    metadata = store.resource_metadata().model_copy(update={"temporal_annotations": [annotation]})
    monkeypatch.setattr(store, "resource_metadata", lambda: metadata)

    with pytest.raises(RuntimeError, match="vision model unavailable"):
        index_source._resource(store, LocalModels(fail_vision=True))

    assert store.stages["failed_stage"] == "vision:pdf:page:0001"
    assert store.units == []


def test_interaction_locator_keeps_original_message_order_and_internal_id(monkeypatch):
    request = {
        "source_id": "document",
        "source_version": 3,
        "expected_active_version": 2,
        "messages": [
            {
                "message_id": "skipped",
                "role": "assistant",
                "content": "Report-derived answer.",
                "event_time": "2026-09-23T00:00:00Z",
                "learning_eligible": False,
            },
            {
                "message_id": "retained",
                "role": "user",
                "content": "A source statement.",
                "event_time": "2026-09-23T00:00:01Z",
                "learning_eligible": True,
            },
        ],
    }
    store = MemoryStore(content=json.dumps(request).encode(), input_role="interaction")
    store.config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0})}
    )
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    from oam.indexing import links

    monkeypatch.setattr(links, "build_temporal_links", lambda *args, **kwargs: {})
    reject_inline_linking(monkeypatch)

    assert index_source.process_one(
        store.config, "queue", "content", models=LocalModels(), worker_id="worker"
    )

    assert store.finished
    assert len(store.units) == 1
    assert store.units[0]["locator"]["message_index"] == 1
    assert store.units[0]["locator"]["message_id"] == "retained"


def test_partial_interaction_reuses_published_passage_boundaries():
    from oam.config import IngestionChunkConfig
    from oam.contracts.evidence import EvidenceAppendRequest

    request = EvidenceAppendRequest.model_validate(
        {
            "source_id": "document",
            "messages": [
                {
                    "message_id": "first",
                    "role": "user",
                    "content": "甲乙丙丁戊己庚辛壬癸子丑",
                    "event_time": "2026-09-23T00:00:01Z",
                    "learning_eligible": True,
                }
            ],
        }
    )
    store = MemoryStore(content=b"", input_role="interaction")
    limits = IngestionChunkConfig(
        target_estimated_tokens=4,
        max_estimated_tokens=6,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(update={"ingestion": limits}),
            "extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0}),
        }
    )
    evidence_id = index_source.stable_id("evidence", "generation", "document", 3, "first")
    store.resume_source = lambda: (
        {
            0: [
                (0, "甲乙", index_source.stable_id("passage", evidence_id, 0)),
                (1, "丙丁戊己", index_source.stable_id("passage", evidence_id, 1)),
            ]
        },
        {},
        [],
    )
    index_source._index_interaction(store.config, store, LocalModels(), request)
    assert [unit["text"] for unit in store.units[:2]] == ["甲乙", "丙丁戊己"]
    assert "".join(unit["text"] for unit in store.units) == request.messages[0].content


@pytest.mark.parametrize(
    "group_max_estimated_output_tokens",
    [300, 1200],
    ids=["separate-original-groups", "partially-published-group"],
)
def test_resume_reuses_extraction_checkpoints_across_a_completed_passage(
    group_max_estimated_output_tokens,
):
    from oam.contracts.evidence import EvidenceAppendRequest
    from oam.indexing.extraction import Fact, FactExtraction

    first = "Saved alpha. "
    middle = "The retained middle passage was already indexed. " * 8
    last = "Saved beta."
    texts = (first, middle, last)
    request = EvidenceAppendRequest(
        source_id="document",
        messages=[
            {
                "message_id": "statement",
                "role": "user",
                "content": "".join(texts),
                "event_time": "2026-09-23T00:00:01Z",
                "learning_eligible": True,
            }
        ],
    )
    evidence_id = index_source.stable_id("evidence", "generation", "document", 3, "statement")
    passage_ids = [index_source.stable_id("passage", evidence_id, i) for i in range(3)]
    published_id = "middle-fact"
    retained = {
        f"extract:{passage_ids[i]}": {
            **FactExtraction(
                facts=(Fact(output_index=0, text=text.strip(), quote=text.strip()),),
                outcome="facts",
                usage=Usage(),
            ).model_dump(mode="json"),
            "_passage_ids": [passage_ids[i]],
        }
        for i, text in ((0, first), (2, last))
    }

    class ResumingStore(MemoryStore):
        def resume_source(self):
            return (
                {0: [(i, text, passage_ids[i]) for i, text in enumerate(texts)]},
                {passage_ids[1]: {published_id}},
                [],
            )

        def checkpoint(self, stage, inputs, produce):
            if stage in retained:
                assert inputs["text"] in (first, last)
                return retained[stage]
            return super().checkpoint(stage, inputs, produce)

    store = ResumingStore(content=b"", input_role="interaction")
    config = store.config.model_copy(
        update={
            "extraction": store.config.extraction.model_copy(
                update={
                    "group_max_estimated_output_tokens": group_max_estimated_output_tokens,
                    "reference_max_facts": 0,
                }
            ),
        }
    )
    models = LocalModels()
    ids, _ = index_source._index_interaction(config, store, models, request)

    assert {unit["text"] for unit in store.units if unit["kind"] == "fact"} == {
        "Saved alpha.",
        "Saved beta.",
    }
    assert published_id in ids
    assert models.generated_roles == []


@pytest.mark.parametrize("resume", [False, True])
def test_interaction_preserves_assistant_attribution_at_fact_publication(resume):
    from oam.contracts.evidence import EvidenceAppendRequest
    from oam.indexing.extraction import Fact, FactExtraction

    texts = (
        "Consider reserving 20 minutes for the demonstration.",
        "I reserved 10 minutes for setup.",
        "I reproduced the timeout with two workers.",
    )
    roles = ("assistant", "user", "assistant")
    claims = (
        "The user will spend 20 minutes on the demonstration.",
        "The user reserved 10 minutes for setup.",
        "The timeout was reproduced with two workers.",
    )
    request = EvidenceAppendRequest(
        source_id="document",
        messages=[
            {
                "message_id": f"message-{i}",
                "role": role,
                "content": message,
                "event_time": "2026-09-23T00:00:01Z",
                "learning_eligible": True,
            }
            for i, (role, message) in enumerate(zip(roles, texts, strict=True))
        ],
    )
    passage_ids = [
        index_source.stable_id(
            "passage",
            index_source.stable_id("evidence", "generation", "document", 3, f"message-{i}"),
            0,
        )
        for i in range(len(texts))
    ]
    categories = ("preference", "experience", "fact")
    model_facts = [
        {"passage_id": f"p{i}", "text": text, "quote": quote, "category": category}
        for i, (text, quote, category) in enumerate(zip(claims, texts, categories, strict=True))
    ]
    retained = FactExtraction(
        facts=tuple(
            Fact.model_validate({**fact, "passage_id": passage_ids[i], "output_index": i})
            for i, fact in enumerate(model_facts)
        ),
        outcome="facts",
        usage=Usage(),
    ).model_dump(mode="json")

    class InteractionStore(MemoryStore):
        def checkpoint(self, stage, inputs, produce):
            if resume and stage.startswith("extract:"):
                assert len(inputs["passages"]) == 3
                return {**retained, "_passage_ids": passage_ids}
            return super().checkpoint(stage, inputs, produce)

    class InteractionModels(LocalModels):
        def generate(self, role, **kwargs):
            assert not resume and role == "extraction"
            self.generated_roles.append(role)
            return ModelResult(
                content=json.dumps({"outcome": "facts", "facts": model_facts}), usage=Usage()
            )

    store = InteractionStore(content=b"", input_role="interaction")
    config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0})}
    )
    models = InteractionModels()
    fact_ids, _ = index_source._index_interaction(config, store, models, request)

    expected = [
        "Assistant: Consider reserving 20 minutes for the demonstration.",
        "The user reserved 10 minutes for setup.",
        "Assistant: I reproduced the timeout with two workers.",
    ]
    units = [unit for unit in store.units if unit["kind"] == "fact"]
    assert [unit["text"] for unit in units] == expected
    assert [unit["locator"]["role"] for unit in units] == list(roles)
    assert [unit["locator"]["quote"] for unit in units] == list(texts)
    assert models.embedded[-3:] == expected
    memories = [
        args for query, args in store.statements if query.startswith("INSERT INTO memories")
    ]
    assert [(args[3], args[4]) for args in memories] == list(zip(expected, categories, strict=True))
    assert fact_ids == [
        index_source.stable_id("memory", passage_id, i, claims[i])
        for i, passage_id in enumerate(passage_ids)
    ]
    assert models.generated_roles == ([] if resume else ["extraction"])


@pytest.mark.parametrize("source_kind", ["interaction", "text", "native_pdf"])
def test_existing_source_chunking_paths_prefer_embedding_counter(source_kind, monkeypatch):
    from oam.config import TokenizerConfig
    from oam.contracts.evidence import EvidenceAppendRequest
    from oam.models.embedding_inputs import configured_embedding_admission
    from oam.resources.pdf_isolated import InspectedPdfPage

    text = "x" * 80
    store = MemoryStore(content=text.encode())
    defaults = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    assert defaults.models.embedding is not None
    binding = defaults.models.embedding.model_copy(
        update={"tokenizer": TokenizerConfig(type="cl100k_base")}
    )
    store.config = store.config.model_copy(
        update={
            "tokenizer": TokenizerConfig(type="oam-unicode-quarter-v1"),
            "models": ModelsConfig(embedding=binding),
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={"target_estimated_tokens": 12, "max_estimated_tokens": 12}
                    )
                }
            ),
            "extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0}),
        }
    )
    models = LocalModels()
    models.embedding_admission = configured_embedding_admission(store.config)
    if source_kind == "interaction":
        request = EvidenceAppendRequest.model_validate(
            {
                "source_id": "document",
                "messages": [
                    {
                        "message_id": "first",
                        "role": "user",
                        "content": text,
                        "learning_eligible": True,
                        "event_time": "2026-11-07T00:00:00Z",
                    }
                ],
            }
        )
        index_source._index_interaction(store.config, store, models, request)
    else:
        if source_kind == "native_pdf":
            from oam.resources import pdf_isolated

            store.source.update(
                media_type="application/pdf",
                original_bytes=(MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes(),
            )
            monkeypatch.setattr(
                pdf_isolated,
                "inspect_pdf",
                lambda original, limits: (
                    InspectedPdfPage("pdf:page:0001", 1, text, needs_vision=False),
                ),
            )
        index_source._resource(store, models)
    assert [unit["text"] for unit in store.units] == [text]


def test_partial_interaction_does_not_extract_a_published_fact_again():
    from oam.contracts.evidence import EvidenceAppendRequest

    request = EvidenceAppendRequest.model_validate(
        {
            "source_id": "document",
            "messages": [
                {
                    "message_id": "first",
                    "role": "user",
                    "content": "A stored fact.",
                    "event_time": "2026-09-23T00:00:01Z",
                    "learning_eligible": True,
                }
            ],
        }
    )
    store = MemoryStore(content=b"", input_role="interaction")
    store.config = store.config.model_copy(
        update={"extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0})}
    )
    evidence_id = index_source.stable_id("evidence", "generation", "document", 3, "first")
    passage_id = index_source.stable_id("passage", evidence_id, 0)
    diagnostic = {"passage_id": passage_id, "field": "quote", "code": "invalid_quote"}
    store.resume_source = lambda: (
        {0: [(0, "A stored fact.", passage_id)]},
        {passage_id: {"fact"}},
        [diagnostic],
    )
    models = LocalModels()
    fact_ids, diagnostics = index_source._index_interaction(store.config, store, models, request)
    assert fact_ids == ["fact"]
    assert models.generated_roles == []
    assert models.embedded == []
    assert diagnostics == [diagnostic]
    assert store.stages["extraction_diagnostics"] == [diagnostic]


@pytest.mark.parametrize("sibling", ["local_refusal", "paid_split", "unknown_response"])
def test_concurrent_extraction_refusals_report_zero_only_for_local_failures(sibling):
    from oam.contracts.evidence import EvidenceAppendRequest
    from oam.indexing.budget import GenerationBudgetExceeded

    request = EvidenceAppendRequest(
        source_id="document",
        messages=[
            {
                "message_id": text,
                "role": "user",
                "content": text,
                "event_time": "2026-09-23T00:00:00Z",
                "learning_eligible": True,
            }
            for text in ("First.", "Second.")
        ],
    )
    store = MemoryStore(content=b"", input_role="interaction")
    config = store.config.model_copy(
        update={
            "provider": store.config.provider.model_copy(update={"llm_concurrency": 2}),
            "extraction": store.config.extraction.model_copy(
                update={"group_max_passages": 1, "reference_max_facts": 0}
            ),
        }
    )
    rendezvous = threading.Barrier(2, timeout=3)

    class RefusingModels(LocalModels):
        def generate(self, role, **kwargs):
            rendezvous.wait()
            rejection = GenerationBudgetExceeded("source budget exhausted")
            if json.loads(kwargs["user"])["center"]["text"] == "Second.":
                if sibling == "unknown_response":
                    raise ModelOutputError("missing provider response")
                if sibling == "paid_split":
                    rejection.usage = Usage(total_tokens=27, coverage="complete")
            raise rejection

    expected = GenerationBudgetExceeded if sibling == "local_refusal" else RuntimeError
    with pytest.raises(expected) as caught:
        index_source._index_interaction(config, store, RefusingModels(), request)
    assert not rendezvous.broken
    if sibling == "local_refusal":
        assert caught.value.usage.total_tokens == 0
        assert caught.value.usage.coverage == "complete"


def test_fact_publication_persists_its_diagnostics_with_the_fact(monkeypatch):
    from oam.indexing.chunker import TextChunk
    from oam.indexing.extraction import Fact, FactExtraction

    store = MemoryStore(content=b"", input_role="interaction")
    message = SimpleNamespace(content="A stored fact.", event_time=None, temporal_annotations=[])
    chunk = TextChunk("A stored fact.", 0, 14, "", 4)
    result = FactExtraction(
        outcome="facts",
        facts=(
            Fact(
                output_index=0,
                text="A stored fact.",
                quote="A stored fact.",
                related_existing_ids=("unavailable",),
            ),
        ),
        usage=Usage(),
    )
    monkeypatch.setattr(index_source, "eligible_reference_ids", lambda *args: set())
    diagnostics = []
    index_source._publish_facts(
        store,
        {"passage": (message, "evidence", {}, chunk)},
        result,
        ((1.0, 0.0),),
        texts=("A stored fact.",),
        metadata=SimpleNamespace(source_created_at=None, source_updated_at=None),
        diagnostics=diagnostics,
    )
    assert diagnostics[0]["code"] == "ineligible_reference"
    assert store.stages["extraction_diagnostics"] == diagnostics


def test_extraction_sliding_window_embeds_ready_facts_before_slow_sibling_finishes(monkeypatch):
    request = {
        "source_id": "document",
        "messages": [
            {
                "message_id": f"message-{index}",
                "role": "user",
                "content": f"Statement {index}.",
                "event_time": "2026-09-23T00:00:00Z",
                "learning_eligible": True,
            }
            for index in range(9)
        ],
    }
    fact_embedded = Event()
    sibling_started = Event()
    lock = Lock()
    active = maximum = 0
    published = []

    class InteractionStore(MemoryStore):
        def checkpoints(self, stages):
            with ThreadPoolExecutor(max_workers=len(stages)) as executor:
                futures = [executor.submit(self.checkpoint, *stage) for stage in stages]
                return [future.result() for future in futures]

    class PipelineModels(LocalModels):
        def generate(self, role, **kwargs):
            nonlocal active, maximum
            text = json.loads(kwargs["user"])["center"]["text"]
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if text == "Statement 0.":
                    assert sibling_started.wait(2), "extraction did not overlap"
                elif text == "Statement 1.":
                    sibling_started.set()
                    assert fact_embedded.wait(2), "ready facts waited for a slow extraction sibling"
                return ModelResult(
                    content=json.dumps(
                        {"outcome": "facts", "facts": [{"text": text, "quote": text}]}
                    ),
                    usage=Usage(),
                )
            finally:
                with lock:
                    active -= 1

        def embed(self, texts, **kwargs):
            if texts == ("Statement 0.",):
                assert len(store.units) <= 4, "source preparation exceeded the bounded window"
                fact_embedded.set()
            return super().embed(texts, **kwargs)

    store = InteractionStore(content=json.dumps(request).encode(), input_role="interaction")
    store.config = store.config.model_copy(
        update={
            "provider": store.config.provider.model_copy(update={"llm_concurrency": 2}),
            "extraction": store.config.extraction.model_copy(
                update={"group_max_passages": 1, "reference_max_facts": 0}
            ),
        }
    )
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    from oam.indexing import links

    monkeypatch.setattr(links, "build_temporal_links", lambda *args, **kwargs: {})
    reject_inline_linking(monkeypatch)

    def publish(_store, _members, result, _vectors, **kwargs):
        published.extend(fact.text for fact in result.facts)
        return [fact.text for fact in result.facts]

    monkeypatch.setattr(index_source, "_publish_facts", publish)

    assert index_source.process_one(
        store.config, "queue", "content", models=PipelineModels(), worker_id="worker"
    )
    assert fact_embedded.is_set()
    assert maximum == 2 and active == 0
    assert sorted(published) == sorted(f"Statement {index}." for index in range(9))
    assert [unit["locator"]["message_id"] for unit in store.units] == [
        f"message-{index}" for index in range(9)
    ]
    assert store.finished and store.stages["extraction_coverage"] == "complete"


def test_ready_later_extraction_group_publishes_before_slow_earlier_group(monkeypatch):
    request = {
        "source_id": "document",
        "messages": [
            {
                "message_id": f"message-{index}",
                "role": "user",
                "content": f"Statement {index}.",
                "event_time": "2026-09-23T00:00:00Z",
                "learning_eligible": True,
            }
            for index in range(2)
        ],
    }
    first_started = Event()
    release_first = Event()
    later_fact_embedded = Event()

    class OutOfOrderModels(LocalModels):
        def generate(self, role, **kwargs):
            if role != "extraction":
                return super().generate(role, **kwargs)
            text = json.loads(kwargs["user"])["center"]["text"]
            if text == "Statement 0.":
                first_started.set()
                assert release_first.wait(2), "slow first extraction was not released"
            return ModelResult(
                content=json.dumps(
                    {
                        "outcome": "facts",
                        "facts": [{"text": f"Fact for {text}", "quote": text}],
                    }
                ),
                usage=Usage(),
            )

        def embed(self, texts, **kwargs):
            if texts == ("Fact for Statement 1.",):
                later_fact_embedded.set()
            return super().embed(texts, **kwargs)

    store = MemoryStore(content=json.dumps(request).encode(), input_role="interaction")
    store.config = store.config.model_copy(
        update={
            "provider": store.config.provider.model_copy(update={"llm_concurrency": 2}),
            "extraction": store.config.extraction.model_copy(
                update={"group_max_passages": 1, "reference_max_facts": 0}
            ),
        }
    )
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    from oam.indexing import links

    monkeypatch.setattr(links, "build_temporal_links", lambda *args, **kwargs: {})
    reject_inline_linking(monkeypatch)
    published = []

    def publish(_store, owners, result, _vectors, **kwargs):
        owner = owners[next(iter(owners))][0].message_id
        published.extend((fact.text, owner) for fact in result.facts)
        return [fact.text for fact in result.facts]

    monkeypatch.setattr(index_source, "_publish_facts", publish)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        index_source.process_one,
        store.config,
        "queue",
        "content",
        models=OutOfOrderModels(),
        worker_id="worker",
    )
    try:
        assert first_started.wait(1), "first extraction did not start"
        assert later_fact_embedded.wait(1), (
            "a ready later extraction group waited for the slow earlier group"
        )
    finally:
        release_first.set()
        assert future.result(timeout=3)
        executor.shutdown(wait=True)

    assert sorted(published) == [
        ("Fact for Statement 0.", "message-0"),
        ("Fact for Statement 1.", "message-1"),
    ]


def test_interaction_pipeline_overlaps_next_extraction_with_previous_publish(monkeypatch):
    request = {
        "source_id": "document",
        "messages": [
            {
                "message_id": f"message-{index}",
                "role": "user",
                "content": f"Statement {index}.",
                "event_time": "2026-09-23T00:00:00Z",
                "learning_eligible": True,
            }
            for index in range(3)
        ],
    }

    class PipelineStore(MemoryStore):
        def checkpoints(self, stages):
            return [self.checkpoint(*stage) for stage in stages]

    store = PipelineStore(content=json.dumps(request).encode(), input_role="interaction")
    store.config = store.config.model_copy(
        update={
            "provider": store.config.provider.model_copy(update={"llm_concurrency": 2}),
            "extraction": store.config.extraction.model_copy(
                update={"group_max_passages": 1, "reference_max_facts": 0}
            ),
        }
    )
    second_extraction_started = threading.Event()
    embedding_started = threading.Event()
    overlap = threading.Event()
    extraction_count = 0
    extraction_lock = threading.Lock()
    active_calls = 0
    max_active_calls = 0
    active_lock = threading.Lock()

    class OverlapModels(LocalModels):
        def generate(self, role, **kwargs):
            nonlocal extraction_count, active_calls, max_active_calls
            with extraction_lock:
                extraction_count += 1
                call_number = extraction_count
            with active_lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                if call_number == 2:
                    second_extraction_started.set()
                    assert embedding_started.wait(1)
                if role == "extraction":
                    center = json.loads(kwargs["user"])["center"]
                    return ModelResult(
                        content=json.dumps(
                            {
                                "outcome": "facts",
                                "facts": [
                                    {
                                        "text": "The user made a concrete statement.",
                                        "quote": center["text"],
                                    }
                                ],
                            }
                        ),
                        usage=Usage(),
                    )
                return super().generate(role, **kwargs)
            finally:
                with active_lock:
                    active_calls -= 1

        def embed(self, texts, **kwargs):
            nonlocal active_calls, max_active_calls
            with active_lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                if (
                    texts == ("The user made a concrete statement.",)
                    and not embedding_started.is_set()
                ):
                    assert second_extraction_started.wait(1)
                    with active_lock:
                        assert 2 <= active_calls <= 3
                    overlap.set()
                    embedding_started.set()
                return super().embed(texts, **kwargs)
            finally:
                with active_lock:
                    active_calls -= 1

    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    from oam.indexing import links

    monkeypatch.setattr(links, "build_temporal_links", lambda *args, **kwargs: {})
    reject_inline_linking(monkeypatch)
    assert index_source.process_one(
        store.config, "queue", "content", models=OverlapModels(), worker_id="worker"
    )
    assert embedding_started.is_set()
    assert overlap.is_set()
    assert 2 <= max_active_calls <= 3
    assert active_calls == 0


def test_caller_quote_crossing_chunks_preserves_passage_time_without_coordinates():
    from oam.contracts.evidence import EvidenceAppendRequest

    content = "Apple harvest\nwas in 2025."
    request = EvidenceAppendRequest(
        source_id="document",
        messages=[
            {
                "message_id": "m",
                "role": "user",
                "content": content,
                "event_time": "2026-01-01T00:00:00Z",
                "learning_eligible": True,
                "temporal_annotations": [
                    {
                        "role": "event",
                        "start": "2025-01-01T00:00:00Z",
                        "end": "2026-01-01T00:00:00Z",
                        "quote": "harvest\nwas in 2025",
                    }
                ],
            }
        ],
    )
    store = MemoryStore(content=b"", input_role="interaction")
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={
                    "ingestion": store.config.chunk.ingestion.model_copy(
                        update={
                            "target_estimated_tokens": 4,
                            "max_estimated_tokens": 5,
                        }
                    ),
                }
            ),
            "extraction": store.config.extraction.model_copy(update={"reference_max_facts": 0}),
        }
    )
    recorded = []
    store.insert_annotation = lambda connection, unit_id, annotation, **kwargs: recorded.append(
        (unit_id, annotation.model_dump(mode="json"))
    )
    index_source._index_interaction(store.config, store, LocalModels(), request)
    assert len(recorded) >= 2
    assert all(detail["quote"] == "harvest\nwas in 2025" for _, detail in recorded)
    assert all({"span_start", "span_end"}.isdisjoint(detail) for _, detail in recorded)
    assert "".join(unit["text"] for unit in store.units) == content
    assert [unit["locator"]["passage_index"] for unit in store.units] == list(
        range(len(store.units))
    )
    assert all({"start", "end"}.isdisjoint(unit["locator"]) for unit in store.units)


def test_worker_indexes_new_resource_in_existing_namespace_after_recipe_change(monkeypatch):
    store = MemoryStore(content=b"A source remains indexable after an upgrade.")
    store.generation_id = "existing-user-namespace"
    store.require_source_recipe = lambda: pytest.fail(
        "namespace must not be used as a recipe fence"
    )
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)

    assert index_source.process_one(
        store.config, "queue", "content", models=LocalModels(), worker_id="worker"
    )
    assert store.finished
    assert any(
        unit["text"] == "A source remains indexable after an upgrade." for unit in store.units
    )
    assert store.generation_id == "existing-user-namespace"


def test_resource_can_skip_abstract_without_skipping_searchable_evidence():
    from oam.config import ResourceConfig
    from oam.indexing.identity import index_generation

    store = MemoryStore(content=b"Full original document evidence.")
    original_identity = index_generation(store.config, {})
    resource = ResourceConfig.model_validate(
        {**store.config.resource.model_dump(), "generate_abstract": False}
    )
    store.config = store.config.model_copy(update={"resource": resource})
    models = LocalModels()

    index_source._resource(store, models)

    assert models.generated_roles == []
    assert models.embedded == ["Full original document evidence."]
    assert store.stages["source_searchable"] is True
    assert store.stages["parse_coverage"] == "complete"
    assert store.stages["abstract_state"] == "skipped"
    assert store.stages["abstract_coverage"] == "none"
    assert index_generation(store.config, {}) == original_identity


def test_worker_finishes_resource_with_intentionally_skipped_abstract(monkeypatch):
    store = MemoryStore(content=b"Searchable evidence without derivative metadata.")
    store.config = store.config.model_copy(
        update={"resource": store.config.resource.model_copy(update={"generate_abstract": False})}
    )
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(
        index_source, "claim_one", lambda *args, **kwargs: SimpleNamespace(stage="index_source")
    )
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    models = LocalModels()

    assert index_source.process_one(
        store.config, "queue", "content", models=models, worker_id="worker"
    )
    assert store.finished
    assert store.stages["abstract_state"] == "skipped"
    assert store.stages["parse_coverage"] == "complete"
    assert models.generated_roles == []


def test_skipping_abstract_preserves_required_pdf_vision():
    store = MemoryStore(
        content=(MEDICAL_FIXTURES / "bilingual/en-scanned.pdf").read_bytes(),
        media_type="application/pdf",
    )
    store.config = store.config.model_copy(
        update={"resource": store.config.resource.model_copy(update={"generate_abstract": False})}
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert models.generated_roles == ["extraction"]
    assert store.stages["abstract_state"] == "skipped"
    assert store.stages["parse_coverage"] == "complete"
    assert store.units and models.embedded
