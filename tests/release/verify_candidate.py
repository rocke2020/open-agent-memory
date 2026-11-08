"""Opt-in public HTTP probe against an isolated candidate deployment.

Set OAM_RELEASE_BASE_URL, OAM_RELEASE_ROOT_KEY and OAM_RELEASE_OUTPUT.
This command creates synthetic users and calls the deployment's configured models.
It is deliberately outside default pytest collection.
Use the isolated probe configuration in deploy/README.md: the normal L2 delay is
24 hours, while this bounded command waits four minutes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx

ROOT = Path(__file__).resolve().parents[2]


def save(path: Path, value: object, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    if private:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(value, indent=2) + "\n")
    else:
        pending.write_text(json.dumps(value, indent=2) + "\n")
    pending.replace(path)


def wait_operation(
    client: httpx.Client, user: str, receipt: dict, *, insights: bool = False
) -> dict:
    deadline = time.monotonic() + 240
    while True:
        response = client.get(f"/v1/users/{user}/operations/{receipt['operation_id']}")
        response.raise_for_status()
        value = response.json()
        assert value["status"] not in {"failed", "cancelled", "partial"}, value
        if insights:
            assert value.get("stage_state", {}).get("consolidation_status") != "failed", value
        if value["status"] == "succeeded" and (
            not insights or value["readiness"].get("insights_ready")
        ):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(1)


def main() -> None:
    base = os.environ["OAM_RELEASE_BASE_URL"]
    out = Path(os.environ["OAM_RELEASE_OUTPUT"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        capabilities = client.get("/v1/capabilities").json()
        save(out / "capabilities.json", capabilities)
        identity = out / "user.private.json"
        root_headers = {"Authorization": "Bearer " + os.environ["OAM_RELEASE_ROOT_KEY"]}
        if identity.exists():
            account = json.loads(identity.read_text())
            assert account["generation"] == capabilities["index_generation_id"]
        else:
            user = "release-" + out.name
            response = client.post("/v1/users", headers=root_headers, json={"user_id": user})
            assert response.status_code == 201, response.text
            account = {
                "user": user,
                "key": response.json()["secret"],
                "generation": capabilities["index_generation_id"],
            }
            save(identity, account, private=True)
        user = account["user"]
        client.headers["Authorization"] = "Bearer " + account["key"]
        sources = [
            (
                "2026-11-01T09:00:00Z",
                "I am Mira, a clinical pharmacist. On November 1 I reviewed a hospital discharge "
                "plan and asked for a brief written checklist before discussing it aloud. "
                "I prefer concise written instructions and morning updates.",
            ),
            (
                "2026-11-04T09:00:00Z",
                "On November 4 I reviewed a different patient's hospital discharge plan. I again "
                "requested a brief written checklist before the spoken discussion. Written "
                "checklists help me reliably complete my clinical pharmacist reviews.",
            ),
        ]
        receipts = []
        for index, (event_time, content) in enumerate(sources):
            path = out / f"chat-{index}.json"
            body = {
                "user_id": user,
                "input_type": "interaction",
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                        "event_time": event_time,
                        "learning_eligible": True,
                    }
                ],
            }
            if path.exists():
                saved = json.loads(path.read_text())
                assert saved["input"] == body
                receipt = saved["receipt"]
            else:
                response = client.post(
                    "/v1/retain", json=body, headers={"Idempotency-Key": f"candidate-chat-{index}"}
                )
                assert response.status_code == 202, response.text
                receipt = response.json()
                save(path, {"input": body, "receipt": receipt})
            receipts.append(receipt)
        for index, receipt in enumerate(receipts):
            save(
                out / f"chat-{index}-ready.json",
                wait_operation(client, user, receipt, insights=True),
            )
        deadline = time.monotonic() + 240
        while True:
            response = client.get(f"/v1/users/{user}/memories", params={"memory_type": "insight"})
            response.raise_for_status()
            insights = response.json()
            if insights["items"]:
                break
            assert time.monotonic() < deadline, "No positive persisted L2 Insight"
            time.sleep(1)
        save(out / "insights.json", insights)
        for mode in ("fast", "normal"):
            response = client.post(
                "/v1/recall",
                json={
                    "user_id": user,
                    "query": (
                        "How does Mira prefer instructions for clinical pharmacist "
                        "discharge reviews?"
                    ),
                    "recall_level": mode,
                },
            )
            assert response.status_code == 200, response.text
            recalled = response.json()
            assert "checklist" in recalled["context"].lower()
            assert recalled["diagnostics"]["generative_calls"] == 0
            assert any(item["kind"] == "insight" for item in recalled["items"])
            assert all(item.get("citations") for item in recalled["items"])
            save(out / f"recall-{mode}.json", recalled)
        response = client.post(f"/v1/users/{user}/profile/refresh", json={})
        assert response.status_code in {200, 202}, response.text
        if response.status_code == 202:
            save(out / "profile-operation.json", wait_operation(client, user, response.json()))
        response = client.get(f"/v1/users/{user}/profile")
        assert response.status_code == 200, response.text
        profile = response.json()
        assert "pharmacist" in json.dumps(profile).lower()
        assert (
            profile["pending_inputs"] == profile["failed_inputs"] == profile["blocked_inputs"] == 0
        )
        assert profile["uncovered_inputs"] == 0
        save(out / "profile.json", profile)
        unchanged = client.post(f"/v1/users/{user}/profile/refresh", json={})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["generation_calls"] == 0
        save(out / "profile-unchanged.json", unchanged.json())
        fixtures = ROOT / "conformance/fixtures/medical"
        for kind, mime in (("html", "text/html"), ("pdf", "application/pdf"), ("png", "image/png")):
            path = out / f"resource-{kind}.json"
            original = (
                (fixtures / "synthetic-lab-report.html").read_bytes()
                if kind == "html"
                else base64.b64decode((fixtures / f"synthetic-lab-report.{kind}.b64").read_text())
            )
            if path.exists():
                saved = json.loads(path.read_text())
                assert saved["original_sha256"] == hashlib.sha256(original).hexdigest()
                receipt = saved["receipt"]
            else:
                response = client.post(
                    f"/v1/users/{user}/resources",
                    headers={"Idempotency-Key": "candidate-" + kind},
                    data={
                        "metadata": json.dumps(
                            {
                                "input_type": "document",
                                "document_id": "candidate-" + kind,
                                "update_mode": "replace",
                            }
                        )
                    },
                    files={"original": ("report." + kind, original, mime)},
                )
                assert response.status_code == 202, response.text
                receipt = response.json()
                save(
                    path,
                    {"original_sha256": hashlib.sha256(original).hexdigest(), "receipt": receipt},
                )
            save(out / f"resource-{kind}-ready.json", wait_operation(client, user, receipt))
        response = client.post(
            "/v1/recall",
            json={
                "user_id": user,
                "query": "Mira written checklists and the glucose laboratory report result",
                "recall_level": "normal",
            },
        )
        assert response.status_code == 200, response.text
        joint = response.json()
        assert "glucose" in joint["context"].lower() and "checklist" in joint["context"].lower()
        assert joint["diagnostics"]["generative_calls"] == 0
        save(out / "joint-recall.json", joint)
        barrier = Barrier(10)

        def read(index: int) -> dict:
            barrier.wait(timeout=10)
            started = time.monotonic()
            response = client.get(f"/v1/users/{user}/memories", params={"limit": 10})
            assert response.status_code == 200, response.text
            return {
                "index": index,
                "started": started,
                "finished": time.monotonic(),
                "replica": response.headers.get("X-OAM-Replica"),
                "items": len(response.json()["items"]),
            }

        with ThreadPoolExecutor(max_workers=10) as executor:
            requests = list(executor.map(read, range(10)))
        assert all(item["items"] for item in requests)
        assert len({item["replica"] for item in requests}) == 2, requests
        save(out / "ten-requests.json", requests)
        save(
            out / "golden-complete.json",
            {
                "user": user,
                "chat_sources": 2,
                "insight_count": len(insights["items"]),
                "profile_unchanged_calls": 0,
                "resources": ["html", "pdf", "png"],
                "served_by_replicas": 2,
            },
        )
        print(
            "PASS: public candidate L0/L1/L2, fast/normal recall, Profile reuse, "
            "HTML/PDF/PNG and joint recall; ten requests served by both API replicas"
        )


if __name__ == "__main__":
    main()
