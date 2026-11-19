"""The header follows the selected collection without borrowing another date."""

import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest
from test_inspector_ui import WEB, QuietHandler

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("has_dates", [True, False])
def test_header_uses_collection_time_across_navigation_search_and_language(tmp_path, has_dates):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
            websocket = json.load(response)["webSocketDebuggerUrl"]
    except OSError:
        pytest.skip("Existing debug Chrome is unavailable")
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    times = {
        "memories": "2026-11-10T10:00:00+00:00",
        "insights": "2026-11-11T11:00:00+00:00",
        "resources": "2026-11-12T12:00:00+00:00",
        "profiles": "2026-11-13T13:00:00+00:00",
    }
    profile_headers = []
    calls = []

    def respond(route):
        url = urlparse(route.request.url)
        calls.append(url.path)
        if url.path.endswith("/profile"):
            if "max_tokens" in parse_qs(url.query):
                profile_headers.append(route)
                return
            data = {"profile_revision": 1, "updated_at": times["profiles"]}
        elif url.path.endswith("/memories"):
            collection = "insights" if "memory_type" in parse_qs(url.query) else "memories"
            data = {"items": [], "updated_at": times[collection] if has_dates else None}
        elif url.path.endswith("/resources"):
            data = {"items": [], "updated_at": times["resources"] if has_dates else None}
        elif url.path.endswith("/recall"):
            data = {"items": [], "context": "Search context", "context_tokens": 2}
        else:
            raise AssertionError(f"Unexpected request: {url.path}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket)
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.set_default_timeout(3000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.locator("#user").fill("alice")
                page.locator("#connect-form button").click()
                page.locator("#workspace").wait_for()
                header = page.locator("#collection-updated")
                if has_dates:
                    playwright.expect(header.locator("time")).to_have_attribute(
                        "datetime", times["memories"]
                    )
                    assert "Memory updated" in header.inner_text()
                else:
                    assert header.is_hidden()
                assert page.locator('[data-view="profile-data"]').count() == 0
                # A newer Profile read must win over its older header prefetch.
                page.locator('[data-view="profiles"]').click()
                playwright.expect(header.locator("time")).to_have_attribute(
                    "datetime", times["profiles"]
                )
                with page.expect_response("**/profile?max_tokens=0"):
                    profile_headers.pop().fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"profile_revision": None, "updated_at": None}),
                    )
                playwright.expect(header.locator("time")).to_have_attribute(
                    "datetime", times["profiles"]
                )
                for view, label in [
                    ("insights", "Insight updated"),
                    ("resources", "Resource updated"),
                    ("profiles", "Profile updated"),
                    ("retrieve", "Memory updated"),
                    ("memories", "Memory updated"),
                ]:
                    page.locator(f'[data-view="{view}"]').click()
                    if has_dates or view == "profiles":
                        expected = times["memories" if view == "retrieve" else view]
                        playwright.expect(header.locator("time")).to_have_attribute(
                            "datetime", expected
                        )
                        assert label in header.inner_text()
                    else:
                        playwright.expect(header).to_be_hidden()
                page.locator("#query").fill("A search must not change collection time")
                page.locator("#search-form button").click()
                page.get_by_text("Search context", exact=True).wait_for()
                if has_dates:
                    assert header.locator("time").get_attribute("datetime") == times["memories"]
                    page.locator("#language").select_option("zh-CN")
                    assert "记忆更新于" in header.inner_text()
                    assert header.locator("time").get_attribute("datetime") == times["memories"]
                else:
                    assert header.is_hidden()
                assert not any("/profile-data" in path for path in calls)
                assert errors == []
                page.locator("#sign-out").click()
                assert header.is_hidden()
                assert header.inner_text() == ""
            finally:
                context.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
