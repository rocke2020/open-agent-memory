"""Public tenant/user admission and server-owned document revisions."""

import asyncio

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from test_core_http import ROOT, use_created_user_key
from test_core_http import core_database as core_database

pytestmark = pytest.mark.integration


@pytest.fixture
async def retain_client(core_database):
    dsn, tenant, headers = core_database
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        await use_created_user_key(client, "lin")
        yield client, dsn, tenant


async def test_retain_interaction_assigns_identity_and_reuses_exact_logical_request(retain_client):
    client, dsn, tenant = retain_client
    body = {
        "user_id": "lin",
        "input_type": "interaction",
        "messages": [
            {
                "role": "user",
                "content": "Concise summaries, please.",
                "event_time": "2026-09-23T10:00:00+08:00",
                "learning_eligible": True,
            },
            {
                "role": "assistant",
                "content": "Report glucose is 99.",
                "event_time": "2026-09-23T10:00:02+08:00",
                "learning_eligible": False,
            },
        ],
    }
    first = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "chat"})
    assert first.status_code == 202, first.text
    second = await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "chat"})
    assert second.json()["source_id"] == first.json()["source_id"]
    assert second.json()["operation_id"] == first.json()["operation_id"]
    operation = await client.get(f"/v1/users/lin/operations/{first.json()['operation_id']}")
    assert operation.status_code == 200
    assert operation.json()["source_id"] == first.json()["source_id"]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sources WHERE tenant_id=%s", (tenant,)
            ).fetchone()[0]
            == 1
        )
    body["messages"][0]["content"] = "Changed."
    assert (
        await client.post("/v1/retain", json=body, headers={"Idempotency-Key": "chat"})
    ).status_code == 409


async def test_concurrent_document_appends_and_retry_do_not_lose_or_duplicate_text(retain_client):
    client, dsn, tenant = retain_client
    base = {"input_type": "document", "document_id": "report"}
    first = await client.post(
        "/v1/users/lin/resources",
        json={**base, "update_mode": "replace", "text": "First."},
        headers={"Idempotency-Key": "base"},
    )
    assert first.status_code == 202, first.text

    async def append(text, key):
        return await client.post(
            "/v1/users/lin/resources",
            json={**base, "update_mode": "append", "text": text},
            headers={"Idempotency-Key": key},
        )

    responses = await asyncio.gather(append("\nSecond.", "second"), append("\nThird.", "third"))
    assert all(response.status_code == 202 for response in responses), [r.text for r in responses]
    assert sorted(r.json()["revision"] for r in responses) == [2, 3]
    retry = await append("\nSecond.", "second")
    assert retry.json()["operation_id"] == responses[0].json()["operation_id"]
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE tenant_id=%s AND status='active'",
            (tenant,),
        ).fetchall()
        assert len(rows) == 1
        assert bytes(rows[0][0]) in (b"First.\nSecond.\nThird.", b"First.\nThird.\nSecond.")


async def test_user_without_a_grant_cannot_retain_or_recall(retain_client):
    client, _, _ = retain_client
    denied = await client.post(
        "/v1/users/ungranted/resources",
        json={
            "input_type": "document",
            "document_id": "report",
            "update_mode": "replace",
            "text": "Private.",
        },
        headers={"Idempotency-Key": "private"},
    )
    assert denied.status_code == 404
    assert (
        await client.post("/v1/recall", json={"user_id": "ungranted", "query": "Private"})
    ).status_code == 404


async def test_invalid_database_credential_is_rejected_before_upload(retain_client):
    client, _, _ = retain_client
    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b'{"user_id":"lin","input_type":"document"}'

    response = await client.post(
        "/v1/users/lin/resources",
        content=body(),
        headers={
            "Authorization": "Bearer invalid-credential",
            "Content-Type": "application/json",
            "Idempotency-Key": "invalid-credential",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AuthenticationFailed"
    assert not consumed
