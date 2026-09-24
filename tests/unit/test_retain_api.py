"""The ordinary caller supplies a user and content, not storage revisions."""

from pathlib import Path

import httpx
import pytest

from oam.api.app import create_app

ROOT = Path(__file__).parents[2]


class RetainRuntime:
    def authenticate(self, secret):
        pass

    def retain(self, secret, body, idempotency_key, **upload):
        return {"operation_id": "op-accepted", "source_id": "src-assigned", "revision": 1}


@pytest.mark.parametrize(
    "body",
    [
        {
            "user_id": "lin",
            "input_type": "interaction",
            "messages": [
                {
                    "role": "user",
                    "content": "I prefer concise summaries.",
                    "event_time": "2026-09-23T10:00:00+08:00",
                    "learning_eligible": True,
                }
            ],
        },
        {
            "user_id": "lin",
            "input_type": "document",
            "document_id": "checkup",
            "update_mode": "replace",
            "text": "Glucose: 5.2 mmol/L.",
        },
    ],
)
async def test_retain_accepts_the_minimal_developer_request(body):
    app = create_app(ROOT / "configs/oam.yaml", runtime=RetainRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "logical-request"},
    ) as client:
        response = await client.post("/v1/retain", json=body)
    assert response.status_code == 202, response.text
    assert response.json()["source_id"] == "src-assigned"


@pytest.mark.parametrize(
    "field,value",
    [("source_version", 2), ("scope_id", "other"), ("tenant_id", "other"), ("source_id", "chat")],
)
async def test_retain_rejects_caller_storage_and_tenant_selectors(field, value):
    app = create_app(ROOT / "configs/oam.yaml", runtime=RetainRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "logical-request"},
    ) as client:
        response = await client.post(
            "/v1/retain",
            json={
                "user_id": "lin",
                "input_type": "document",
                "document_id": "checkup",
                "update_mode": "replace",
                "text": "Glucose: 5.2 mmol/L.",
                field: value,
            },
        )
    assert response.status_code == 422, response.text


async def test_retain_requires_explicit_learning_eligibility_and_rejects_file_append():
    app = create_app(ROOT / "configs/oam.yaml", runtime=RetainRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "logical-request"},
    ) as client:
        missing = await client.post(
            "/v1/retain",
            json={
                "user_id": "lin",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "The report says 99.",
                        "event_time": "2026-09-23T10:00:00+08:00",
                    }
                ],
            },
        )
        append = await client.post(
            "/v1/retain",
            data={
                "metadata": (
                    '{"user_id":"lin","input_type":"document",'
                    '"document_id":"scan","update_mode":"append"}'
                )
            },
            files={"original": ("scan.html", b"<p>99</p>", "text/html")},
        )
    assert missing.status_code == 422
    assert append.status_code == 422


async def test_recall_requires_user_and_does_not_accept_tenant_or_scope_selection():
    app = create_app(ROOT / "configs/oam.yaml", runtime=RetainRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic"},
    ) as client:
        for body in (
            {"query": "summary"},
            {"user_id": "lin", "query": "summary", "scope_id": "x"},
            {"user_id": "lin", "query": "summary", "tenant_id": "x"},
        ):
            assert (await client.post("/v1/recall", json=body)).status_code == 422
        assert (
            await client.post("/v1/users/lin/search", json={"query": "summary"})
        ).status_code == 404
