"""Bounded Insight generation through accepted evidence and native workers."""

import asyncio
import json
import time
from collections import Counter
from threading import Barrier, Event, Lock

import httpx
import psycopg
import pytest
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, SyntheticIndexModels, process_source, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.models.runtime import EmbeddingResult, ModelResult, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration
TOPICS = ("Aurora", "Birch", "Cobalt", "Dahlia")


class TopicModels(SyntheticIndexModels):
    """Deterministic evidence and vectors; count real generator overlap."""

    def __init__(self, *, overlap=1, failures=0):
        super().__init__()
        self.barrier = Barrier(overlap) if overlap > 1 else None
        self.lock = Lock()
        self.active = self.peak = 0
        self.calls = []
        self.failures = failures
        self.pause = None

    def embed(self, texts, **kwargs):
        def vector(text):
            topic = next((i for i, name in enumerate(TOPICS) if name in text), 0)
            return tuple(float(i == topic) for i in range(1024))

        return EmbeddingResult(vectors=tuple(vector(text) for text in texts), usage=Usage())

    def generate(self, role, **kwargs):
        if role != "insights":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        with self.lock:
            number = len(self.calls)
            self.calls.append(envelope)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier and number < self.barrier.parties:
                self.barrier.wait(timeout=5)
            if self.pause is not None:
                assert self.pause.wait(timeout=5)
            time.sleep(0.02)
            if number < self.failures:
                raise httpx.ReadTimeout("isolated synthetic timeout")
            facts = envelope["facts"]
            actions = []
            for topic in TOPICS:
                topic_facts = [fact for fact in facts if topic in fact["text"]]
                seed_ids = [
                    fact["id"] for fact in topic_facts if fact["id"] in envelope["seed_ids"]
                ]
                if not seed_ids:
                    continue
                target = next(
                    (target for target in envelope["targets"] if topic in target["text"]), None
                )
                action = {
                    "action": "KEEP_TEXT" if target else "CREATE",
                    "target_id": target["id"] if target else None,
                    "support_ids": [fact["id"] for fact in topic_facts],
                    "seed_ids": seed_ids,
                }
                if not target:
                    action.update(
                        text=" ".join(fact["text"] for fact in topic_facts),
                        title=f"{topic} project records",
                        subtype="projects",
                    )
                actions.append(action)
            return ModelResult(
                content=json.dumps({"actions": actions}),
                usage=Usage(
                    input_tokens=100,
                    output_tokens=20,
                    reasoning_tokens=5,
                    total_tokens=120,
                    coverage="complete",
                ),
            )
        finally:
            with self.lock:
                self.active -= 1


async def retain_topics(client, *, same_topic=False, key="cold"):
    messages = [
        {
            "role": "user",
            "content": f"{topic} records item {i} with label {topic}-{i}.",
            "learning_eligible": True,
            "event_time": "2026-05-03T18:00:00Z",
        }
        for topic in (TOPICS[:1] if same_topic else TOPICS)
        for i in range(18 if same_topic else 2)
    ]
    receipt = await client.post(
        "/v1/retain",
        json={
            "input_type": "interaction",
            "user_id": "scope",
            "messages": messages,
        },
        headers={"Idempotency-Key": key},
    )
    assert receipt.status_code == 202, receipt.text
    return receipt.json()


@pytest.mark.parametrize("cap", [1, 4])
@pytest.mark.parametrize("before,after", [("source", "related"), ("related", "source")])
async def test_seed_policy_resume_reuses_paid_outputs_and_grounded_public_recall(
    core_database, tmp_path, monkeypatch, cap, before, after
):
    from oam.indexing import consolidation

    dsn, tenant, headers = core_database
    raw = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    raw["index"].update(
        insight_generation_concurrency=cap,
        insight_consolidation_delay_hours=1e-8,
        insight_seed_policy=before,
    )
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path).config
    app, models = create_app(path), TopicModels()
    queue, content = (
        make_conninfo(dsn, options="-c role=" + role) for role in ("oam_queue", "oam_content")
    )
    publish = consolidation._publish_group
    interrupted = []

    def interrupt(*args, **kwargs):
        if not interrupted:
            raise httpx.ReadTimeout("isolated pause after saved model output")
        return publish(*args, **kwargs)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipt = await retain_topics(client)
        process_source(config, queue, content, models=models, worker_id="policy-l1")
        monkeypatch.setattr(consolidation, "_publish_group", interrupt)
        with pytest.raises(httpx.ReadTimeout):
            process_one(config, queue, content, models=models, worker_id="policy-interrupt")
        interrupted.append(True)
        operation = (
            await client.get(f"/v1/users/scope/operations/{receipt['operation_id']}")
        ).json()
        child = operation["stage_state"]["consolidation_operation_id"]
        if before == "related":
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE operations SET stage_state=stage_state-'l2_seed_policy' "
                    "WHERE tenant_id=%s AND user_id='scope' AND operation_id=%s",
                    (tenant, child),
                )
        raw["index"]["insight_seed_policy"] = after
        path.write_text(yaml.safe_dump(raw))
        resumed = load_config(path).config
        for _ in range(30):
            process_one(resumed, queue, content, models=models, worker_id="policy-resume")
            operation = (
                await client.get(f"/v1/users/scope/operations/{receipt['operation_id']}")
            ).json()
            if operation["readiness"].get("insights_ready"):
                break
            await asyncio.sleep(0.02)
        assert operation["readiness"]["insights_ready"]
        assert len(models.calls) == (1 if before == "source" else 4)
        with psycopg.connect(dsn) as connection:
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id='scope' "
                "AND operation_id=%s",
                (tenant, child),
            ).fetchone()[0]
            assert state["l2_seed_policy"] == before
            assert len(state["l2_examined_ids"]) == 8
            assert (
                connection.execute(
                    "SELECT count(*) FROM attempts WHERE disposition='unknown_external_outcome'"
                ).fetchone()[0]
                == 0
            )
        app.state.runtime.models = models
        for topic in TOPICS:
            response = await client.post(
                "/v1/recall", json={"user_id": "scope", "query": topic, "recall_level": "normal"}
            )
            assert response.status_code == 200
            items = [
                item
                for item in response.json()["items"]
                if item["kind"] == "insight" and topic in item["text"]
            ]
            assert len(items) == 1
            assert all(f"{topic}-{number}" in items[0]["text"] for number in (0, 1))
            assert items[0]["citations"]


@pytest.mark.parametrize("entity_candidates", [False, True])
async def test_insight_lexical_lookup_ranks_only_insight_hits(
    core_database, tmp_path, entity_candidates, monkeypatch
):
    from oam.auth.credentials import AuthorizedUser
    from oam.indexing.consolidation import InsightLookup

    class UnfilteredLookup(InsightLookup):
        @property
        def lexical_candidate_predicate(self):
            return "true"

    dsn, tenant, headers = core_database
    raw = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    raw["index"]["insight_consolidation_delay_hours"] = 1e-8
    path = tmp_path / "lexical.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path).config
    app = create_app(path)
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    models = TopicModels()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        await retain_topics(client)
        process_source(config, queue, content, models=models, worker_id="lexical-l1")
        assert process_one(config, queue, content, models=models, worker_id="lexical-l2")

    authority = AuthorizedUser("probe", tenant, "probe", "scope", frozenset({"memory:read"}))
    plans = []
    with psycopg.connect(dsn) as connection:
        generation = connection.execute(
            "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s",
            (tenant, "scope"),
        ).fetchone()[0]
        kwargs = {"generation_id": generation, "deadline": time.monotonic() + 60}
        lookup = InsightLookup(connection, authority, config, **kwargs)
        original = UnfilteredLookup(connection, authority, config, **kwargs)
        if entity_candidates:
            for repository in (lookup, original):
                monkeypatch.setattr(
                    repository,
                    "_query_entity_names",
                    lambda _: [{"entity_id": "unmatched-entity", "name_text": "Aurora"}],
                )
        execute = lookup._execute

        def capture(query, parameters=(), **kwargs):
            rows = execute(query, parameters, **kwargs)
            if "hits AS MATERIALIZED" in query:
                plans.append(
                    connection.execute(
                        "EXPLAIN (ANALYZE, FORMAT JSON) " + query, parameters
                    ).fetchone()[0][0]["Plan"]
                )
            return rows

        monkeypatch.setattr(lookup, "_execute", capture)
        query = "Aurora records item"
        assert lookup.lexical(query, 8) == original.lexical(query, 8)
        before = lookup.lexical(query, 8)
        assert len(before) == 4
        connection.execute(
            "UPDATE memories SET visible_to=1 "
            "WHERE tenant_id=%s AND user_id='scope' AND memory_id IN ("
            "SELECT memory_id FROM search_units WHERE tenant_id=%s AND user_id='scope' "
            "AND kind='fact' AND content_text LIKE 'Aurora%%')",
            (tenant, tenant),
        )
        after = lookup.lexical(query, 8)
        assert after == original.lexical(query, 8)
        assert len(after) == 3
        assert before[0]["unit_id"] not in {row["unit_id"] for row in after}

    def materialized_hits(node):
        if node.get("Subplan Name") == "CTE hits":
            yield node["Actual Rows"]
        for child in node.get("Plans", []):
            yield from materialized_hits(child)

    # Facts can contribute entity names, but never enter Insight-only term ranking.
    assert plans
    hits = [count for plan in plans for count in materialized_hits(plan)]
    assert hits and max(hits) == 4


@pytest.mark.parametrize(
    "scenario",
    [
        "independent",
        "same_topic",
        "retry",
        "cached",
        "publication_retry",
        "shared_targets",
        "cancel",
    ],
)
async def test_parallel_l2_native_path_is_bounded_grounded_and_resumable(
    core_database,
    tmp_path,
    scenario,
    monkeypatch,
):
    dsn, tenant, headers = core_database
    raw = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    raw["index"].update(insight_generation_concurrency=4, insight_consolidation_delay_hours=1e-8)
    path = tmp_path / "parallel.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path).config
    app = create_app(path)
    models = TopicModels(
        overlap=1 if scenario == "same_topic" else 4,
        failures=3 if scenario == "cached" else 2 if scenario == "retry" else 0,
    )
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        receipt = await retain_topics(client, same_topic=scenario == "same_topic")
        process_source(config, queue, content, models=models, worker_id="parallel-stage")
        if scenario == "cancel":
            from oam.indexing.store import StaleIndexWork

            models.pause = Event()
            running = asyncio.create_task(
                asyncio.to_thread(
                    process_one, config, queue, content, models=models, worker_id="cancel-worker"
                )
            )
            deadline = time.monotonic() + 5
            while models.active < 4 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert models.active == 4
            operation = (
                await client.get(f"/v1/users/scope/operations/{receipt['operation_id']}")
            ).json()
            child = operation["stage_state"]["consolidation_operation_id"]
            cancelled = await client.post(f"/v1/users/scope/operations/{child}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            models.pause.set()
            with pytest.raises(StaleIndexWork):
                await running
            assert models.active == 0
            with psycopg.connect(dsn) as connection:
                assert connection.execute("SELECT count(*) FROM insights").fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 8
                assert (
                    connection.execute(
                        "SELECT count(*) FROM index_stage_results WHERE stage_key='insight'"
                    ).fetchone()[0]
                    == 0
                )
            return
        if scenario == "cached":
            from oam.indexing import consolidation

            publish = consolidation._publish_group
            failed_publication = []

            def interrupt_after_saved_response(*args, **kwargs):
                if not failed_publication:
                    failed_publication.append(True)
                    raise httpx.ReadTimeout(
                        "isolated interruption after checkpoint before publication"
                    )
                return publish(*args, **kwargs)

            monkeypatch.setattr(consolidation, "_publish_group", interrupt_after_saved_response)
        if scenario == "publication_retry":
            from oam.indexing import consolidation

            publish = consolidation._publish_group
            calls = 0

            def interrupt_second_publication(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise httpx.ReadTimeout("interrupted after a sibling published")
                return publish(*args, **kwargs)

            monkeypatch.setattr(consolidation, "_publish_group", interrupt_second_publication)
        if scenario in {"retry", "cached", "publication_retry"}:
            with pytest.raises(httpx.ReadTimeout):
                process_one(config, queue, content, models=models, worker_id="parallel-timeout")
            assert models.active == 0
            with psycopg.connect(dsn) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM insights WHERE visible_to IS NULL"
                ).fetchone()[0] == (0 if scenario in {"cached", "publication_retry"} else 2)
                assert (
                    connection.execute(
                        "SELECT count(*) FROM attempts WHERE disposition='unknown_external_outcome'"
                    ).fetchone()[0]
                    == 0
                )
        for _ in range(30):
            process_one(config, queue, content, models=models, worker_id="parallel-finish")
            operation = (
                await client.get(f"/v1/users/scope/operations/{receipt['operation_id']}")
            ).json()
            if operation["readiness"].get("insights_ready"):
                break
            await asyncio.sleep(0.02)
        assert operation["readiness"]["insights_ready"], operation
        assert models.peak == (1 if scenario == "same_topic" else 4)
        assert models.active == 0
        expected = 1 if scenario == "same_topic" else 4
        with psycopg.connect(dsn) as connection:
            rows = connection.execute(
                "SELECT u.insight_id,u.content_text FROM search_units u JOIN insights i ON "
                "(i.tenant_id,i.user_id,i.insight_id,i.revision)="
                "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision) "
                "WHERE u.kind='insight' AND i.visible_to IS NULL"
            ).fetchall()
            assert len(rows) == expected
            assert (
                connection.execute("SELECT count(*) FROM insight_support").fetchone()[0]
                >= 2 * expected
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM attempts WHERE disposition='unknown_external_outcome'"
                ).fetchone()[0]
                == 0
            )
        for topic in TOPICS[:1] if scenario == "same_topic" else TOPICS:
            assert sum(topic in text for _, text in rows) == 1
        if scenario in {"retry", "cached"}:
            counts = Counter(seed for call in models.calls for seed in call["seed_ids"])
            assert sorted(counts.values()) == (
                [1, 1, 2, 2, 2, 2, 2, 2] if scenario == "cached" else [1, 1, 1, 1, 2, 2, 2, 2]
            )
        if scenario == "publication_retry":
            counts = Counter(seed for call in models.calls for seed in call["seed_ids"])
            assert len(models.calls) == 4
            assert set(counts.values()) == {1}
        app.state.runtime.models = models
        recalled = await client.post(
            "/v1/recall", json={"user_id": "scope", "query": "Aurora", "recall_level": "normal"}
        )
        assert recalled.status_code == 200, recalled.text
        assert "Aurora-0" in recalled.json()["context"]
        if scenario != "same_topic":
            assert any(item.get("kind") == "insight" for item in recalled.json()["items"])
        if scenario == "shared_targets":
            models.peak = 0
            receipt = await retain_topics(client, key="update")
            process_source(config, queue, content, models=models, worker_id="shared-stage")
            assert process_one(config, queue, content, models=models, worker_id="shared-finish")
            assert models.peak == 1
            with psycopg.connect(dsn) as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM insights WHERE visible_to IS NULL"
                    ).fetchone()[0]
                    == 4
                )
