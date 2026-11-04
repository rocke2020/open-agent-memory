"""Content-free request history. Records persist without automatic expiry."""

import base64
import json
from datetime import datetime

from oam.db.context import bind_user


def record_request(connection, authority, record):
    with connection.cursor() as cursor:
        bind_user(cursor, authority)
        cursor.execute(
            """INSERT INTO request_logs
        (tenant_id,user_id,request_id,operation,status,duration_ms,error_code,operation_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                authority.tenant_id,
                authority.user_id,
                record["request_id"],
                record["operation"],
                record["status"],
                record["duration_ms"],
                record.get("error_code"),
                record.get("operation_id"),
            ),
        )


def list_request_logs(
    connection,
    authority,
    *,
    operation=None,
    errors_only=False,
    request_id=None,
    after=None,
    limit=50,
    offset=0,
    status_code=None,
):
    if not 1 <= limit <= 500 or offset < 0 or (after and offset):
        raise ValueError("Invalid pagination")
    if status_code is not None and not 100 <= status_code <= 599:
        raise ValueError("Invalid status code")
    boundary = None
    if after:
        try:
            boundary = json.loads(base64.urlsafe_b64decode(after).decode())
            if not isinstance(boundary, list) or len(boundary) != 2:
                raise ValueError
            if not all(isinstance(value, str) and value for value in boundary):
                raise ValueError
            timestamp = datetime.fromisoformat(boundary[0])
            if timestamp.tzinfo is None:
                raise ValueError
        except Exception as exc:
            raise ValueError("Invalid cursor") from exc
    with connection.cursor() as cursor:
        bind_user(cursor, authority)
        clauses = ["tenant_id=%s", "user_id=%s"]
        args = [authority.tenant_id, authority.user_id]
        for condition, value in [
            ("strpos(lower(operation),lower(%s))>0", operation),
            ("request_id=%s", request_id),
        ]:
            if value is not None:
                clauses.append(condition)
                args.append(value)
        if errors_only:
            clauses.append("status >= 400")
        if status_code is not None:
            clauses.append("status=%s")
            args.append(status_code)
        filter_sql = " AND ".join(clauses)
        page_where = ""
        if boundary:
            page_where = " WHERE (created_at,request_id)<(%s::timestamptz,%s)"
        cursor.execute(
            "WITH scoped AS MATERIALIZED (SELECT * FROM request_logs "
            "WHERE tenant_id=%s AND user_id=%s), filtered AS MATERIALIZED "
            "(SELECT * FROM scoped WHERE " + filter_sql + "), "
            "summary AS (SELECT count(*) AS calls,avg((status<400)::int)*100 AS rate "
            "FROM scoped), totals AS (SELECT count(*) AS total FROM filtered) "
            "SELECT summary.calls,summary.rate,totals.total,p.* FROM summary CROSS JOIN totals "
            "LEFT JOIN LATERAL (SELECT created_at,request_id,operation,status,duration_ms,"
            "error_code,operation_id FROM filtered"
            + page_where
            + " ORDER BY created_at DESC,request_id DESC LIMIT %s OFFSET %s) p ON true "
            "ORDER BY p.created_at DESC,p.request_id DESC",
            [authority.tenant_id, authority.user_id, *args, *(boundary or []), limit + 1, offset],
        )
        result = cursor.fetchall()
        calls, rate, total = result[0][:3]
        rows = [row[3:] for row in result if row[3] is not None]
    keys = (
        "time",
        "request_id",
        "operation",
        "status",
        "duration_ms",
        "error_code",
        "operation_id",
    )
    items = [dict(zip(keys, (row[0].isoformat(), *row[1:]), strict=True)) for row in rows[:limit]]
    next_after = (
        base64.urlsafe_b64encode(
            json.dumps([items[-1]["time"], items[-1]["request_id"]]).encode()
        ).decode()
        if len(rows) > limit
        else None
    )
    return {
        "items": items,
        "next_after": next_after,
        "total": total,
        "limit": limit,
        "offset": None if after else offset,
        "next_offset": offset + limit if len(rows) > limit and not after else None,
        "summary": {"total_calls": calls, "success_rate": None if rate is None else float(rate)},
        "scope": {"tenant_id": authority.tenant_id, "user_id": authority.user_id},
    }
