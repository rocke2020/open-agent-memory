"""Public HTTP and real model-client paths against isolated storage and a fake server."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from test_core_http import use_created_user_key

from oam.api.app import create_app

pytestmark = pytest.mark.integration
pytest_plugins = ["test_core_http"]


def provider_role(payload, path):
    """Identify the operation when several generation roles share one model."""
    if path.endswith("/embeddings"):
        return "embedding"
    content = payload["messages"][-1]["content"]
    if isinstance(content, list):
        return "extraction"
    envelope = json.loads(content)
    if "center" in envelope or any("center" in p for p in envelope.get("passages", [])):
        return "extraction"
    if "facts" in envelope:
        return "insights"
    if "inputs" in envelope:
        return "profile"
    return "abstract"


@pytest.fixture
def fake_provider(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            vision = isinstance(request.get("messages", [{}])[-1].get("content"), list)
            calls.append((self.path, provider_role(request, self.path), vision))
            if self.path.endswith("/embeddings"):
                response = {
                    "data": [
                        {"index": index, "embedding": [1.0] + [0.0] * 1023}
                        for index, _ in enumerate(request["input"])
                    ],
                    "usage": {
                        "prompt_tokens": len(request["input"]),
                        "total_tokens": len(request["input"]),
                    },
                }
            else:
                user = request["messages"][-1]["content"]
                prompt = None if isinstance(user, list) else json.loads(user)
                if isinstance(user, list):
                    output = {"blocks": [{"text": "Glucose 5.2 mmol/L"}]}
                elif "center" in prompt:
                    center = prompt["center"]
                    output = {
                        "outcome": "facts",
                        "facts": [
                            {
                                "text": center["text"],
                                "category": "preference",
                                "quote": center["text"],
                                "entities": [],
                                "caused_by": [],
                                "updates": [],
                                "annotations": [],
                            }
                        ],
                    }
                elif "facts" in prompt:
                    output = {"outcome": "no_insight"}
                else:
                    output = {"abstract": "Glucose 5.2 mmol/L."}
                response = {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(output)}}
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CAPACITY_FAKE_PROVIDER", f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def provider_configuration(path: Path, *, embedded=True):
    path.write_text(
        yaml.safe_dump(
            {
                "worker": {"embedded_enabled": embedded},
                "models": {
                    "reasoning_effort": {
                        role: "none" for role in ("extraction", "insights", "abstract", "profile")
                    },
                    "vlm": {
                        "protocol": "openai_chat",
                        "model": "synthetic-vlm",
                        "model_env": None,
                        "endpoint_env": "CAPACITY_FAKE_PROVIDER",
                        "api_key_env": None,
                        "capabilities": ["text", "vision"],
                    },
                    "embedding": {
                        "protocol": "openai_compatible",
                        "model": "embedding",
                        "model_env": None,
                        "endpoint_env": "CAPACITY_FAKE_PROVIDER",
                        "api_key_env": None,
                        "capabilities": ["embeddings"],
                    },
                },
            }
        )
    )
    return path


async def test_embedded_runner_accepts_indexes_and_recalls_both_origins(
    core_database,
    fake_provider,
    monkeypatch,
    tmp_path,
):
    dsn, _, headers = core_database
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", make_conninfo(dsn, options="-c role=oam_queue"))
    monkeypatch.setenv("OAM_WORKER_CONTENT_DSN", make_conninfo(dsn, options="-c role=oam_content"))
    path = provider_configuration(tmp_path / "embedded.yaml")
    app = create_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
        ) as client,
    ):
        assert app.state.worker.healthy
        await use_created_user_key(client, "alice")
        interaction = await client.post(
            "/v1/retain",
            headers={"Idempotency-Key": "chat"},
            json={
                "user_id": "alice",
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prefer short explanations.",
                        "event_time": "2026-09-01T09:00:00Z",
                        "learning_eligible": True,
                    }
                ],
            },
        )
        assert interaction.status_code == 202, interaction.text
        evidence = await client.post(
            "/v1/users/alice/resources",
            headers={"Idempotency-Key": "report"},
            data={
                "metadata": json.dumps(
                    {
                        "input_type": "document",
                        "document_id": "lab",
                        "update_mode": "replace",
                    }
                )
            },
            files={
                "original": (
                    "lab.html",
                    b"<html><body>Glucose 5.2 mmol/L</body></html>",
                    "text/html",
                )
            },
        )
        assert evidence.status_code == 202, evidence.text
        operations = [interaction.json()["operation_id"], evidence.json()["operation_id"]]
        deadline = time.monotonic() + 15
        while True:
            states = [
                (await client.get(f"/v1/users/alice/operations/{op}")).json() for op in operations
            ]
            assert all(state["status"] not in {"failed", "partial"} for state in states), states
            if all(
                state["status"] == "succeeded" and state["readiness"].get("source_searchable")
                for state in states
            ):
                break
            assert time.monotonic() < deadline, states
            await asyncio.sleep(0.05)
        results = {}
        for mode, limit in [("fast", 3), ("mid", 5)]:
            start = time.monotonic()
            response = await client.post(
                "/v1/recall",
                json={
                    "user_id": "alice",
                    "query": "glucose short explanations",
                    "recall_level": mode,
                },
            )
            assert response.status_code == 200, response.text
            elapsed = time.monotonic() - start
            assert elapsed < limit
            payload = response.json()
            assert {item["origin"] for item in payload["items"]} == {"memory", "evidence"}
            assert "5.2 mmol/L" in payload["context"]
            assert "short explanations" in payload["context"]
            assert all(item["citations"] for item in payload["items"])
            results[mode] = {"elapsed_seconds": elapsed, "response": payload}
        (tmp_path / "cited-recall.json").write_text(json.dumps(results, indent=2))
    assert app.state.worker.stop_event.is_set()
    assert any(role == "extraction" for _, role, _ in fake_provider)
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM model_permits WHERE state IN "
                "('reserved','dispatched','unknown')"
            ).fetchone()[0]
            == 0
        )
