"""Entity graph/drawer and request overview through the actual bundled UI."""

import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest
from test_inspector_ui import WEB, QuietHandler, playwright


def test_entities_and_request_overview(tmp_path):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
            websocket = json.load(response)["webSocketDebuggerUrl"]
    except OSError:
        pytest.skip("Existing debug Chrome is unavailable")
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    Thread(target=server.serve_forever, daemon=True).start()
    fact = {
        "unit_id": "unit-one",
        "memory_id": "one",
        "kind": "fact",
        "origin": "chat",
        "text": "The team welcomed a new colleague.",
        "source_chunks": [
            {
                "source_id": "chat-one",
                "source_version": 1,
                "passage_id": "chunk-one",
                "text": "Original: Alice joined AT during launch. <img src=x onerror=alert(1)>",
                "mentions": ["AT"],
                "quote": "Alice joined AT",
            }
        ],
        "created_at": "2026-11-03T10:00:00Z",
        "mention_time": "2026-10-01T10:00:00Z",
        "citations": [],
        "supports": [],
        "affected_insight_ids": [],
        "entities": [],
        "relationships": [],
    }
    fact["source_chunks"].append(
        {
            "source_id": "chat-two",
            "source_version": 1,
            "passage_id": "chunk-two",
            "text": "😀 Alex introduced another Alex.",
            "mentions": ["Alex"],
            "quote": "Alex introduced another Alex.",
            "mention_spans": [[2, 6]],
        }
    )
    entity = {
        "entity_id": "atlas",
        "name": "Atlas",
        "memory_count": 2,
        "first_mentioned_at": "2026-10-01T10:00:00Z",
        "last_mentioned_at": "2026-11-03T10:00:00Z",
    }
    peer = {**entity, "entity_id": "alice", "name": "Alice"}
    log = {
        "request_id": "req-one",
        "time": "2026-11-03T10:00:00Z",
        "operation": "POST /v1/retain",
        "status": 202,
        "duration_ms": 12.5,
        "operation_id": "op-one",
        "error_code": None,
    }
    queries = []
    held = []
    pause_graph = False

    def respond(route):
        parsed = urlparse(route.request.url)
        q = parse_qs(parsed.query)
        queries.append((parsed.path, q))
        if parsed.path.endswith("/entities/graph"):
            if pause_graph:
                held.append(route)
                return
            data = {
                "nodes": [entity, peer],
                "edges": [{"source": "atlas", "target": "alice", "weight": 2}],
                "total": 2,
                "truncated": False,
                "edges_truncated": False,
            }
        elif parsed.path.endswith("/entities/atlas/memories"):
            data = {
                **entity,
                "original_names": ["Alex"] if q.get("names_after") else ["Atlas"],
                "aliases": ["AT"],
                "names_next_after": None if q.get("names_after") else "more-names",
                "items": [] if q.get("names_after") else [fact],
                "next_after": None,
            }
        elif parsed.path.endswith("/entities"):
            data = {"items": [entity], "next_after": None, "total": 1}
        elif parsed.path.endswith("/request-logs"):
            failed = q.get("errors_only") == ["true"]
            data = {
                "items": [
                    {
                        **log,
                        "request_id": "req-failed",
                        "status": 503,
                        "error_code": "RecallUnavailable",
                    }
                ]
                if failed
                else [{**log, "request_id": "req-next"}]
                if q.get("offset") == ["10"]
                else [log],
                "next_after": None,
                "summary": {"total_calls": 16, "success_rate": 75},
                "scope": {"tenant_id": "default", "user_id": "alice"},
                "total": 1 if failed else 80,
                "limit": 10,
                "offset": int(q.get("offset", ["0"])[0]),
                "next_offset": 10 if not failed and q.get("offset", ["0"])[0] == "0" else None,
            }
        elif parsed.path.endswith("/memories/one"):
            data = fact
        elif parsed.path.endswith("/memories"):
            data = {"items": [fact], "next_after": None}
        elif parsed.path.endswith("/resources"):
            data = {"items": [], "next_after": None}
        else:
            raise AssertionError(f"Unexpected request: {parsed.path}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket)
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_default_timeout(3000)
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.locator("#user").fill("alice")
                page.get_by_role("button", name="Connect", exact=True).click()
                page.get_by_role("button", name="Entities", exact=True).click()
                graph = page.get_by_role("region", name="Entity relationships", exact=True)
                graph.get_by_role("button", name="Atlas", exact=True).wait_for()
                assert graph.locator("line[data-weight='2']").count() == 1
                page.get_by_role("button", name="Pause", exact=True).click()
                assert page.get_by_role("button", name="Resume", exact=True).is_visible()
                before_zoom = graph.locator("svg").get_attribute("viewBox")
                page.get_by_role("button", name="Zoom in", exact=True).click()
                assert graph.locator("svg").get_attribute("viewBox") != before_zoom
                with page.expect_download() as exported:
                    page.get_by_role("button", name="Share", exact=True).click()
                download = exported.value
                assert download.suggested_filename == "entity-relationships.svg"
                assert "Atlas" in download.path().read_text()
                download.delete()
                graph.get_by_role("button", name="Atlas", exact=True).click()
                drawer = page.get_by_role("dialog", name="Entity details", exact=True)
                drawer.get_by_role(
                    "button", name="Open learned memory", exact=True
                ).first.wait_for()
                assert drawer.get_by_text("First seen", exact=True).is_visible()
                assert drawer.get_by_text("Original names", exact=True).is_visible()
                assert drawer.get_by_text("Aliases", exact=True).is_visible()
                assert drawer.locator("p").get_by_text("AT", exact=True).is_visible()
                assert drawer.locator(".entity-source-chunk mark").all_text_contents() == [
                    "AT",
                    "Alex",
                ]
                assert (
                    drawer.locator(".entity-source-chunk").nth(1).inner_text()
                    == "😀 Alex introduced another Alex."
                )
                drawer.get_by_role("button", name="Load more names", exact=True).click()
                drawer.locator("p").get_by_text("Atlas, Alex", exact=True).wait_for()
                assert drawer.locator(".entity-source-chunk").count() == 2
                assert (
                    sum(
                        path.endswith("/entities/atlas/memories") and not q.get("names_after")
                        for path, q in queries
                    )
                    == 1
                )
                assert (
                    "Original: Alice joined AT during launch."
                    in drawer.locator(".entity-source-chunk").first.inner_text()
                )
                assert fact["text"] not in drawer.inner_text()
                assert drawer.locator(".entity-source-chunk img").count() == 0
                assert (
                    "10/1/2026"
                    in drawer.locator(
                        ".entity-timeline-entry > small:first-child"
                    ).first.inner_text()
                )
                drawer.get_by_role("button", name="Close", exact=True).click()
                page.get_by_role("button", name="List", exact=True).click()
                page.get_by_role("columnheader", name="Linked memories", exact=True).wait_for()
                assert page.get_by_role("columnheader", name="First seen", exact=True).is_visible()
                page.get_by_role("button", name="Atlas", exact=True).click()
                drawer.get_by_role("button", name="Open learned memory", exact=True).first.click()
                page.locator("#detail .source-text").get_by_text(
                    fact["text"], exact=True
                ).wait_for()
                assert not drawer.is_visible()
                page.get_by_role("button", name="Request Logs", exact=True).click()
                page.get_by_text("75%", exact=True).wait_for()
                assert page.locator("#log-total-calls").inner_text() == "16"
                assert page.locator(".detail-panel").is_hidden()
                assert page.get_by_role("columnheader", name="Method", exact=True).is_visible()
                assert page.get_by_role("columnheader", name="Path", exact=True).is_visible()
                page.get_by_role("button", name="Next page", exact=True).click()
                page.get_by_role("button", name="req-next", exact=True).wait_for()
                assert any(
                    q.get("offset") == ["10"] and q.get("limit") == ["10"] for _, q in queries
                )
                page.get_by_role("button", name="3", exact=True).click()
                page.get_by_role("button", name="req-one", exact=True).wait_for()
                assert "…" in page.locator("#log-page-buttons").inner_text()
                page.get_by_role("button", name="Error logs", exact=True).click()
                page.get_by_role("button", name="req-failed", exact=True).wait_for()
                assert page.locator("#log-success-rate").inner_text() == "75%"
                assert page.locator(".http-status").get_attribute("title") == "RecallUnavailable"
                page.get_by_label("API type", exact=True).fill("recall")
                page.get_by_label("Status code", exact=True).fill("503")
                page.get_by_label("Request ID", exact=True).fill("req-failed")
                page.get_by_role("button", name="Query", exact=True).click()
                page.get_by_role("button", name="req-failed", exact=True).wait_for()
                assert any(
                    q.get("operation") == ["recall"]
                    and q.get("status_code") == ["503"]
                    and q.get("request_id") == ["req-failed"]
                    for _, q in queries
                )
                page.get_by_role("button", name="Reset", exact=True).click()
                page.get_by_role("button", name="req-one", exact=True).wait_for()
                assert page.locator("#log-request-id").input_value() == ""
                assert page.locator("#log-all").get_attribute("aria-pressed") == "true"
                page.locator("#language").select_option("zh-CN")
                assert page.get_by_role("button", name="所有日志", exact=True).is_visible()
                assert page.get_by_role("button", name="错误日志", exact=True).is_visible()
                assert page.get_by_text("总调用次数", exact=True).is_visible()
                assert page.get_by_text("成功率", exact=True).is_visible()
                page.locator("#language").select_option("en")
                pause_graph = True
                page.get_by_role("button", name="Entities", exact=True).click()
                page.get_by_role("button", name="Relationships", exact=True).click()
                page.get_by_role("button", name="Request Logs", exact=True).click()
                for route in held:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"nodes": [entity], "edges": [], "total": 1}),
                    )
                page.get_by_role("button", name="req-one", exact=True).wait_for()
                assert page.locator(".detail-panel").is_hidden()
                page.get_by_role("button", name="Sign out", exact=True).click()
                assert page.locator("#workspace").is_hidden()
                assert page.locator("#item-list").inner_text() == ""
                assert page.locator("#entity-drawer-title").inner_text() == ""
                assert page.locator("#log-page-summary").inner_text() == ""
                assert page.locator("#log-page-buttons").inner_text() == ""
                assert not errors
            finally:
                context.close()
    finally:
        server.shutdown()
        server.server_close()
