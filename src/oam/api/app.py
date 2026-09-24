import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

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
from oam.contracts.operations import (
    OperationReadiness,
    OperationResponse,
    OperationStatus,
    ReconcileAttemptRequest,
)
from oam.contracts.profile import ProfileRefreshRequest
from oam.contracts.retain import RETAIN_JSON, RecallRequest, RetainFileDocument, UserRequest
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.db.sources import IdempotencyConflict, SourceVersionConflict
from oam.indexing.identity import index_generation
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
from oam.retrieval.service import RecallUnavailable

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
            "reason": "core_only",
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
            "index_generation_id": index_generation(config, os.environ),
            "limits": limits,
            "features": {
                "python_sdk": False,
                "fast_recall": True,
                "mid_recall": True,
                "deep_recall": True,
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
    # The retain endpoint dispatches JSON and multipart itself; register the
    # JSON union's definitions for OpenAPI clients as well as runtime validation.
    definitions = RETAIN_JSON.json_schema(ref_template="#/components/schemas/{model}")["$defs"]
    app.openapi().setdefault("components", {}).setdefault("schemas", {}).update(definitions)
    return app


def _register_reserved_routes(
    app: FastAPI,
    runtime: APIRuntime | None,
    config: OAMConfig,
) -> None:
    @app.post("/v1/users", status_code=201, tags=["Administration"])
    def create_user(request: Request, body: UserRequest) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).create_user(request.state.bearer_secret, body.user_id)
        )

    @app.put(
        "/v1/users/{user_id}/grants/{principal_id}",
        tags=["Administration"],
    )
    async def replace_scope_grant(
        user_id: str,
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

    @app.post(
        "/v1/retain",
        status_code=202,
        tags=["Retain"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            key: value
                            for key, value in RETAIN_JSON.json_schema(
                                ref_template="#/components/schemas/{model}"
                            ).items()
                            if key != "$defs"
                        }
                    },
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["metadata", "original"],
                            "properties": {
                                "metadata": {
                                    "type": "string",
                                    "description": (
                                        "JSON: user_id, input_type=document, "
                                        "document_id, update_mode=replace."
                                    ),
                                },
                                "original": {"type": "string", "format": "binary"},
                            },
                        }
                    },
                },
            }
        },
    )
    async def retain(
        request: Request,
        idempotency_key: str = Header(min_length=1),
    ) -> dict:
        await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).authenticate(request.state.bearer_secret)
            )
        )
        limit = config.api.max_request_bytes
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(400, detail={"code": "InvalidContentLength"}) from exc
            if length > limit:
                raise HTTPException(413, detail={"code": "RequestTooLarge"})
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await request.receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                # The multipart parser closes its partial upload files on this
                # exception; JSON body reads propagate it to the same 413 below.
                raise MultiPartException("retain request exceeds byte limit")
            return message

        bounded = Request(request.scope, receive=limited_receive)
        upload = {}
        try:
            if bounded.headers.get("content-type", "").startswith("multipart/form-data"):
                async with bounded.form(max_files=1, max_fields=1) as form:
                    body = RetainFileDocument.model_validate_json(form.get("metadata", ""))
                    original = form.get("original")
                    if original is None or not hasattr(original, "read"):
                        raise ValueError("original file is required")
                    content = await original.read(config.resource.max_bytes + 1)
                    if not content or len(content) > config.resource.max_bytes:
                        raise ValueError("original exceeds byte limit or is empty")
                    upload = {
                        "content": content,
                        "media_type": original.content_type,
                        "filename": original.filename,
                    }
            else:
                body = RETAIN_JSON.validate_json(await bounded.body())
                _validate_limits(body, config)
        except (MultiPartException, StarletteHTTPException) as exc:
            if consumed > limit:
                raise HTTPException(413, detail={"code": "RequestTooLarge"}) from exc
            raise
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "InvalidRetainRequest"}) from exc
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).retain(
                    request.state.bearer_secret, body, idempotency_key, **upload
                )
            )
        )

    @app.get("/v1/users/{user_id}/resources", tags=["Resources"])
    async def list_resources(user_id: str) -> None:
        _unavailable()

    @app.get("/v1/users/{user_id}/memories", tags=["Memory"])
    async def list_memories(user_id: str) -> None:
        _unavailable()

    @app.post("/v1/recall", tags=["Recall"])
    def search(request: Request, body: RecallRequest) -> dict:
        _validate_limits(body, config)
        return _content_call(
            lambda: _require_runtime(runtime).search(
                request.state.bearer_secret, body.user_id, body
            )
        )

    @app.post("/v1/users/{user_id}/search-many", tags=["Recall"])
    async def search_many(user_id: str, body: SearchManyRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.post("/v1/users/{user_id}/decompose-and-search", tags=["Recall"])
    async def decompose_and_search(user_id: str, body: SearchRequest) -> None:
        _validate_limits(body, config)
        _unavailable()

    @app.get("/v1/users/{user_id}/profile", tags=["Profiles"])
    async def get_profile(user_id: str) -> None:
        _unavailable()

    @app.post("/v1/users/{user_id}/profile/refresh", tags=["Profiles"])
    async def refresh_profile(user_id: str, body: ProfileRefreshRequest) -> None:
        _unavailable()

    @app.get(
        "/v1/users/{user_id}/operations/{operation_id}",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def get_operation(
        request: Request,
        user_id: str,
        operation_id: str,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.get(request.state.bearer_secret, user_id, operation_id)
        )

    @app.post(
        "/v1/users/{user_id}/operations/{operation_id}/cancel",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def cancel_operation(
        request: Request,
        user_id: str,
        operation_id: str,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.cancel(request.state.bearer_secret, user_id, operation_id)
        )

    @app.post(
        "/v1/users/{user_id}/operations/{operation_id}/retry",
        response_model=OperationResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Operations"],
    )
    def retry_operation(
        request: Request,
        user_id: str,
        operation_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.retry(
                request.state.bearer_secret,
                user_id,
                operation_id,
                idempotency_key,
            )
        )

    @app.post(
        "/v1/users/{user_id}/operations/{operation_id}/reconcile-attempt",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def reconcile_attempt(
        request: Request,
        user_id: str,
        operation_id: str,
        body: ReconcileAttemptRequest,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.reconcile(
                request.state.bearer_secret,
                user_id,
                operation_id,
                body,
            )
        )

    @app.get("/v1/users/{user_id}/debug/traces/{trace_id}", tags=["Debug"])
    async def get_trace(user_id: str, trace_id: str) -> None:
        _unavailable()

    @app.get("/v1/users/{user_id}/profile/diff", tags=["Debug"])
    async def profile_diff(user_id: str, from_revision: str, to_revision: str) -> None:
        _unavailable()

    @app.post("/v1/users/{user_id}/replays", status_code=202, tags=["Debug"])
    async def create_replay(user_id: str, body: dict[str, Any]) -> None:
        _unavailable()

    @app.post("/v1/users/{user_id}/exports", status_code=202, tags=["Exports"])
    async def create_export(user_id: str, body: dict[str, Any]) -> None:
        _unavailable()


def _validate_limits(body, config: OAMConfig) -> None:
    try:
        body.validate_limits(config)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "InputLimitExceeded", "retryable": False},
        ) from exc


def _content_call(call):
    try:
        return call()
    except AuthenticationFailed as exc:
        raise HTTPException(401, detail={"code": "AuthenticationFailed"}) from exc
    except PermissionError as exc:
        raise HTTPException(404, detail={"code": "UserUnavailable"}) from exc
    except NotImplementedError as exc:
        raise HTTPException(503, detail={"code": "CapabilityUnavailable"}) from exc
    except RecallUnavailable as exc:
        raise HTTPException(503, detail={"code": "RecallUnavailable"}) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail={"code": "RecallDeadlineExceeded"}) from exc
    except (IdempotencyConflict, SourceVersionConflict) as exc:
        raise HTTPException(409, detail={"code": type(exc).__name__}) from exc
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "InvalidRequest", "message": str(exc)}) from exc


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
    return path.startswith("/v1/") and path != "/v1/capabilities"


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
        source_id=record.source_id,
        revision=record.source_version,
        kind=record.kind,
        status=statuses[record.status],
        readiness=OperationReadiness(
            accepted=True,
            **{
                name: bool(record.stage_state.get(name, False))
                for name in (
                    "source_searchable",
                    "memory_ready",
                    "insights_ready",
                    "profile_current",
                )
            },
        ),
        stage_state=record.stage_state,
        usage=record.stage_state.get("usage", {}),
        embedding_usage=record.stage_state.get("embedding_usage", {}),
    )
