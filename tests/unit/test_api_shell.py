from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from oam.api.app import create_app
from oam.operations.repository import OperationRecord
from oam.operations.state import OperationStatus
from oam.retrieval.service import RecallUnavailable

REPOSITORY_ROOT = Path(__file__).parents[2]


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=create_app(REPOSITORY_ROOT / "configs" / "oam.yaml"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def test_liveness_only_claims_process_health(client) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive", "contract_version": "0.0.1"}
    assert response.headers["oam-contract-version"] == "0.0.1"
    assert response.headers["x-request-id"]


async def test_inspector_serves_a_same_origin_script_under_its_csp(client) -> None:
    page = await client.get("/ui/")
    script = await client.get("/ui/app.js")

    assert page.status_code == 200
    assert '<script src="/ui/app.js" defer></script>' in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")


async def test_readiness_fails_when_required_model_roles_are_unbound(client) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["missing_required_roles"] == [
        "abstract",
        "document_vision",
        "embedding",
        "extraction",
        "insights",
        "profile",
    ]


async def test_binding_names_alone_cannot_make_the_foundation_ready(tmp_path) -> None:
    config = tmp_path / "oam.yaml"
    config.write_text(
        "models:\n  roles:\n"
        + "".join(
            f"    {role}: synthetic\n"
            for role in (
                "extraction",
                "insights",
                "embedding",
                "document_vision",
                "abstract",
                "profile",
            )
        )
        + "  bindings:\n    synthetic:\n      protocol: openai_compatible\n"
        "      model: synthetic\n      endpoint_env: UNSET_SYNTHETIC_ENDPOINT\n"
        "      capabilities: [chat, vision, embeddings]\n",
        encoding="utf-8",
    )
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["missing_required_roles"] == []


@pytest.mark.parametrize(
    ("config_text", "route", "payload", "expected_status"),
    [
        (
            "resource:\n  max_bytes: 4\n",
            "resources",
            {
                "resource_id": "report",
                "media_type": "text/html",
                "filename": "report.html",
                "byte_length": 5,
                "sha256": "a" * 64,
            },
            422,
        ),
        (
            "ingest:\n  max_bytes: 4\n",
            "evidence",
            {
                "source_id": "source",
                "messages": [
                    {
                        "message_id": "m",
                        "role": "user",
                        "content": "12345",
                        "event_time": "2026-09-21T00:00:00Z",
                    }
                ],
            },
            422,
        ),
        ("search:\n  max_queries: 1\n", "search-many", {"queries": ["one", "two"]}, 422),
        ("search:\n  max_queries: 5\n", "search-many", {"queries": ["q"] * 5}, 200),
        (
            "context:\n  default_tokens: 16\n  max_tokens: 32\n",
            "search",
            {
                "query": "q",
                "max_context_tokens": 33,
            },
            422,
        ),
        (
            "chunks:\n  default_tokens: 16\n  max_tokens: 32\n",
            "decompose-and-search",
            {
                "query": "q",
                "include": {"chunks": {"max_tokens": 33}},
            },
            422,
        ),
    ],
)
async def test_http_admission_uses_selected_config_limits(
    tmp_path, config_text, route, payload, expected_status
) -> None:
    class AuthenticatedRuntime:
        def authenticate(self, secret):
            pass

        def search_many(self, actor, user_id, body):
            return {"queries": body.queries}

    config = tmp_path / "oam.yaml"
    config.write_text(config_text, encoding="utf-8")
    transport = httpx.ASGITransport(app=create_app(config, runtime=AuthenticatedRuntime()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if route == "evidence":
            payload = {
                "user_id": "scope",
                "input_type": "interaction",
                "messages": [
                    {key: value for key, value in message.items() if key != "message_id"}
                    | {"learning_eligible": True}
                    for message in payload["messages"]
                ],
            }
        elif route == "resources":
            payload = {
                "user_id": "scope",
                "input_type": "document",
                "document_id": "report",
                "update_mode": "replace",
                "text": "12345",
            }
        elif route == "search":
            payload = {"user_id": "scope", **payload}
        path = (
            "/v1/retain"
            if route in {"evidence", "resources"}
            else ("/v1/recall" if route == "search" else f"/v1/users/scope/{route}")
        )
        response = await client.post(
            path,
            json=payload,
            headers={"Authorization": "Bearer synthetic-key", "Idempotency-Key": "limit-test"},
        )
    assert response.status_code == expected_status


async def test_capabilities_publish_limits_without_secrets(client) -> None:
    response = await client.get("/v1/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "0.0.1"
    assert payload["limits"]["resource"]["max_bytes"] == 20 * 1024 * 1024
    assert payload["limits"]["chunk"]["max_estimated_tokens"] == 4096
    assert payload["features"]["source_lifecycle"] is True
    assert payload["features"]["recorded_debug"] is True
    assert payload["features"]["portable_export"] is True
    assert payload["features"]["profiles"] is False
    assert payload["export_format_versions"] == [1]
    assert payload["limits"]["model"]["attempt_timeout_seconds"] == 60
    assert payload["limits"]["user"]["max_chats"] == 1000
    assert payload["features"]["python_sdk"] is False
    assert all(payload["features"][f"{mode}_recall"] is True for mode in ("fast", "mid", "deep"))
    assert payload["features"]["batch_recall"] is True
    assert payload["features"]["planner"] is False
    assert "api_key" not in response.text.lower()


@pytest.mark.parametrize(
    ("error", "expected_status", "code"),
    [
        (RecallUnavailable("internal model details"), 503, "RecallUnavailable"),
        (TimeoutError("internal connection details"), 504, "RecallDeadlineExceeded"),
        (NotImplementedError("later-stage role"), 503, "CapabilityUnavailable"),
        (PermissionError("private scope"), 404, "UserUnavailable"),
    ],
)
async def test_search_reports_unavailable_deadline_and_authority_without_internal_details(
    error, expected_status, code
):
    class Runtime:
        def search(self, *args):
            raise error

    app = create_app(REPOSITORY_ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/recall",
            json={"user_id": "test", "query": "test"},
            headers={"Authorization": "Bearer synthetic"},
        )
    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == code
    assert str(error) not in response.text


async def test_search_many_http_returns_ordered_query_outcomes() -> None:
    class Runtime:
        def search_many(self, actor, user_id, body):
            assert user_id == "lin"
            assert body.queries == ["current city", "preferred style"]
            return {
                "status": "ok",
                "results": [
                    {"index": 0, "query": "current city", "status": "ok", "result": {"items": []}},
                    {
                        "index": 1,
                        "query": "preferred style",
                        "status": "ok",
                        "result": {"items": []},
                    },
                ],
            }

    app = create_app(REPOSITORY_ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/users/lin/search-many",
            json={"queries": ["current city", "preferred style"]},
            headers={"Authorization": "Bearer synthetic"},
        )

    assert response.status_code == 200
    assert [row["query"] for row in response.json()["results"]] == [
        "current city",
        "preferred style",
    ]


async def test_decompose_http_returns_plan_and_fallback_state() -> None:
    class Runtime:
        def decompose_and_search(self, actor, user_id, body):
            assert user_id == "lin"
            assert body.query == "city and style"
            return {
                "status": "ok",
                "plan": {"queries": ["city", "style"]},
                "planner": {"calls": 1, "status": "used"},
                "fallback": None,
                "results": [],
            }

    app = create_app(REPOSITORY_ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/users/lin/decompose-and-search",
            json={"query": "city and style"},
            headers={"Authorization": "Bearer synthetic"},
        )

    assert response.status_code == 200
    assert response.json()["plan"] == {"queries": ["city", "style"]}
    assert response.json()["planner"]["calls"] == 1


async def test_openapi_reserves_public_step_four_routes(client) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert {
        "/v1/users",
        "/v1/users/{user_id}/grants/{principal_id}",
        "/v1/credentials",
        "/v1/credentials/{credential_id}",
        "/v1/retain",
        "/v1/users/{user_id}/resources",
        "/v1/users/{user_id}/resources/{resource_id}",
        "/v1/users/{user_id}/resources/{resource_id}/original",
        "/v1/users/{user_id}/evidence/{source_id}",
        "/v1/users/{user_id}/memories",
        "/v1/recall",
        "/v1/users/{user_id}/search-many",
        "/v1/users/{user_id}/decompose-and-search",
        "/v1/users/{user_id}/profile",
        "/v1/users/{user_id}/profile/refresh",
        "/v1/users/{user_id}/operations/{operation_id}",
        "/v1/users/{user_id}/operations/{operation_id}/cancel",
        "/v1/users/{user_id}/operations/{operation_id}/retry",
        "/v1/users/{user_id}/operations/{operation_id}/reconcile-attempt",
        "/v1/users/{user_id}/debug/traces/{trace_id}",
        "/v1/users/{user_id}/debug/diff",
        "/v1/users/{user_id}/profile/diff",
        "/v1/users/{user_id}/replays",
        "/v1/users/{user_id}/exports",
        "/v1/users/{user_id}/exports/{export_id}",
        "/v1/users/{user_id}/exports/{export_id}/records/{index}",
        "/v1/users/{user_id}/exports/{export_id}/originals/{index}",
    } <= set(paths)


async def test_openapi_exposes_https_evidence_url_retain_variant(client) -> None:
    schema = (await client.get("/openapi.json")).json()
    request = schema["paths"]["/v1/retain"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]

    assert request["discriminator"]["mapping"]["document_url"] == (
        "#/components/schemas/RetainURLDocument"
    )
    assert set(schema["components"]["schemas"]["RetainURLDocument"]["properties"]) >= {
        "url",
        "media_type",
        "document_id",
    }


async def test_evidence_route_rejects_body_owned_identity_before_capability_check(client) -> None:
    response = await client.post(
        "/v1/retain",
        headers={"Authorization": "Bearer synthetic-key"},
        json={
            "tenant_id": "tenant-1",
            "user_id": "scope-1",
            "input_type": "interaction",
            "messages": [
                {
                    "learning_eligible": True,
                    "role": "user",
                    "content": "Hello",
                    "event_time": "2026-09-21T00:00:00Z",
                }
            ],
        },
    )

    assert response.status_code == 422


async def test_valid_unwired_evidence_route_fails_closed(client) -> None:
    response = await client.post(
        "/v1/retain",
        headers={"Authorization": "Bearer synthetic-key", "Idempotency-Key": "synthetic-input"},
        json={
            "user_id": "scope-1",
            "input_type": "interaction",
            "messages": [
                {
                    "learning_eligible": True,
                    "role": "user",
                    "content": "Hello",
                    "event_time": "2026-09-21T00:00:00Z",
                }
            ],
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CapabilityUnavailable"


async def test_scoped_routes_require_bearer_before_parsing_content(client) -> None:
    response = await client.post("/v1/retain", content=b"not-json")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AuthenticationRequired"


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/users"), ("GET", "/v1/credentials")],
)
async def test_administration_routes_require_bearer(client, method: str, path: str) -> None:
    response = await client.request(method, path, content=b"not-json")

    assert response.status_code == 401


def _operation_record(
    *, status: OperationStatus = OperationStatus.RUNNING, operation_id: str = "op-1"
) -> OperationRecord:
    now = datetime(2026, 9, 21, tzinfo=UTC)
    return OperationRecord(
        operation_id=operation_id,
        kind="evidence_append",
        status=status,
        accepted_seq=1,
        plan_sealed=True,
        cancellation_revision=0,
        retry_of_operation_id=None,
        source_id="source-1",
        source_version=1,
        created_at=now,
        updated_at=now,
    )


class RecordingAPIRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def get(self, bearer_secret: str, user_id: str, operation_id: str) -> OperationRecord:
        self.calls.append(("get", (bearer_secret, user_id, operation_id)))
        return _operation_record(operation_id=operation_id)

    def cancel(self, bearer_secret: str, user_id: str, operation_id: str) -> OperationRecord:
        self.calls.append(("cancel", (bearer_secret, user_id, operation_id)))
        return _operation_record(status=OperationStatus.CANCELLED, operation_id=operation_id)

    def retry(
        self,
        bearer_secret: str,
        user_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> OperationRecord:
        self.calls.append(("retry", (bearer_secret, user_id, operation_id, idempotency_key)))
        return _operation_record(status=OperationStatus.ACCEPTED, operation_id="op-retry")

    def reconcile(
        self,
        bearer_secret: str,
        user_id: str,
        operation_id: str,
        request,
    ) -> OperationRecord:
        self.calls.append(("reconcile", (bearer_secret, user_id, operation_id, request.attempt_id)))
        return _operation_record(operation_id=operation_id)


@pytest.fixture
async def operation_client():
    runtime = RecordingAPIRuntime()
    transport = httpx.ASGITransport(
        app=create_app(
            REPOSITORY_ROOT / "configs" / "oam.yaml",
            runtime=runtime,
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value, runtime


async def test_operation_routes_delegate_with_bearer_scope_and_exact_attempt(
    operation_client,
) -> None:
    client, runtime = operation_client
    headers = {"Authorization": "Bearer synthetic-key"}

    fetched = await client.get("/v1/users/scope-1/operations/op-1", headers=headers)
    cancelled = await client.post("/v1/users/scope-1/operations/op-1/cancel", headers=headers)
    retried = await client.post(
        "/v1/users/scope-1/operations/op-1/retry",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    reconciled = await client.post(
        "/v1/users/scope-1/operations/op-1/reconcile-attempt",
        headers=headers,
        json={
            "work_unit_id": "work-1",
            "attempt_id": "attempt-1",
            "owner_id": "worker-1",
            "fence": 4,
            "disposition": "failed",
            "response_ref": "provider-receipt-1",
            "usage": {"input_tokens": 12},
        },
    )

    assert [response.status_code for response in (fetched, cancelled, retried, reconciled)] == [
        200,
        200,
        201,
        200,
    ]
    assert fetched.json()["status"] == "processing"
    assert cancelled.json()["status"] == "cancelled"
    assert retried.json()["operation_id"] == "op-retry"
    assert [name for name, _ in runtime.calls] == ["get", "cancel", "retry", "reconcile"]
    assert runtime.calls[-1][1] == ("synthetic-key", "scope-1", "op-1", "attempt-1")


async def test_inspector_shell_has_same_origin_policy_and_no_embedded_memory(client) -> None:
    response = await client.get("/ui/")
    styles = await client.get("/ui/styles.css")

    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text
    assert "Open Agent Memory Inspector" in response.text
    assert '<meta name="color-scheme" content="light dark">' in response.text
    assert 'href="/ui/styles.css"' in response.text
    assert styles.status_code == 200
    assert "prefers-color-scheme: dark" in styles.text
