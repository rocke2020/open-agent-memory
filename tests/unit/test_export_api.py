"""Portable export delivery is authorized through the public API."""

from pathlib import Path

import httpx

from oam.api.app import create_app

ROOT = Path(__file__).parents[2]


async def test_export_receipt_manifest_and_members_use_one_user_runtime():
    class Runtime:
        def create_export(self, actor, user_id):
            assert user_id == "lin"
            return {"export_id": "export_one", "operation_id": "export_one", "state": "ready"}

        def get_export(self, actor, user_id, export_id):
            assert (user_id, export_id) == ("lin", "export_one")
            return {"format_version": 1, "parts": [{"index": 0}], "originals": [{"index": 0}]}

        def get_export_part(self, actor, user_id, export_id, index):
            assert (user_id, export_id, index) == ("lin", "export_one", 0)
            return b'{"kind":"source"}\n'

        def get_export_original(self, actor, user_id, export_id, index):
            assert (user_id, export_id, index) == ("lin", "export_one", 0)
            return b"original bytes", "text/plain"

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer synthetic"}
        created = await client.post("/v1/users/lin/exports", headers=headers)
        manifest = await client.get("/v1/users/lin/exports/export_one", headers=headers)
        part = await client.get("/v1/users/lin/exports/export_one/records/0", headers=headers)
        original = await client.get("/v1/users/lin/exports/export_one/originals/0", headers=headers)

    assert created.status_code == 202
    assert manifest.json()["format_version"] == 1
    assert part.content == b'{"kind":"source"}\n'
    assert original.content == b"original bytes"
    assert original.headers["content-disposition"].startswith("attachment;")
