"""Retain authenticates before consumption and bounds actual incoming bytes."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import starlette.formparsers

import oam.api.app as api
from oam.api.runtime import AuthenticationFailed
from oam.config import load_config

ROOT = Path(__file__).parents[2]


class AdmissionRuntime:
    def authenticate(self, secret):
        if secret != "valid":
            raise AuthenticationFailed

    def retain(self, secret, body, idempotency_key, **upload):
        self.authenticate(secret)
        return {"operation_id": "accepted", "source_id": "assigned", "revision": 1}

    def preflight_revision(
        self, secret, user_id, profile_data_id, expected_version, idempotency_key, *, input_role
    ):
        self.authenticate(secret)

    def retain_profile_data(self, secret, user_id, body, idempotency_key, *, expected_version):
        self.authenticate(secret)
        return {"operation_id": "accepted", "source_id": body.profile_data_id, "revision": 1}


class IncomingBody(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content
        self.consumed = 0

    async def __aiter__(self):
        for offset in range(0, len(self.content), 128):
            chunk = self.content[offset : offset + 128]
            self.consumed += len(chunk)
            yield chunk


def payload(kind, size):
    body = {
        "input_type": "document",
        "document_id": "doc",
        "update_mode": "replace",
    }
    if kind == "json":
        return json.dumps({**body, "text": "x" * size}).encode(), "application/json"
    request = httpx.Request(
        "POST",
        "http://synthetic/v1/users/lin/resources",
        data={"metadata": json.dumps(body)},
        files={"original": ("doc.html", b"x" * size, "text/html")},
    )
    return request.read(), request.headers["content-type"]


@pytest.fixture
async def client(monkeypatch):
    resolved = load_config(ROOT / "configs/oam.yaml")
    config = resolved.config.model_copy(
        update={"api": resolved.config.api.model_copy(update={"max_request_bytes": 512})}
    )
    monkeypatch.setattr(api, "load_config", lambda path: replace(resolved, config=config))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=api.create_app(ROOT / "configs/oam.yaml", runtime=AdmissionRuntime()),
            raise_app_exceptions=False,
        ),
        base_url="http://synthetic",
    ) as value:
        yield value


@pytest.mark.parametrize("kind", ["json", "multipart"])
async def test_invalid_credential_does_not_consume_retain_body(client, kind):
    raw, content_type = payload(kind, 4096)
    stream = IncomingBody(raw)
    response = await client.post(
        "/v1/users/lin/resources",
        content=stream,
        headers={
            "Authorization": "Bearer invalid",
            "Idempotency-Key": "input",
            "Content-Type": content_type,
        },
    )
    assert response.status_code == 401
    assert stream.consumed == 0


@pytest.mark.parametrize("kind", ["json", "multipart"])
@pytest.mark.parametrize("declared_length", [None, "1", "100000"])
async def test_retain_rejects_before_consuming_oversized_body(client, kind, declared_length):
    raw, content_type = payload(kind, 4096)
    stream = IncomingBody(raw)
    headers = {
        "Authorization": "Bearer valid",
        "Idempotency-Key": "input",
        "Content-Type": content_type,
    }
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    response = await client.post("/v1/users/lin/resources", content=stream, headers=headers)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "RequestTooLarge"
    assert stream.consumed < len(raw)
    assert response.headers["oam-contract-version"] == "0.0.1"
    if declared_length == "100000":
        assert stream.consumed == 0


async def test_limit_rejection_closes_partial_multipart_upload(client, monkeypatch):
    opened = []
    original = starlette.formparsers.SpooledTemporaryFile

    def tracked_file(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked_file)
    raw, content_type = payload("multipart", 4096)
    response = await client.post(
        "/v1/users/lin/resources",
        content=IncomingBody(raw),
        headers={
            "Authorization": "Bearer valid",
            "Idempotency-Key": "input",
            "Content-Type": content_type,
        },
    )
    assert response.status_code == 413
    assert opened and all(file.closed for file in opened)


@pytest.mark.parametrize("kind", ["json", "multipart"])
async def test_bounded_retain_still_accepts_valid_content(client, kind):
    raw, content_type = payload(kind, 20)
    response = await client.post(
        "/v1/users/lin/resources",
        content=IncomingBody(raw),
        headers={
            "Authorization": "Bearer valid",
            "Idempotency-Key": "input",
            "Content-Type": content_type,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["source_id"] == "assigned"


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_profile_data_stream_overflow_returns_413(client, method, declared_length):
    raw = json.dumps(
        {
            "profile_data_id": "basic",
            "subtype": "basic-profile/v1",
            "content": {"interests": ["x" * 4096]},
        }
    ).encode()
    stream = IncomingBody(raw)
    headers = {
        "Authorization": "Bearer valid",
        "Idempotency-Key": "profile-data",
        "Content-Type": "application/json",
    }
    path = "/v1/users/lin/profile-data"
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    if method == "PUT":
        path += "/basic"
        headers["If-Match"] = "1"
    response = await client.request(method, path, content=stream, headers=headers)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "RequestTooLarge"
    assert stream.consumed < len(raw)
    assert response.headers["oam-contract-version"] == "0.0.1"
