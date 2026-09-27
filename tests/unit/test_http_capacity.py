from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/retain"),
        ("PUT", "/v1/users/u/resources/search-many"),
        ("DELETE", "/v1/users/u/resources/decompose-and-search"),
    ],
)
async def test_capacity_rejection_does_not_read_or_accept_request_body(method, path):
    from oam.api.admission import RequestAdmissionMiddleware

    class FullCapacity:
        def try_http(self, *, write):
            assert write is True
            return None

    async def downstream(*_):
        pytest.fail("rejected write reached application")

    async def receive():
        pytest.fail("rejected write read request body")

    sent = []

    async def send(message):
        sent.append(message)

    app = RequestAdmissionMiddleware(downstream, runtime=SimpleNamespace(capacity=FullCapacity()))
    await app({"type": "http", "method": method, "path": path}, receive, send)
    assert sent[0]["status"] == 429
    assert (b"retry-after", b"1") in sent[0]["headers"]
    assert b'"retryable":true' in sent[1]["body"]


@pytest.mark.parametrize(
    "path", ["/v1/recall", "/v1/users/u/search-many", "/v1/users/u/decompose-and-search"]
)
async def test_post_search_uses_read_capacity_and_releases_after_response(path):
    from oam.api.admission import RequestAdmissionMiddleware

    events = []

    class Permit:
        def close(self):
            events.append("released")

    class Capacity:
        def try_http(self, *, write):
            events.append(write)
            return Permit()

    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        events.append("body")
        await send({"type": "http.response.body", "body": b"ok"})
        events.append("finished")

    app = RequestAdmissionMiddleware(application, runtime=SimpleNamespace(capacity=Capacity()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.post(path)).status_code == 200
    assert events == [False, "body", "finished", "released"]


async def test_readiness_reports_a_pool_blocked_by_unknown_model_calls(tmp_path):
    from oam.api.app import create_app

    class Capacity:
        def status(self):
            return {
                "llm_unknown": 32,
                "embedding_unknown": 0,
                "physical_concurrency_verified": False,
            }

    config = tmp_path / "api.yaml"
    config.write_text("worker:\n  embedded_enabled: false\n")
    app = create_app(config, runtime=SimpleNamespace(capacity=Capacity()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "model_capacity_blocked"
    assert response.json()["capacity"]["physical_concurrency_verified"] is False
