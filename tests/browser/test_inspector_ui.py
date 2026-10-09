"""Browser check for the same-origin Inspector, using the existing debug Chrome."""

import json
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest

from oam.contracts.retain import RecallRequest

playwright = pytest.importorskip("playwright.sync_api")
WEB = Path(__file__).parents[2] / "src" / "oam" / "web"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_memory_source_messages_and_profile_create_flow(tmp_path):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 9222)):
            pytest.skip("Existing debug Chrome is unavailable")
    with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
        websocket_url = json.load(response)["webSocketDebuggerUrl"]
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile_missing = False
    profile_failed = False
    profile_blocked = False
    profile_indexing = False
    profile_overrides = {}
    calls = []
    profile_data = None
    profile_data_version = 0

    def respond(route):
        nonlocal \
            profile_missing, \
            profile_failed, \
            profile_blocked, \
            profile_indexing, \
            profile_data, \
            profile_data_version
        path = route.request.url
        calls.append((route.request.method, path, route.request.post_data))
        if path.endswith("/recall"):
            request = RecallRequest.model_validate_json(route.request.post_data)
            assert request.user_id == "alice"
            assert request.query == "brief replies"
            data = {
                "items": [],
                "context": "Please keep replies brief.",
                "context_tokens": 7,
                "diagnostics": {"generative_calls": 0},
            }
        elif path.endswith("/memories"):
            data = {
                "items": [
                    {
                        "memory_id": "fact-1",
                        "kind": "fact",
                        "origin": "chat",
                        "text": "Prefers concise replies.",
                        "supports": [],
                        "citations": [
                            {"source_id": "chat-1", "source_version": 1, "passage_id": "p1"}
                        ],
                    },
                    {
                        "insight_id": "insight-1",
                        "insight_revision": 1,
                        "kind": "insight",
                        "subtype": "preferences",
                        "origin": "memory",
                        "text": "The user prefers brief replies.",
                        "supports": [],
                        "citations": [],
                    },
                ],
                "next_after": None,
            }
        elif "/profile-data" in path:
            if route.request.method in {"POST", "PUT"}:
                profile_data = json.loads(route.request.post_data)
                profile_data_version += 1
                data = {"revision": profile_data_version, "operation_id": "data-op"}
            elif path.endswith("/original"):
                data = profile_data
            elif path.endswith("/profile-data"):
                data = {
                    "items": [
                        {
                            "profile_data_id": "basic",
                            "media_type": "application/json",
                            "active_version": profile_data_version,
                        }
                    ]
                    if profile_data
                    else [],
                    "next_after": None,
                }
            else:
                data = {
                    "profile_data_id": "basic",
                    "subtype": "basic-profile/v1",
                    "media_type": "application/json",
                    "active_version": profile_data_version,
                    "mapped_fields": [
                        {
                            "section": "main",
                            "key": "date_of_birth",
                            "value": profile_data["content"]["date_of_birth"],
                        }
                    ],
                }
        elif path.endswith("/resources"):
            data = {"items": [], "next_after": None}
        elif path.endswith("/evidence/chat-1"):
            data = {
                "original": {
                    "messages": [{"role": "user", "content": "Please keep replies brief."}]
                }
            }
        elif "/profile/refresh" in path:
            if profile_indexing:
                route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps({"detail": {"code": "ProfileInputsNotReady"}}),
                )
                return
            data = {"status": "accepted", "operation_id": "create-op"}
        elif path.endswith("/profile") or "/profile?" in path:
            data = {
                "profile_revision": None if profile_missing else 3,
                "updated_at": None if profile_missing else "2026-09-29T12:30:00+00:00",
                "freshness": "partial"
                if profile_failed
                else "dirty"
                if profile_blocked
                else "missing"
                if profile_missing
                else "current",
                "tokens": 0,
                "pending_inputs": 1
                if profile_failed or profile_blocked
                else 6
                if profile_missing
                else 0,
                "failed_inputs": 1 if profile_failed else 0,
                "blocked_inputs": 1 if profile_blocked else 0,
                "content": None
                if profile_missing
                else "## Main content\n\nThe user is an **engineer**. Keep personal data local [1]."
                "\n\n## References\n\n1. Conversation chat-1",
                "references": []
                if profile_missing
                else [
                    {
                        "kind": "fact",
                        "support_id": "fact-1",
                        "source_id": "chat-1",
                        "passage_id": "p1",
                        "event_time": "2026-09-29T12:30:00+00:00",
                    }
                ],
            }
            data.update(profile_overrides)
        else:
            raise AssertionError(f"Unexpected request: {path}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket_url)
            context = browser.new_context()
            page = context.new_page()
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.locator("#user").fill("alice")
                page.locator("#connect-form button").click()
                page.locator("#item-list button").first.click()
                detail = page.locator("#detail")
                detail.get_by_text("Please keep replies brief.").wait_for()
                assert detail.get_by_text("Prefers concise replies.").is_visible()
                assert detail.get_by_text("Source messages").is_visible()
                assert len([call for call in calls if "/evidence/chat-1" in call[1]]) == 1
                assert "insight · preferences" in page.locator("#item-list").inner_text()
                page.locator("#item-list button").nth(1).click()
                assert detail.get_by_text("Subtype", exact=True).is_visible()
                assert detail.get_by_text("preferences", exact=True).is_visible()

                page.locator("#query").fill("brief replies")
                page.locator("#search-form button").click()
                detail.get_by_text("Current search context", exact=True).wait_for()
                assert "Please keep replies brief." in detail.locator("pre").inner_text()
                assert not any("/profile" in call[1] for call in calls)
                assert "include_profile" not in json.loads(calls[-1][2])

                page.locator('[data-view="profile-data"]').click()
                page.get_by_role("button", name="Add Profile Data", exact=True).click()
                page.get_by_label("Profile Data ID", exact=True).fill("basic")
                page.get_by_label("Date of birth", exact=True).fill("1990-05-10")
                page.get_by_role("button", name="Create Profile Data", exact=True).click()
                page.get_by_role("button", name="basic", exact=False).click()
                detail.get_by_text("1990-05-10", exact=True).wait_for()
                detail.get_by_role("button", name="Revise", exact=True).click()
                page.get_by_label("Date of birth", exact=True).fill("1991-05-10")
                page.get_by_role("button", name="Create immutable revision", exact=True).click()
                page.get_by_role("button", name="basic", exact=False).click()
                detail.get_by_text("1991-05-10", exact=True).wait_for()
                writes = [
                    call
                    for call in calls
                    if call[0] in {"POST", "PUT"} and "/profile-data" in call[1]
                ]
                assert [call[0] for call in writes] == ["POST", "PUT"]
                assert json.loads(writes[-1][2]) == {
                    "profile_data_id": "basic",
                    "subtype": "basic-profile/v1",
                    "content": {"date_of_birth": "1991-05-10"},
                }
                page.locator('[data-view="resources"]').click()
                page.get_by_role("button", name="Add Resource", exact=True).click()
                assert page.get_by_label("Resource type", exact=True).count() == 0
                assert page.get_by_label("Date of birth", exact=True).is_hidden()

                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("Read-only User Profile", exact=True).wait_for()
                assert detail.locator(".profile-content strong").count() == 1
                assert detail.locator(".profile-content strong").inner_text() == "engineer"
                detail.get_by_text(
                    "The user is an engineer. Keep personal data local [1].", exact=True
                ).wait_for()
                assert detail.get_by_text("L3 User Profile", exact=True).count() == 0
                assert detail.locator(".profile-content h4").all_inner_texts() == ["Main content"]
                assert "Conversation chat-1" not in detail.locator(".profile-content").inner_text()
                top_level = detail.evaluate(
                    "root => [...root.children].map(el => el.textContent.trim())"
                )
                summary = detail.get_by_label("User Profile status", exact=True)
                assert summary.get_by_text("Up to date", exact=True).is_visible()
                updated = summary.locator("time")
                assert updated.get_attribute("datetime") == "2026-09-29T12:30:00+00:00"
                assert "Updated" in updated.inner_text()
                assert "ago" in updated.inner_text()
                assert "2026" in updated.get_attribute("title")
                assert top_level[1].startswith("Up to date")
                assert detail.get_by_text("Freshness", exact=True).count() == 0
                assert detail.get_by_text("Revision", exact=True).count() == 0
                assert detail.get_by_text("Pending inputs", exact=True).count() == 0
                assert top_level[-1].startswith("Reference details")
                assert not detail.locator("details").evaluate("element => element.open")
                assert detail.get_by_text("References", exact=True).count() == 0
                reference = detail.get_by_text("[1] Conversation on 2026-09-29", exact=True)
                assert not reference.is_visible()
                detail.locator("details summary").click()
                assert reference.is_visible()
                assert detail.get_by_text("Support · fact", exact=True).count() == 0
                assert detail.get_by_text("Conversation", exact=True).is_visible()
                assert calls[-1][1].endswith("/profile")

                profile_missing = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("No generated User Profile yet", exact=False).wait_for()
                assert summary.get_by_text("Not created", exact=True).is_visible()
                assert summary.locator("time").count() == 0
                assert detail.get_by_text("Last refreshed", exact=True).count() == 0
                assert detail.get_by_text("partial User Profile", exact=False).count() == 0
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert (
                    "Create/Refresh User Profile" in page.locator("#refresh-profile").inner_text()
                )
                assert json.loads(calls[-1][2]) == {}

                profile_indexing = True
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "User Profile inputs are not ready", exact=False
                ).wait_for()
                profile_indexing = False

                profile_missing = False
                profile_failed = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("1 Insight input failed", exact=False).wait_for()
                assert summary.get_by_text("Incomplete", exact=True).is_visible()
                assert detail.get_by_text("partial", exact=True).count() == 0

                profile_failed = False
                profile_blocked = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("Failed initial processing", exact=True).wait_for()
                assert summary.get_by_text("Out of date", exact=True).is_visible()
                assert detail.get_by_text("partial", exact=True).count() == 0

                profile_blocked = False
                profile_overrides.update(
                    profile_revision=4,
                    content=None,
                    updated_at=None,
                    freshness="missing",
                    pending_inputs=1,
                    failed_inputs=1,
                    references=[],
                    omitted=[],
                )
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text(
                    "This stored revision has no User Profile document", exact=False
                ).wait_for()
                assert summary.get_by_text("Needs rebuild", exact=True).is_visible()
                assert summary.get_by_text("Updated time not recorded", exact=True).is_visible()
                assert summary.locator("time").count() == 0
                assert detail.get_by_text("Last refreshed", exact=True).count() == 0
                assert (
                    detail.get_by_text(
                        "This User Profile uses only ready information", exact=False
                    ).count()
                    == 0
                )
                assert "no User Profile document" in page.locator("#status").inner_text()
                assert detail.get_by_text("1 Insight input failed", exact=False).is_visible()

                profile_overrides.update(
                    updated_at="2026-09-29T12:30:00+00:00",
                    freshness="current",
                    failed_inputs=0,
                    pending_inputs=0,
                    omitted=[{"reason": "budget"}],
                )
                page.locator("#profile-budget").fill("0")
                page.locator("#profile-form button").first.click()
                detail.get_by_text(
                    "User Profile exceeds the preview token budget", exact=False
                ).wait_for()
                assert "preview token budget" in page.locator("#status").inner_text()
                assert detail.get_by_text("Refresh it to rebuild", exact=False).count() == 0
                assert summary.get_by_text("Up to date", exact=True).is_visible()
                assert summary.locator("time").is_visible()

                profile_overrides.update(freshness="stale", omitted=[])
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("stored User Profile is unavailable", exact=False).wait_for()
                assert summary.get_by_text("Out of date", exact=True).is_visible()

                profile_overrides.update(
                    content='{"occupation":"Engineer","note":"<script>alert(1)</script>"}',
                    instruction="Return JSON.",
                    freshness="current",
                )
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("Saved instruction", exact=True).wait_for()
                rendered = detail.locator("pre").inner_text()
                assert json.loads(rendered)["occupation"] == "Engineer"
                assert detail.locator("script").count() == 0
                assert page.locator("#status").inner_text() == "User Profile loaded."
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("target_kind", ["memory", "insight"])
def test_unit_removal_preserves_sources_and_invalidates_pending_reads(tmp_path, target_kind):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 9222)):
            pytest.skip("Existing debug Chrome is unavailable")
    with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
        websocket_url = json.load(response)["webSocketDebuggerUrl"]
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []
    memory_present = True
    insight_present = True
    paused_reads = []
    pause_reads = False
    memory = {
        "unit_id": "search-fact-42",
        "memory_id": "mem-42",
        "kind": "fact",
        "origin": "chat",
        "text": "Prefers concise replies.",
        "supports": [],
        "affected_insight_ids": ["insight-7"],
        "citations": [{"source_id": "chat-1", "source_version": 1, "passage_id": "p1"}],
    }
    insight = {
        "unit_id": "search-insight-7-revision-4",
        "insight_id": "insight-7",
        "insight_revision": 4,
        "kind": "insight",
        "subtype": "preferences",
        "origin": "memory",
        "text": "The user prefers brief replies.",
        "supports": [{"memory_id": "mem-42", "support_revision": 3}],
        "affected_insight_ids": [],
        "citations": [],
    }

    def fulfill(route, data):
        route.fulfill(
            status=202 if route.request.method == "DELETE" else 200,
            content_type="application/json",
            body=json.dumps(data),
        )

    def respond(route):
        nonlocal memory_present, insight_present
        path = route.request.url
        calls.append((route.request.method, path, route.request.post_data))
        if path.endswith("/memories"):
            data = {
                "items": ([memory] if memory_present else [])
                + ([insight] if insight_present else []),
                "next_after": None,
            }
            if pause_reads:
                paused_reads.append((route, data))
                return
        elif path.endswith("/recall"):
            paused_reads.append(
                (
                    route,
                    {
                        "items": [memory, insight],
                        "context": "Stale pre-removal context",
                        "context_tokens": 5,
                        "diagnostics": {},
                    },
                )
            )
            return
        elif path.endswith("/evidence/chat-1"):
            data = {
                "original": {
                    "messages": [{"role": "user", "content": "Please keep replies brief."}]
                }
            }
        elif path.endswith("/memories/mem-42") and route.request.method == "DELETE":
            memory_present = False
            insight_present = False
            data = {
                "target_kind": "memory",
                "memory_id": "mem-42",
                "status": "excluded",
                "operation_id": "remove-op",
                "policy_epoch": 8,
                "cleanup_status": "queued",
                "reused": False,
            }
        elif path.endswith("/insights/insight-7") and route.request.method == "DELETE":
            insight_present = False
            data = {
                "target_kind": "insight",
                "insight_id": "insight-7",
                "status": "excluded",
                "operation_id": "remove-insight-op",
                "policy_epoch": 9,
                "cleanup_status": "accepted",
                "reused": False,
            }
        elif path.endswith("/resources"):
            data = {"items": [], "next_after": None}
        else:
            raise AssertionError(f"Unexpected request: {path}")
        fulfill(route, data)

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket_url)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(5000)
            page.add_init_script("""
                const originalFetch = window.fetch;
                window.completedReads = 0;
                window.fetch = async (...args) => {
                    const response = await originalFetch(...args);
                    const originalJson = response.json.bind(response);
                    response.json = async () => {
                        const result = await originalJson();
                        window.completedReads += 1;
                        return result;
                    };
                    return response;
                };
            """)
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.locator("#user").fill("alice")
                page.locator("#connect-form button").click()
                page.locator("#item-list button").nth(0 if target_kind == "memory" else 1).click()
                detail = page.locator("#detail")
                target_id = "mem-42" if target_kind == "memory" else "insight-7"
                action = "Remove Memory Unit" if target_kind == "memory" else "Remove Insight"
                operation_id = "remove-op" if target_kind == "memory" else "remove-insight-op"
                endpoint = "/memories/mem-42" if target_kind == "memory" else "/insights/insight-7"
                detail.get_by_role("heading", name=target_id, exact=True).wait_for()
                assert detail.get_by_text("Canonical ID", exact=True).is_visible()
                if target_kind == "memory":
                    assert detail.get_by_text("insight-7", exact=True).is_visible()
                else:
                    assert detail.get_by_text("4", exact=True).is_visible()
                    assert detail.get_by_text("mem-42", exact=True).is_visible()
                    assert detail.get_by_text("3", exact=True).is_visible()

                page.once("dialog", lambda dialog: dialog.dismiss())
                detail.get_by_role("button", name=action, exact=True).click()
                assert not any(method == "DELETE" for method, _, _ in calls)

                pause_reads = True
                with page.expect_request("**/memories"):
                    page.locator("#refresh-list").click()
                page.locator("#query").fill("brief replies")
                with page.expect_request("**/recall"):
                    page.locator("#search-form button").click()
                page.wait_for_function("window.completedReads >= 2")
                assert len(paused_reads) == 2
                pause_reads = False
                page.once("dialog", lambda dialog: dialog.accept())
                detail.get_by_role("button", name=action, exact=True).click()
                detail.get_by_text(f'"operation_id": "{operation_id}"', exact=False).wait_for()
                receipt = json.loads(detail.locator("pre").inner_text())
                assert receipt["target_kind"] == target_kind
                assert receipt[f"{target_kind}_id"] == target_id
                assert receipt["status"] == "excluded"
                assert receipt["policy_epoch"] == (8 if target_kind == "memory" else 9)
                assert receipt["reused"] is False
                deletes = [path for method, path, _ in calls if method == "DELETE"]
                assert len(deletes) == 1
                assert deletes[0].endswith(endpoint)

                completed = page.evaluate("window.completedReads")
                for route, data in paused_reads:
                    fulfill(route, data)
                page.wait_for_function(f"window.completedReads === {completed + 2}")
                page.evaluate("() => new Promise(requestAnimationFrame)")
                assert json.loads(detail.locator("pre").inner_text()) == receipt
                assert page.locator("#item-list button").count() == (
                    0 if target_kind == "memory" else 1
                )
                if target_kind == "insight":
                    page.locator("#item-list button").first.click()
                    detail.get_by_text("Please keep replies brief.", exact=True).wait_for()
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
