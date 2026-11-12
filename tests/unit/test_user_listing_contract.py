from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from oam.api.app import create_app
from oam.api.runtime import DatabaseRuntime
from oam.auth.credentials import digest_secret
from oam.contracts.users import UserListResponse, parse_last_write_at_gte

ROOT = Path(__file__).parents[2]


async def test_user_list_route_rejects_missing_admin_and_invalid_queries():
    class Runtime:
        def list_users(self, *args, **kwargs):
            raise AssertionError("invalid or unauthenticated request reached runtime")

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.get("/v1/users")
        naive = await client.get(
            "/v1/users?last_write_at_gte=2026-11-12T08%3A30%3A00",
            headers={"Authorization": "Bearer synthetic"},
        )
        high_limit = await client.get(
            "/v1/users?limit=10001", headers={"Authorization": "Bearer synthetic"}
        )

    assert missing.status_code == 401
    assert naive.status_code == 422
    assert high_limit.status_code == 422


def test_user_list_runtime_uses_admin_sql_and_decodes_page():
    result = {
        "items": [{"user_id": "alice", "last_write_at": None}],
        "total": 1,
        "limit": 7,
        "offset": 14,
    }
    calls = []

    class Connection:
        def execute(self, query, parameters):
            calls.append((query, parameters))
            return self

        def fetchone(self):
            return (result,)

    class Pool:
        def connection(self):
            from contextlib import nullcontext

            return nullcontext(Connection())

    runtime = object.__new__(DatabaseRuntime)
    runtime.pool = Pool()
    response = runtime.list_users(
        "root-secret", limit=7, offset=14, last_write_at_gte=datetime(2026, 11, 12, tzinfo=UTC)
    )

    assert response.model_dump(mode="json") == {
        "items": [{"user_id": "alice", "last_write_at": None}],
        "total": 1,
        "limit": 7,
        "offset": 14,
    }
    assert calls == [
        (
            "SELECT oam_list_users(%s,%s,%s,%s)",
            (digest_secret("root-secret"), 7, 14, datetime(2026, 11, 12, tzinfo=UTC)),
        )
    ]


def test_user_list_contract_normalizes_timestamps_and_reports_page_metadata():
    response = UserListResponse.model_validate(
        {
            "items": [{"user_id": "alice", "last_write_at": "2026-11-12T08:30:00+08:00"}],
            "total": 3,
            "limit": 1,
            "offset": 2,
        }
    )

    assert response.model_dump(mode="json") == {
        "items": [{"user_id": "alice", "last_write_at": "2026-11-12T00:30:00Z"}],
        "total": 3,
        "limit": 1,
        "offset": 2,
    }


def test_last_write_filter_requires_rfc3339_with_explicit_offset():
    assert parse_last_write_at_gte("2026-11-12T08:30:00Z") == datetime(
        2026, 11, 12, 8, 30, tzinfo=UTC
    )
    assert parse_last_write_at_gte("2026-11-12T16:30:00+08:00") == datetime(
        2026, 11, 12, 8, 30, tzinfo=UTC
    )

    for value in (
        "2026-11-12T08:30:00",
        "2026-11-12 08:30:00Z",
        "2026-11-12T08:30:00+08:60",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "not-a-time",
    ):
        with pytest.raises(ValueError):
            parse_last_write_at_gte(value)
