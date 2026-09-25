import base64
import hashlib
import json
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfWriter

from oam.config import load_config
from oam.contracts.resources import EvidenceResourceCreate
from oam.models.runtime import EmbeddingResult, ModelOutputError, ModelResult, Usage
from oam.workers import index_source

MEDICAL_FIXTURES = Path(__file__).parents[2] / "conformance/fixtures/medical"


class MemoryStore:
    def __init__(self, *, content, media_type="text/plain", input_role="evidence_resource"):
        self.config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
        self.key = ("tenant", "scope")
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

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, parameters):
        self.statements.append((query, parameters))

    def guard(self, connection):
        pass

    def checkpoint(self, stage, inputs, produce):
        result = produce()
        self.completed_stages.append(stage)
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def insert_unit(self, connection, **unit):
        self.units.append(unit)

    def readiness(self, connection, **stages):
        self.stages.update(stages)

    def mark_deferred_failure_stage(self, stage):
        self.stages["failed_stage"] = stage

    def resource_metadata(self):
        content = self.source["original_bytes"]
        return EvidenceResourceCreate(
            resource_id=self.source["source_id"],
            media_type=self.source["media_type"],
            filename="document.txt",
            byte_length=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def finish(self):
        self.finished = True

    def fail(self, error):
        raise error


class LocalModels:
    def __init__(self, *, vision_blocks=None, fail_abstract=False, fail_vision=False):
        self.embedded = []
        self.generated_roles = []
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
        if role == "abstract" and self.fail_abstract:
            raise RuntimeError("abstract model unavailable")
        content = (
            {"abstract": "A document summary."}
            if role == "abstract"
            else {"outcome": "no_facts", "facts": []}
        )
        return ModelResult(content=json.dumps(content), usage=Usage())

    def generate_vision(self, image, **kwargs):
        self.generated_roles.append("document_vision")
        if self.fail_vision:
            raise RuntimeError("vision model unavailable")
        return ModelResult(
            content=json.dumps({"blocks": self.vision_blocks}),
            usage=Usage(),
        )


def test_plain_text_resource_preserves_exact_chunks_and_document_revision():
    text = "  Dose <10 mg.\n\n" + "Read literally <tag> & keep spaces.\n" * 200
    store = MemoryStore(content=text.encode())
    store.config = store.config.model_copy(
        update={
            "chunk": store.config.chunk.model_copy(
                update={"target_estimated_tokens": 512, "max_estimated_tokens": 768}
            )
        }
    )
    models = LocalModels()

    index_source._resource(store, models)

    assert len(store.units) > 1
    assert "".join(unit["text"] for unit in store.units) == text
    assert models.embedded == [unit["text"] for unit in store.units]
    assert models.generated_roles == ["abstract"]
    for index, unit in enumerate(store.units):
        assert unit["kind"] == "resource_passage"
        locator = unit["locator"]
        assert locator["document_id"] == "document"
        assert locator["revision"] == 3
        assert locator["block"] == f"text:{index}"
        assert text[locator["start"] : locator["end"]] == unit["text"]
        assert len(unit["text"]) <= store.config.chunk.max_estimated_tokens * 4
    evidence_values = [
        parameters
        for query, parameters in store.statements
        if query.startswith("INSERT INTO evidence")
    ]
    assert all(parameters[-1] == text for parameters in evidence_values)
    assert store.stages["abstract_state"] == "current"


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
    assert models.generated_roles == ["document_vision", "abstract"]


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

    assert models.generated_roles == ["document_vision", "abstract"]
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
    assert models.generated_roles == ["document_vision", "abstract"]


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
    assert models.generated_roles == ["document_vision", "abstract"]


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
    assert models.generated_roles == ["document_vision", "abstract"]


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


def test_later_scanned_pdf_page_failure_preserves_earlier_page_as_partial():
    native = (MEDICAL_FIXTURES / "bilingual/en-native.pdf").read_bytes()
    scanned = (MEDICAL_FIXTURES / "bilingual/en-scanned.pdf").read_bytes()
    writer = PdfWriter()
    writer.append(BytesIO(native))
    writer.append(BytesIO(scanned))
    combined = BytesIO()
    writer.write(combined)
    store = MemoryStore(content=combined.getvalue(), media_type="application/pdf")

    with pytest.raises(RuntimeError, match="vision model unavailable"):
        index_source._resource(store, LocalModels(fail_vision=True))

    assert any("Glucose 92 mg/dL" in unit["text"] for unit in store.units)
    assert all(unit["locator"]["page"] == 1 for unit in store.units)
    assert store.stages["source_searchable"] is True
    assert store.stages["parse_coverage"] == "partial"
    assert store.stages["abstract_state"] == "pending"


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
    connection = SimpleNamespace(cursor=lambda: None)

    @contextmanager
    def connect(*args, **kwargs):
        yield connection

    monkeypatch.setattr(index_source.psycopg, "connect", connect)
    monkeypatch.setattr(index_source, "claim_one", lambda *args, **kwargs: object())
    monkeypatch.setattr(index_source, "IndexStore", lambda *args, **kwargs: store)
    from oam.indexing import links

    monkeypatch.setattr(links, "build_links", lambda *args: {})

    assert index_source.process_one(
        store.config, "queue", "content", models=LocalModels(), worker_id="worker"
    )

    assert store.finished
    assert len(store.units) == 1
    assert store.units[0]["locator"]["message_index"] == 1
    assert store.units[0]["locator"]["message_id"] == "retained"
