"""Real API and worker processes sharing isolated storage and deterministic model HTTP."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import httpx
import psycopg
import pytest
import test_capacity_http
import test_core_http
from PIL import Image, ImageDraw
from psycopg.conninfo import make_conninfo
from test_capacity_http import provider_configuration

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration
fake_provider = test_capacity_http.fake_provider
core_database = test_core_http.core_database


@pytest.fixture
def delayed_provider(monkeypatch):
    """Delay the existing fake handler to make simultaneous real claims observable."""
    events = []
    lock = threading.Lock()
    server_type = test_capacity_http.ThreadingHTTPServer

    class DelayedServer(server_type):
        def __init__(self, address, handler):
            class DelayedHandler(handler):
                def do_POST(self):
                    event = {"path": self.path, "started_at": time.time()}
                    with lock:
                        events.append(event)
                    time.sleep(0.08)
                    try:
                        super().do_POST()
                    finally:
                        event["finished_at"] = time.time()

            super().__init__(address, DelayedHandler)

    monkeypatch.setattr(test_capacity_http, "ThreadingHTTPServer", DelayedServer)
    return events


@pytest.fixture
def process_provider(delayed_provider, fake_provider):
    return fake_provider, delayed_provider


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def originals(dsn, tenant):
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT source_id,source_version,original_bytes,content_sha256 "
            "FROM source_versions WHERE tenant_id=%s AND user_id='alice' ORDER BY source_id",
            (tenant,),
        ).fetchall()
    for _, _, data, digest in rows:
        assert hashlib.sha256(bytes(data)).digest() == bytes(digest)
    return {(row[0], row[1]): bytes(row[2]) for row in rows}


def document_originals():
    page = Image.new("RGB", (600, 100), "white")
    ImageDraw.Draw(page).text((10, 20), "Glucose 5.2 mmol/L", fill="black", font_size=28)
    documents = {"html": ("text/html", b"<html><body>Glucose 5.2 mmol/L</body></html>")}
    for kind, media_type in (("pdf", "application/pdf"), ("png", "image/png")):
        output = BytesIO()
        page.save(output, format=kind.upper())
        documents[kind] = media_type, output.getvalue()
    return documents


async def await_operations(client, headers, user, operation_ids, *, deadline):
    while True:
        states = []
        for operation_id in operation_ids:
            response = await client.get(
                f"/v1/users/{user}/operations/{operation_id}", headers=headers
            )
            assert response.status_code == 200, response.text
            states.append(response.json())
        assert all(state["status"] not in {"failed", "partial"} for state in states), states
        if all(
            state["status"] == "succeeded" and state["readiness"].get("source_searchable")
            for state in states
        ):
            return states
        assert time.monotonic() < deadline, states
        await asyncio.sleep(0.04)


@pytest.mark.parametrize(
    ("embedded", "api_count", "worker_count"),
    [(True, 1, 0), (False, 2, 2), (True, 2, 0)],
    ids=["embedded", "dedicated-replicas", "embedded-replicas"],
)
async def test_process_topology_indexes_and_recalls_during_imports(
    core_database, process_provider, tmp_path, embedded, api_count, worker_count
):
    dsn, tenant, root_headers = core_database
    calls, provider_events = process_provider
    config = provider_configuration(tmp_path / "process.yaml", embedded=embedded)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "CAPACITY_FAKE_PROVIDER": os.environ["CAPACITY_FAKE_PROVIDER"],
        "OAM_API_DSN": make_conninfo(dsn, options="-c role=oam_api"),
        "OAM_WORKER_QUEUE_DSN": make_conninfo(dsn, options="-c role=oam_queue"),
        "OAM_WORKER_CONTENT_DSN": make_conninfo(dsn, options="-c role=oam_content"),
    }
    if os.environ.get("PGPASSWORD"):
        environment["PGPASSWORD"] = os.environ["PGPASSWORD"]
    artifact = {"processes": [], "claims": [], "recalls": [], "writes": []}
    children = []
    clients = []
    write_tasks = []
    retained_originals = None
    started_at = time.monotonic()
    deadline = started_at + 30
    with ExitStack() as logs:
        try:
            for role, count in (("api", api_count), ("worker", worker_count)):
                for index in range(count):
                    port = unused_port() if role == "api" else None
                    arguments = [sys.executable, "-m", "oam.cli", role, "--config", str(config)]
                    if port is not None:
                        arguments += ["--host", "127.0.0.1", "--port", str(port)]
                    log_path = tmp_path / f"{role}-{index}.log"
                    process = subprocess.Popen(
                        arguments,
                        cwd=tmp_path,
                        env={**environment, "PGAPPNAME": f"capacity-{role}-{index}"},
                        stdin=subprocess.DEVNULL,
                        stdout=logs.enter_context(log_path.open("wb")),
                        stderr=subprocess.STDOUT,
                    )
                    entry = {
                        "role": role,
                        "pid": process.pid,
                        "started_at": time.time(),
                        "port": port,
                        "log": log_path.name,
                    }
                    artifact["processes"].append(entry)
                    children.append((process, entry, log_path))
                    if port is not None:
                        clients.append(
                            httpx.AsyncClient(
                                base_url=f"http://127.0.0.1:{port}", timeout=6, trust_env=False
                            )
                        )
            assert len({child.pid for child, _, _ in children}) == api_count + worker_count
            for client in clients:
                while True:
                    assert all(process.poll() is None for process, _, _ in children), [
                        path.read_text() for _, _, path in children
                    ]
                    try:
                        response = await client.get("/health/live")
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    assert time.monotonic() < deadline, "API subprocess startup timed out"
                    await asyncio.sleep(0.04)
                readiness = (await client.get("/health/ready")).json()
                assert readiness["embedded_worker_running"] is (True if embedded else None)

            user_headers = {}
            for user in ("alice", "load-0", "load-1", "load-2", "load-3"):
                response = await clients[0].post(
                    "/v1/users", headers=root_headers, json={"user_id": user}
                )
                assert response.status_code == 201, response.text
                user_headers[user] = {"Authorization": f"Bearer {response.json()['secret']}"}
            chat = {
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
            }
            response = await clients[0].post(
                "/v1/retain",
                json=chat,
                headers={**user_headers["alice"], "Idempotency-Key": "baseline-chat"},
            )
            assert response.status_code == 202, response.text
            operations = [response.json()["operation_id"]]
            documents = document_originals()
            document_sources = set()
            for kind, (media_type, original) in documents.items():
                response = await clients[0].post(
                    "/v1/users/alice/resources",
                    headers={**user_headers["alice"], "Idempotency-Key": f"baseline-{kind}"},
                    data={
                        "metadata": json.dumps(
                            {
                                "input_type": "document",
                                "document_id": kind,
                                "update_mode": "replace",
                            }
                        )
                    },
                    files={"original": (f"report.{kind}", original, media_type)},
                )
                assert response.status_code == 202, response.text
                operations.append(response.json()["operation_id"])
                document_sources.add(response.json()["source_id"])
            artifact["baseline_states"] = await await_operations(
                clients[0], user_headers["alice"], "alice", operations, deadline=deadline
            )
            retained_originals = originals(dsn, tenant)
            artifact["original_sha256"] = {
                f"{source}:{version}": hashlib.sha256(data).hexdigest()
                for (source, version), data in retained_originals.items()
            }
            assert len(retained_originals) == 4
            assert all(
                original in retained_originals.values() for _, original in documents.values()
            )
            recall_started = asyncio.Event()

            async def submit_writes(user_index):
                user = f"load-{user_index}"
                client = clients[user_index % api_count]
                operation_ids = []
                for number in range(4):
                    if number == 2:
                        await recall_started.wait()
                    start = time.time()
                    headers = {**user_headers[user], "Idempotency-Key": f"load-{number}"}
                    kind = "interaction" if number == 0 else ("html", "pdf", "png")[number - 1]
                    if number == 0:
                        response = await client.post(
                            "/v1/retain", headers=headers, json={**chat, "user_id": user}
                        )
                    else:
                        media_type, original = documents[kind]
                        response = await client.post(
                            f"/v1/users/{user}/resources",
                            headers=headers,
                            data={
                                "metadata": json.dumps(
                                    {
                                        "input_type": "document",
                                        "document_id": kind,
                                        "update_mode": "replace",
                                    }
                                )
                            },
                            files={"original": (f"report.{kind}", original, media_type)},
                        )
                    artifact["writes"].append(
                        {
                            "user": user,
                            "input_kind": kind,
                            "started_at": start,
                            "finished_at": time.time(),
                            "status": response.status_code,
                        }
                    )
                    assert response.status_code == 202, response.text
                    operation_ids.append(response.json()["operation_id"])
                return user, operation_ids

            write_tasks = [asyncio.create_task(submit_writes(index)) for index in range(4)]
            maximum_owners = 0
            maximum_tasks = 0
            with psycopg.connect(dsn, autocommit=True) as observer:
                while True:
                    rows = observer.execute(
                        "SELECT owner_id,user_id,work_unit_id FROM work_units "
                        "WHERE tenant_id=%s AND status='running' "
                        "AND lease_expires_at>clock_timestamp() ORDER BY owner_id,user_id",
                        (tenant,),
                    ).fetchall()
                    artifact["claims"].append({"at": time.time(), "rows": rows})
                    owners = {row[0] for row in rows}
                    maximum_owners = max(maximum_owners, len(owners))
                    maximum_tasks = max(maximum_tasks, len(rows))
                    assert all(sum(row[0] == owner for row in rows) <= 10 for owner in owners)
                    assert all(sum(row[1] == user for row in rows) <= 2 for user in user_headers)
                    target_owners = api_count if embedded else worker_count
                    if len(owners) == target_owners and len(rows) >= 4:
                        break
                    assert time.monotonic() < deadline, artifact["claims"][-5:]
                    await asyncio.sleep(0.02)

                async def recall(client_index, mode):
                    start = time.time()
                    recall_started.set()
                    response = await clients[client_index].post(
                        "/v1/recall",
                        headers=user_headers["alice"],
                        json={
                            "user_id": "alice",
                            "query": "glucose short explanations",
                            "recall_level": mode,
                        },
                    )
                    elapsed = time.time() - start
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    assert {item["origin"] for item in payload["items"]} == {"memory", "evidence"}
                    assert "5.2 mmol/L" in payload["context"]
                    assert "short explanations" in payload["context"]
                    assert all(item["citations"] for item in payload["items"])
                    cited_sources = {
                        citation["source_id"]
                        for item in payload["items"]
                        for citation in item["citations"]
                    }
                    assert document_sources <= cited_sources
                    assert elapsed < (3 if mode == "fast" else 5)
                    artifact["recalls"].append(
                        {
                            "api": client_index,
                            "mode": mode,
                            "started_at": start,
                            "finished_at": time.time(),
                            "response": payload,
                        }
                    )

                await asyncio.gather(
                    *(recall(index, mode) for index in range(api_count) for mode in ("fast", "mid"))
                )
                written = await asyncio.gather(*write_tasks)
                for user, operation_ids in written:
                    await await_operations(
                        clients[0], user_headers[user], user, operation_ids, deadline=deadline
                    )
            artifact["maximum_live_owners"] = maximum_owners
            artifact["maximum_live_tasks"] = maximum_tasks
            assert maximum_owners == (api_count if embedded else worker_count)
            assert maximum_tasks >= 4
            assert any(
                write["started_at"] < recall["finished_at"]
                and recall["started_at"] < write["finished_at"]
                for write in artifact["writes"]
                for recall in artifact["recalls"]
            ), "no admitted HTTP write overlapped a recall"
            assert any(role == "extraction" and vision for _, role, vision in calls)
            assert time.monotonic() < deadline, "process acceptance exceeded its 30-second budget"
        finally:
            for task in write_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*write_tasks, return_exceptions=True)
            for client in clients:
                await client.aclose()
            for process, entry, _ in children:
                if process.poll() is None:
                    entry["stop_requested_at"] = time.time()
                    process.send_signal(signal.SIGTERM)
            for process, entry, _ in children:
                try:
                    entry["exit_code"] = await asyncio.to_thread(process.wait, 8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 3)
                    entry["forced_cleanup"] = True
                entry["stopped_at"] = time.time()
            artifact["provider_events"] = provider_events
            artifact["elapsed_seconds"] = time.monotonic() - started_at
            (tmp_path / "process-evidence.json").write_text(json.dumps(artifact, indent=2))
    assert all(not entry.get("forced_cleanup") for _, entry, _ in children), artifact["processes"]
    for _, entry, path in children:
        if entry["role"] == "api":
            assert "Application shutdown complete" in path.read_text()
            assert entry["exit_code"] in (0, -signal.SIGTERM)
        else:
            assert entry["exit_code"] == 0, path.read_text()
    assert retained_originals is not None
    assert originals(dsn, tenant) == retained_originals
    artifact["originals_unchanged_after_shutdown"] = True
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM model_permits "
                "WHERE state IN ('reserved','dispatched','unknown')"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM work_units WHERE tenant_id=%s AND status='running'", (tenant,)
            ).fetchone()[0]
            == 0
        )
    (tmp_path / "process-evidence.json").write_text(json.dumps(artifact, indent=2))
