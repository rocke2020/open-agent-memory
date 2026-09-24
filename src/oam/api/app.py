import os
from contextlib import asynccontextmanager
from importlib.resources import files
from ipaddress import ip_address
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
from oam.auth.modes import AssertedIdentity
from oam.config import ConfigurationError, OAMConfig, load_config
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
    "user",
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
    bind_host: str = "127.0.0.1",
) -> FastAPI:
    resolved = load_config(config_path)
    configured_root = os.environ.get(resolved.config.auth.root_key_env)
    if configured_root == "":
        raise ConfigurationError("configured root key cannot be empty")
    loopback = _is_loopback_bind(bind_host)
    if resolved.config.auth.mode == "dev" and not loopback:
        raise ConfigurationError("dev authentication requires a loopback bind")
    if resolved.config.auth.mode == "dev" and configured_root is not None:
        raise ConfigurationError("dev authentication cannot use a configured root key")
    owned_runtime: DatabaseRuntime | None = None
    if runtime is None:
        dsn = os.environ.get(resolved.config.database.api_dsn_env)
        if dsn:
            owned_runtime = DatabaseRuntime.from_dsn(resolved.config, dsn)
            runtime = owned_runtime
    if resolved.config.auth.mode == "trusted" and not loopback and owned_runtime is None:
        raise ConfigurationError("nonlocal trusted authentication requires an active root key")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if owned_runtime is not None:
                owned_runtime.open()
                if configured_root is not None and not owned_runtime.has_configured_root(
                    configured_root
                ):
                    raise ConfigurationError("configured root key is not active in this database")
                if resolved.config.auth.mode is None and configured_root is not None:
                    app.state.auth_mode = "api_key"
                elif resolved.config.auth.mode is None:
                    app.state.auth_mode = owned_runtime.choose_auto_auth_mode()
                if app.state.auth_mode == "dev" and not loopback:
                    raise ConfigurationError("dev authentication requires a loopback bind")
                if (
                    app.state.auth_mode == "api_key"
                    or (app.state.auth_mode == "trusted" and not loopback)
                ) and not owned_runtime.has_active_root():
                    raise ConfigurationError("authentication mode requires an active root key")
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
    app.state.auth_mode = resolved.config.auth.mode or "api_key"

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        bearer_secret = _bearer_secret(request.headers.get("authorization"))
        mode = app.state.auth_mode
        needs_key = _requires_bearer(request.url.path) and (
            mode == "api_key"
            or _is_admin_route(request.url.path)
            or (mode == "trusted" and not loopback)
        )
        if mode == "dev" and _is_admin_route(request.url.path):
            response = JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": {"code": "CapabilityUnavailable", "retryable": False}},
            )
        elif needs_key and bearer_secret is None:
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
            actor: str | AssertedIdentity | None = bearer_secret
            if _requires_bearer(request.url.path) and not _is_admin_route(request.url.path):
                if mode == "trusted":
                    tenant = request.headers.get("x-oam-tenant", "").strip()
                    user = request.headers.get("x-oam-user", "").strip()
                    raw_role = request.headers.get("x-oam-role")
                    asserted_role = raw_role.strip().lower() if raw_role is not None else "user"
                    if not tenant or not user:
                        response = JSONResponse(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content={
                                "detail": {"code": "AuthenticationRequired", "retryable": False}
                            },
                        )
                    elif asserted_role not in ("user", "admin"):
                        response = JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={"detail": {"code": "InvalidIdentityRole", "retryable": False}},
                        )
                    elif raw_role is not None and bearer_secret is None:
                        response = JSONResponse(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content={
                                "detail": {"code": "AuthenticationRequired", "retryable": False}
                            },
                        )
                    else:
                        actor = AssertedIdentity(
                            "trusted", tenant, user, bearer_secret, asserted_role
                        )
                        request.state.actor = actor
                        response = await call_next(request)
                elif mode == "dev":
                    actor = AssertedIdentity(
                        "dev",
                        request.headers.get("x-oam-tenant", "default").strip(),
                        request.headers.get("x-oam-user", "default").strip(),
                    )
                    request.state.actor = actor
                    response = await call_next(request)
                else:
                    request.state.actor = actor
                    response = await call_next(request)
            else:
                request.state.actor = actor
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
        limits["auth"] = config.auth.model_dump(mode="json", exclude={"mode", "root_key_env"})
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
            lambda: _require_runtime(runtime).create_user(request.state.actor, body.user_id)
        )

    @app.put(
        "/v1/users/{user_id}/grants/{principal_id}",
        tags=["Administration"],
    )
    async def replace_user_grant(
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
                request.state.actor, limit=selected_limit, after=after
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
            lambda: _require_runtime(runtime).issue_credential(request.state.actor, body)
        )

    @app.delete(
        "/v1/credentials/{credential_id}",
        tags=["Administration"],
        response_model=CredentialRevokedResponse,
    )
    def revoke_credential(request: Request, credential_id: str) -> CredentialRevokedResponse:
        return _credential_call(
            lambda: _require_runtime(runtime).revoke_credential(request.state.actor, credential_id)
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
                lambda: _require_runtime(runtime).authenticate(request.state.actor)
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
                    request.state.actor, body, idempotency_key, **upload
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
            lambda: _require_runtime(runtime).search(request.state.actor, body.user_id, body)
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
            lambda: active_runtime.get(request.state.actor, user_id, operation_id)
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
            lambda: active_runtime.cancel(request.state.actor, user_id, operation_id)
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
                request.state.actor,
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
                request.state.actor,
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


def _is_admin_route(path: str) -> bool:
    if path == "/v1/users" or path == "/v1/credentials" or path.startswith("/v1/credentials/"):
        return True
    parts = path.split("/")
    return len(parts) == 6 and parts[1:3] == ["v1", "users"] and parts[4] == "grants"


def _is_loopback_bind(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


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
