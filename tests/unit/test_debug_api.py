"""Recorded debug operations remain authorized public API calls."""

from pathlib import Path

import httpx

from oam.api.app import create_app

ROOT = Path(__file__).parents[2]


async def test_explain_replay_and_diff_routes_use_the_authorized_runtime():
    class Runtime:
        def get_trace(self, actor, user_id, trace_id):
            assert (user_id, trace_id) == ("lin", "trace_one")
            return {"trace_id": trace_id, "status": "recorded", "result": {"items": []}}

        def replay(self, actor, user_id, body):
            assert (user_id, body.trace_id, body.mode) == ("lin", "trace_one", "inspect_recorded")
            return {"trace_id": body.trace_id, "status": "exact", "result": {"items": []}}

        def diff_traces(self, actor, user_id, before, after):
            assert (user_id, before, after) == ("lin", "trace_one", "trace_two")
            return {"status": "comparable", "added": ["fact_two"], "removed": []}

    app = create_app(ROOT / "configs/oam.yaml", runtime=Runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer synthetic"}
        trace = await client.get("/v1/users/lin/debug/traces/trace_one", headers=headers)
        replay = await client.post(
            "/v1/users/lin/replays",
            headers=headers,
            json={"trace_id": "trace_one", "mode": "inspect_recorded"},
        )
        diff = await client.get(
            "/v1/users/lin/debug/diff?before=trace_one&after=trace_two", headers=headers
        )

    assert trace.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["status"] == "exact"
    assert diff.json()["added"] == ["fact_two"]
