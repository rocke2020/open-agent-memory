import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from oam.api.runtime import (
    APIRuntime,
    AuthenticationFailed,
    AuthorizationFailed,
    DatabaseRuntime,
)
from oam.auth.administration import (
    CredentialAdministrationDenied,
    CredentialAuthenticationFailed,
    CredentialPrincipalUnavailable,
)
from oam.config import OAMConfig, load_config
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
from oam.contracts.evidence import EvidenceAppendRequest
from oam.contracts.operations import (
    OperationReadiness,
    OperationResponse,
    OperationStatus,
    ReconcileAttemptRequest,
)
from oam.contracts.profile import ProfileRefreshRequest
from oam.contracts.resources import EvidenceResourceCreate
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.operations.repository import (
    AttemptOwnershipMismatch,
    OperationIdempotencyConflict,
    OperationNotCancellable,
    OperationNotFound,
    OperationNotRetryable,
    OperationRecord,
    RetryRequiresPlanning,
)
from oam.operations.state import OperationStatus as StoredOperationStatus

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)
PUBLIC_LIMIT_SECTIONS = (
    "resource",
    "api",
    "chunk",
    "extraction",
    "search",
    "index",
    "recall",
    "context",
    "chunks",
    "profile",
    "planner",
    "request",
    "model",
    "retry",
    "worker",
    "lease",
    "auth",
    "ingest",
    "queue",
    "provider",
    "scope",
    "trace",
    "evidence",
    "audit",
    "export",
    "backup",
)


def _unavailable() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "CapabilityUnavailable", "retryable": False},
    )


def create_app(
    config_path: str | Path,
    *,
    runtime: APIRuntime | None = None,
) -> FastAPI:
    resolved = load_config(config_path)
    owned_runtime: DatabaseRuntime | None = None
    if runtime is None:
        dsn = os.environ.get(resolved.config.database.api_dsn_env)
        if dsn:
            owned_runtime = DatabaseRuntime.from_dsn(resolved.config, dsn)
            runtime = owned_runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owned_runtime is not None:
            owned_runtime.open()
        try:
            yield
        finally:
            if owned_runtime is not None:
                owned_runtime.close()

    app = FastAPI(
        title="Open Agent Memory",
        version=resolved.config.contract_version,
        lifespan=lifespan,
    )
    app.state.resolved_config = resolved
    app.state.runtime = runtime

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        bearer_secret = _bearer_secret(request.headers.get("authorization"))
        if _requires_bearer(request.url.path) and bearer_secret is None:
            response = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": {
                        "code": "AuthenticationRequired",
                        "retryable": False,
                    }
                },
            )
        else:
            request.state.bearer_secret = bearer_secret
            response = await call_next(request)
        response.headers["X-Request-ID"] = f"req_{uuid4().hex}"
        response.headers["OAM-Contract-Version"] = resolved.config.contract_version
        return response

    @app.get("/health/live", tags=["Health"])
    async def liveness() -> dict[str, str]:
        return {"status": "alive", "contract_version": resolved.config.contract_version}

    @app.get("/health/ready", tags=["Health"])
    async def readiness(response: Response) -> dict[str, Any]:
        missing = sorted(resolved.config.models.required_unbound_roles())
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "ready": False,
            "missing_required_roles": missing,
            "reason": "foundation_only",
        }

    @app.get("/v1/capabilities", tags=["Capabilities"])
    async def capabilities() -> dict[str, Any]:
        config = resolved.config
        limits = {
            section: getattr(config, section).model_dump(mode="json")
            for section in PUBLIC_LIMIT_SECTIONS
        }
        limits["database"] = {
            "api_pool_max": config.database.api_pool_max,
            "worker_pool_max": config.database.worker_pool_max,
        }
        return {
            "contract_version": config.contract_version,
            "config_sha256": resolved.sha256,
            "limits": limits,
            "features": {
                "python_sdk": False,
                "fast_recall": False,
                "deep_recall": False,
                "profiles": False,
                "inspector": True,
            },
        }

    @app.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
    async def inspector() -> HTMLResponse:
        return HTMLResponse(
            files("oam.web").joinpath("index.html").read_text(encoding="utf-8"),
            headers={"Content-Security-Policy": CSP},
        )

    @app.get("/ui/styles.css", include_in_schema=False)
    async def inspector_styles() -> Response:
        return Response(
            files("oam.web").joinpath("styles.css").read_text(encoding="utf-8"),
            media_type="text/css",
            headers={"Content-Security-Policy": CSP},
        )

    _register_reserved_routes(app, runtime, resolved.config)
    return app


def _register_reserved_routes(
    app: FastAPI,
    runtime: APIRuntime | None,
    config: OAMConfig,
) -> None:
    @app.post("/v1/scopes", status_code=201, tags=["Administration"])
    async def create_scope(body: dict[str, Any]) -> None:
        _unavailable()

    @app.put(
        "/v1/scopes/{scope_id}/grants/{principal_id}",
        tags=["Administration"],
    )
    async def replace_scope_grant(
        scope_id: str,
        principal_id: str,
        body: dict[str, Any],
    ) -> None:
        _unavailable()

    @app.get("/v1/credentials", tags=["Administration"], response_model=CredentialListResponse)
    def list_credentials(
        request: Request, limit: int | None = Query(default=None, ge=1), after: str | None = None
    ) -> CredentialListResponse:
        selected_limit = config.auth.credential_page_size if limit is None else limit
        if selected_limit > config.auth.credential_page_size:
            raise HTTPException(
                status_code=422, detail={"code": "InputLimitExceeded", "retryable": False}
            )
        return _credential_call(
            lambda: _require_runtime(runtime).list_credentials(
                request.state.bearer_secret, limit=selected_limit, after=after
            )
        )

    @app.post(
        "/v1/credentials",
        status_code=201,
        tags=["Administration"],
        response_model=CredentialIssuedResponse,
    )
    def issue_credential(
        request: Request, body: CredentialIssueRequest
    ) -> CredentialIssuedResponse:
        return _credential_call(
            lambda: _require_runtime(runtime).issue_credential(request.state.bearer_secret, body)
        )

    @app.delete(
        "/v1/credentials/{credential_id}",
        tags=["Administration"],
        response_model=CredentialRevokedResponse,
    )
    def revoke_credential(request: Request, credential_id: str) -> CredentialRevokedResponse:
        return _credential_call(
            lambda: _require_runtime(runtime).revoke_credential(
                request.state.bearer_secret, credential_id
            )
        )

    @app.post("/v1/scopes/{scope_id}/evidence", status_code=202, tags=["Evidence"])
    async def append_evidence(scope_id: str, body: EvidenceAppendRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/resources", status_code=202, tags=["Resources"])
    async def create_resource(scope_id: str, body: EvidenceResourceCreate) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.get("/v1/scopes/{scope_id}/resources", tags=["Resources"])
    async def list_resources(scope_id: str) -> None:
        _unavailable()

    @app.get("/v1/scopes/{scope_id}/memories", tags=["Memory"])
    async def list_memories(scope_id: str) -> None:
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/search", tags=["Recall"])
    async def search(scope_id: str, body: SearchRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/search-many", tags=["Recall"])
    async def search_many(scope_id: str, body: SearchManyRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/decompose-and-search", tags=["Recall"])
    async def decompose_and_search(scope_id: str, body: SearchRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.get("/v1/scopes/{scope_id}/profile", tags=["Profiles"])
    async def get_profile(scope_id: str) -> None:
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/profile/refresh", tags=["Profiles"])
    async def refresh_profile(scope_id: str, body: ProfileRefreshRequest) -> None:
        _unavailable()

    @app.get(
        "/v1/scopes/{scope_id}/operations/{operation_id}",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def get_operation(
        request: Request,
        scope_id: str,
        operation_id: str,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.get(request.state.bearer_secret, scope_id, operation_id)
        )

    @app.post(
        "/v1/scopes/{scope_id}/operations/{operation_id}/cancel",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def cancel_operation(
        request: Request,
        scope_id: str,
        operation_id: str,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.cancel(request.state.bearer_secret, scope_id, operation_id)
        )

    @app.post(
        "/v1/scopes/{scope_id}/operations/{operation_id}/retry",
        response_model=OperationResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Operations"],
    )
    def retry_operation(
        request: Request,
        scope_id: str,
        operation_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.retry(
                request.state.bearer_secret,
                scope_id,
                operation_id,
                idempotency_key,
            )
        )

    @app.post(
        "/v1/scopes/{scope_id}/operations/{operation_id}/reconcile-attempt",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def reconcile_attempt(
        request: Request,
        scope_id: str,
        operation_id: str,
        body: ReconcileAttemptRequest,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.reconcile(
                request.state.bearer_secret,
                scope_id,
                operation_id,
                body,
            )
        )

    @app.get("/v1/scopes/{scope_id}/debug/traces/{trace_id}", tags=["Debug"])
    async def get_trace(scope_id: str, trace_id: str) -> None:
        _unavailable()

    @app.get("/v1/scopes/{scope_id}/profile/diff", tags=["Debug"])
    async def profile_diff(scope_id: str, from_revision: str, to_revision: str) -> None:
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/replays", status_code=202, tags=["Debug"])
    async def create_replay(scope_id: str, body: dict[str, Any]) -> None:
        _unavailable()

    @app.post("/v1/scopes/{scope_id}/exports", status_code=202, tags=["Exports"])
    async def create_export(scope_id: str, body: dict[str, Any]) -> None:
        _unavailable()


def _validate_limits(body, config: OAMConfig) -> None:
    try:
        body.validate_limits(config)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "InputLimitExceeded", "retryable": False},
        ) from exc


def _credential_call(call):
    try:
        return call()
    except CredentialAuthenticationFailed as exc:
        raise HTTPException(
            status_code=401, detail={"code": "AuthenticationFailed", "retryable": False}
        ) from exc
    except CredentialAdministrationDenied as exc:
        raise HTTPException(
            status_code=403, detail={"code": "AdministrationDenied", "retryable": False}
        ) from exc
    except CredentialPrincipalUnavailable as exc:
        raise HTTPException(
            status_code=404, detail={"code": "PrincipalUnavailable", "retryable": False}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "InvalidCredentialRequest", "retryable": False}
        ) from exc


def _bearer_secret(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, secret = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not secret.strip():
        return None
    return secret.strip()


def _requires_bearer(path: str) -> bool:
    return path == "/v1/scopes" or path.startswith(("/v1/scopes/", "/v1/credentials"))


def _require_runtime(runtime: APIRuntime | None) -> APIRuntime:
    if runtime is None:
        _unavailable()
    return runtime


def _operation_call(call) -> OperationResponse:
    try:
        return _operation_response(call())
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AuthenticationFailed", "retryable": False},
        ) from exc
    except (AuthorizationFailed, OperationNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "OperationNotFound", "retryable": False},
        ) from exc
    except (
        OperationNotCancellable,
        OperationNotRetryable,
        OperationIdempotencyConflict,
        RetryRequiresPlanning,
        AttemptOwnershipMismatch,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": type(exc).__name__, "retryable": False},
        ) from exc


def _operation_response(record: OperationRecord) -> OperationResponse:
    statuses = {
        StoredOperationStatus.ACCEPTED: OperationStatus.PENDING,
        StoredOperationStatus.RUNNING: OperationStatus.PROCESSING,
        StoredOperationStatus.SUCCEEDED: OperationStatus.SUCCEEDED,
        StoredOperationStatus.PARTIAL_FAILED: OperationStatus.PARTIAL,
        StoredOperationStatus.FAILED_TERMINAL: OperationStatus.FAILED,
        StoredOperationStatus.CANCELLED: OperationStatus.CANCELLED,
    }
    return OperationResponse(
        operation_id=record.operation_id,
        kind=record.kind,
        status=statuses[record.status],
        readiness=OperationReadiness(accepted=True),
    )
