"""Known invalid model output uses the existing bounded work-claim budget."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event

import httpx
import psycopg
import pytest
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_core_http import ROOT, SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.indexing.budget import GenerationBudgetRejection
from oam.indexing.extraction import ExtractionError
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore, input_digest
from oam.models.runtime import ModelOutputError, ModelResult, Usage, _generation_result
from oam.workers.claims import claim_one
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


class IntermittentModels(SyntheticIndexModels):
    def __init__(self, failures, *, timeout=False, overbound_records=0, empty_content=False):
        super().__init__()
        self.failures = failures
        self.timeout = timeout
        self.overbound_records = overbound_records
        self.empty_content = empty_content
        self.first_calls = 0

    def generate(self, role, **kwargs):
        if role == "extraction":
            text = json.loads(kwargs["user"])["center"]["text"]
            if text == "Lin prefers concise updates.":
                self.first_calls += 1
            elif self.failures:
                self.failures -= 1
                if self.timeout:
                    raise self.timeout("response outcome is unknown")
                self.generation_calls.append(role)
                if self.empty_content:
                    return _generation_result(
                        {
                            "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
                            "usage": {
                                "prompt_tokens": 20,
                                "completion_tokens": 10,
                                "total_tokens": 30,
                            },
                        }
                    )
                content = (
                    json.dumps(
                        {
                            "outcome": "facts",
                            "facts": [{"text": text, "quote": text}] * self.overbound_records,
                        }
                    )
                    if self.overbound_records
                    else '{"outcome":"facts","facts":[]}'
                )
                return ModelResult(
                    content=content,
                    usage=Usage(
                        input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                    ),
                )
        return super().generate(role, **kwargs)


class ConnectTimeoutModels(IntermittentModels):
    def generate(self, role, **kwargs):
        if role == "extraction":
            text = json.loads(kwargs["user"])["center"]["text"]
            if text != "Lin prefers concise updates." and self.failures:
                self.failures -= 1
                raise httpx.ConnectTimeout("connection failed before request dispatch")
        return super().generate(role, **kwargs)


class CachedUsageModels(SyntheticIndexModels):
    def generate(self, role, **kwargs):
        result = super().generate(role, **kwargs)
        usage = result.usage.model_copy(
            update={
                "cached_input_tokens": 12,
                "uncached_input_tokens": 8,
                "cache_creation_input_tokens": 1,
            }
        )
        return result.model_copy(update={"usage": usage})


class CompletedInvalidModels(SyntheticIndexModels):
    def __init__(self, failed_role, usage):
        super().__init__()
        self.failed_role = failed_role
        self.usage = usage
        self.embedding_calls = 0
        self.first_fact_embedded = Event()

    def generate(self, role, **kwargs):
        if self.failed_role == "generation" and self.generation_calls:
            assert self.first_fact_embedded.wait(3), "ready fact did not embed during extraction"
            raise ModelOutputError("generation content is unusable", usage=self.usage)
        return super().generate(role, **kwargs)

    def embed(self, texts, **kwargs):
        self.embedding_calls += 1
        if self.failed_role == "embedding" and self.embedding_calls == 2:
            raise ModelOutputError("embedding dimension is invalid", usage=self.usage)
        result = super().embed(texts, **kwargs)
        if self.embedding_calls == 2:
            self.first_fact_embedded.set()
        return result


@pytest.fixture
async def accepted_source(core_database, tmp_path):
    dsn, tenant, headers = core_database
    values = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    values["extraction"]["group_max_passages"] = 1
    values["index"]["insights_enabled"] = True
    path = tmp_path / "singleton.yaml"
    path.write_text(yaml.safe_dump(values))
    config = load_config(path).config
    app = create_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        response = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "chat"},
            json={
                "user_id": "lin",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "event_time": "2026-09-24T09:00:00Z",
                        "learning_eligible": True,
                    }
                    for text in ("Lin prefers concise updates.", "Lin prefers morning updates.")
                ],
            },
        )
        assert response.status_code == 202, response.text
        yield client, dsn, tenant, config, response.json()["operation_id"]


def run_source(dsn, config, models):
    processed = process_one(
        config,
        make_conninfo(dsn, options="-c role=oam_queue"),
        make_conninfo(dsn, options="-c role=oam_content"),
        models=models,
        worker_id="bounded-retry",
    )
    if processed:
        with psycopg.connect(dsn) as connection:
            pending = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM work_units w JOIN operations o "
                "USING(tenant_id,user_id,operation_id) WHERE w.stage='index_semantic_links' "
                "AND w.status='queued' AND o.status='running' AND NOT EXISTS("
                "SELECT 1 FROM work_units d WHERE d.work_unit_id=ANY(w.dependency_ids) "
                "AND d.status<>'succeeded'))"
            ).fetchone()[0]
        if pending:
            assert process_one(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=models,
                worker_id="bounded-retry",
            )
    return processed


async def test_source_claim_finishes_complete_l1_pipeline(accepted_source):
    client, dsn, tenant, config, operation = accepted_source
    assert run_source(dsn, config, SyntheticIndexModels())
    result = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert result["status"] == "succeeded"
    assert all(
        result["readiness"][key]
        for key in ("source_searchable", "memory_ready", "temporal_links_ready", "links_ready")
    )
    with psycopg.connect(dsn) as connection:
        assert set(
            connection.execute(
                "SELECT stage,status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation),
            ).fetchall()
        ) == {("index_source", "succeeded"), ("index_semantic_links", "succeeded")}


@pytest.mark.parametrize("receipt_version", ["current", "legacy_singleton", "link_only"])
async def test_retry_publishes_saved_fact_even_when_its_passage_already_has_a_fact(
    accepted_source, monkeypatch, receipt_version
):
    from oam.indexing.store import stable_id
    from oam.workers import index_source

    client, dsn, tenant, config, operation = accepted_source
    publish = index_source._publish_facts
    published = []
    added_text = "The same passage records a separate accepted fact."

    def leave_checkpoint_after_partial_publication(store, owners, result, vectors, **kwargs):
        ids = publish(store, owners, result, vectors, **kwargs)
        published.extend(ids)
        if len(published) == 2:
            passage_id = next(iter(owners))
            message, _, _, chunk = owners[passage_id]
            additional = result.facts[0].model_copy(
                update={"text": added_text, "output_index": 1, "updates": (0,)}
            )
            checkpoint = result.model_copy(update={"facts": (*result.facts, additional)})
            payload = checkpoint.model_dump(mode="json")
            if receipt_version != "legacy_singleton":
                payload["_passage_ids"] = [passage_id]
            store.checkpoint(
                "extract:" + passage_id,
                {
                    "text": chunk.text,
                    "context": chunk.previous_context,
                    "role": message.role.value,
                    "mentioned_at": message.event_time.isoformat(),
                    "reference_facts": [
                        {
                            "id": ids[0],
                            "text": result.facts[0].text,
                            "role": message.role.value,
                            "mentioned_at": message.event_time.isoformat(),
                            "time_qualifiers": [],
                        }
                    ],
                },
                lambda: payload,
            )
            if receipt_version == "link_only":
                earlier = checkpoint.model_copy(
                    update={"facts": (*result.facts, additional.model_copy(update={"updates": ()}))}
                )
                publish(
                    store,
                    owners,
                    earlier,
                    (*vectors, vectors[0]),
                    **{**kwargs, "texts": (*kwargs["texts"], added_text)},
                )
            published.append(stable_id("memory", passage_id, 1, added_text))
            raise ModelOutputError(
                "fixture interrupted publication after saving a second result",
                usage=Usage(total_tokens=0, coverage="complete"),
            )
        return ids

    with monkeypatch.context() as patch:
        patch.setattr(index_source, "_publish_facts", leave_checkpoint_after_partial_publication)
        with pytest.raises(ModelOutputError, match="fixture interrupted publication"):
            run_source(dsn, config, SyntheticIndexModels())
    first, second, missing = published

    url = f"/v1/users/lin/operations/{operation}"
    failed = (await client.get(url)).json()
    assert failed["status"] == "partial"
    recovered_id = missing
    if receipt_version == "link_only":
        with psycopg.connect(dsn) as db:
            assert db.execute(
                "SELECT 1 FROM search_units WHERE tenant_id=%s AND unit_id=%s",
                (tenant, recovered_id),
            ).fetchone() == (1,)
            assert (
                db.execute(
                    "SELECT 1 FROM unit_links WHERE tenant_id=%s AND source_id=%s "
                    "AND target_id=%s AND kind='updates'",
                    (tenant, recovered_id, second),
                ).fetchone()
                is None
            )
    retry = await client.post(url + "/retry", headers={"Idempotency-Key": "replay-saved-fact"})
    assert retry.status_code == 201, retry.text
    replacement = retry.json()["operation_id"]
    models = SyntheticIndexModels()
    assert run_source(dsn, config, models)
    ready = (await client.get(f"/v1/users/lin/operations/{replacement}")).json()
    assert ready["status"] == "succeeded" and ready["readiness"]["links_ready"]
    assert ready["readiness"]["source_searchable"]
    assert models.generation_calls == []
    if receipt_version == "link_only":
        assert models.embedding_batches == []
    with psycopg.connect(dsn) as db:
        facts = db.execute(
            "SELECT unit_id,content_text,embedding IS NOT NULL FROM search_units "
            "WHERE tenant_id=%s AND kind='fact' ORDER BY unit_id",
            (tenant,),
        ).fetchall()
        assert {row[0] for row in facts} == {first, second, missing}
        assert (missing, added_text, True) in facts
        assert db.execute(
            "SELECT 1 FROM unit_links WHERE tenant_id=%s "
            "AND source_id=%s AND target_id=%s AND kind='updates'",
            (tenant, missing, second),
        ).fetchone() == (1,)

    repeat = await client.post(url + "/retry", headers={"Idempotency-Key": "recheck-saved-fact"})
    assert repeat.status_code == 201, repeat.text
    repeat_id = repeat.json()["operation_id"]
    models = SyntheticIndexModels()
    assert run_source(dsn, config, models)
    ready = (await client.get(f"/v1/users/lin/operations/{repeat_id}")).json()
    assert ready["status"] == "succeeded"
    assert ready["readiness"]["source_searchable"]
    assert models.generation_calls == models.embedding_batches == []


@pytest.mark.parametrize("input_bound", [8192, 1])
async def test_serialized_input_bound_preserves_source_or_reports_known_local_failure(
    core_database, tmp_path, input_bound
):
    from oam.indexing.extraction import model_input_tokens

    dsn, tenant, headers = core_database
    values = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    values["extraction"]["max_input_tokens"] = input_bound
    path = tmp_path / "input-bound.yaml"
    path.write_text(yaml.safe_dump(values))
    config = load_config(path).config
    text = "".join(f"Line {index}: " + "\\" * 50 + ".\n" for index in range(120))
    if input_bound == 1:
        text = "Tea."

    class BoundedModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            assert model_input_tokens(kwargs["system"], json.loads(kwargs["user"])) <= input_bound
            return super().generate(role, **kwargs)

    models = BoundedModels()
    app = create_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "escaped-source")
        accepted = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "escaped-source"},
            json={
                "user_id": "escaped-source",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "event_time": "2026-09-24T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
        )
        assert accepted.status_code == 202, accepted.text
        operation = accepted.json()["operation_id"]
        if input_bound == 1:
            with pytest.raises(ExtractionError, match="input exceeds"):
                run_source(dsn, config, models)
        else:
            assert run_source(dsn, config, models)
        value = (await client.get(f"/v1/users/escaped-source/operations/{operation}")).json()
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT normalized_text FROM passages WHERE tenant_id=%s", (tenant,)
            ).fetchall() == [(text,)]
            if input_bound == 1:
                assert models.generation_calls == []
                assert value["status"] == "partial"
                assert connection.execute(
                    "SELECT disposition FROM attempts WHERE tenant_id=%s", (tenant,)
                ).fetchall() == [("failed",)]
            else:
                assert value["status"] == "succeeded"
                assert all(
                    value["readiness"][key]
                    for key in (
                        "source_searchable",
                        "memory_ready",
                        "temporal_links_ready",
                        "links_ready",
                    )
                )
                quotes = connection.execute(
                    "SELECT span->>'quote' FROM support WHERE tenant_id=%s ORDER BY span->>'quote'",
                    (tenant,),
                ).fetchall()
                assert sum(len(row[0]) for row in quotes) == len(text)
                assert len(models.generation_calls) >= 2
                assert value["usage"]["coverage"] == "complete"
                assert value["usage"]["total_tokens"] == 30 * len(models.generation_calls)


async def test_recall_trace_sql_timeout_returns_deadline_status(accepted_source, monkeypatch):
    from oam.retrieval.repository import RecallRepository

    client, dsn, _, config, _ = accepted_source
    models = SyntheticIndexModels()
    assert run_source(dsn, config, models)

    def timed_out(*args):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(RecallRepository, "record_trace", timed_out)
    monkeypatch.setattr(client._transport.app.state.runtime, "models", models)
    response = await client.post(
        "/v1/recall", json={"user_id": "lin", "query": "Lin prefers concise updates."}
    )
    assert response.status_code == 504, response.text
    assert response.json()["detail"]["code"] == "RecallDeadlineExceeded"


async def test_semantic_retry_reuses_published_facts_without_model_calls(
    accepted_source, monkeypatch
):
    from oam.workers import semantic_links

    client, dsn, tenant, config, operation = accepted_source
    original = semantic_links.propose_semantic_links

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("synthetic semantic timeout")

    monkeypatch.setattr(semantic_links, "propose_semantic_links", timeout)
    with pytest.raises(psycopg.errors.QueryCanceled):
        run_source(dsn, config, SyntheticIndexModels())
    partial = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert partial["readiness"]["memory_ready"]
    assert partial["readiness"]["temporal_links_ready"]
    assert not partial["readiness"]["links_ready"]
    with psycopg.connect(dsn) as connection:
        checkpoints = connection.execute(
            "SELECT stage_key,input_digest,output FROM index_stage_results "
            "WHERE tenant_id=%s ORDER BY stage_key",
            (tenant,),
        ).fetchall()
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation),
            ).fetchone()[0]
            == "queued"
        )

    class NoModels:
        def generate(self, *args, **kwargs):
            pytest.fail("resume repeated extraction")

        def embed(self, *args, **kwargs):
            pytest.fail("resume repeated embeddings")

    monkeypatch.setattr(semantic_links, "propose_semantic_links", original)
    assert run_source(dsn, config, NoModels())
    complete = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert complete["status"] == "succeeded" and complete["readiness"]["links_ready"]
    assert complete["usage"] == partial["usage"]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT stage_key,input_digest,output FROM index_stage_results "
                "WHERE tenant_id=%s ORDER BY stage_key",
                (tenant,),
            ).fetchall()
            == checkpoints
        )


class CheckpointOnlyModels:
    def generate(self, *args, **kwargs):
        pytest.fail("checkpoint resume repeated extraction")

    def embed(self, *args, **kwargs):
        pytest.fail("checkpoint resume repeated embeddings")


async def test_legacy_temporal_ready_claim_resumes_without_semantic_marker(
    accepted_source, monkeypatch
):
    from oam.contracts.evidence import EvidenceAppendRequest
    from oam.indexing.links import build_temporal_links
    from oam.workers.index_source import _index_interaction

    client, dsn, tenant, config, operation = accepted_source
    store, _ = claimed_checkpoint_store(accepted_source)
    request = EvidenceAppendRequest.model_validate_json(bytes(store.source["original_bytes"]))
    with store.model_pipeline():
        seeds, _ = _index_interaction(config, store, SyntheticIndexModels(), request)
    # Exact older persisted shape: temporal readiness, no semantic marker or child work.
    with store.transaction() as connection:
        store.guard(connection)
        build_temporal_links(connection, store.authority, config, store.generation_id, seeds)
        store.readiness(connection, memory_ready=True, temporal_links_ready=True)
        connection.execute(
            "UPDATE work_units SET status='queued' WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        )
    with psycopg.connect(dsn) as connection:
        checkpoints = connection.execute(
            "SELECT stage_key,input_digest,output FROM index_stage_results "
            "WHERE tenant_id=%s ORDER BY stage_key",
            (tenant,),
        ).fetchall()

    assert run_source(dsn, config, CheckpointOnlyModels())
    complete = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert complete["status"] == "succeeded" and complete["readiness"]["links_ready"]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT stage_key,input_digest,output FROM index_stage_results "
                "WHERE tenant_id=%s ORDER BY stage_key",
                (tenant,),
            ).fetchall()
            == checkpoints
        )


@pytest.mark.parametrize("phase", ["fresh", "resume"])
async def test_semantic_entry_database_error_retries_without_model_replay(
    accepted_source, monkeypatch, phase
):
    from oam.workers import semantic_links

    client, dsn, tenant, config, operation = accepted_source
    original = IndexStore.semantic_seed_manifest

    if phase == "resume":
        proposal = semantic_links.propose_semantic_links

        def proposal_timeout(*args, **kwargs):
            raise psycopg.errors.QueryCanceled("synthetic semantic timeout")

        monkeypatch.setattr(semantic_links, "propose_semantic_links", proposal_timeout)
        with pytest.raises(psycopg.errors.QueryCanceled):
            run_source(dsn, config, SyntheticIndexModels())
        monkeypatch.setattr(semantic_links, "propose_semantic_links", proposal)

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("synthetic semantic entry timeout")

    monkeypatch.setattr(IndexStore, "semantic_seed_manifest", timeout)
    with pytest.raises(psycopg.errors.QueryCanceled):
        models = CheckpointOnlyModels() if phase == "resume" else SyntheticIndexModels()
        run_source(dsn, config, models)
    partial = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert partial["readiness"]["memory_ready"] is (phase == "resume")
    assert partial["readiness"]["temporal_links_ready"] is (phase == "resume")
    assert not partial["readiness"]["links_ready"]
    assert partial["stage_state"]["semantic_links_status"] == "pending"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation),
            ).fetchone()[0]
            == "queued"
        )

    monkeypatch.setattr(IndexStore, "semantic_seed_manifest", original)
    assert run_source(dsn, config, CheckpointOnlyModels())
    complete = (await client.get(f"/v1/users/lin/operations/{operation}")).json()
    assert complete["status"] == "succeeded" and complete["readiness"]["links_ready"]
    assert complete["usage"] == partial["usage"]


@pytest.mark.parametrize(
    "corruption", ["missing", "scalar", "source_version", "generation_id", "recipe"]
)
async def test_semantic_resume_rejects_invalid_manifest_without_models(
    accepted_source, monkeypatch, corruption
):
    from oam.indexing.store import StaleIndexWork
    from oam.workers import semantic_links

    _, dsn, tenant, config, operation = accepted_source

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("synthetic semantic timeout")

    monkeypatch.setattr(semantic_links, "propose_semantic_links", timeout)
    with pytest.raises(psycopg.errors.QueryCanceled):
        run_source(dsn, config, SyntheticIndexModels())
    with psycopg.connect(dsn) as connection:
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        ).fetchone()[0]
        if corruption == "missing":
            del state["semantic_link_manifest"]
        elif corruption == "scalar":
            state["semantic_link_manifest"] = "invalid"
        else:
            state["semantic_link_manifest"][corruption] = "invalid"
        connection.execute(
            "UPDATE operations SET stage_state=%s WHERE tenant_id=%s AND operation_id=%s",
            (Jsonb(state), tenant, operation),
        )

    class NoModels:
        def generate(self, *args, **kwargs):
            pytest.fail("invalid resume repeated extraction")

        def embed(self, *args, **kwargs):
            pytest.fail("invalid resume repeated embeddings")

    monkeypatch.setattr(IndexStore, "model_pipeline", lambda *args: pytest.fail("replayed models"))
    with pytest.raises(StaleIndexWork, match="semantic seed manifest is incompatible"):
        run_source(dsn, config, NoModels())


async def test_expired_last_source_attempt_in_semantic_phase_settles(accepted_source, monkeypatch):
    from oam.workers import semantic_links
    from oam.workers.claims import claim_one

    _, dsn, tenant, config, operation = accepted_source

    def worker_death(*args, **kwargs):
        raise SystemExit("synthetic worker death during semantic processing")

    monkeypatch.setattr(semantic_links, "propose_semantic_links", worker_death)
    monkeypatch.setattr(IndexStore, "fail", lambda *args: None)
    with pytest.raises(SystemExit):
        run_source(dsn, config, SyntheticIndexModels())
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second',"
            "attempts_remaining=0 WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        )
        assert (
            claim_one(
                connection.cursor(),
                worker_id="settler",
                attempt_id="settle-attempt",
                lease_seconds=config.lease.seconds,
                user_concurrency=config.user.concurrency,
            )
            is None
        )
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
                (tenant, operation),
            ).fetchone()[0]
            == "failed_terminal"
        )
        status, state = connection.execute(
            "SELECT status,stage_state FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        ).fetchone()
        assert status == "partial_failed"
        assert state["memory_ready"] and state["temporal_links_ready"]
        assert not state["links_ready"]


def claimed_checkpoint_store(accepted_source):
    _, dsn, _, config, _ = accepted_source
    with psycopg.connect(make_conninfo(dsn, options="-c role=oam_queue")) as connection:
        claim = claim_one(
            connection.cursor(),
            worker_id="overlap",
            attempt_id="overlap-attempt",
            lease_seconds=config.lease.seconds,
            user_concurrency=config.user.concurrency,
        )
    store = IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"),
        config,
        claim,
        "overlap",
        index_generation(config, os.environ),
    )
    return store, dsn


@pytest.mark.parametrize(
    "disposition", ["claimed", "claimed_after_checkpoint", "succeeded", "unknown_external_outcome"]
)
async def test_expired_final_direct_source_claim_settles_without_replaying_model_work(
    accepted_source, disposition
):
    client, dsn, tenant, config, operation = accepted_source
    store, _ = claimed_checkpoint_store(accepted_source)
    checkpoint = {
        "facts": [],
        "usage": {
            "coverage": "complete",
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
            "cached_input_tokens": 12,
            "uncached_input_tokens": 8,
            "cache_creation_input_tokens": 1,
        },
    }
    store.checkpoint("extract:completed", {"source": "test"}, lambda: checkpoint)
    with store.transaction() as connection:
        store.guard(connection)
        store.readiness(
            connection,
            source_searchable=True,
            memory_ready=False,
            **(
                {"usage": {"coverage": "complete", "total_tokens": 10}}
                if disposition == "unknown_external_outcome"
                else {}
            ),
        )

    queue_dsn = make_conninfo(dsn, options="-c role=oam_queue")

    def scheduler_tick():
        with psycopg.connect(queue_dsn) as queue:
            queue.execute("SET LOCAL statement_timeout='2s'")
            assert (
                claim_one(
                    queue.cursor(),
                    worker_id="settler",
                    attempt_id="settle-direct-source",
                    lease_seconds=config.lease.seconds,
                    user_concurrency=config.user.concurrency,
                )
                is None
            )

    with psycopg.connect(dsn) as db:
        db.execute(
            "UPDATE work_units SET attempts_remaining=0 WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        )
        if disposition == "claimed":
            db.execute(
                "UPDATE attempts SET request_ref=NULL,response_ref=NULL "
                "WHERE tenant_id=%s AND attempt_id=%s",
                (tenant, store.claim.attempt_id),
            )
        elif disposition != "claimed_after_checkpoint":
            db.execute(
                "UPDATE attempts SET disposition=%s WHERE tenant_id=%s AND attempt_id=%s",
                (disposition, tenant, store.claim.attempt_id),
            )
    scheduler_tick()
    url = f"/v1/users/lin/operations/{operation}"
    assert (await client.get(url)).json()["status"] == "processing"

    with psycopg.connect(dsn) as db:
        db.execute(
            "UPDATE work_units SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        )
    if disposition == "claimed":
        with psycopg.connect(dsn) as publisher:
            publisher.execute(
                "SELECT 1 FROM operations WHERE tenant_id=%s AND operation_id=%s FOR UPDATE",
                (tenant, operation),
            )
            scheduler_tick()
            assert (await client.get(url)).json()["status"] == "processing"
    scheduler_tick()
    scheduler_tick()

    current = (await client.get(url)).json()
    assert current["status"] == "partial"
    assert current["stage_state"]["error"] == "SourceLeaseExhausted"
    assert current["readiness"]["source_searchable"] and not current["readiness"]["memory_ready"]
    assert current["usage"]["coverage"] == current["embedding_usage"]["coverage"] == "partial"
    assert current["usage"].get("total_tokens") == (
        10 if disposition == "unknown_external_outcome" else None
    )
    completed_ref = "extract:completed:" + input_digest({"source": "test"}).hex()
    was_claimed = disposition.startswith("claimed")
    with psycopg.connect(dsn) as db:
        assert db.execute(
            "SELECT w.status,a.disposition,a.request_ref,a.response_ref,a.usage "
            "FROM work_units w JOIN attempts a "
            "USING(tenant_id,user_id,work_unit_id) WHERE w.tenant_id=%s AND w.operation_id=%s",
            (tenant, operation),
        ).fetchall() == [
            (
                "failed_terminal",
                "failed" if was_claimed else disposition,
                "source_lease_exhausted" if disposition == "claimed" else completed_ref,
                None if disposition == "claimed" else completed_ref,
                {
                    "coverage": "complete",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "reasoning_tokens": 0,
                }
                if was_claimed
                else None,
            )
        ]
        assert db.execute(
            "SELECT stage_key,output FROM index_stage_results WHERE tenant_id=%s", (tenant,)
        ).fetchall() == [("extract:completed", checkpoint)]

    retry = await client.post(url + "/retry", headers={"Idempotency-Key": "resume-expired-direct"})
    if disposition == "unknown_external_outcome":
        assert retry.status_code == 409
        assert retry.json()["detail"]["code"] == "RetryRequiresReconciliation"
    else:
        assert retry.status_code == 201, retry.text
        replacement = retry.json()["operation_id"]
        assert replacement != operation
        assert retry.json()["source_id"] == current["source_id"]
        bounded = config.model_copy(
            update={"index": config.index.model_copy(update={"generation_token_budget": 200_000})}
        )
        models = CachedUsageModels()
        assert run_source(dsn, bounded, models)
        ready = (await client.get(f"/v1/users/lin/operations/{replacement}")).json()
        assert ready["status"] == "succeeded" and ready["readiness"]["links_ready"]
        assert ready["usage"]["cached_input_tokens"] == 12 * (1 + len(models.generation_calls))
        assert ready["usage"]["cache_creation_input_tokens"] == 1 + len(models.generation_calls)


@pytest.mark.parametrize(("count", "split_tokens"), [(1, 0), (2, 0), (1, 27)])
async def test_local_dispatch_refusal_can_retry_with_a_new_source_budget(
    accepted_source, count, split_tokens
):
    from oam.indexing.extraction import retry_split_extractions
    from oam.models.runtime import ModelDispatchUnavailable, ModelOutputTooLongError

    client, dsn, tenant, config, operation = accepted_source
    store, _ = claimed_checkpoint_store(accepted_source)
    assert config.index.generation_token_budget is None

    def refuse():
        raise ModelDispatchUnavailable("local admission refused before dispatch")

    def extract():
        if split_tokens:
            overflow = ModelOutputTooLongError(
                "completed parent reached the output limit",
                usage=Usage(
                    input_tokens=20,
                    output_tokens=7,
                    total_tokens=split_tokens,
                    cached_input_tokens=12,
                    uncached_input_tokens=8,
                    cache_creation_input_tokens=1,
                    coverage="complete",
                ),
            )
            return retry_split_extractions((refuse, refuse), overflow)
        return refuse()

    with psycopg.connect(dsn) as db:
        db.execute(
            "UPDATE work_units SET attempts_remaining=0 WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation),
        )
    for index in range(count):
        with pytest.raises(ModelDispatchUnavailable) as refused:
            store.checkpoint(f"extract:group-{index}", {"group": index}, extract)
    store.fail(refused.value)

    url = f"/v1/users/lin/operations/{operation}"
    failed = (await client.get(url)).json()
    assert failed["status"] == "partial"
    assert failed["usage"]["coverage"] == "complete"
    assert failed["usage"]["total_tokens"] == split_tokens

    retry = await client.post(url + "/retry", headers={"Idempotency-Key": "apply-budget"})
    assert retry.status_code == 201, retry.text
    replacement = retry.json()["operation_id"]
    bounded = config.model_copy(
        update={"index": config.index.model_copy(update={"generation_token_budget": 200_000})}
    )
    models = CachedUsageModels()
    assert run_source(dsn, bounded, models)
    ready = (await client.get(f"/v1/users/lin/operations/{replacement}")).json()
    assert ready["status"] == "succeeded" and ready["readiness"]["links_ready"]
    calls = len(models.generation_calls) + bool(split_tokens)
    assert ready["usage"]["cached_input_tokens"] == 12 * calls
    assert ready["usage"]["uncached_input_tokens"] == 8 * calls
    assert ready["usage"]["cache_creation_input_tokens"] == calls


@pytest.mark.parametrize("overlap", [False, True])
async def test_source_budget_reuses_capacity_when_model_dispatch_never_starts(
    accepted_source, overlap
):
    from oam.capacity import CapacityUnavailable
    from oam.indexing.budget import GenerationBudgetExceeded, SourceBudgetModels
    from oam.models.runtime import ModelDispatchUnavailable, RuntimeModels

    store, _ = claimed_checkpoint_store(accepted_source)
    store.config = store.config.model_copy(
        update={
            "index": store.config.index.model_copy(
                update={"generation_token_budget": 2600 if overlap else 1300}
            )
        }
    )
    admission_waiting, reject_dispatch = Event(), Event()
    started_requests = []

    class DenyDispatch:
        def acquire_model(self, *args, **kwargs):
            admission_waiting.set()
            assert reject_dispatch.wait(5), "test did not settle the pending admission"
            raise CapacityUnavailable("local generation capacity exhausted")

    def read_timeout(request):
        started_requests.append(json.loads(request.content)["messages"][-1]["content"])
        raise httpx.ReadTimeout("response outcome is unknown", request=request)

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        httpx.Client(transport=httpx.MockTransport(read_timeout)) as http,
    ):
        blocked = SourceBudgetModels(
            store,
            RuntimeModels(
                store.config, os.environ, http, background=True, admission=DenyDispatch()
            ),
        )
        models = SourceBudgetModels(
            store, RuntimeModels(store.config, os.environ, http, background=True)
        )
        request = {"system": "s", "max_output_tokens": 1024, "timeout_seconds": 5}
        undispatched = executor.submit(blocked.generate, "extraction", user="local", **request)
        expected_requests = ["sibling"] if overlap else []
        try:
            assert admission_waiting.wait(5), "the first reservation did not reach local admission"
            if overlap:
                with pytest.raises(httpx.ReadTimeout):
                    models.generate("extraction", user="sibling", **request)
            assert started_requests == expected_requests
        finally:
            reject_dispatch.set()
        with pytest.raises(ModelDispatchUnavailable):
            undispatched.result(timeout=5)
        with pytest.raises(httpx.ReadTimeout):
            models.generate("extraction", user="retry", **request)
        with pytest.raises(GenerationBudgetExceeded):
            models.generate("extraction", user="retry again", **request)
        assert started_requests == [*expected_requests, "retry"]


@pytest.mark.parametrize("unresolved", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_overlapping_checkpoints_preserve_outstanding_external_state(
    accepted_source, unresolved, batch
):
    store, dsn = claimed_checkpoint_store(accepted_source)
    started, release = Event(), Event()

    def extraction():
        started.set()
        assert release.wait(5)
        if unresolved:
            raise httpx.ReadTimeout("synthetic outstanding extraction")
        return {"usage": {}}

    def peer_extraction():
        assert release.wait(5)
        return {"usage": {}}

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = (
            executor.submit(
                store.checkpoints,
                [
                    ("extract:outstanding", {}, extraction),
                    ("extract:peer", {}, peer_extraction),
                ],
            )
            if batch
            else executor.submit(store.checkpoint, "extract:outstanding", {}, extraction)
        )
        try:
            assert started.wait(5)
            store.checkpoint("facts_embed:complete", {}, lambda: {"usage": {}})
            with psycopg.connect(dsn) as connection:
                assert connection.execute(
                    "SELECT disposition,request_ref,response_ref FROM attempts "
                    "WHERE attempt_id='overlap-attempt'"
                ).fetchone() == (
                    "unknown_external_outcome",
                    "extract:outstanding:" + input_digest({}).hex(),
                    None,
                )
        finally:
            release.set()
        if unresolved:
            if batch:
                assert any(isinstance(output, httpx.ReadTimeout) for output in future.result(5))
            else:
                with pytest.raises(httpx.ReadTimeout):
                    future.result(timeout=5)
            store.checkpoint("passage_embed:later", {}, lambda: {"usage": {}})
        else:
            future.result(timeout=5)
    with psycopg.connect(dsn) as connection:
        disposition = connection.execute(
            "SELECT disposition FROM attempts WHERE attempt_id='overlap-attempt'"
        ).fetchone()[0]
        assert disposition == ("unknown_external_outcome" if unresolved else "claimed")


async def test_failed_checkpoint_commit_keeps_attempt_unresolved(accepted_source, monkeypatch):
    store, dsn = claimed_checkpoint_store(accepted_source)
    original = store.transaction
    returned = Event()

    @contextmanager
    def failed_commit():
        with original() as connection:
            yield connection
            if returned.is_set():
                raise psycopg.OperationalError("synthetic checkpoint commit failure")

    def completed():
        returned.set()
        return {"usage": {}}

    with monkeypatch.context() as patch:
        patch.setattr(store, "transaction", failed_commit)
        with pytest.raises(psycopg.OperationalError):
            store.checkpoint("extract:unsaved", {}, completed)
    store.checkpoint("facts_embed:later", {}, lambda: {"usage": {}})
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT disposition,request_ref,response_ref FROM attempts "
            "WHERE attempt_id='overlap-attempt'"
        ).fetchone() == (
            "unknown_external_outcome",
            "extract:unsaved:" + input_digest({}).hex(),
            None,
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM index_stage_results WHERE stage_key='extract:unsaved'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("known_usage", [False, True])
async def test_consumer_failure_keeps_partial_usage_for_timed_out_extraction(
    accepted_source, known_usage
):
    client, dsn, tenant, config, operation_id = accepted_source
    config = config.model_copy(
        update={
            "provider": config.provider.model_copy(update={"llm_concurrency": 2}),
        }
    )
    producer_started, consumer_failed = Event(), Event()

    class MixedFailureModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role == "extraction":
                text = json.loads(kwargs["user"])["center"]["text"]
                if text == "Lin prefers morning updates.":
                    producer_started.set()
                    assert consumer_failed.wait(5)
                    raise httpx.ReadTimeout("overlapping extraction outcome is unknown")
            return super().generate(role, **kwargs)

        def embed(self, texts, **kwargs):
            if texts == ("Lin prefers concise updates.",):
                assert producer_started.wait(5)
                consumer_failed.set()
                if known_usage:
                    raise ModelOutputError(
                        "completed fact embedding is invalid",
                        usage=Usage(
                            input_tokens=9, output_tokens=0, total_tokens=9, coverage="complete"
                        ),
                    )
                raise httpx.ConnectTimeout("fact embedding failed before dispatch")
            return super().embed(texts, **kwargs)

    expected = RuntimeError if known_usage else httpx.ReadTimeout
    with pytest.raises(expected) as failure:
        run_source(dsn, config, MixedFailureModels())
    if not known_usage:
        operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
        assert operation["status"] == "processing"
        assert operation["usage"]["coverage"] == "partial"
        assert run_source(dsn, config, SyntheticIndexModels()) is True
        return
    assert isinstance(failure.value.__cause__, httpx.ReadTimeout)
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["coverage"] == "partial"
    retried = await client.post(
        f"/v1/users/lin/operations/{operation_id}/retry",
        headers={"Idempotency-Key": "caught-timeout-retry"},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["operation_id"] != operation_id
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT disposition FROM attempts WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation_id),
        ).fetchone() == ("failed_terminal",)
        assert connection.execute(
            "SELECT status FROM work_units WHERE tenant_id=%s AND operation_id=%s",
            (tenant, retried.json()["operation_id"]),
        ).fetchone() == ("queued",)
    assert run_source(dsn, config, SyntheticIndexModels()) is True


async def test_two_extractions_overlap_and_keep_distinct_source_support(accepted_source):
    client, dsn, tenant, config, operation_id = accepted_source

    class OverlapModels(SyntheticIndexModels):
        def __init__(self):
            super().__init__()
            self.barrier = Barrier(2, timeout=3)

        def generate(self, role, **kwargs):
            if role == "extraction":
                self.barrier.wait()
            return super().generate(role, **kwargs)

    models = OverlapModels()
    assert run_source(dsn, config, models) is True
    direct = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert direct["status"] == "succeeded" and direct["readiness"]["memory_ready"] is True
    assert direct["readiness"]["links_ready"] is True
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["usage"]["total_tokens"] == 60
    assert operation["usage"]["coverage"] == "complete"
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT m.canonical_claim,p.normalized_text FROM memories m "
            "JOIN support s USING(tenant_id,user_id,memory_id) "
            "JOIN passages p USING(tenant_id,user_id,passage_id) "
            "WHERE m.tenant_id=%s ORDER BY m.canonical_claim",
            (tenant,),
        ).fetchall()
    assert rows == [
        ("Lin prefers concise updates.", "Lin prefers concise updates."),
        ("Lin prefers morning updates.", "Lin prefers morning updates."),
    ]


async def test_failed_consolidation_keeps_completed_l1(accepted_source):
    client, dsn, tenant, config, operation_id = accepted_source
    assert run_source(dsn, config, SyntheticIndexModels()) is True
    source = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    consolidate_id = source["stage_state"]["consolidation_operation_id"]
    assert source["status"] == "succeeded" and source["readiness"]["links_ready"] is True
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET available_at=clock_timestamp() WHERE tenant_id=%s "
            "AND operation_id=%s",
            (tenant, consolidate_id),
        )

    class FailedInsightModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if role == "insights":
                raise ModelOutputError("invalid synthesis", usage=Usage(coverage="complete"))
            return super().generate(role, **kwargs)

    with pytest.raises(ModelOutputError):
        run_source(dsn, config, FailedInsightModels())
    source = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    consolidation = (await client.get(f"/v1/users/lin/operations/{consolidate_id}")).json()
    assert source["status"] == "succeeded"
    assert source["readiness"]["memory_ready"] is True
    assert source["readiness"]["insights_ready"] is False
    assert source["stage_state"]["consolidation_status"] == "failed"
    assert consolidation["status"] == "partial"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM search_units WHERE tenant_id=%s AND kind='fact'", (tenant,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM insights WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("failure_kind", ["empty_facts", "overbound", "empty_content"])
async def test_known_invalid_responses_reuse_completed_stages_and_meter_every_attempt(
    accepted_source, failure_kind
):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(
        2,
        overbound_records=config.extraction.max_facts_per_group + 1
        if failure_kind == "overbound"
        else 0,
        empty_content=failure_kind == "empty_content",
    )
    for _ in range(2):
        with pytest.raises((ExtractionError, ModelOutputError)):
            run_source(dsn, config, models)
        operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
        assert operation["status"] == "processing"
    assert run_source(dsn, config, models) is True
    direct = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert direct["status"] == "succeeded" and direct["readiness"]["links_ready"] is True
    assert run_source(dsn, config, models) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["usage"]["total_tokens"] == 120
    assert operation["usage"]["coverage"] == "complete"
    assert operation["embedding_usage"]["total_tokens"] == 4
    assert operation["stage_state"]["index_attempts"] == 3
    assert "error" not in operation["stage_state"]
    assert models.first_calls == 1
    with psycopg.connect(dsn) as connection:
        attempts = connection.execute(
            "SELECT a.disposition,a.usage FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) WHERE a.tenant_id=%s "
            "AND w.stage='index_source' ORDER BY a.ordinal",
            (tenant,),
        ).fetchall()
        assert [row[0] for row in attempts] == ["failed", "failed", "succeeded"]
        assert [row[1]["total_tokens"] for row in attempts[:2]] == [30, 30]
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("failure_kind", ["empty_facts", "overbound", "empty_content"])
async def test_known_invalid_responses_stop_at_the_existing_claim_budget(
    accepted_source, failure_kind
):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(
        10,
        overbound_records=config.extraction.max_facts_per_group + 1
        if failure_kind == "overbound"
        else 0,
        empty_content=failure_kind == "empty_content",
    )
    for _ in range(config.retry.max_attempts):
        with pytest.raises((ExtractionError, ModelOutputError)):
            run_source(dsn, config, models)
    assert run_source(dsn, config, models) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["total_tokens"] == 120
    assert operation["usage"]["coverage"] == "complete"
    assert models.first_calls == 1
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT status,attempts_remaining FROM work_units WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("failed_terminal", 0)

    retried = await client.post(
        f"/v1/users/lin/operations/{operation_id}/retry",
        headers={"Idempotency-Key": "resume-known-failure"},
    )
    assert retried.status_code == 201, retried.text
    replacement = retried.json()["operation_id"]
    models.failures = 0
    assert run_source(dsn, config, models)
    assert (await client.get(f"/v1/users/lin/operations/{replacement}")).json()["status"] == (
        "succeeded"
    )
    assert models.first_calls == 1
    repeated = await client.post(
        f"/v1/users/lin/operations/{operation_id}/retry",
        headers={"Idempotency-Key": "resume-known-failure"},
    )
    assert repeated.json()["operation_id"] == replacement


async def test_pre_dispatch_connect_timeout_reuses_completed_stages(accepted_source):
    client, dsn, tenant, config, operation_id = accepted_source
    models = ConnectTimeoutModels(1)

    with pytest.raises(httpx.ConnectTimeout):
        run_source(dsn, config, models)
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "processing"
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT disposition,usage,response_ref FROM attempts WHERE tenant_id=%s",
            (tenant,),
        ).fetchone() == ("failed", None, None)

    assert run_source(dsn, config, models) is True
    direct = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert direct["status"] == "succeeded" and direct["readiness"]["links_ready"] is True
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["stage_state"]["index_attempts"] == 2
    assert operation["usage"]["coverage"] == "complete"
    assert operation["usage"]["total_tokens"] == 60
    assert models.first_calls == 1


async def test_pre_dispatch_connect_timeout_stops_at_claim_budget(accepted_source):
    client, dsn, tenant, config, operation_id = accepted_source
    models = ConnectTimeoutModels(config.retry.max_attempts + 1)
    for attempt in range(config.retry.max_attempts):
        with pytest.raises(httpx.ConnectTimeout):
            run_source(dsn, config, models)
        operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
        assert operation["status"] == (
            "partial" if attempt + 1 == config.retry.max_attempts else "processing"
        )
    assert run_source(dsn, config, models) is False
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT status,attempts_remaining FROM work_units WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("failed_terminal", 0)
        assert (
            connection.execute(
                "SELECT count(*) FROM attempts WHERE tenant_id=%s AND disposition='failed'",
                (tenant,),
            ).fetchone()[0]
            == config.retry.max_attempts
        )


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.WriteTimeout])
async def test_transport_timeout_retries_and_reuses_completed_stages(accepted_source, timeout):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(1, timeout=timeout)
    with pytest.raises(timeout):
        run_source(dsn, config, models)
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "processing"
    assert run_source(dsn, config, models) is True
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["stage_state"]["index_attempts"] == 2
    assert operation["usage"]["coverage"] == "partial"
    assert operation["usage"]["total_tokens"] is None
    assert models.first_calls == 1
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM attempts WHERE tenant_id=%s "
                "AND disposition='unknown_external_outcome'",
                (tenant,),
            ).fetchone()[0]
            == 0
        )


async def test_transport_timeout_stops_at_existing_claim_budget(accepted_source):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(config.retry.max_attempts + 1, timeout=httpx.ReadTimeout)
    for _ in range(config.retry.max_attempts):
        with pytest.raises(httpx.ReadTimeout):
            run_source(dsn, config, models)
    assert run_source(dsn, config, models) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["coverage"] == "partial"
    assert operation["stage_state"]["index_attempts"] == 3


@pytest.mark.parametrize("status", [429, 503, 401])
async def test_provider_failure_retries_only_transient_statuses(accepted_source, status):
    client, dsn, tenant, config, operation_id = accepted_source

    def fail_http(message):
        request = httpx.Request("POST", "https://model.example/chat/completions")
        return httpx.HTTPStatusError(
            message, request=request, response=httpx.Response(status, request=request)
        )

    models = IntermittentModels(1, timeout=fail_http)
    with pytest.raises(httpx.HTTPStatusError):
        run_source(dsn, config, models)
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == ("partial" if status == 401 else "processing")
    assert run_source(dsn, config, models) is (status != 401)


@pytest.mark.parametrize("local_rejection", [False, True])
async def test_concurrent_extraction_failures_preserve_retry_eligibility(
    accepted_source, local_rejection
):
    client, dsn, tenant, config, operation_id = accepted_source
    config = config.model_copy(
        update={"provider": config.provider.model_copy(update={"llm_concurrency": 2})}
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET attempts_remaining=1 WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation_id),
        )

    class InvalidModels(SyntheticIndexModels):
        def __init__(self):
            super().__init__()
            self.calls = Barrier(2)

        def generate(self, role, **kwargs):
            if role == "extraction":
                self.calls.wait(timeout=5)
                center = json.loads(kwargs["user"])["center"]["text"]
                if local_rejection and center == "Lin prefers concise updates.":
                    raise GenerationBudgetRejection("source generation budget rejected dispatch")
                raise ExtractionError(
                    "provider returned invalid extraction JSON",
                    usage=Usage(total_tokens=12, coverage="complete"),
                    retryable=True,
                )
            return super().generate(role, **kwargs)

    with pytest.raises(RuntimeError, match="multiple indexing stages failed"):
        run_source(dsn, config, InvalidModels())
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["stage_state"]["model_failure"] == {
        "http_status": None,
        "retryable": not local_rejection,
        "retry_after_seconds": None if local_rejection else 0,
    }
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT disposition FROM attempts WHERE tenant_id=%s", (tenant,)
        ).fetchall() == [("failed",)]


async def test_model_timeout_can_settle_after_ordinary_claim_lease(accepted_source):
    import time

    client, dsn, tenant, config, operation_id = accepted_source
    config = config.model_copy(
        update={
            "lease": config.lease.model_copy(update={"seconds": 1}),
            "model": config.model.model_copy(update={"attempt_timeout_seconds": 3}),
        }
    )

    class SlowTimeoutModels(IntermittentModels):
        def generate(self, role, **kwargs):
            if self.failures and "morning" in kwargs["user"]:
                time.sleep(1.1)
            return super().generate(role, **kwargs)

    models = SlowTimeoutModels(1, timeout=httpx.ReadTimeout)
    with pytest.raises(httpx.ReadTimeout):
        run_source(dsn, config, models)
    assert (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()["status"] == (
        "processing"
    )
    assert run_source(dsn, config, models) is True


@pytest.mark.parametrize("embedding_error", [httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_mixed_extraction_and_embedding_failures_preserve_unknown_usage(
    accepted_source, embedding_error
):
    client, dsn, tenant, config, operation_id = accepted_source

    class MixedTimeoutModels(SyntheticIndexModels):
        def __init__(self):
            super().__init__()
            self.embedding_calls = 0
            self.fact_embedding_started = Event()

        def embed(self, texts, **kwargs):
            self.embedding_calls += 1
            if self.embedding_calls == 2:
                self.fact_embedding_started.set()
                raise embedding_error("fact embedding failed")
            return super().embed(texts, **kwargs)

        def generate(self, role, **kwargs):
            if role == "extraction":
                text = json.loads(kwargs["user"])["center"]["text"]
                if text == "Lin prefers morning updates.":
                    assert self.fact_embedding_started.wait(3), "fact embedding did not overlap"
                    raise httpx.ReadTimeout("extraction outcome is unknown")
            return super().generate(role, **kwargs)

    models = MixedTimeoutModels()
    with pytest.raises(httpx.ReadTimeout):
        run_source(dsn, config, models)
    assert models.embedding_calls == 2
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "processing"
    assert operation["usage"]["coverage"] == "partial"
    if embedding_error is httpx.ReadTimeout:
        assert operation["embedding_usage"]["coverage"] == "partial"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == "queued"
        )
        disposition, request_ref = connection.execute(
            "SELECT disposition,request_ref FROM attempts WHERE tenant_id=%s", (tenant,)
        ).fetchone()
        assert disposition == "failed"
        assert request_ref.startswith("extract:")
        assert (
            connection.execute(
                "SELECT count(*) FROM index_stage_results WHERE tenant_id=%s "
                "AND stage_key LIKE 'extract:%%'",
                (tenant,),
            ).fetchone()[0]
            == 1
        )
    assert run_source(dsn, config, SyntheticIndexModels()) is True
    if embedding_error is httpx.ReadTimeout:
        operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
        assert operation["embedding_usage"]["coverage"] == "partial"


@pytest.mark.parametrize(
    ("failed_role", "usage", "generation_total", "embedding_total", "generation_coverage"),
    [
        (
            "generation",
            Usage(input_tokens=100, output_tokens=40, total_tokens=140, coverage="complete"),
            170,
            3,
            "complete",
        ),
        (
            "embedding",
            Usage(input_tokens=9, output_tokens=0, total_tokens=9, coverage="complete"),
            60,
            11,
            "complete",
        ),
        ("generation", Usage(), None, 3, "partial"),
    ],
)
async def test_completed_invalid_outputs_record_known_usage_once_without_retrying(
    accepted_source, failed_role, usage, generation_total, embedding_total, generation_coverage
):
    client, dsn, tenant, config, operation_id = accepted_source
    models = CompletedInvalidModels(failed_role, usage)
    with pytest.raises(ModelOutputError):
        run_source(dsn, config, models)
    assert run_source(dsn, config, models) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["total_tokens"] == generation_total
    assert operation["usage"]["coverage"] == generation_coverage
    assert operation["embedding_usage"]["total_tokens"] == embedding_total
    assert operation["embedding_usage"]["coverage"] == "complete"
    assert operation["stage_state"]["index_attempts"] == 1
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT status,attempts_remaining FROM work_units WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("failed_terminal", config.retry.max_attempts - 1)
        assert connection.execute(
            "SELECT disposition,usage,response_ref=request_ref,finished_at IS NOT NULL "
            "FROM attempts WHERE tenant_id=%s",
            (tenant,),
        ).fetchone() == ("failed", usage.model_dump(mode="json"), True, True)


async def test_stale_owner_cannot_record_usage_or_requeue_work(accepted_source):
    import os

    _client, dsn, tenant, config, _operation_id = accepted_source
    with psycopg.connect(make_conninfo(dsn, options="-c role=oam_queue")) as connection:
        claim = claim_one(
            connection.cursor(),
            worker_id="old",
            attempt_id="old-attempt",
            lease_seconds=config.lease.seconds,
            user_concurrency=config.user.concurrency,
        )
    store = IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"),
        config,
        claim,
        "old",
        index_generation(config, os.environ),
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE work_units SET fence=fence+1,owner_id='new' WHERE tenant_id=%s", (tenant,)
        )
    store.fail(
        ExtractionError(
            "invalid output", usage=Usage(total_tokens=30, coverage="partial"), retryable=True
        )
    )
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT status,owner_id FROM work_units WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("running", "new")
        assert connection.execute(
            "SELECT disposition,usage FROM attempts WHERE tenant_id=%s", (tenant,)
        ).fetchone() == ("claimed", None)


@pytest.mark.parametrize("change_recipe", [False, True])
async def test_retry_reuses_completed_fact_vectors_when_ready_group_batching_changes(
    accepted_source, monkeypatch, change_recipe
):
    from concurrent.futures import ThreadPoolExecutor

    from oam.workers import index_source

    client, dsn, tenant, config, operation_id = accepted_source
    first = "Lin prefers concise updates."
    second = "Lin prefers morning updates."
    first_embedded = Event()

    class Models(SyntheticIndexModels):
        def __init__(self):
            super().__init__()
            self.fail_second = True
            self.extraction_texts = []

        def generate(self, role, **kwargs):
            if role == "extraction":
                text = json.loads(kwargs["user"])["center"]["text"]
                self.extraction_texts.append(text)
                if text == second and self.fail_second:
                    self.fail_second = False
                    assert first_embedded.wait(5), "first group never completed fact embedding"
                    raise httpx.ConnectTimeout("second extraction failed before dispatch")
            return super().generate(role, **kwargs)

        def embed(self, texts, **kwargs):
            result = super().embed(texts, **kwargs)
            if texts == (first,):
                first_embedded.set()
            return result

    models = Models()
    with pytest.raises(httpx.ConnectTimeout, match="second extraction failed before dispatch"):
        run_source(dsn, config, models)
    assert models.embedding_batches == [(first, second), (first,)]
    failed = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert failed["status"] == "processing"

    class ReadyTogetherExecutor(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.submitted = []

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submitted.append(future)
            if len(self.submitted) == 2:
                for submitted in self.submitted:
                    submitted.result(timeout=5)
            return future

    monkeypatch.setattr(index_source, "ThreadPoolExecutor", ReadyTogetherExecutor)
    retry_config = config
    if change_recipe:
        retry_config = config.model_copy(
            update={"extraction": config.extraction.model_copy(update={"group_max_passages": 64})}
        )
        assert index_generation(retry_config, os.environ) != index_generation(config, os.environ)
    assert run_source(dsn, retry_config, models) is True
    indexed = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert indexed["status"] == "succeeded"
    assert indexed["readiness"]["memory_ready"] is True
    assert indexed["readiness"]["links_ready"] is True
    finished = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert finished["status"] == "succeeded"
    assert finished["readiness"]["links_ready"] is True
    assert finished["stage_state"]["index_attempts"] == 2
    assert models.extraction_texts.count(first) == 1
    indexed_batches = list(models.embedding_batches)
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM index_stage_results WHERE tenant_id=%s "
                "AND stage_key LIKE 'extract:%%'",
                (tenant,),
            ).fetchone()[0]
            == 2
        )
    client._transport.app.state.runtime.models = models
    recalled = await client.post(
        "/v1/recall",
        json={"user_id": "lin", "query": "Lin updates"},
    )
    assert recalled.status_code == 200, recalled.text
    facts = [item for item in recalled.json()["items"] if item["kind"] == "fact"]
    assert {item["text"] for item in facts} == {first, second}
    assert all(item["citations"] for item in facts)
    assert indexed_batches == [(first, second), (first,), (second,)]


def test_accepted_source_runs_after_recipe_change(accepted_source):
    _, dsn, tenant, config, operation_id = accepted_source
    changed = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(
                update={"max_facts_per_group": config.extraction.max_facts_per_group - 1}
            )
        }
    )
    assert index_generation(changed, os.environ) != index_generation(config, os.environ)
    assert run_source(dsn, changed, SyntheticIndexModels())
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT status FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation_id),
        ).fetchone()
    assert row[0] == "succeeded"


@pytest.mark.parametrize("legacy_receipt", [False, True])
async def test_retry_does_not_recreate_removed_fact_from_completed_passage(
    core_database, tmp_path, legacy_receipt
):
    dsn, tenant, headers = core_database
    values = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    values["extraction"]["group_max_passages"] = 2
    values["index"]["insights_enabled"] = False
    path = tmp_path / "grouped.yaml"
    path.write_text(yaml.safe_dump(values))
    config = load_config(path).config
    app = create_app(path)
    first = "Lin prefers concise updates."
    removed_text = "Lin prefers morning updates."
    third = "Lin prefers written updates."
    published = Event()

    class Models(SyntheticIndexModels):
        def __init__(self):
            super().__init__()
            self.group_calls = []
            self.fail = True

        def generate(self, role, **kwargs):
            if role == "extraction":
                envelope = json.loads(kwargs["user"])
                if "passages" in envelope:
                    self.group_calls.append([p["center"]["text"] for p in envelope["passages"]])
                elif envelope["center"]["text"] == third and self.fail:
                    self.fail = False
                    assert published.wait(5)
                    raise httpx.ConnectTimeout("later passage failed")
            return super().generate(role, **kwargs)

        def embed(self, texts, **kwargs):
            result = super().embed(texts, **kwargs)
            if texts == (first, removed_text):
                published.set()
            return result

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        response = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "removal-retry"},
            json={
                "user_id": "lin",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "event_time": "2026-09-24T09:00:00Z",
                        "learning_eligible": True,
                    }
                    for text in (first, removed_text, third)
                ],
            },
        )
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        models = Models()
        with pytest.raises(httpx.ConnectTimeout, match="later passage failed"):
            run_source(dsn, config, models)
        with psycopg.connect(dsn) as connection:
            removed_id = connection.execute(
                "SELECT memory_id FROM memories WHERE tenant_id=%s AND canonical_claim=%s",
                (tenant, removed_text),
            ).fetchone()[0]
        response = await client.delete(f"/v1/users/lin/memories/{removed_id}")
        assert response.status_code == 202, response.text
        removal_operation = response.json()["operation_id"]
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp()+interval '1 hour' "
                "WHERE tenant_id=%s AND operation_id=%s AND stage='index_source'",
                (tenant, operation_id),
            )
            if legacy_receipt:
                connection.execute(
                    "UPDATE operations SET stage_state=stage_state-'removed_passage_ids' "
                    "WHERE tenant_id=%s AND operation_id=%s",
                    (tenant, removal_operation),
                )
        assert run_source(dsn, config, models)
        removal_state = (await client.get(f"/v1/users/lin/operations/{removal_operation}")).json()
        assert removal_state["stage_state"]["cleanup_status"] == "complete"
        assert removal_state["stage_state"]["removed_passage_ids"]
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM support WHERE tenant_id=%s AND memory_id=%s",
                    (tenant, removed_id),
                ).fetchone()[0]
                == 0
            )
            connection.execute(
                "UPDATE work_units SET available_at=clock_timestamp() "
                "WHERE tenant_id=%s AND operation_id=%s AND stage='index_source'",
                (tenant, operation_id),
            )
        assert run_source(dsn, config, models)
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM memories WHERE tenant_id=%s AND canonical_claim=%s "
                    "AND lifecycle_state='active' AND visible_to IS NULL",
                    (tenant, removed_text),
                ).fetchone()[0]
                == 0
            )
        assert models.group_calls == [[first, removed_text]]
        assert (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()[
            "status"
        ] == "succeeded"


async def test_explicit_retry_reuses_partial_source_under_same_new_recipe(accepted_source):
    client, dsn, _tenant, config, operation_id = accepted_source
    changed = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(
                update={"max_facts_per_group": config.extraction.max_facts_per_group - 1}
            )
        }
    )
    models = IntermittentModels(3)
    for _ in range(3):
        with pytest.raises(ExtractionError):
            run_source(dsn, changed, models)
    retried = await client.post(
        f"/v1/users/lin/operations/{operation_id}/retry",
        headers={"Idempotency-Key": "new-recipe-retry"},
    )
    assert retried.status_code == 201, retried.text
    assert run_source(dsn, changed, IntermittentModels(0))
    result = (await client.get(f"/v1/users/lin/operations/{retried.json()['operation_id']}")).json()
    assert result["status"] == "succeeded"
