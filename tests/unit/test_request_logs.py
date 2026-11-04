from types import SimpleNamespace

import httpx

from oam.api.app import create_app
from oam.api.request_logging import capture_authority


async def test_request_logs_capture_only_authorized_template_and_safe_metadata(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  embedding: null\n  reasoning_effort:\n"
        + "".join(
            f"    {role}: null\n" for role in ("abstract", "extraction", "insights", "profile")
        )
    )

    class Runtime:
        records = []

        def authenticate(self, actor):
            pass

        def list_entities(self, actor, user_id, **kwargs):
            capture_authority(SimpleNamespace(tenant_id="t", user_id=user_id))
            if kwargs["query"] == "bad":
                raise ValueError("SECRET")
            return {"items": [], "next_after": None}

        def record_request(self, authority, record):
            self.records.append((authority.user_id, record))

        def list_request_logs(self, actor, user_id, **kwargs):
            capture_authority(SimpleNamespace(tenant_id="t", user_id=user_id))
            return {"items": [], "next_after": None}

    runtime = Runtime()
    app = create_app(config, runtime=runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer SECRET"}
        response = await client.get("/v1/users/private/entities?query=bad", headers=headers)
        assert response.status_code == 422
        await client.get("/v1/users/private/request-logs", headers=headers)
        await client.get("/v1/users/unauthorized/entities")
    assert len(runtime.records) == 1
    user, record = runtime.records[0]
    assert user == "private"
    assert record["operation"] == "GET /v1/users/{user_id}/entities"
    assert record["error_code"] == "InvalidRequest"
    assert record["status"] == 422
    assert record["request_id"] == response.headers["x-request-id"]
    assert "SECRET" not in str(record) and "private" not in str(record)


def test_cursor_validation_rejects_wrong_timestamp_before_database():
    import base64

    import pytest

    from oam.db.request_logs import list_request_logs

    cursor = base64.urlsafe_b64encode(b'["not-a-time", "req"]')
    with pytest.raises(ValueError, match="Invalid cursor"):
        list_request_logs(None, None, after=cursor)


async def test_parallel_receipts_failure_and_denied_scopes(tmp_path):
    import asyncio
    from threading import Barrier

    import pytest

    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  embedding: null\n  reasoning_effort:\n"
        + "".join(
            f"    {role}: null\n" for role in ("abstract", "extraction", "insights", "profile")
        )
    )
    barrier = Barrier(2)

    class Runtime:
        records = []
        fail_storage = False

        def authenticate(self, actor):
            pass

        def list_entities(self, actor, user_id, **kwargs):
            if user_id == "denied":
                raise PermissionError("secret")
            capture_authority(SimpleNamespace(tenant_id="t", user_id=user_id))
            if user_id == "crash":
                raise RuntimeError("secret")
            if user_id in ("alice", "bob"):
                barrier.wait(timeout=5)
            return {"operation_id": "op_" + user_id}

        def record_request(self, authority, record):
            self.records.append((authority.user_id, record))
            if self.fail_storage:
                raise RuntimeError("secret")

    runtime = Runtime()
    app = create_app(config, runtime=runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer secret"}
        await asyncio.gather(
            *(
                client.get(f"/v1/users/{user}/entities", headers=headers)
                for user in ("alice", "bob")
            )
        )
        assert {user: record["operation_id"] for user, record in runtime.records} == {
            "alice": "op_alice",
            "bob": "op_bob",
        }
        denied = await client.get("/v1/users/denied/entities", headers=headers)
        assert denied.status_code == 404 and len(runtime.records) == 2
        runtime.fail_storage = True
        response = await client.get("/v1/users/ok/entities", headers=headers)
        assert response.status_code == 200
        with pytest.raises(RuntimeError, match="secret"):
            await client.get("/v1/users/crash/entities", headers=headers)
        assert runtime.records[-1][0] == "crash"
        assert runtime.records[-1][1]["status"] == 500
        assert runtime.records[-1][1]["error_code"] == "InternalError"


async def test_retain_receipt_is_logged_without_body():
    from pathlib import Path

    class Runtime:
        records = []

        def authenticate(self, actor):
            pass

        def retain(self, actor, body, idempotency_key, **upload):
            capture_authority(SimpleNamespace(tenant_id="t", user_id="alice"))
            return {"operation_id": "op_accepted", "source_id": "src", "revision": 1}

        def record_request(self, authority, record):
            self.records.append(record)

    runtime = Runtime()
    app = create_app(Path(__file__).parents[2] / "configs/oam.yaml", runtime=runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/retain",
            headers={"Authorization": "Bearer secret", "Idempotency-Key": "test-retain"},
            json={
                "user_id": "alice",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "PRIVATE BODY",
                        "event_time": "2026-09-23T10:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
        )
    assert response.status_code == 202, response.text
    assert runtime.records[0]["operation_id"] == "op_accepted"
    assert "PRIVATE BODY" not in str(runtime.records)


async def test_log_storage_does_not_delay_response_and_is_drained(tmp_path):
    import asyncio
    from threading import Event

    # A blocked diagnostic write must not hold the business response body.
    released = Event()
    recorded = Event()
    body_sent = asyncio.Event()
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  embedding: null\n")

    class Runtime:
        def authenticate(self, actor):
            pass

        def list_entities(self, actor, user_id, **kwargs):
            capture_authority(SimpleNamespace(tenant_id="t", user_id=user_id))
            return {"items": [], "next_after": None}

        def record_request(self, authority, record):
            assert released.wait(5), "diagnostic writer was not released"
            recorded.set()

    app = create_app(config, runtime=Runtime())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/users/alice/entities",
        "raw_path": b"/v1/users/alice/entities",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer fixture")],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }

    async def receive():
        await asyncio.Event().wait()

    messages = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            body_sent.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(body_sent.wait(), 1)
        assert not recorded.is_set()
    finally:
        released.set()
        await asyncio.wait_for(task, 5)
    assert recorded.is_set(), "response-owned diagnostic work must drain"
    assert messages[0]["status"] == 200
    assert b'"items":[]' in b"".join(m.get("body", b"") for m in messages)
