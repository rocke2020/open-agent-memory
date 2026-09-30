"""Browser check for the same-origin Inspector, using the existing debug Chrome."""

import json
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest

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
    calls = []

    def respond(route):
        nonlocal profile_missing, profile_failed, profile_blocked, profile_indexing
        path = route.request.url
        calls.append((route.request.method, path, route.request.post_data))
        if path.endswith("/memories"):
            data = {
                "items": [
                    {
                        "unit_id": "fact-1",
                        "kind": "fact",
                        "origin": "chat",
                        "text": "Prefers concise replies.",
                        "supports": [],
                        "citations": [
                            {"source_id": "chat-1", "source_version": 1, "passage_id": "p1"}
                        ],
                    },
                    {
                        "unit_id": "insight-1",
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
                else "## Main content\n\nThe user is an engineer. Keep personal data local [1]."
                "\n\n## References\n\n1. Conversation chat-1",
                "references": []
                if profile_missing
                else [
                    {
                        "kind": "fact",
                        "support_id": "fact-1",
                        "source_id": "chat-1",
                        "passage_id": "p1",
                    }
                ],
            }
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

                page.locator('[data-view="profiles"]').click()
                detail.get_by_text(
                    "The user is an engineer. Keep personal data local [1].", exact=True
                ).wait_for()
                assert detail.get_by_text("L3 Profile", exact=True).count() == 0
                assert detail.locator("h4").all_inner_texts() == [
                    "Main content",
                    "References",
                ]
                top_level = detail.evaluate(
                    "root => [...root.children].map(el => el.textContent.trim())"
                )
                assert top_level.index(
                    next(text for text in top_level if text.startswith("Main content"))
                ) < next(i for i, text in enumerate(top_level) if text.startswith("Last refreshed"))
                assert detail.get_by_text("Last refreshed", exact=True).is_visible()
                assert (
                    "2026"
                    in detail.get_by_text("Last refreshed", exact=True).locator("..").inner_text()
                )
                assert detail.get_by_text("Freshness", exact=True).count() == 0
                assert detail.get_by_text("Revision", exact=True).count() == 0
                assert detail.get_by_text("Pending inputs", exact=True).count() == 0
                assert top_level[-1].startswith("Reference details")
                assert not detail.locator("details").evaluate("element => element.open")
                assert detail.get_by_text("References", exact=True).count() == 1
                detail.locator("details summary").click()
                assert detail.get_by_text("Support · fact", exact=True).count() == 0
                assert detail.get_by_text("Conversation", exact=True).is_visible()
                assert calls[-1][1].endswith("/profile")

                profile_missing = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("No generated Profile yet", exact=False).wait_for()
                assert detail.get_by_text("Last refreshed", exact=True).count() == 0
                assert detail.get_by_text("partial Profile", exact=False).count() == 0
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert "Create/Refresh Profile" in page.locator("#refresh-profile").inner_text()
                assert json.loads(calls[-1][2]) == {}

                profile_indexing = True
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "Profile inputs are not ready", exact=False
                ).wait_for()
                profile_indexing = False

                profile_missing = False
                profile_failed = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("1 Insight input failed", exact=False).wait_for()
                assert detail.get_by_text("partial", exact=True).count() == 0

                profile_failed = False
                profile_blocked = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("Failed initial processing", exact=True).wait_for()
                assert detail.get_by_text("partial", exact=True).count() == 0
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
