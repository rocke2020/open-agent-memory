"""Real HTTP model adapter and worker overflow recovery in isolated storage."""

import asyncio
import base64
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import psycopg
import pytest
import uvicorn
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_capacity_http import provider_configuration
from test_core_http import MEDICAL, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.models.runtime import RuntimeModels
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


async def test_http_overflow_consumes_all_source_spans_and_keeps_role_budgets(
    core_database, monkeypatch, tmp_path
):
    dsn, tenant, headers = core_database
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role = data["model"]
            calls.append((role, data.get("max_tokens")))
            finish = "stop"
            if self.path.endswith("/embeddings"):
                body = {
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * 1023}
                        for i, _ in enumerate(data["input"])
                    ],
                    "usage": {
                        "prompt_tokens": len(data["input"]),
                        "total_tokens": len(data["input"]),
                    },
                }
            else:
                if role == "extraction" and isinstance(data["messages"][-1]["content"], list):
                    output = json.dumps({"blocks": [{"text": "Glucose 5.2 mmol/L"}]})
                elif role == "extraction":
                    envelope = json.loads(data["messages"][-1]["content"])
                    members = envelope.get("passages", [{"center": envelope.get("center")}])
                    if len(members) > 1 or len(members[0]["center"]["text"]) > 500:
                        finish, output = "length", "{truncated"
                    else:
                        p = members[0]
                        fact = {"text": p["center"]["text"], "quote": p["center"]["text"]}
                        if "passage_id" in p:
                            fact["passage_id"] = p["passage_id"]
                        output = json.dumps({"outcome": "facts", "facts": [fact]})
                else:
                    output = json.dumps({"abstract": "Glucose 5.2 mmol/L."})
                body = {
                    "choices": [{"finish_reason": finish, "message": {"content": output}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    endpoint = f"http://127.0.0.1:{provider.server_port}/v1"
    monkeypatch.setenv("CAPACITY_FAKE_PROVIDER", endpoint)
    path = provider_configuration(tmp_path / "runtime.yaml", embedded=False)
    config = load_config(path).config
    app = create_app(path)
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    try:
        with socket.socket() as listener, httpx.Client(trust_env=False) as model_http:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            api_thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            api_thread.start()
            try:
                for _ in range(500):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                assert server.started
                models = RuntimeModels(config, {"CAPACITY_FAKE_PROVIDER": endpoint}, model_http)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                    headers=headers,
                    trust_env=False,
                ) as client:
                    await use_created_user_key(client, "overflow")
                    capabilities = (await client.get("/v1/capabilities")).json()
                    assert capabilities["limits"]["chunk"]["recall"]["max_tokens"] == 8192
                    assert capabilities["limits"]["resource"]["max_output_tokens"] == 4096
                    assert capabilities["limits"]["extraction"]["max_output_tokens"] == 64000
                    texts = (
                        "a" * 300 + ". " + "b" * 300 + ". ",
                        "c" * 300 + ". " + "d" * 300 + ". ",
                    )
                    accepted = await client.post(
                        "/v1/retain",
                        headers={"Idempotency-Key": "overflow"},
                        json={
                            "user_id": "overflow",
                            "input_type": "interaction",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": text,
                                    "event_time": "2026-10-01T01:00:00Z",
                                    "learning_eligible": True,
                                }
                                for text in texts
                            ],
                        },
                    )
                    assert accepted.status_code == 202, accepted.text
                    assert process_one(
                        config, queue, content, models=models, worker_id="overflow-http"
                    )
                    operation = (
                        await client.get(
                            f"/v1/users/overflow/operations/{accepted.json()['operation_id']}"
                        )
                    ).json()
                    assert operation["status"] == "succeeded"
                    assert operation["usage"]["total_tokens"] == 210
                    assert operation["usage"]["coverage"] == "complete"
                    assert calls.count(("extraction", 64000)) == 7
                    original = base64.b64decode(
                        (MEDICAL / "synthetic-lab-report.png.b64").read_text()
                    )
                    resource = await client.post(
                        "/v1/users/overflow/resources",
                        headers={"Idempotency-Key": "image"},
                        data={
                            "metadata": json.dumps(
                                {
                                    "resource_type": "evidence",
                                    "input_type": "document",
                                    "document_id": "image",
                                    "update_mode": "replace",
                                }
                            )
                        },
                        files={"original": ("report.png", original, "image/png")},
                    )
                    assert resource.status_code == 202, resource.text
                    assert process_one(
                        config, queue, content, models=models, worker_id="resource-http"
                    )
                    assert ("extraction", 4096) in calls
                    assert ("abstract", 4096) in calls
                    for mode in ("fast", "mid"):
                        recalled = await client.post(
                            "/v1/recall",
                            json={
                                "user_id": "overflow",
                                "query": "Glucose",
                                "recall_level": mode,
                            },
                        )
                        assert recalled.status_code == 200, recalled.text
                        assert "Glucose 5.2 mmol/L" in recalled.json()["context"]
                    for route, body in (
                        ("/v1/recall", {"user_id": "overflow", "query": "Glucose"}),
                        ("/v1/users/overflow/search-many", {"queries": ["Glucose"]}),
                        ("/v1/users/overflow/decompose-and-search", {"query": "Glucose"}),
                    ):
                        bad = await client.post(
                            route,
                            json={
                                **body,
                                "include": {"chunks": {"enabled": True, "max_tokens": 8193}},
                            },
                        )
                        assert bad.status_code == 422, bad.text
                    (tmp_path / "overflow-http.json").write_text(
                        json.dumps(
                            {
                                "provider": "deterministic_local_http",
                                "calls": calls,
                                "operation": operation,
                                "capabilities": capabilities,
                            },
                            indent=2,
                        )
                    )
            finally:
                server.should_exit = True
                api_thread.join(timeout=5)
                assert not api_thread.is_alive()
        with psycopg.connect(dsn) as connection:
            rows = connection.execute(
                "SELECT e.content_text,s.span FROM support s "
                "JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                "WHERE s.tenant_id=%s ORDER BY e.evidence_id,(s.span->>'start')::int",
                (tenant,),
            ).fetchall()
            assert len(rows) == 4
            for text in texts:
                spans = sorted(
                    (span for source, span in rows if source == text), key=lambda s: s["start"]
                )
                assert "".join(text[s["start"] : s["end"]] for s in spans) == text
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=2)
