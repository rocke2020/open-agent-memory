import json

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.models.runtime import ModelResult, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


@pytest.fixture
async def temporal_client(core_database):
    dsn, tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        yield client, app, config, dsn, tenant


def process(config, dsn, models):
    return process_one(
        config,
        make_conninfo(dsn, options="-c role=oam_queue"),
        make_conninfo(dsn, options="-c role=oam_content"),
        models=models,
        worker_id="temporal-fixture",
    )


def interaction(content="Apple harvest was in 2025.", **fields):
    return {
        "user_id": "scope",
        "input_type": "interaction",
        "messages": [
            {
                "role": "user",
                "content": content,
                "event_time": "2026-01-01T00:00:00Z",
                "learning_eligible": True,
            }
        ],
        **fields,
    }


def caller_annotation(content, *, year=2025, **changes):
    return {
        "role": "event",
        "start": f"{year}-01-01T00:00:00Z",
        "end": f"{year + 1}-01-01T00:00:00Z",
        "quote": str(year),
        **changes,
    }


def timed_query(year, *, basis="event"):
    return {
        "user_id": "scope",
        "query": "apple harvest",
        "time_basis": basis,
        "time_window": {"start": f"{year}-01-01T00:00:00Z", "end": f"{year + 1}-01-01T00:00:00Z"},
    }


class NoFactModels(SyntheticIndexModels):
    def __init__(self):
        super().__init__()
        self.embedding_calls = 0
        self.reference_timezones = []

    def generate(self, role, **kwargs):
        if role == "extraction":
            self.reference_timezones.append(json.loads(kwargs["user"])["reference_timezone"])
            self.generation_calls.append(role)
            return ModelResult(
                content='{"outcome":"no_facts","facts":[]}',
                usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15, coverage="complete"),
            )
        return super().generate(role, **kwargs)

    def embed(self, texts, **kwargs):
        self.embedding_calls += 1
        return super().embed(texts, **kwargs)


async def test_caller_time_and_source_clock_survive_no_fact_extraction(temporal_client):
    client, app, config, dsn, _tenant = temporal_client
    body = interaction(
        source_created_at="2023-06-01T00:00:00Z",
        source_updated_at="2024-06-01T00:00:00Z",
        reference_timezone="Asia/Shanghai",
    )
    body["messages"][0]["temporal_annotations"] = [
        caller_annotation(body["messages"][0]["content"])
    ]
    receipt = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "annotated"})
    assert receipt.status_code == 202
    models = NoFactModels()
    assert process(config, dsn, models)
    app.state.runtime.models = models

    response = await client.post("/v1/recall", json=timed_query(2025))

    assert response.status_code == 200, response.text
    assert [item["kind"] for item in response.json()["items"]] == ["passage"]
    assert response.json()["items"][0]["time_evidence"][0]["detail"]["origin"] == "caller"
    detail = response.json()["items"][0]["time_evidence"][0]["detail"]
    assert {"span_start", "span_end"}.isdisjoint(detail)
    assert detail["quote"] == "2025"
    assert {"start", "end"}.isdisjoint(response.json()["items"][0]["citations"][0])
    created = await client.post("/v1/recall", json=timed_query(2023, basis="created"))
    assert len(created.json()["items"]) == 1
    updated = await client.post("/v1/recall", json=timed_query(2024, basis="updated"))
    assert len(updated.json()["items"]) == 1
    assert models.reference_timezones == ["Asia/Shanghai"]


async def test_fast_http_infers_query_year_without_excluding_other_passages(
    temporal_client,
):
    client, app, config, dsn, _tenant = temporal_client
    models = NoFactModels()
    for year in (2024, 2025):
        body = interaction(f"Apple harvest was in {year}.")
        body["messages"][0]["temporal_annotations"] = [
            caller_annotation(body["messages"][0]["content"], year=year)
        ]
        receipt = await client.post(
            "/v1/retain",
            json=body,
            headers={"Idempotency-Key": f"harvest-{year}"},
        )
        assert receipt.status_code == 202, receipt.text
        assert process(config, dsn, models)
        assert process(config, dsn, models)  # Finish queued links before the next source.
        completed = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
        assert completed.json()["status"] == "succeeded"
    app.state.runtime.models = models

    response = await client.post(
        "/v1/recall",
        json={"user_id": "scope", "query": "apple harvest in 2025", "recall_level": "fast"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["text"] == "Apple harvest was in 2025."
    assert {item["text"] for item in response.json()["items"]} == {
        "Apple harvest was in 2024.",
        "Apple harvest was in 2025.",
    }
    assert response.json()["diagnostics"]["query_time"]["status"] == "inferred"
    assert response.json()["diagnostics"]["generative_calls"] == 0


@pytest.mark.parametrize(
    "start,end,content,bounds,role",
    [
        (
            "2025-03-01T10:15:30Z",
            "2025-03-01T10:15:30Z",
            "Apple harvest pulse exactly at 2025-03-01T10:15:30Z.",
            "[]",
            "event",
        ),
        (
            "2025-03-01T10:15:00Z",
            "2025-03-01T10:16:00Z",
            "Apple harvest alert during the 10:15 minute on March 1, 2025.",
            "[)",
            "event",
        ),
        (
            "2025-03-01T10:00:00Z",
            "2025-03-01T11:00:00Z",
            "Apple harvest sometime in the 10 AM hour on March 1, 2025.",
            "[)",
            "event",
        ),
        (
            "2025-03-01T00:00:00Z",
            "2025-03-02T00:00:00Z",
            "Apple harvest on March 1, 2025.",
            "[)",
            "event",
        ),
        (
            "2025-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "Apple harvest probably happened in 2025.",
            "[)",
            "event",
        ),
        (
            "2025-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "We discussed the possible 2025 apple harvest.",
            "[)",
            "date_reference",
        ),
    ],
)
async def test_public_four_field_time_publishes_canonical_range(
    temporal_client, start, end, content, bounds, role
):
    client, app, config, dsn, tenant = temporal_client
    body = interaction(content, reference_timezone="UTC")
    body["messages"][0]["temporal_annotations"] = [
        {
            "role": role,
            "start": start,
            "end": end,
            "quote": content,
        }
    ]
    receipt = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "bounds"})
    assert receipt.status_code == 202, receipt.text
    models = NoFactModels()
    assert process(config, dsn, models)
    app.state.runtime.models = models
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT lower_inc(time_span),upper_inc(time_span),isempty(time_span),detail "
            "FROM unit_times WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()
    assert row[:3] == (True, bounds == "[]", False)
    assert row[3] == {
        "role": role,
        "start": start,
        "end": end,
        "quote": content,
        "origin": "caller",
        "reference_timezone": "UTC",
    }
    response = await client.post(
        "/v1/recall", json=timed_query(2025, basis="event" if role == "event" else "referenced")
    )
    assert response.status_code == 200, response.text
    assert content in response.json()["context"]
    assert any(
        path["arm"] == "temporal" for item in response.json()["items"] for path in item["paths"]
    )
    other = await client.post(
        "/v1/recall", json=timed_query(2025, basis="referenced" if role == "event" else "event")
    )
    assert other.status_code == 200, other.text
    assert all(
        path["arm"] != "temporal" for item in other.json()["items"] for path in item["paths"]
    )


@pytest.mark.parametrize("generated_year", [2024, 2025])
async def test_caller_annotation_rejects_conflicting_generated_time(
    temporal_client, generated_year
):
    client, app, config, dsn, tenant = temporal_client
    body = interaction()
    body["messages"][0]["temporal_annotations"] = [
        caller_annotation(body["messages"][0]["content"])
    ]

    class ConflictingModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            result = super().generate(role, **kwargs)
            if role == "extraction":
                value = json.loads(result.content)
                supplied = caller_annotation(
                    body["messages"][0]["content"],
                    start=f"{generated_year}-01-01T00:00:00Z",
                    end=f"{generated_year + 1}-01-01T00:00:00Z",
                )
                value["facts"][0]["annotations"] = [supplied]
                return result.model_copy(update={"content": json.dumps(value)})
            return result

    receipt = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "conflict"})
    assert receipt.status_code == 202
    models = ConflictingModels()
    assert process(config, dsn, models)
    app.state.runtime.models = models

    wrong = await client.post("/v1/recall", json=timed_query(2024))
    correct = await client.post("/v1/recall", json=timed_query(2025))

    assert wrong.status_code == correct.status_code == 200
    assert {item["kind"] for item in wrong.json()["items"]} == {"passage", "fact"}
    passage = next(item for item in wrong.json()["items"] if item["kind"] == "passage")
    assert passage["text"] == "Apple harvest was in 2025."
    event = next(value for value in passage["time_evidence"] if value["role"] == "event")
    assert event["detail"]["origin"] == "caller"
    assert event["detail"]["start"] == "2025-01-01T00:00:00Z"
    assert event["detail"]["end"] == "2026-01-01T00:00:00Z"
    assert all(not item["paths"] for item in wrong.json()["items"])
    assert {item["kind"] for item in correct.json()["items"]} == {"passage", "fact"}
    assert all(
        any(path["arm"] == "temporal" for path in item["paths"]) for item in correct.json()["items"]
    )
    operation = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
    expected_coverage = "partial" if generated_year == 2024 else "complete"
    assert operation.json()["stage_state"]["extraction_coverage"] == expected_coverage
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT detail->>'origin' FROM unit_times WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert rows and {row[0] for row in rows} == {"caller"}
        supports = connection.execute(
            "SELECT span FROM support WHERE tenant_id=%s", (tenant,)
        ).fetchall()
        assert supports and all({"start", "end"}.isdisjoint(row[0]) for row in supports)
        assert all(row[0]["quote"] == "Apple harvest was in 2025." for row in supports)
        assert all(isinstance(row[0]["passage_id"], str) for row in supports)


@pytest.mark.parametrize("valid_quote", [True, False])
async def test_html_annotations_stay_evidence_and_validate_before_models(
    temporal_client, valid_quote
):
    client, app, config, dsn, tenant = temporal_client
    text = "Apple harvest was in 2025."
    original = f"<p>{text}</p>".encode()
    annotation = {**caller_annotation(text), "locator": "html:block:0001"}
    if not valid_quote:
        annotation["quote"] = "2024"
    metadata = {
        "input_type": "document",
        "document_id": "report",
        "update_mode": "replace",
        "source_created_at": "2023-06-01T00:00:00Z",
        "temporal_annotations": [annotation],
    }
    receipt = await client.post(
        "/v1/users/scope/resources",
        data={"metadata": json.dumps(metadata)},
        files={"original": ("report.html", original, "text/html")},
        headers={"Idempotency-Key": "report"},
    )
    assert receipt.status_code == 202, receipt.text
    models = NoFactModels()
    if not valid_quote:
        with pytest.raises(ValueError, match="quote"):
            process(config, dsn, models)
        assert models.embedding_calls == 0 and models.generation_calls == []
        return
    assert process(config, dsn, models)
    app.state.runtime.models = models

    response = await client.post("/v1/recall", json=timed_query(2025))

    assert response.status_code == 200, response.text
    assert response.json()["items"] and {item["origin"] for item in response.json()["items"]} == {
        "evidence"
    }
    created = await client.post("/v1/recall", json=timed_query(2023, basis="created"))
    assert len(created.json()["items"]) == 1
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 0
        )


async def test_document_replace_immediately_hides_old_projection(temporal_client):
    client, app, config, dsn, _tenant = temporal_client
    models = NoFactModels()
    app.state.runtime.models = models
    base = {
        "input_type": "document",
        "document_id": "garden",
        "update_mode": "replace",
    }
    first = await client.post(
        "/v1/users/scope/resources",
        json={**base, "text": "Old apple grove."},
        headers={"Idempotency-Key": "v1"},
    )
    assert first.status_code == 202
    assert process(config, dsn, models)
    second = await client.post(
        "/v1/users/scope/resources",
        json={**base, "text": "New apple garden."},
        headers={"Idempotency-Key": "v2"},
    )
    assert second.status_code == 202, second.text
    assert second.json()["revision"] == 2
    pending = await client.post("/v1/recall", json={"user_id": "scope", "query": "apple"})
    assert pending.json()["items"] == []
    assert process(config, dsn, models)
    current = await client.post("/v1/recall", json={"user_id": "scope", "query": "apple"})
    assert "New apple garden" in current.json()["context"]
    assert "Old apple grove" not in current.json()["context"]


@pytest.mark.parametrize("safe_detail", [True, False])
async def test_public_failure_records_only_safe_owned_model_error_text(
    temporal_client, safe_detail
):
    from oam.indexing.extraction import ExtractionError

    client, _app, config, dsn, _tenant = temporal_client

    class FailingModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            if safe_detail:
                raise ExtractionError("fact output requires outcome and facts")
            raise RuntimeError("private-http-credential")

    receipt = await client.post(
        "/v1/retain", json=interaction(), headers={"Idempotency-Key": "failure"}
    )
    assert receipt.status_code == 202
    with pytest.raises((ExtractionError, RuntimeError)):
        process(config, dsn, FailingModels())
    result = await client.get(f"/v1/users/scope/operations/{receipt.json()['operation_id']}")
    assert result.status_code == 200
    stage = result.json()["stage_state"]
    if safe_detail:
        assert stage["error_detail"] == "fact output requires outcome and facts"
    else:
        assert "error_detail" not in stage
        assert "private-http-credential" not in result.text
