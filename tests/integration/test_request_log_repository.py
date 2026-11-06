"""Request history SQL contract in a newly created, isolated database."""

import base64
from types import SimpleNamespace

import psycopg
import pytest
from test_core_http import core_database  # noqa: F401

from oam.db.request_logs import list_request_logs, record_request

pytestmark = pytest.mark.integration


def test_history_pagination_filters_and_forced_user_scope(core_database):  # noqa: F811
    dsn, tenant, _ = core_database
    alice = SimpleNamespace(tenant_id=tenant, user_id="alice")
    bob = SimpleNamespace(tenant_id=tenant, user_id="bob")
    with psycopg.connect(dsn) as connection:
        connection.execute("SET ROLE oam_content")
        for index in range(51):
            record_request(
                connection,
                alice,
                {
                    "request_id": f"req_{index:03}",
                    "operation": "POST /v1/recall",
                    "status": 200,
                    "duration_ms": 1.0,
                },
            )
        record_request(
            connection,
            bob,
            {
                "request_id": "req_bob",
                "operation": "GET /v1/private",
                "status": 500,
                "duration_ms": 1.0,
                "error_code": "InternalError",
            },
        )
        page = list_request_logs(connection, alice, operation="ReCaLl")
        assert page["total"] == 51
        assert page["summary"] == {"total_calls": 51, "success_rate": 100.0}
        assert list_request_logs(connection, alice, limit=10, offset=50)["items"]
        assert list_request_logs(connection, alice, status_code=404)["total"] == 0
        assert len(page["items"]) == 50
        assert page["next_after"]
        last = list_request_logs(connection, alice, operation="recall", after=page["next_after"])
        assert len(last["items"]) == 1 and last["next_after"] is None
        assert not (
            {row["request_id"] for row in page["items"]}
            & {row["request_id"] for row in last["items"]}
        )
        assert list_request_logs(connection, alice, errors_only=True)["items"] == []
        assert list_request_logs(connection, alice, request_id="req_bob")["items"] == []
        assert connection.execute("SELECT count(*) FROM request_logs").fetchone()[0] == 51
        with pytest.raises(ValueError, match="Invalid cursor"):
            list_request_logs(connection, alice, after=base64.urlsafe_b64encode(b'["bad","req"]'))


def test_summary_is_scope_wide_and_independent_of_filters(core_database):  # noqa: F811
    dsn, tenant, _ = core_database
    authority = SimpleNamespace(tenant_id=tenant, user_id="alice")
    with psycopg.connect(dsn) as connection:
        connection.execute("SET ROLE oam_content")
        assert list_request_logs(connection, authority)["summary"] == {
            "total_calls": 0,
            "success_rate": None,
        }
        for index, status in enumerate((200, 202, 404, 503)):
            record_request(
                connection,
                authority,
                {
                    "request_id": f"status_{index}",
                    "operation": "GET /v1/test",
                    "status": status,
                    "duration_ms": 1,
                },
            )
        page = list_request_logs(connection, authority, errors_only=True, status_code=404, limit=1)
        assert page["total"] == 1
        assert page["summary"] == {"total_calls": 4, "success_rate": 50.0}
        assert page["scope"] == {"tenant_id": tenant, "user_id": "alice"}
