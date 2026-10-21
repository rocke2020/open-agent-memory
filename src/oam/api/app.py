import asyncio
import json
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from ipaddress import ip_address
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import psycopg
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

from oam.api.admission import RequestAdmissionMiddleware
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
from oam.capacity import CapacityUnavailable
from oam.config import ConfigurationError, OAMConfig, load_config
from oam.contracts.credentials import (
    CredentialIssuedResponse,
    CredentialIssueRequest,
    CredentialListResponse,
    CredentialRevokedResponse,
)
from oam.contracts.debug import ReplayRequest
from oam.contracts.memory import (
    MAX_MEMORY_OFFSET,
    MAX_MEMORY_PAGE_SIZE,
    MEMORY_KIND_BY_TYPE,
    MemoryType,
)
from oam.contracts.operations import (
    OperationReadiness,
    OperationResponse,
    OperationStatus,
    ReconcileAttemptRequest,
)
from oam.contracts.profile import (
    ProfileDataRequest,
    ProfileRefreshRequest,
)
from oam.contracts.retain import (
    RETAIN_JSON,
    RecallRequest,
    RetainFileDocument,
    RetainInteraction,
    RetainTextDocument,
    RetainURLDocument,
    UserRequest,
)
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.db.inspection import MemoryNotFound, SourcePolicyChanged
from oam.db.queue_admission import QueueAdmissionFull
from oam.db.sources import IdempotencyConflict, SourceNotFound, SourceVersionConflict
from oam.debug.traces import TraceNotFound
from oam.exports.repository import ExportNotFound, ExportPolicyChanged
from oam.governance.removal import RemovalNotFound
from oam.indexing.identity import index_generation
from oam.operations.repository import (
    AttemptOwnershipMismatch,
    OperationIdempotencyConflict,
    OperationNotCancellable,
    OperationNotFound,
    OperationNotRetryable,
    OperationRecord,
    RemovalRecoveryConflict,
    RetryRequiresPlanning,
    RetryRequiresReconciliation,
)
from oam.operations.state import OperationStatus as StoredOperationStatus
from oam.profiles.service import ProfileInputsNotReady, ProfileRefreshUnresolved
from oam.resources.url_fetch import ResourceFetchUnavailable
from oam.retrieval.service import RecallUnavailable
from oam.startup import validate_model_environment
from oam.workers.runner import WorkerRunner

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
            if resolved.config.worker.embedded_enabled:
                app.state.worker = WorkerRunner.from_environment(resolved.config)
                app.state.worker.start()
            yield
        finally:
            if app.state.worker is not None:
                await asyncio.to_thread(app.state.worker.shutdown)
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
    app.state.worker = None
    app.add_middleware(RequestAdmissionMiddleware, runtime=runtime)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
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
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["OAM-Contract-Version"] = resolved.config.contract_version
        return response

    @app.get("/health/live", tags=["Health"])
    async def liveness() -> dict[str, str]:
        return {"status": "alive", "contract_version": resolved.config.contract_version}

    @app.get("/health/ready", tags=["Health"])
    async def readiness(response: Response) -> dict[str, Any]:
        missing = sorted(resolved.config.required_unbound_roles())
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        worker = app.state.worker
        worker_running = worker is not None and worker.healthy
        payload = {
            "ready": False,
            "database": "not_configured",
            "schema": "unchecked",
            "missing_required_roles": missing,
            "embedded_worker_running": (
                worker_running if resolved.config.worker.embedded_enabled else None
            ),
            "reason": (
                "embedded_worker_unavailable"
                if resolved.config.worker.embedded_enabled and not worker_running
                else "core_only"
            ),
        }
        probe = getattr(runtime, "readiness", None)
        if probe is not None:
            payload.update(await run_in_threadpool(probe))
            if payload["database"] != "connected" or payload["schema"] != "current":
                return payload
        capacity = getattr(runtime, "capacity", None)
        if capacity is not None:
            try:
                state = await run_in_threadpool(capacity.status)
            except (psycopg.Error, CapacityUnavailable):
                payload["reason"] = "capacity_unavailable"
                return payload
            else:
                payload["capacity"] = state
                if (
                    state["llm_unknown"] >= resolved.config.provider.llm_concurrency
                    or state["embedding_unknown"] >= resolved.config.provider.embedding_concurrency
                ):
                    payload["reason"] = "model_capacity_blocked"
                    return payload
        if probe is None:
            return payload
        try:
            validate_model_environment(resolved.config, os.environ)
        except ConfigurationError:
            payload["reason"] = "model_configuration_invalid"
            return payload
        if resolved.config.worker.embedded_enabled and not worker_running:
            payload["reason"] = "embedded_worker_unavailable"
            return payload
        if resolved.config.worker.embedded_enabled and not await run_in_threadpool(
            worker.databases_ready
        ):
            payload["reason"] = "worker_database_unavailable"
            return payload
        payload.pop("reason", None)
        payload["ready"] = True
        response.status_code = status.HTTP_200_OK
        return payload

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
            "indexing_concurrency": config.database.indexing_concurrency,
        }
        return {
            "contract_version": config.contract_version,
            "config_sha256": resolved.sha256,
            "index_generation_id": index_generation(config, os.environ),
            "limits": limits,
            "export_format_versions": [1],
            "features": {
                "python_sdk": False,
                "fast_recall": True,
                "normal_recall": True,
                "batch_recall": True,
                "planner": config.models.binding_for("planner") is not None,
                "profiles": runtime is not None and config.profile.enabled,
                "inspector": True,
                "source_lifecycle": True,
                "recorded_debug": True,
                "portable_export": True,
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

    @app.get("/ui/oam-logo.svg", include_in_schema=False)
    async def inspector_logo() -> Response:
        return Response(
            files("oam.web").joinpath("oam-logo.svg").read_text(encoding="utf-8"),
            media_type="image/svg+xml",
            headers={"Content-Security-Policy": CSP},
        )

    @app.get("/ui/app.js", include_in_schema=False)
    @app.get("/ui/locales.js", include_in_schema=False)
    async def inspector_script(request: Request) -> Response:
        name = "locales.js" if request.url.path.endswith("/locales.js") else "app.js"
        return Response(
            files("oam.web").joinpath(name).read_text(encoding="utf-8"),
            media_type="text/javascript",
            headers={"Content-Security-Policy": CSP},
        )

    _register_reserved_routes(app, runtime, resolved.config)
    # These endpoints dispatch their own bounded bodies; publish their tagged
    # schemas explicitly for OpenAPI clients.
    definitions = RETAIN_JSON.json_schema(ref_template="#/components/schemas/{model}")["$defs"]
    schemas = app.openapi().setdefault("components", {}).setdefault("schemas", {})
    schemas.update(definitions)
    schemas["ProfileDataRequest"] = ProfileDataRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schemas["EvidenceTextResourceRequest"] = _resource_evidence_schema(RetainTextDocument)
    schemas["EvidenceURLResourceRequest"] = _resource_evidence_schema(RetainURLDocument)
    return app


def _resource_evidence_schema(model) -> dict:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    schema["properties"].pop("user_id")
    schema["required"] = [name for name in schema["required"] if name != "user_id"]
    return schema


def _bounded_request(request: Request, limit: int) -> tuple[Request, Callable[[], bool]]:
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
            raise MultiPartException("request exceeds byte limit")
        return message

    return Request(request.scope, receive=limited_receive), lambda: consumed > limit


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
                            for key, value in RetainInteraction.model_json_schema(
                                ref_template="#/components/schemas/{model}"
                            ).items()
                            if key != "$defs"
                        }
                    }
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
        bounded, over_limit = _bounded_request(request, config.api.max_request_bytes)
        try:
            if bounded.headers.get("content-type", "").startswith("multipart/form-data"):
                raise ValueError("interaction retain requires JSON")
            else:
                body = RetainInteraction.model_validate_json(await bounded.body())
                _validate_limits(body, config)
        except (MultiPartException, StarletteHTTPException) as exc:
            if over_limit():
                raise HTTPException(413, detail={"code": "RequestTooLarge"}) from exc
            raise
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "InvalidRetainRequest"}) from exc
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).retain(request.state.actor, body, idempotency_key)
            )
        )

    async def profile_data_body(request: Request) -> ProfileDataRequest:
        await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).authenticate(request.state.actor)
            )
        )
        bounded, over_limit = _bounded_request(request, config.api.max_request_bytes)
        try:
            return ProfileDataRequest.model_validate_json(await bounded.body())
        except (MultiPartException, StarletteHTTPException) as exc:
            if over_limit():
                raise HTTPException(413, detail={"code": "RequestTooLarge"}) from exc
            raise
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "InvalidProfileDataRequest"}) from exc

    @app.post(
        "/v1/users/{user_id}/profile-data",
        status_code=202,
        tags=["Profile Data"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ProfileDataRequest"}
                    }
                },
            }
        },
    )
    async def create_profile_data(
        request: Request,
        user_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    ) -> dict:
        body = await profile_data_body(request)
        if "if-match" in request.headers:
            raise HTTPException(422, detail={"code": "InvalidProfileDataRequest"})
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).retain_profile_data(
                    request.state.actor, user_id, body, idempotency_key, expected_version=None
                )
            )
        )

    @app.get("/v1/users/{user_id}/profile-data", tags=["Profile Data"])
    def list_profile_data(request: Request, user_id: str, after: str | None = None) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).list_profile_data(
                request.state.actor, user_id, after=after
            )
        )

    @app.get("/v1/users/{user_id}/profile-data/{profile_data_id}", tags=["Profile Data"])
    def get_profile_data(request: Request, user_id: str, profile_data_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).get_profile_data(
                request.state.actor, user_id, profile_data_id
            )
        )

    @app.get("/v1/users/{user_id}/profile-data/{profile_data_id}/original", tags=["Profile Data"])
    def get_profile_data_original(request: Request, user_id: str, profile_data_id: str) -> Response:
        content, media_type = _content_call(
            lambda: _require_runtime(runtime).get_profile_data_original(
                request.state.actor, user_id, profile_data_id
            )
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": 'attachment; filename="original"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put(
        "/v1/users/{user_id}/profile-data/{profile_data_id}",
        status_code=202,
        tags=["Profile Data"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ProfileDataRequest"}
                    }
                },
            }
        },
    )
    async def revise_profile_data(
        request: Request,
        user_id: str,
        profile_data_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        if_match: int = Header(alias="If-Match", ge=1),
    ) -> dict:
        await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).preflight_revision(
                    request.state.actor,
                    user_id,
                    profile_data_id,
                    if_match,
                    idempotency_key,
                    input_role="profile_resource",
                )
            )
        )
        body = await profile_data_body(request)
        if body.profile_data_id != profile_data_id:
            raise HTTPException(422, detail={"code": "InvalidProfileDataRevision"})
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).retain_profile_data(
                    request.state.actor, user_id, body, idempotency_key, expected_version=if_match
                )
            )
        )

    @app.delete(
        "/v1/users/{user_id}/profile-data/{profile_data_id}", status_code=202, tags=["Profile Data"]
    )
    def delete_profile_data(request: Request, user_id: str, profile_data_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).delete_source(
                request.state.actor,
                user_id,
                profile_data_id,
                resource_only=True,
                expected_input_role="profile_resource",
            )
        )

    @app.post(
        "/v1/users/{user_id}/resources",
        status_code=202,
        tags=["Resources"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                {"$ref": "#/components/schemas/EvidenceTextResourceRequest"},
                                {"$ref": "#/components/schemas/EvidenceURLResourceRequest"},
                            ]
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
                                        "JSON with input_type=document, "
                                        "document_id and update_mode=replace"
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
    async def create_resource(
        request: Request,
        user_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        if_match: int | None = Header(default=None, alias="If-Match", ge=1),
    ) -> dict:
        await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).authenticate(request.state.actor)
            )
        )
        bounded, over_limit = _bounded_request(request, config.api.max_request_bytes)
        upload = {}
        try:
            if bounded.headers.get("content-type", "").startswith("multipart/form-data"):
                async with bounded.form(max_files=1, max_fields=1) as form:
                    if if_match is not None:
                        raise ValueError("If-Match is unavailable for Evidence admission")
                    metadata = form.get("metadata")
                    if not isinstance(metadata, str):
                        raise ValueError("multipart Resource metadata must be JSON text")
                    wire = json.loads(metadata)
                    if not isinstance(wire, dict):
                        raise ValueError("Resource metadata must be an object")
                    if "user_id" in wire:
                        raise ValueError("Resource user_id belongs in the path")
                    body = RetainFileDocument.model_validate({**wire, "user_id": user_id})
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
                wire = json.loads(await bounded.body())
                if not isinstance(wire, dict) or "user_id" in wire:
                    raise ValueError("Resource body must be an object without user_id")
                if if_match is not None:
                    raise ValueError("If-Match is unavailable for Resource admission")
                body = RETAIN_JSON.validate_python({**wire, "user_id": user_id})
                if not isinstance(body, (RetainTextDocument, RetainURLDocument)):
                    raise ValueError("Resource input_type must be document or document_url")
                _validate_limits(body, config)
        except (MultiPartException, StarletteHTTPException) as exc:
            if over_limit():
                raise HTTPException(413, detail={"code": "RequestTooLarge"}) from exc
            raise
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "InvalidResourceRequest"}) from exc
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).retain(
                    request.state.actor, body, idempotency_key, **upload
                )
            )
        )

    @app.get("/v1/users/{user_id}/resources", tags=["Resources"])
    def list_resources(request: Request, user_id: str, after: str | None = None) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).list_resources(
                request.state.actor, user_id, after=after
            )
        )

    @app.get("/v1/users/{user_id}/resources/{resource_id}", tags=["Resources"])
    def get_resource(request: Request, user_id: str, resource_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).get_resource(
                request.state.actor, user_id, resource_id
            )
        )

    @app.get("/v1/users/{user_id}/resources/{resource_id}/original", tags=["Resources"])
    def get_resource_original(request: Request, user_id: str, resource_id: str) -> Response:
        content, media_type = _content_call(
            lambda: _require_runtime(runtime).get_resource_original(
                request.state.actor, user_id, resource_id
            )
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": 'attachment; filename="original"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put("/v1/users/{user_id}/resources/{resource_id}", status_code=202, tags=["Resources"])
    async def revise_resource(
        request: Request,
        user_id: str,
        resource_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        if_match: int = Header(alias="If-Match", ge=1),
    ) -> dict:
        await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).preflight_revision(
                    request.state.actor, user_id, resource_id, if_match, idempotency_key
                )
            )
        )
        body, upload = await _parse_retain_request(request, config)
        if not isinstance(body, (RetainTextDocument, RetainURLDocument, RetainFileDocument)):
            raise HTTPException(422, detail={"code": "InvalidResourceRevision"})
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).revise_resource(
                    request.state.actor,
                    user_id,
                    resource_id,
                    body,
                    idempotency_key,
                    if_match,
                    **upload,
                )
            )
        )

    @app.delete("/v1/users/{user_id}/resources/{resource_id}", status_code=202, tags=["Resources"])
    def delete_resource(request: Request, user_id: str, resource_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).delete_source(
                request.state.actor,
                user_id,
                resource_id,
                resource_only=True,
                expected_input_role="evidence_resource",
            )
        )

    @app.delete("/v1/users/{user_id}/evidence/{source_id}", status_code=202, tags=["Retain"])
    def delete_evidence(request: Request, user_id: str, source_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).delete_source(
                request.state.actor, user_id, source_id, resource_only=False
            )
        )

    @app.get("/v1/users/{user_id}/evidence/{source_id}", tags=["Retain"])
    def get_evidence(request: Request, user_id: str, source_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).get_evidence(request.state.actor, user_id, source_id)
        )

    @app.get("/v1/users/{user_id}/memories", tags=["Memory"])
    def list_memories(
        request: Request,
        user_id: str,
        after: str | None = None,
        memory_type: MemoryType | None = None,
        limit: int | None = Query(default=None, ge=1, le=MAX_MEMORY_PAGE_SIZE),
        offset: int = Query(default=0, ge=0, le=MAX_MEMORY_OFFSET),
    ) -> dict:
        if after and offset:
            raise HTTPException(422, detail={"code": "InvalidRequest"})
        return _memory_read_call(
            request,
            lambda: _require_runtime(runtime).list_memories(
                request.state.actor,
                user_id,
                after=after,
                memory_type=memory_type,
                limit=limit,
                offset=offset,
            ),
            operation="list",
            memory_type=memory_type or "all",
        )

    @app.get("/v1/users/{user_id}/memories/{memory_id}", tags=["Memory"])
    def get_memory(
        request: Request,
        user_id: str,
        memory_id: str,
        memory_type: MemoryType,
    ) -> dict:
        return _memory_read_call(
            request,
            lambda: _require_runtime(runtime).get_memory(
                request.state.actor, user_id, memory_id, kind=MEMORY_KIND_BY_TYPE[memory_type]
            ),
            operation="detail",
            memory_type=memory_type,
        )

    @app.delete("/v1/users/{user_id}/memories/{memory_id}", status_code=202, tags=["Memory"])
    def delete_memory(request: Request, user_id: str, memory_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).delete_memory(request.state.actor, user_id, memory_id)
        )

    @app.delete("/v1/users/{user_id}/insights/{insight_id}", status_code=202, tags=["Memory"])
    def delete_insight(request: Request, user_id: str, insight_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).delete_insight(
                request.state.actor, user_id, insight_id
            )
        )

    @app.post("/v1/recall", tags=["Recall"])
    def search(request: Request, body: RecallRequest) -> dict:
        _validate_limits(body, config)
        return _content_call(
            lambda: _require_runtime(runtime).search(request.state.actor, body.user_id, body)
        )

    @app.post("/v1/users/{user_id}/search-many", tags=["Recall"])
    async def search_many(request: Request, user_id: str, body: SearchManyRequest) -> dict:
        _validate_limits(body, config)
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).search_many(request.state.actor, user_id, body)
            )
        )

    @app.post("/v1/users/{user_id}/decompose-and-search", tags=["Recall"])
    async def decompose_and_search(request: Request, user_id: str, body: SearchRequest) -> dict:
        _validate_limits(body, config)
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).decompose_and_search(
                    request.state.actor, user_id, body
                )
            )
        )

    @app.get("/v1/users/{user_id}/profile", tags=["User Profile"])
    async def get_profile(
        request: Request,
        user_id: str,
        max_tokens: int | None = Query(default=None, ge=0),
    ) -> dict:
        budget = max_tokens if max_tokens is not None else config.profile.max_response_tokens
        if budget > config.profile.max_response_tokens:
            raise HTTPException(422, detail={"code": "InputLimitExceeded"})
        return await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).get_profile(request.state.actor, user_id, budget)
            )
        )

    @app.post("/v1/users/{user_id}/profile/refresh", tags=["User Profile"])
    async def refresh_profile(
        request: Request, user_id: str, body: ProfileRefreshRequest, response: Response
    ) -> dict:
        outcome = await run_in_threadpool(
            lambda: _content_call(
                lambda: _require_runtime(runtime).refresh_profile(
                    request.state.actor, user_id, body.instruction, body.reset_instruction
                )
            )
        )
        if outcome["status"] == "accepted":
            response.status_code = status.HTTP_202_ACCEPTED
        return outcome

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
        "/v1/users/{user_id}/operations/{operation_id}/recover",
        response_model=OperationResponse,
        tags=["Operations"],
    )
    def recover_removal_operation(
        request: Request,
        user_id: str,
        operation_id: str,
    ) -> OperationResponse:
        active_runtime = _require_runtime(runtime)
        return _operation_call(
            lambda: active_runtime.recover(request.state.actor, user_id, operation_id)
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
    def get_trace(request: Request, user_id: str, trace_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).get_trace(request.state.actor, user_id, trace_id)
        )

    @app.get("/v1/users/{user_id}/debug/diff", tags=["Debug"])
    def trace_diff(request: Request, user_id: str, before: str, after: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).diff_traces(
                request.state.actor, user_id, before, after
            )
        )

    @app.get("/v1/users/{user_id}/profile/diff", tags=["Debug"])
    async def profile_diff(user_id: str, from_revision: str, to_revision: str) -> None:
        _unavailable()

    @app.post("/v1/users/{user_id}/replays", tags=["Debug"])
    def create_replay(request: Request, user_id: str, body: ReplayRequest) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).replay(request.state.actor, user_id, body)
        )

    @app.post("/v1/users/{user_id}/exports", status_code=202, tags=["Exports"])
    def create_export(request: Request, user_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).create_export(request.state.actor, user_id)
        )

    @app.get("/v1/users/{user_id}/exports/{export_id}", tags=["Exports"])
    def get_export(request: Request, user_id: str, export_id: str) -> dict:
        return _content_call(
            lambda: _require_runtime(runtime).get_export(request.state.actor, user_id, export_id)
        )

    @app.get("/v1/users/{user_id}/exports/{export_id}/records/{index}", tags=["Exports"])
    def get_export_part(request: Request, user_id: str, export_id: str, index: int) -> Response:
        content = _content_call(
            lambda: _require_runtime(runtime).get_export_part(
                request.state.actor, user_id, export_id, index
            )
        )
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="records-{index:06d}.jsonl"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/users/{user_id}/exports/{export_id}/originals/{index}", tags=["Exports"])
    def get_export_original(request: Request, user_id: str, export_id: str, index: int) -> Response:
        content, media_type = _content_call(
            lambda: _require_runtime(runtime).get_export_original(
                request.state.actor, user_id, export_id, index
            )
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="original-{index:06d}.bin"',
                "X-Content-Type-Options": "nosniff",
            },
        )


async def _parse_retain_request(request: Request, config: OAMConfig):
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
    return body, upload


def _validate_limits(body, config: OAMConfig) -> None:
    try:
        body.validate_limits(config)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "InputLimitExceeded", "retryable": False},
        ) from exc


def _memory_read_call(
    request: Request, call: Callable[[], dict], *, operation: str, memory_type: str
):
    began = monotonic()
    result: dict = {}
    status_code, reason = 500, "InternalError"
    try:
        result = _content_call(call)
        status_code, reason = 200, "none"
        return result
    except HTTPException as exc:
        status_code = exc.status_code
        reason = exc.detail.get("code", "RequestFailed")
        raise
    finally:
        returned = len(result["items"]) if "items" in result else int(status_code == 200)
        logging.getLogger("uvicorn.error").info(
            "memory_read request_id=%s operation=%s memory_type=%s status=%s "
            "limit=%s offset=%s pagination=%s returned=%s total=%s policy_epoch=%s "
            "reason=%s duration_ms=%.2f",
            request.state.request_id,
            operation,
            memory_type,
            status_code,
            result.get("limit"),
            result.get("offset"),
            "cursor" if request.query_params.get("after") else "offset",
            returned,
            result.get("total"),
            result.get("policy_epoch"),
            reason,
            (monotonic() - began) * 1000,
        )


def _content_call(call):
    try:
        return call()
    except QueueAdmissionFull as exc:
        raise _queue_capacity_error() from exc
    except AuthenticationFailed as exc:
        raise HTTPException(401, detail={"code": "AuthenticationFailed"}) from exc
    except PermissionError as exc:
        raise HTTPException(404, detail={"code": "UserUnavailable"}) from exc
    except NotImplementedError as exc:
        raise HTTPException(503, detail={"code": "CapabilityUnavailable"}) from exc
    except RecallUnavailable as exc:
        raise HTTPException(503, detail={"code": "RecallUnavailable"}) from exc
    except SourcePolicyChanged as exc:
        raise HTTPException(503, detail={"code": "SourcePolicyChanged"}) from exc
    except ResourceFetchUnavailable as exc:
        raise HTTPException(502, detail={"code": "ResourceFetchUnavailable"}) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail={"code": "RecallDeadlineExceeded"}) from exc
    except (IdempotencyConflict, SourceVersionConflict) as exc:
        raise HTTPException(409, detail={"code": type(exc).__name__}) from exc
    except SourceNotFound as exc:
        raise HTTPException(404, detail={"code": "SourceUnavailable"}) from exc
    except (RemovalNotFound, MemoryNotFound) as exc:
        raise HTTPException(404, detail={"code": "MemoryUnavailable"}) from exc
    except TraceNotFound as exc:
        raise HTTPException(404, detail={"code": "TraceUnavailable"}) from exc
    except ExportNotFound as exc:
        raise HTTPException(404, detail={"code": "ExportUnavailable"}) from exc
    except ExportPolicyChanged as exc:
        raise HTTPException(409, detail={"code": "ExportPolicyChanged"}) from exc
    except ProfileRefreshUnresolved as exc:
        raise HTTPException(409, detail={"code": "ProfileRefreshUnresolved"}) from exc
    except ProfileInputsNotReady as exc:
        raise HTTPException(409, detail={"code": "ProfileInputsNotReady"}) from exc
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
    except QueueAdmissionFull as exc:
        raise _queue_capacity_error() from exc
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
        RetryRequiresReconciliation,
        RemovalRecoveryConflict,
        AttemptOwnershipMismatch,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": type(exc).__name__, "retryable": False},
        ) from exc


def _queue_capacity_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": "QueueAdmissionFull", "retryable": True},
        headers={"Retry-After": "1"},
    )


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
                    "links_ready",
                    "temporal_links_ready",
                    "insights_ready",
                    "profile_current",
                )
            },
        ),
        stage_state=record.stage_state,
        usage=record.stage_state.get("usage", {}),
        embedding_usage=record.stage_state.get("embedding_usage", {}),
    )
