"""Read all learned layers through authenticated, non-generative HTTP routes."""

import json
import logging

import pytest
from psycopg.types.json import Jsonb
from test_core_http import core_database as core_database
from test_memory_removal import (
    DEPENDENT_INSIGHT,
    INDEPENDENT_INSIGHT,
    INSIGHT,
    OTHER_MEMORIES,
    PROFILE_TEXT,
    REMOVED_MEMORY,
    SOURCE,
    SURVIVING_MEMORY,
    USER,
    _client,
    _content,
)
from test_memory_removal import removal_database as removal_database

pytestmark = pytest.mark.integration


async def test_memory_offset_pages_cover_each_layer_without_duplicates(removal_database):
    database = removal_database
    # A timestamp tie must still give every record a deterministic position.
    with _content(database.dsn, database.tenant) as connection:
        connection.execute("UPDATE search_units SET created_at='2026-10-01T00:00:00Z'")
    async with _client(database.headers) as client:
        path = f"/v1/users/{USER}/memories"
        seen = []
        for offset, expected in (
            (0, [SURVIVING_MEMORY, REMOVED_MEMORY]),
            (2, [OTHER_MEMORIES[1], OTHER_MEMORIES[0]]),
            (4, []),
        ):
            response = await client.get(
                path, params={"memory_type": "basic", "limit": 2, "offset": offset}
            )
            assert response.status_code == 200, response.text
            page = response.json()
            assert [item["memory_id"] for item in page["items"]] == expected
            assert page["total"] == 4
            assert (page["limit"], page["offset"]) == (2, offset)
            assert page["next_offset"] == (2 if offset == 0 else None)
            assert all(
                item["memory_type"] == "basic" and item["kind"] == "fact" and item["citations"]
                for item in page["items"]
            )
            seen.extend(expected)
        assert len(set(seen)) == 4
        beyond = await client.get(
            path, params={"memory_type": "basic", "offset": 9223372036854775807}
        )
        assert beyond.status_code == 200, beyond.text
        assert beyond.json()["items"] == [] and beyond.json()["total"] == 4
        middle = await client.get(path, params={"memory_type": "basic", "limit": 2, "offset": 1})
        assert [item["memory_id"] for item in middle.json()["items"]] == [
            REMOVED_MEMORY,
            OTHER_MEMORIES[1],
        ]
        insights = await client.get(path, params={"memory_type": "insight", "limit": 2})
        assert insights.status_code == 200, insights.text
        first = insights.json()
        assert first["total"] == 3
        assert [item["insight_id"] for item in first["items"]] == [INSIGHT, INDEPENDENT_INSIGHT]
        assert first["items"][0]["insight_revision"] == 2
        assert all(
            item["memory_type"] == "insight" and len(item["supports"]) == 2
            for item in first["items"]
        )
        cursor_page = await client.get(
            path, params={"memory_type": "insight", "limit": 2, "after": first["next_after"]}
        )
        assert [item["insight_id"] for item in cursor_page.json()["items"]] == [DEPENDENT_INSIGHT]
        last = await client.get(path, params={"memory_type": "insight", "limit": 2, "offset": 2})
        assert [item["insight_id"] for item in last.json()["items"]] == [DEPENDENT_INSIGHT]
        assert last.json()["next_offset"] is None
        # The existing Inspector can keep using its mixed-layer cursor.
        mixed = (await client.get(path, params={"limit": 6})).json()
        following = (
            await client.get(path, params={"limit": 6, "after": mixed["next_after"]})
        ).json()
        assert mixed["total"] == following["total"] == 7
        ids = [item["unit_id"] for item in mixed["items"] + following["items"]]
        assert len(ids) == len(set(ids)) == 7
        assert following["next_after"] is None


async def test_typed_memory_details_and_stored_profile_are_readable(removal_database):
    database = removal_database
    async with _client(database.headers) as client:
        for memory_type, identity, kind, text in (
            ("basic", REMOVED_MEMORY, "fact", f"Synthetic claim {REMOVED_MEMORY}."),
            ("insight", INSIGHT, "insight", f"Synthetic synthesis {INSIGHT} revision 2."),
        ):
            response = await client.get(
                f"/v1/users/{USER}/memories/{identity}", params={"memory_type": memory_type}
            )
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["memory_type"] == memory_type
            assert detail["kind"] == kind
            assert detail["text"] == text
            assert detail["citations"][0]["source_id"] == SOURCE
            assert "policy_epoch" in detail
        profile = await client.get(f"/v1/users/{USER}/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["content"] == PROFILE_TEXT
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute("SELECT count(*) FROM operations").fetchone() == (0,)


async def test_memory_read_logs_correlate_outcome_without_content(removal_database, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        async with _client(removal_database.headers) as client:
            page = await client.get(
                f"/v1/users/{USER}/memories",
                params={"memory_type": "basic", "limit": 1, "offset": 1},
            )
            missing = await client.get(
                f"/v1/users/{USER}/memories/missing", params={"memory_type": "insight"}
            )
    assert page.status_code == 200, page.text
    assert missing.status_code == 404, missing.text
    lines = [
        record.getMessage() for record in caplog.records if "memory_read " in record.getMessage()
    ]
    assert len(lines) == 2
    assert f"request_id={page.headers['X-Request-ID']}" in lines[0]
    assert "memory_type=basic" in lines[0] and "limit=1 offset=1" in lines[0]
    assert "returned=1 total=4" in lines[0] and "duration_ms=" in lines[0]
    assert f"request_id={missing.headers['X-Request-ID']}" in lines[1]
    assert "status=404" in lines[1] and "reason=MemoryUnavailable" in lines[1]
    assert "Synthetic claim" not in " ".join(lines)
    assert removal_database.headers["Authorization"] not in " ".join(lines)


async def test_legacy_profile_keeps_eligible_l1_references_without_generation(removal_database):
    database = removal_database
    sections = {
        "main": [
            {
                "key": "preference",
                "value": "Concise explanations",
                "qualifier": "as stated in September",
                "supports": [{"kind": "fact", "id": REMOVED_MEMORY}],
                "state": "active",
            }
        ]
    }
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "UPDATE profiles SET content=NULL,sections=%s,support_ids='[]',\"references\"='[]'",
            (Jsonb(sections),),
        )
    async with _client(database.headers) as client:
        response = await client.get(f"/v1/users/{USER}/profile")
        assert response.status_code == 200, response.text
        profile = response.json()
        assert profile["content"] is not None
        assert "Concise explanations (as stated in September)" in profile["content"]
        assert profile["support_ids"] == [REMOVED_MEMORY]
        assert profile["references"][0]["kind"] == "fact"
        assert profile["references"][0]["source_id"] == SOURCE
        assert profile["generation_calls"] == 0
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute("SELECT content FROM profiles").fetchone() == (None,)
            assert connection.execute("SELECT count(*) FROM operations").fetchone() == (0,)
        exported = await client.post(f"/v1/users/{USER}/exports")
        assert exported.status_code == 202, exported.text
        export_id = exported.json()["export_id"]
        manifest = await client.get(f"/v1/users/{USER}/exports/{export_id}")
        assert manifest.status_code == 200, manifest.text
        records = []
        for part in manifest.json()["parts"]:
            downloaded = await client.get(
                f"/v1/users/{USER}/exports/{export_id}/records/{part['index']}"
            )
            assert downloaded.status_code == 200, downloaded.text
            records.extend(json.loads(line) for line in downloaded.content.splitlines())
        stored = [record for record in records if record["kind"] == "profile"]
        assert len(stored) == 1
        assert stored[0]["content"] is not None
        assert "Concise explanations (as stated in September)" in stored[0]["content"]
        assert stored[0]["support_ids"] == [REMOVED_MEMORY]
        assert stored[0]["references"][0]["source_id"] == SOURCE
        with _content(database.dsn, database.tenant) as connection:
            connection.execute("UPDATE sources SET status='deleted'")
            connection.execute("UPDATE users SET policy_epoch=policy_epoch+1")
        withdrawn = await client.get(f"/v1/users/{USER}/profile")
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["content"] is None
        assert withdrawn.json()["freshness"] == "stale"
        assert withdrawn.json()["references"] == []


async def test_memory_reads_do_not_expose_foreign_wrong_kind_or_hidden_items(removal_database):
    database = removal_database
    paths = [
        f"/v1/users/{USER}/memories?memory_type=basic&limit=1&offset=0",
        f"/v1/users/{USER}/memories/{REMOVED_MEMORY}?memory_type=basic",
        f"/v1/users/{USER}/memories/{INSIGHT}?memory_type=insight",
        f"/v1/users/{USER}/profile",
    ]
    async with _client(database.headers) as client:
        for headers in (database.other_headers, database.root_headers):
            for path in paths:
                denied = await client.get(path, headers=headers)
                assert denied.status_code == 404, denied.text
        for memory_type, identity in (
            ("basic", INSIGHT),
            ("insight", REMOVED_MEMORY),
            ("basic", "missing"),
            ("insight", "missing"),
            ("basic", "other-" + REMOVED_MEMORY),
        ):
            hidden = await client.get(
                f"/v1/users/{USER}/memories/{identity}", params={"memory_type": memory_type}
            )
            assert hidden.status_code == 404, hidden.text
            assert hidden.json()["detail"]["code"] == "MemoryUnavailable"
        # Simulate the committed exclusion boundary without invoking deletion APIs.
        with _content(database.dsn, database.tenant) as connection:
            connection.execute("UPDATE sources SET status='deleted'")
            connection.execute("UPDATE users SET policy_epoch=policy_epoch+1")
        for path in paths[1:3]:
            assert (await client.get(path)).status_code == 404
        for memory_type in ("basic", "insight"):
            page = await client.get(
                f"/v1/users/{USER}/memories", params={"memory_type": memory_type}
            )
            assert page.status_code == 200, page.text
            assert page.json()["items"] == []
            assert page.json()["total"] == 0


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"offset": 9223372036854775808},
        {"memory_type": "profile"},
        {"after": REMOVED_MEMORY, "offset": 1},
    ],
)
async def test_invalid_memory_page_is_rejected(removal_database, params):
    async with _client(removal_database.headers) as client:
        response = await client.get(f"/v1/users/{USER}/memories", params=params)
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("params", [{}, {"memory_type": "unknown"}])
async def test_memory_detail_requires_explicit_known_type(removal_database, params):
    async with _client(removal_database.headers) as client:
        response = await client.get(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}", params=params)
        assert response.status_code == 422, response.text


async def test_single_detail_read_contract_preserves_typed_deletion(removal_database):
    async with _client(removal_database.headers) as client:
        schema = (await client.get("/openapi.json")).json()
        paths = schema["paths"]
        assert "get" in paths["/v1/users/{user_id}/memories/{memory_id}"]
        assert "get" not in paths["/v1/users/{user_id}/insights/{insight_id}"]
        assert "delete" in paths["/v1/users/{user_id}/insights/{insight_id}"]
        response = await client.get(f"/v1/users/{USER}/insights/{INSIGHT}")
        assert response.status_code == 405, response.text
