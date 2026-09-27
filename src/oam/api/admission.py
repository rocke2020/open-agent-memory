"""Bound business HTTP execution before receiving request bodies."""

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from oam.capacity import CapacityUnavailable

RETRY_AFTER_SECONDS = 1
SEARCH_ACTIONS = {"search-many", "decompose-and-search"}


class RequestAdmissionMiddleware:
    def __init__(self, app, *, runtime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope, receive, send):
        capacity = getattr(self.runtime, "capacity", None)
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/v1/") or capacity is None:
            await self.app(scope, receive, send)
            return
        segments = path.strip("/").split("/")
        search = scope["method"] == "POST" and (
            path.rstrip("/") == "/v1/recall"
            or (
                len(segments) == 4
                and segments[:2] == ["v1", "users"]
                and segments[3] in SEARCH_ACTIONS
            )
        )
        write = scope["method"] not in {"GET", "HEAD", "OPTIONS"} and not search
        try:
            permit = await run_in_threadpool(capacity.try_http, write=write)
        except (psycopg.Error, CapacityUnavailable):
            await JSONResponse(
                {"detail": {"code": "CapacityUnavailable", "retryable": True}},
                status_code=503,
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )(scope, receive, send)
            return
        if permit is None:
            await JSONResponse(
                {"detail": {"code": "CapacityExceeded", "retryable": True}},
                status_code=429,
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await run_in_threadpool(permit.close)
