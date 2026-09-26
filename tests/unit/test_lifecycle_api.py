"""Public source controls use the same user-bound runtime as retain."""

from pathlib import Path

import httpx

from oam.api.app import create_app
from oam.db.sources import SourceVersionConflict

ROOT = Path(__file__).parents[2]


async def test_delete_source_returns_exclusion_and_cleanup_receipt():
    class Runtime:
        def delete_source(self, actor, user_id, source_id, *, resource_only):
            assert (user_id, source_id, resource_only) == ("lin", "chat-1", False)
            return {
                "source_id": source_id,
                "status": "excluded",
                "excluded_versions": [1, 2],
                "policy_epoch": 4,
                "cleanup_operation_id": "op_cleanup",
                "cleanup_status": "accepted",
            }

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            "/v1/users/lin/evidence/chat-1",
            headers={"Authorization": "Bearer synthetic"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "excluded"
    assert response.json()["cleanup_operation_id"] == "op_cleanup"


async def test_resource_revision_requires_expected_active_version():
    class Runtime:
        def authenticate(self, actor):
            return None

        def preflight_revision(self, actor, user_id, resource_id, expected_version, key):
            return None

        def revise_resource(self, actor, user_id, resource_id, body, key, expected_version):
            assert (user_id, resource_id, key, expected_version) == ("lin", "report", "r2", 1)
            assert body.text == "Updated report."
            return {"source_id": resource_id, "revision": 2, "operation_id": "op_r2"}

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/v1/users/lin/resources/report"
        body = {
            "user_id": "lin",
            "input_type": "document",
            "document_id": "report",
            "update_mode": "replace",
            "text": "Updated report.",
        }
        missing = await client.put(
            url, json=body, headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "r2"}
        )
        result = await client.put(
            url,
            json=body,
            headers={
                "Authorization": "Bearer synthetic",
                "Idempotency-Key": "r2",
                "If-Match": "1",
            },
        )

    assert missing.status_code == 422
    assert result.status_code == 202
    assert result.json()["revision"] == 2


async def test_resource_revision_accepts_bounded_file_original():
    class Runtime:
        def authenticate(self, actor):
            return None

        def preflight_revision(self, actor, user_id, resource_id, expected_version, key):
            return None

        def revise_resource(
            self, actor, user_id, resource_id, body, key, expected_version, **upload
        ):
            assert (user_id, resource_id, expected_version) == ("lin", "scan", 1)
            assert upload["content"] == b"synthetic png"
            assert upload["media_type"] == "image/png"
            return {"source_id": resource_id, "revision": 2, "operation_id": "op_scan"}

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/users/lin/resources/scan",
            headers={
                "Authorization": "Bearer synthetic",
                "Idempotency-Key": "scan-v2",
                "If-Match": "1",
            },
            data={
                "metadata": '{"user_id":"lin","input_type":"document",'
                '"document_id":"scan","update_mode":"replace"}'
            },
            files={"original": ("scan.png", b"synthetic png", "image/png")},
        )

    assert response.status_code == 202
    assert response.json()["revision"] == 2


async def test_stale_revision_rejects_before_upload_body_is_consumed():
    consumed = False

    class Runtime:
        def authenticate(self, actor):
            return None

        def preflight_revision(self, actor, user_id, resource_id, expected_version, key):
            raise SourceVersionConflict(resource_id)

        def revise_resource(self, *args, **kwargs):
            raise AssertionError("stale revision reached acceptance")

    async def body():
        nonlocal consumed
        consumed = True
        yield b"file bytes"

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/users/lin/resources/scan",
            content=body(),
            headers={
                "Authorization": "Bearer synthetic",
                "Idempotency-Key": "stale",
                "If-Match": "1",
                "Content-Type": "application/octet-stream",
            },
        )

    assert response.status_code == 409
    assert not consumed


async def test_inspection_reads_current_resources_memories_and_original_bytes():
    class Runtime:
        def list_resources(self, actor, user_id, *, after):
            assert (user_id, after) == ("lin", None)
            return {"items": [{"resource_id": "report", "active_version": 2}], "next_after": None}

        def get_resource(self, actor, user_id, resource_id):
            assert (user_id, resource_id) == ("lin", "report")
            return {"resource_id": "report", "active_version": 2, "abstract_status": "pending"}

        def get_resource_original(self, actor, user_id, resource_id):
            assert (user_id, resource_id) == ("lin", "report")
            return b"<script>alert(1)</script>", "text/html"

        def list_memories(self, actor, user_id, *, after):
            assert (user_id, after) == ("lin", None)
            return {
                "items": [{"id": "fact_1", "kind": "fact", "citations": []}],
                "next_after": None,
            }

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer synthetic"}
        resources = await client.get("/v1/users/lin/resources", headers=headers)
        detail = await client.get("/v1/users/lin/resources/report", headers=headers)
        original = await client.get("/v1/users/lin/resources/report/original", headers=headers)
        memories = await client.get("/v1/users/lin/memories", headers=headers)

    assert resources.json()["items"][0]["active_version"] == 2
    assert detail.json()["abstract_status"] == "pending"
    assert original.content == b"<script>alert(1)</script>"
    assert original.headers["content-disposition"].startswith("attachment;")
    assert memories.json()["items"][0]["kind"] == "fact"


async def test_memory_source_detail_uses_authorized_evidence_route():
    class Runtime:
        def get_evidence(self, actor, user_id, source_id):
            assert (user_id, source_id) == ("lin", "chat-1")
            return {
                "source_id": source_id,
                "active_version": 1,
                "original": {"messages": [{"content": "Supported source text"}]},
                "passages": [{"passage_id": "p1", "text": "Supported source text"}],
            }

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/users/lin/evidence/chat-1", headers={"Authorization": "Bearer synthetic"}
        )

    assert response.status_code == 200
    assert response.json()["original"]["messages"][0]["content"] == "Supported source text"
