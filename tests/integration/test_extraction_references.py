"""Existing-fact context through public retain, the real worker and cited recall."""

import asyncio
import json
import socket
from threading import Barrier, Thread

import httpx
import psycopg
import pytest
import uvicorn
import yaml
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_core_http import ROOT, SyntheticIndexModels, use_created_user_key
from test_core_http import core_database as core_database

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.indexing.extraction import ExtractionError
from oam.indexing.store import StaleIndexWork
from oam.models.runtime import ModelOutputTooLongError, ModelResult, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


class ReferenceModels(SyntheticIndexModels):
    def __init__(self, *, overlap=False):
        super().__init__()
        self.inputs = []
        self.barrier = Barrier(2, timeout=5) if overlap else None

    def generate(self, role, **kwargs):
        if role != "extraction":
            return super().generate(role, **kwargs)
        self.generation_calls.append(role)
        envelope = json.loads(kwargs["user"])
        self.inputs.append(envelope)
        if self.barrier:
            self.barrier.wait()
        text = envelope["center"]["text"]
        ids = [item["id"] for item in envelope.get("reference_facts", [])]
        return ModelResult(
            content=json.dumps(
                {
                    "outcome": "facts",
                    "facts": [{"text": text, "quote": text, "related_existing_ids": ids}],
                }
            ),
            usage=Usage(
                input_tokens=20,
                output_tokens=10,
                total_tokens=30,
                reasoning_tokens=0,
                coverage="complete",
            ),
        )


@pytest.fixture
async def reference_client(core_database, tmp_path, request):
    dsn, tenant, headers = core_database
    values = yaml.safe_load((ROOT / "configs/oam.yaml").read_text())
    selection = getattr(request, "param", 8)
    reference_limit, group_limit = selection if isinstance(selection, tuple) else (selection, 1)
    values["extraction"].update(reference_max_facts=reference_limit, reference_max_input_tokens=512)
    values["extraction"]["group_max_passages"] = group_limit
    path = tmp_path / "references.yaml"
    path.write_text(yaml.safe_dump(values))
    config = load_config(path).config
    app = create_app(path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                headers=headers,
                trust_env=False,
            ) as client:
                await use_created_user_key(client, "lin")
                app.state.runtime.models = ReferenceModels()
                yield client, dsn, tenant, config, app
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


async def retain(client, key, texts):
    response = await client.post(
        "/v1/retain",
        headers={"Idempotency-Key": key},
        json={
            "user_id": "lin",
            "input_type": "interaction",
            "messages": [
                {
                    "role": "user",
                    "content": text,
                    "learning_eligible": True,
                    "event_time": "2026-09-24T09:00:00Z",
                }
                for text in texts
            ],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["operation_id"]


def run_source(dsn, config, models):
    processed = process_one(
        config,
        make_conninfo(dsn, options="-c role=oam_queue"),
        make_conninfo(dsn, options="-c role=oam_content"),
        models=models,
        worker_id="reference-worker",
    )
    if processed:
        process_one(
            config,
            make_conninfo(dsn, options="-c role=oam_queue"),
            make_conninfo(dsn, options="-c role=oam_content"),
            models=models,
            worker_id="reference-worker",
        )
    return processed


async def test_reference_checkpoint_keeps_full_claim_while_wire_uses_a_semantic_preview(
    reference_client,
):
    client, dsn, tenant, config, _ = reference_client
    old_text = (("C" * 398 + ". ") * 8).rstrip()
    await retain(client, "full-old-context", [old_text])
    assert run_source(dsn, config, ReferenceModels())
    operation_id = await retain(client, "preview-context", ["A new assertion."])
    models = ReferenceModels()
    assert run_source(dsn, config, models)
    assert len(models.inputs) == 1
    offered = models.inputs[0]["reference_facts"]
    assert len(offered) == 1
    assert old_text.startswith(offered[0]["text"]) and offered[0]["text"] != old_text
    assert offered[0]["text"].endswith(".") and offered[0]["text_truncated"]
    with psycopg.connect(dsn) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        source_id = connection.execute(
            "SELECT source_id FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation_id),
        ).fetchone()[0]
        frozen = connection.execute(
            "SELECT output FROM index_stage_results WHERE tenant_id=%s AND source_id=%s "
            "AND stage_key LIKE 'references:%%'",
            (tenant, source_id),
        ).fetchone()[0]["references"]
    assert frozen[0]["id"] == offered[0]["id"] and frozen[0]["text"] == old_text
    assert "text_truncated" not in frozen[0]


async def test_public_reference_path_preserves_occurrences_and_concurrent_support(
    reference_client, tmp_path
):
    client, dsn, tenant, config, app = reference_client
    event_qualifier = {
        "role": "event",
        "start": "2020-01-01T00:00:00Z",
        "end": "2021-01-01T00:00:00Z",
        "quote": "I prefer tea.",
    }
    persisted_qualifier = {
        **event_qualifier,
        "origin": "caller",
        "reference_timezone": "UTC",
    }
    evidence = {"provider": "deterministic_fake", "operations": [], "recalls": {}}
    for key, texts, models in (
        ("old", ["I prefer tea."], ReferenceModels()),
        ("new", ["I prefer tea.", "I now prefer coffee."], ReferenceModels(overlap=True)),
    ):
        response = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": key},
            json={
                "user_id": "lin",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "learning_eligible": True,
                        "event_time": "2026-09-24T09:00:00Z",
                    }
                    for text in texts
                ],
            },
        )
        assert response.status_code == 202, response.text
        assert run_source(dsn, config, models)
        operation = (
            await client.get(f"/v1/users/lin/operations/{response.json()['operation_id']}")
        ).json()
        assert operation["readiness"]["memory_ready"] is True
        assert operation["usage"]["total_tokens"] == 30 * len(texts)
        evidence["operations"].append(operation)
        if key == "old":
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,"
                    "role,time_span,detail) SELECT tenant_id,user_id,'old-event',"
                    "unit_id,'event',tstzrange('2020-01-01','2021-01-01','[)'),%s "
                    "FROM search_units WHERE tenant_id=%s AND kind='fact'",
                    (Jsonb(persisted_qualifier), tenant),
                )
        if key == "new":
            assert len(models.inputs) == 2
            assert all(
                [ref["text"] for ref in envelope["reference_facts"]] == ["I prefer tea."]
                for envelope in models.inputs
            )
            assert all(
                envelope["reference_facts"][0]["role"] == "user" for envelope in models.inputs
            )
            app.state.runtime.models = models
            reference = models.inputs[0]["reference_facts"][0]
            assert reference["mentioned_at"] == "2026-09-24T09:00:00+00:00"
            assert reference["time_qualifiers"] == [event_qualifier]
            evidence["extraction_inputs"] = models.inputs
    for mode in ("fast", "normal"):
        recall = await client.post(
            "/v1/recall",
            json={
                "user_id": "lin",
                "query": "tea coffee",
                "recall_level": mode,
            },
        )
        assert recall.status_code == 200, recall.text
        assert "I now prefer coffee." in recall.json()["context"]
        assert all(item["citations"] for item in recall.json()["items"])
        evidence["recalls"][mode] = recall.json()
    assert models.generation_calls == ["extraction", "extraction"]
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT canonical_claim FROM memories WHERE tenant_id=%s ORDER BY canonical_claim",
            (tenant,),
        ).fetchall()
        assert rows == [("I now prefer coffee.",), ("I prefer tea.",), ("I prefer tea.",)]
        stages = connection.execute(
            "SELECT output FROM index_stage_results "
            "WHERE tenant_id=%s AND stage_key LIKE 'extract:%%'",
            (tenant,),
        ).fetchall()
        assert sum(bool(row[0]["facts"][0].get("related_existing_ids")) for row in stages) == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM unit_links WHERE tenant_id=%s "
                "AND kind NOT IN ('semantic','caused_by','updates')",
                (tenant,),
            ).fetchone()[0]
            == 0
        )
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT detail FROM unit_times WHERE tenant_id=%s AND annotation_id='old-event'",
            (tenant,),
        ).fetchone()[0]
    assert stored == persisted_qualifier
    (tmp_path / "reference-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)
    )


async def test_retry_reuses_completed_extraction_and_revokes_only_unfinished_references(
    reference_client, monkeypatch
):
    from oam.workers import index_source

    client, dsn, tenant, config, _ = reference_client
    await retain(client, "old", ["I prefer tea."])
    assert run_source(dsn, config, ReferenceModels())
    with psycopg.connect(dsn) as connection:
        old_source = connection.execute(
            "SELECT source_id FROM sources WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
    freeze = index_source.freeze_extraction_references
    dispositions = []

    def observe_local_freeze(store, *args):
        references = freeze(store, *args)
        with psycopg.connect(dsn) as connection:
            dispositions.append(
                connection.execute(
                    "SELECT disposition FROM attempts WHERE tenant_id=%s AND attempt_id=%s",
                    (tenant, store.claim.attempt_id),
                ).fetchone()[0]
            )
        return references

    monkeypatch.setattr(index_source, "freeze_extraction_references", observe_local_freeze)

    class FailedOnce(ReferenceModels):
        failed = False

        def generate(self, role, **kwargs):
            result = super().generate(role, **kwargs)
            if (
                role == "extraction"
                and json.loads(kwargs["user"])["center"]["text"] == "I now prefer coffee."
                and not self.failed
            ):
                self.failed = True
                raise ExtractionError("known invalid reply", usage=result.usage, retryable=True)
            return result

    operation_id = await retain(client, "new", ["I prefer tea.", "I now prefer coffee."])
    models = FailedOnce()
    with pytest.raises(ExtractionError):
        run_source(dsn, config, models)
    with psycopg.connect(dsn) as connection:
        new_source = connection.execute(
            "SELECT source_id FROM operations WHERE tenant_id=%s AND operation_id=%s",
            (tenant, operation_id),
        ).fetchone()[0]
        # This fact was published while its historical reference was still eligible.
        assert (
            connection.execute(
                "SELECT count(*) FROM search_units WHERE tenant_id=%s AND source_id=%s "
                "AND kind='fact'",
                (tenant, new_source),
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND source_id=%s",
            (tenant, old_source),
        )
    assert run_source(dsn, config, models)
    assert len(models.inputs) == 3
    assert models.inputs[-1]["center"]["text"] == "I now prefer coffee."
    assert models.inputs[-1]["reference_facts"] == []
    assert set(dispositions) <= {"claimed", "unknown_external_outcome"}
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["usage"]["total_tokens"] == 90
    assert operation["usage"]["coverage"] == "complete"
    # Resume keeps completed publication; only unfinished extraction is revalidated.
    assert not any(
        diagnostic["code"] == "ineligible_reference"
        for diagnostic in operation["stage_state"]["extraction_diagnostics"]
    )
    from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS

    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT set_config('oam.tenant_id',%s,true)", (tenant,))
        connection.execute("SELECT set_config('oam.user_id','lin',true)")
        visible = connection.execute(
            f"SELECT source_id,count(*) FROM ({ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS}) f "
            "WHERE kind='fact' GROUP BY source_id"
        ).fetchall()
    assert visible == [(new_source, 2)]


@pytest.mark.parametrize("reference_client", [1], indirect=True)
async def test_reference_eligibility_precedes_dense_cap(reference_client):
    client, dsn, tenant, config, _ = reference_client
    sources = {}
    for label in ("deleted", "stale", "ineligible", "old_generation", "good"):
        op_id = await retain(client, label, [label + " source claim."])
        assert run_source(dsn, config, ReferenceModels())
        with psycopg.connect(dsn) as connection:
            sources[label] = connection.execute(
                "SELECT source_id FROM operations WHERE tenant_id=%s AND operation_id=%s",
                (tenant, op_id),
            ).fetchone()[0]
    with psycopg.connect(dsn) as connection:
        from test_index_links import add_fact

        generation = connection.execute(
            "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id='lin'", (tenant,)
        ).fetchone()[0]
        foreign_tenant = tenant + "-other"
        connection.execute(
            "INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (foreign_tenant,)
        )
        for target_tenant, target_user in ((tenant, "foreign"), (foreign_tenant, "lin")):
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,"
                "index_generation_id) VALUES (%s,%s,'synthetic','test','persistent',%s)",
                (target_tenant, target_user, generation),
            )
            authority = AuthorizedUser(
                "key",
                target_tenant,
                "worker",
                target_user,
                frozenset({"memory:append", "memory:read"}),
            )
            add_fact(connection, authority, "foreign", generation=generation)
        connection.execute(
            "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND source_id=%s",
            (tenant, sources["deleted"]),
        )
        connection.execute(
            "UPDATE source_versions SET status='superseded' WHERE tenant_id=%s AND source_id=%s",
            (tenant, sources["stale"]),
        )
        connection.execute(
            "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND source_id=%s",
            (tenant, sources["ineligible"]),
        )
        connection.execute(
            "UPDATE search_units SET generation_id='old' WHERE tenant_id=%s AND source_id=%s",
            (tenant, sources["old_generation"]),
        )
        connection.execute(
            "UPDATE search_units SET embedding=%s::vector "
            "WHERE tenant_id=%s AND source_id=%s AND kind='fact'",
            (str([0.5, 0.866] + [0.0] * 1022), tenant, sources["good"]),
        )
    await retain(client, "query", ["A new source claim."])
    models = ReferenceModels()
    assert run_source(dsn, config, models)
    assert [ref["text"] for ref in models.inputs[0]["reference_facts"]] == ["good source claim."]


@pytest.mark.parametrize("reference_client", [(8, 64)], indirect=True)
async def test_short_source_uses_one_call_and_keeps_mixed_message_citations(
    reference_client, tmp_path
):
    client, dsn, tenant, config, app = reference_client
    await retain(client, "old", ["I prefer tea."])
    assert run_source(dsn, config, ReferenceModels())

    semantic_time = {
        "role": "event",
        "start": "2020-01-01T00:00:00Z",
        "end": "2021-01-01T00:00:00Z",
        "quote": "I prefer tea.",
    }
    persisted_time = {**semantic_time, "origin": "caller", "reference_timezone": "UTC"}
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,time_span,detail) "
            "SELECT tenant_id,user_id,'old-group-event',unit_id,'event',"
            "tstzrange('2020-01-01','2021-01-01','[)'),%s FROM search_units "
            "WHERE tenant_id=%s AND kind='fact'",
            (Jsonb(persisted_time), tenant),
        )

    class GroupModels(ReferenceModels):
        def generate(self, role, **kwargs):
            envelope = json.loads(kwargs["user"])
            if role != "extraction" or "passages" not in envelope:
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            self.inputs.append(envelope)
            records = []
            for index, p in enumerate(envelope["passages"]):
                records.append(
                    {
                        "passage_id": p["passage_id"],
                        "text": p["center"]["text"],
                        "quote": p["center"]["text"],
                        "related_existing_ids": [r["id"] for r in envelope["reference_facts"]],
                        "updates": [0] if index == 2 else [],
                    }
                )
            return ModelResult(
                content=json.dumps({"outcome": "facts", "facts": records}),
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    messages = [
        {
            "role": "user",
            "content": "I prefer tea.",
            "event_time": "2026-09-24T09:00:00Z",
            "learning_eligible": True,
        },
        {
            "role": "assistant",
            "content": "Try green tea.",
            "event_time": "2026-09-24T10:00:00Z",
            "learning_eligible": True,
        },
        {
            "role": "user",
            "content": "I now prefer coffee.",
            "event_time": "2026-09-25T09:00:00Z",
            "learning_eligible": True,
        },
    ]
    response = await client.post(
        "/v1/retain",
        headers={"Idempotency-Key": "group"},
        json={"user_id": "lin", "input_type": "interaction", "messages": messages},
    )
    assert response.status_code == 202, response.text
    models = GroupModels()
    assert run_source(dsn, config, models)
    assert models.generation_calls == ["extraction"]
    assert len(models.inputs[0]["passages"]) == 3
    assert models.inputs[0]["reference_facts"][0]["time_qualifiers"] == [semantic_time]
    assert [ref["text"] for ref in models.inputs[0]["reference_facts"]] == ["I prefer tea."]
    app.state.runtime.models = models
    recalls = {}
    for mode in ("fast", "normal"):
        recall = await client.post(
            "/v1/recall",
            json={
                "user_id": "lin",
                "query": "tea coffee",
                "recall_level": mode,
            },
        )
        assert recall.status_code == 200, recall.text
        assert "I now prefer coffee." in recall.json()["context"]
        assert any(
            "Try green tea." in item["text"] and item["citations"][0]["span"]["role"] == "assistant"
            for item in recall.json()["items"]
        )
        recalls[mode] = recall.json()
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT content_text,locator->>'role',mentioned_at FROM search_units "
            "WHERE tenant_id=%s AND kind='fact' ORDER BY mentioned_at,content_text",
            (tenant,),
        ).fetchall()
        assert [(r[0], r[1], r[2].isoformat()) for r in rows] == [
            ("I prefer tea.", "user", "2026-09-24T09:00:00+00:00"),
            ("I prefer tea.", "user", "2026-09-24T09:00:00+00:00"),
            ("Try green tea.", "assistant", "2026-09-24T10:00:00+00:00"),
            ("I now prefer coffee.", "user", "2026-09-25T09:00:00+00:00"),
        ]
        relation = connection.execute(
            "SELECT a.content_text,b.content_text FROM unit_links l "
            "JOIN search_units a ON (a.tenant_id,a.user_id,a.unit_id)="
            "(l.tenant_id,l.user_id,l.source_id) "
            "JOIN search_units b ON (b.tenant_id,b.user_id,b.unit_id)="
            "(l.tenant_id,l.user_id,l.target_id) "
            "WHERE l.tenant_id=%s AND l.kind='updates'",
            (tenant,),
        ).fetchall()
        assert relation == [("I now prefer coffee.", "I prefer tea.")]
    (tmp_path / "grouped-http.json").write_text(
        json.dumps(
            {
                "provider": "deterministic_fake",
                "extraction_inputs": models.inputs,
                "generation_calls": models.generation_calls,
                "recalls": recalls,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize("reference_client", [(0, 2)], indirect=True)
async def test_group_retry_reuses_the_completed_group(reference_client):
    client, dsn, tenant, config, _ = reference_client
    operation_id = await retain(client, "groups", ["First.", "Second.", "Third.", "Fourth."])

    class FailedGroupOnce(SyntheticIndexModels):
        failed = False
        inputs = []

        def generate(self, role, **kwargs):
            result = super().generate(role, **kwargs)
            if role == "extraction":
                texts = [p["center"]["text"] for p in json.loads(kwargs["user"])["passages"]]
                self.inputs.append(texts)
                if texts[0] == "Third." and not self.failed:
                    self.failed = True
                    raise ExtractionError(
                        "known invalid group reply", usage=result.usage, retryable=True
                    )
            return result

    models = FailedGroupOnce()
    with pytest.raises(ExtractionError):
        run_source(dsn, config, models)
    assert run_source(dsn, config, models)
    assert models.inputs.count(["First.", "Second."]) == 1
    assert models.inputs.count(["Third.", "Fourth."]) == 2
    operation = (await client.get(f"/v1/users/lin/operations/{operation_id}")).json()
    assert operation["status"] == "succeeded"
    assert operation["usage"]["total_tokens"] == 90
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 4
        )


@pytest.mark.parametrize("reference_client", [(0, 64)], indirect=True)
async def test_cancelled_operation_stops_overflow_child_dispatch(reference_client):
    client, dsn, tenant, config, _ = reference_client
    operation_id = await retain(client, "cancel-overflow", ["First.", "Second."])

    class CancelOnOverflow(SyntheticIndexModels):
        calls = 0

        def generate(self, role, **kwargs):
            self.calls += 1
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE operations SET status='cancelled',"
                    "cancellation_revision=cancellation_revision+1 "
                    "WHERE tenant_id=%s AND operation_id=%s",
                    (tenant, operation_id),
                )
            raise ModelOutputTooLongError(
                "known overflow",
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = CancelOnOverflow()
    with pytest.raises(StaleIndexWork):
        run_source(dsn, config, models)
    assert models.calls == 1
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memories WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 0
        )
