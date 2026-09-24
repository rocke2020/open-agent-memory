import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from oam.config import load_config
from oam.contracts.resources import EvidenceResourceCreate
from oam.models.runtime import EmbeddingResult, ModelResult, Usage
from oam.workers import index_source


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
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def insert_unit(self, connection, **unit):
        self.units.append(unit)

    def readiness(self, connection, **stages):
        self.stages.update(stages)

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
    def __init__(self):
        self.embedded = []
        self.generated_roles = []

    def embed(self, texts, **kwargs):
        self.embedded.extend(texts)
        return EmbeddingResult(vectors=tuple((1.0, 0.0) for _ in texts), usage=Usage())

    def generate(self, role, **kwargs):
        self.generated_roles.append(role)
        content = (
            {"abstract": "A document summary."}
            if role == "abstract"
            else {"outcome": "no_facts", "facts": []}
        )
        return ModelResult(content=json.dumps(content), usage=Usage())


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
