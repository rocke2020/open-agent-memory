import json
from pathlib import Path

import httpx

from oam.api.app import create_app

ROOT = Path(__file__).parents[2]


async def test_profile_read_and_refresh_http_status_contract() -> None:
    class Runtime:
        def get_profile(self, actor, user_id, max_tokens):
            return {"content": None, "freshness": "missing", "generation_calls": 0}

        def refresh_profile(self, actor, user_id, instruction, reset_instruction):
            return {"status": "accepted", "operation_id": "op_profile"}

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic"},
    ) as client:
        read = await client.get("/v1/users/alice/profile")
        accepted = await client.post("/v1/users/alice/profile/refresh", json={})

    assert read.status_code == 200
    assert read.json()["freshness"] == "missing"
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "accepted", "operation_id": "op_profile"}


async def test_unchanged_profile_refresh_returns_200_without_operation() -> None:
    class Runtime:
        def refresh_profile(self, actor, user_id, instruction, reset_instruction):
            return {
                "status": "unchanged",
                "profile_revision": 3,
                "generation_calls": 0,
                "content": "## Main content\n\nReady\n\n## References\n\n1. Chat",
            }

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic"},
    ) as client:
        response = await client.post("/v1/users/alice/profile/refresh", json={})

    assert response.status_code == 200
    assert response.json()["generation_calls"] == 0


async def test_user_resources_dispatch_evidence_and_profile_by_declared_type() -> None:
    accepted = []

    class Runtime:
        def authenticate(self, actor):
            pass

        def retain_profile_resource(
            self, actor, user_id, body, idempotency_key, *, expected_version
        ):
            accepted.append(("profile", user_id, body.resource_id, expected_version))
            return {"operation_id": "op_profile", "revision": 1}

        def retain(self, actor, body, idempotency_key, **upload):
            accepted.append(("evidence", body.user_id, body.document_id, None))
            return {"operation_id": "op_evidence", "revision": 1}

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "resource-1"},
    ) as client:
        profile = await client.post(
            "/v1/users/alice/resources",
            json={
                "resource_id": "basic",
                "resource_type": "profile",
                "subtype": "basic-profile/v1",
                "content": {"date_of_birth": "1990-05-10"},
            },
        )
        evidence = await client.post(
            "/v1/users/alice/resources",
            json={
                "resource_type": "evidence",
                "input_type": "document",
                "document_id": "report",
                "update_mode": "replace",
                "text": "A report.",
            },
        )
        old_resource = await client.post(
            "/v1/resources",
            json={
                "resource_id": "basic",
                "resource_type": "profile",
                "subtype": "basic-profile/v1",
                "content": {"date_of_birth": "1990-05-10"},
            },
        )
        old_evidence = await client.post(
            "/v1/retain",
            json={
                "user_id": "alice",
                "input_type": "document",
                "document_id": "legacy",
                "update_mode": "replace",
                "text": "old",
            },
        )
        evidence_with_revision = await client.post(
            "/v1/users/alice/resources",
            data={
                "metadata": json.dumps(
                    {
                        "resource_type": "evidence",
                        "input_type": "document",
                        "document_id": "report-file",
                        "update_mode": "replace",
                    }
                )
            },
            files={"original": ("report.txt", b"A report.", "text/plain")},
            headers={"If-Match": "1"},
        )
        metadata_as_file = await client.post(
            "/v1/users/alice/resources",
            files={"metadata": ("metadata.json", b"{}", "application/json")},
        )

    assert profile.status_code == 202
    assert evidence.status_code == 202
    assert old_resource.status_code == 404
    assert old_evidence.status_code == 422
    assert evidence_with_revision.status_code == 422
    assert metadata_as_file.status_code == 422
    assert metadata_as_file.json()["detail"]["code"] == "InvalidResourceRequest"
    assert accepted == [
        ("profile", "alice", "basic", None),
        ("evidence", "alice", "report", None),
    ]
