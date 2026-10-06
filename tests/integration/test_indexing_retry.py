"""Known invalid model output uses the existing bounded work-claim budget."""

import json
from threading import Barrier, Event

import httpx
import psycopg
import pytest
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.indexing.extraction import ExtractionError
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore
from oam.models.runtime import ModelOutputError, ModelResult, Usage
from oam.workers.claims import claim_one
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


class IntermittentModels(SyntheticIndexModels):
    def __init__(self, failures, *, timeout=False, overbound_records=0):
        super().__init__()
        self.failures = failures
        self.timeout = timeout
        self.overbound_records = overbound_records
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
    return process_one(
        config,
        make_conninfo(dsn, options="-c role=oam_queue"),
        make_conninfo(dsn, options="-c role=oam_content"),
        models=models,
        worker_id="bounded-retry",
    )


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

    assert run_source(dsn, config, OverlapModels()) is True
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


@pytest.mark.parametrize("overbound", [False, True])
async def test_known_invalid_responses_reuse_completed_stages_and_meter_every_attempt(
    accepted_source, overbound
):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(
        2, overbound_records=config.extraction.max_facts_per_group + 1 if overbound else 0
    )
    for _ in range(2):
        with pytest.raises(ExtractionError):
            run_source(dsn, config, models)
        operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
        assert operation["status"] == "processing"
    assert run_source(dsn, config, models) is True
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
            "SELECT disposition,usage FROM attempts WHERE tenant_id=%s ORDER BY ordinal", (tenant,)
        ).fetchall()
        assert [row[0] for row in attempts] == ["failed", "failed", "succeeded"]
        assert [row[1]["total_tokens"] for row in attempts[:2]] == [30, 30]
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("overbound", [False, True])
async def test_known_invalid_responses_stop_at_the_existing_claim_budget(
    accepted_source, overbound
):
    client, dsn, tenant, config, operation_id = accepted_source
    models = IntermittentModels(
        10, overbound_records=config.extraction.max_facts_per_group + 1 if overbound else 0
    )
    for _ in range(config.retry.max_attempts):
        with pytest.raises(ExtractionError):
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
async def test_unknown_timeout_is_not_automatically_requeued(accepted_source, timeout):
    client, dsn, tenant, config, operation_id = accepted_source
    with pytest.raises(timeout):
        run_source(dsn, config, IntermittentModels(1, timeout=timeout))
    assert run_source(dsn, config, IntermittentModels(0)) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["coverage"] == "partial"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT disposition FROM attempts WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == "unknown_external_outcome"
        )


async def test_mixed_extraction_and_embedding_failures_preserve_unknown_usage(accepted_source):
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
                raise httpx.ConnectTimeout("fact embedding was dispatched")
            return super().embed(texts, **kwargs)

        def generate(self, role, **kwargs):
            if role == "extraction":
                text = json.loads(kwargs["user"])["center"]["text"]
                if text == "Lin prefers morning updates.":
                    assert self.fact_embedding_started.wait(3), "fact embedding did not overlap"
                    raise httpx.ReadTimeout("extraction outcome is unknown")
            return super().generate(role, **kwargs)

    models = MixedTimeoutModels()
    with pytest.raises(RuntimeError, match="multiple indexing stages failed") as failure:
        run_source(dsn, config, models)
    assert isinstance(failure.value.__cause__, httpx.ReadTimeout)
    assert models.embedding_calls == 2
    assert run_source(dsn, config, models) is False
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "partial"
    assert operation["usage"]["coverage"] == "partial"
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_units WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == "failed_terminal"
        )
        disposition, request_ref = connection.execute(
            "SELECT disposition,request_ref FROM attempts WHERE tenant_id=%s", (tenant,)
        ).fetchone()
        assert disposition == "unknown_external_outcome"
        assert request_ref.startswith("extract:")
        assert (
            connection.execute(
                "SELECT count(*) FROM index_stage_results WHERE tenant_id=%s "
                "AND stage_key LIKE 'extract:%%'",
                (tenant,),
            ).fetchone()[0]
            == 1
        )


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


async def test_retry_reuses_completed_fact_vectors_when_ready_group_batching_changes(
    accepted_source, monkeypatch
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
    assert run_source(dsn, config, models) is True
    finished = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert finished["status"] == "succeeded"
    assert finished["readiness"]["memory_ready"] is True
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
        json={"user_id": "lin", "query": "Lin updates", "include_profile": False},
    )
    assert recalled.status_code == 200, recalled.text
    facts = [item for item in recalled.json()["items"] if item["kind"] == "fact"]
    assert {item["text"] for item in facts} == {first, second}
    assert all(item["citations"] for item in facts)
    assert indexed_batches == [(first, second), (first,), (second,)]
