"""Frozen retrieval workload for entity reuse, with raw timings and final source support."""

import json
import os
import time
from pathlib import Path

import pytest
from test_core_http import core_database as core_database
from test_recall_repository import TransactionPool, add_fact
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.indexing.entities import EntityRepository
from oam.models.runtime import EmbeddingResult, Usage
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration
QUERIES = (
    ("cron", "daily-digest breakfast news schedule"),
    ("weekly", "weekly-survey Friday"),
    ("product", "OrbitApp design files"),
    ("timezone", "Asia/Tokyo timezone"),
    ("person", "Alex Riverside surgeon"),
    ("alias", "Silver Workshop designer"),
)


def test_entity_retrieval_frozen_workload(recall_database, tmp_path):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    arm = os.environ.get("OAM_ENTITY_PARITY_ARM", "candidate")
    assert arm in ("baseline", "candidate")
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    objects, gold = {}, {key: set() for key, _ in QUERIES}
    dimensions = config.index.embedding_dimensions
    rows = [
        (
            "cron",
            f"cron-{i:03d}",
            "daily-digest",
            f"The daily-digest job I mentioned delivered breakfast news on execution {i}.",
        )
        for i in range(96)
    ]
    rows += [
        ("weekly", "weekly", "weekly-survey", "The weekly-survey job runs every Friday."),
        ("product", "product", "OrbitApp", "OrbitApp synchronizes design files."),
        (
            "timezone",
            "timezone",
            "Asia/Tokyo",
            "The user schedules work in the Asia/Tokyo timezone.",
        ),
        ("person", "surgeon", "Alex", "Alex is a surgeon at Riverside."),
        ("baker", "baker", "Alex", "Another Alex is a baker at Hilltop."),
        (
            "alias",
            "studio",
            "Silver Workshop",
            "Silver Workshop, also called SW, hired a designer.",
        ),
        ("alias", "studio-use", "SW", "SW moved the design office in June."),
    ]
    vectors = {}
    for index, (key, _query) in enumerate(QUERIES):
        vector = [0.0] * dimensions
        vector[index] = 1.0
        vectors[key] = tuple(vector)
    for topic, memory, name, text in rows:
        add_fact(connection, authority, memory, text, with_original=True)
        if topic in gold:
            gold[topic].add(memory)
        vector = vectors.get(topic, tuple(-value for value in vectors["person"]))
        connection.execute(
            "UPDATE search_units SET embedding=%s::vector WHERE memory_id=%s",
            (str(list(vector)), memory),
        )
        aliases = (
            ({"canonical_name": name, "alias": "SW", "quote": text, "object": topic},)
            if memory == "studio"
            else ()
        )
        if arm == "baseline":
            # Three historical job identities, each with grounded continuations, match
            # the observed failure shape while still exercising a 96-memory merged hub.
            aliases = tuple({k: v for k, v in alias.items() if k != "object"} for alias in aliases)
            references = ()
            if topic == "cron" and int(memory[-3:]) % 32:
                root = f"cron-{int(memory[-3:]) // 32 * 32:03d}"
                references = ({"name": name, "reference_id": root, "quote": text},)
            elif memory == "studio-use":
                references = ({"name": "SW", "reference_id": "studio", "quote": text},)
            entities.publish_fact(memory, (name,), text, aliases=aliases, references=references)
        else:
            mention = {
                "name": name,
                "quote": text,
                "object": topic,
                "decision": "different" if topic == "baker" else "new",
                "span": [text.index(name), text.index(name) + len(name)],
            }
            entities.publish_fact(
                memory, (), text, aliases=aliases, entity_mentions=(mention,), objects=objects
            )
    connection.execute("ANALYZE search_units")
    if arm == "candidate":
        graph = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
        assert "baker" not in graph.ordinary(("surgeon",), vectors["person"])[0]

    class Models:
        def embed(self, texts, **kwargs):
            assert len(texts) == 1
            topic = next(key for key, query in QUERIES if query == texts[0])
            return EmbeddingResult(vectors=(vectors[topic],), usage=Usage(coverage="unavailable"))

    service = RecallService(
        config, TransactionPool(connection), Models(), generation_id="generation"
    )
    samples = []
    # Ten repeats, no dropped samples, no automatic remeasurement or model calls.
    for repeat in range(10):
        for topic, query in QUERIES:
            for mode, graph_option in (("fast", None), ("fast", True), ("normal", None)):
                started = time.monotonic()
                result = service.search(
                    authority,
                    SearchRequest(query=query, recall_level=mode, fast_graph=graph_option),
                )
                cited = {
                    citation["source_id"]
                    for item in result["items"]
                    for citation in item["citations"]
                }
                relevant = cited & gold[topic]
                assert relevant, (topic, mode, result["context"])
                assert result["diagnostics"]["generative_calls"] == 0
                samples.append(
                    {
                        "repeat": repeat,
                        "question_id": topic,
                        "mode": mode,
                        "fast_graph": graph_option,
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                        "timing_ms": result["diagnostics"]["timing_ms"],
                        "context_tokens": result["context_tokens"],
                        "source_ids": sorted(cited),
                        "support_recall": len(relevant) / len(gold[topic]),
                        "source_precision": len(relevant) / len(cited),
                        "degraded": result["diagnostics"]["degraded"],
                    }
                )
    (tmp_path / "entity-retrieval-parity.json").write_text(
        json.dumps(
            {
                "arm": arm,
                "provider": "deterministic local embedding fixture",
                "repeat_count": 10,
                "excluded_samples": [],
                "config": config.model_dump(mode="json"),
                "samples": samples,
            },
            indent=2,
        )
    )
