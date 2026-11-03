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
from oam.profiles.prompt import PROFILE_SYSTEM

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
    resource = None
    resource_version = 0
    pause_action = False
    paused_actions = []
    pause_resource_list = False
    paused_resource_lists = []

    def respond(route):
        nonlocal \
            profile_missing, \
            profile_failed, \
            profile_blocked, \
            profile_indexing, \
            resource, \
            resource_version
        path = route.request.url
        calls.append((route.request.method, path, route.request.post_data))
        memory_id = None
        if "/memories/" in path and route.request.method == "GET":
            path, memory_id = path.split("/memories/", 1)
            memory_id = memory_id.split("?", 1)[0]
            path += "/memories"
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
        elif "/resources" in path:
            if route.request.method in {"POST", "PUT"}:
                if route.request.method == "PUT":
                    assert route.request.headers["if-match"] == str(resource_version)
                resource = json.loads(route.request.post_data)
                resource_version += 1
                data = {"revision": resource_version, "operation_id": "resource-op"}
            elif path.endswith("/original"):
                data = resource
            elif path.endswith("/resources"):
                data = {
                    "items": [
                        {
                            "resource_id": "report",
                            "media_type": "text/html",
                            "active_version": resource_version,
                        }
                    ]
                    if resource
                    else [],
                    "next_after": None,
                }
                if pause_resource_list:
                    paused_resource_lists.append((route, data))
                    return
            else:
                data = {
                    "resource_id": "report",
                    "media_type": "text/html",
                    "active_version": resource_version,
                    "abstract_status": "ready",
                    "abstract": resource["text"],
                }
        elif path.endswith("/evidence/chat-1"):
            data = {
                "original": {
                    "messages": [{"role": "user", "content": "Please keep replies brief."}]
                }
            }
            if pause_action:
                paused_actions.append((route, data))
                return
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
                "default_prompt": PROFILE_SYSTEM,
                "instruction": None,
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
                    },
                    {"kind": "profile_data", "profile_data_id": "basic"},
                ],
            }
            data.update(profile_overrides)
        else:
            raise AssertionError(f"Unexpected request: {path}")
        if memory_id:
            data = next(
                item
                for item in data["items"]
                if item.get("memory_id", item.get("insight_id")) == memory_id
            )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket_url)
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.clock.set_fixed_time("2026-10-15T12:30:00Z")
                header = page.locator(".site-header")
                assert header.bounding_box()["height"] <= 72
                repository = header.get_by_role("link", name="GitHub", exact=False)
                assert repository.get_attribute("href") == (
                    "https://github.com/rocke2020/open-agent-memory"
                )
                assert repository.locator("svg").count() == 1
                page.locator("#user").fill("alice")
                page.locator("#connect-form button").click()
                navigation = page.get_by_role("navigation", name="Inspector views")
                navigation.wait_for()
                assert (
                    navigation.bounding_box()["x"]
                    < page.locator("#workspace-grid").bounding_box()["x"]
                )
                assert navigation.bounding_box()["width"] <= 190
                assert header.bounding_box()["height"] <= 72
                assert header.locator("#scope-label").inner_text() == "default / alice"
                assert (
                    navigation.get_by_role("button", name="Profile Data", exact=True).count() == 0
                )
                assert page.get_by_role("button", name="Create export", exact=True).count() == 0
                page.locator("#item-list button").first.click()
                detail = page.locator("#detail")
                sources = page.get_by_role("region", name="Source messages", exact=True)
                sources.get_by_text("Please keep replies brief.").wait_for()
                assert detail.get_by_text("Prefers concise replies.").is_visible()
                assert detail.get_by_text("Please keep replies brief.").count() == 0
                assert sources.bounding_box()["x"] > detail.bounding_box()["x"]
                # The source column must not squeeze short headings/actions into fragments.
                assert page.locator("#list-title").bounding_box()["height"] < 45
                assert page.locator("#refresh-list").bounding_box()["height"] < 55
                assert page.locator("#search-form button").bounding_box()["height"] < 55
                page.set_viewport_size({"width": 390, "height": 844})
                assert sources.bounding_box()["y"] > detail.bounding_box()["y"]
                assert header.bounding_box()["height"] <= 112
                assert navigation.bounding_box()["width"] >= 300
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.set_viewport_size({"width": 1440, "height": 1000})
                assert len([call for call in calls if "/evidence/chat-1" in call[1]]) == 1
                assert "insight · preferences" in page.locator("#item-list").inner_text()
                pause_action = True
                pending_path = "**/evidence/chat-1"
                with page.expect_request(pending_path):
                    page.get_by_role(
                        "button",
                        name="Inspect source",
                        exact=True,
                    ).click()
                page.locator("#item-list button").nth(1).click()
                detail.get_by_text("Subtype", exact=True).wait_for()
                paused_route, payload = paused_actions[0]
                with page.expect_response(pending_path) as pending_response:
                    paused_route.fulfill(
                        status=200, content_type="application/json", body=json.dumps(payload)
                    )
                pending_response.value.finished()
                page.evaluate("() => new Promise(requestAnimationFrame)")
                assert detail.get_by_role("heading", name="insight-1", exact=True).is_visible()
                pause_action = False
                assert detail.get_by_text("Subtype", exact=True).is_visible()
                assert detail.get_by_text("preferences", exact=True).is_visible()
                sources.get_by_text("No source messages available.", exact=True).wait_for()
                assert sources.get_by_text("Please keep replies brief.").count() == 0

                page.locator("#query").fill("brief replies")
                page.locator("#search-form button").click()
                detail.get_by_text("Current search context", exact=True).wait_for()
                assert sources.is_hidden()
                assert "Please keep replies brief." in detail.locator("pre").inner_text()
                assert not any("/profile" in call[1] for call in calls)
                assert "include_profile" not in json.loads(calls[-1][2])

                pause_resource_list = True
                page.locator('[data-view="resources"]').click()
                page.get_by_role("button", name="Add Resource", exact=True).click()
                assert page.get_by_label("Resource type", exact=True).count() == 0
                assert page.get_by_label("Date of birth", exact=True).count() == 0
                page.get_by_label("Resource ID", exact=True).fill("report")
                page.get_by_label("Original text", exact=True).fill("The first report.")
                pending_list, empty_list = paused_resource_lists.pop()
                pause_resource_list = False
                with page.expect_response("**/resources"):
                    pending_list.fulfill(
                        status=200, content_type="application/json", body=json.dumps(empty_list)
                    )
                assert page.locator(".resource-form").is_visible()
                page.locator("#language").select_option("zh-CN")
                assert page.locator(".resource-form input[required]").input_value() == "report"
                assert page.locator(".resource-form textarea").input_value() == "The first report."
                page.locator("#language").select_option("en")
                page.get_by_role("button", name="Create Resource", exact=True).click()
                page.locator("#item-list").get_by_role("button", name="report", exact=False).click()
                detail.get_by_text("The first report.", exact=True).wait_for()
                detail.get_by_role("button", name="Revise", exact=True).click()
                page.get_by_label("Original text", exact=True).fill("The revised report.")
                page.get_by_role("button", name="Create immutable revision", exact=True).click()
                page.locator("#item-list").get_by_role("button", name="report", exact=False).click()
                detail.get_by_text("The revised report.", exact=True).wait_for()
                writes = [
                    call for call in calls if call[0] in {"POST", "PUT"} and "/resources" in call[1]
                ]
                assert [call[0] for call in writes] == ["POST", "PUT"]
                assert json.loads(writes[-1][2]) == {
                    "input_type": "document",
                    "document_id": "report",
                    "user_id": "alice",
                    "update_mode": "replace",
                    "text": "The revised report.",
                }

                page.locator('[data-view="profiles"]').click()
                detail.locator(".profile-content").wait_for()
                assert page.locator("#profile-budget").count() == 0
                assert page.locator("#profile-form input[type=checkbox]").count() == 0
                assert page.locator(".list-panel").is_hidden()
                customization = page.locator("#profile-customization")
                assert not customization.evaluate("element => element.open")
                assert page.locator("#profile-instruction").is_hidden()
                assert page.get_by_role("button", name="Refresh profile", exact=True).is_visible()
                refresh_mode = page.get_by_role("combobox", name="Profile refresh mode", exact=True)
                assert refresh_mode.input_value() == "incremental"
                assert refresh_mode.locator("option").evaluate_all(
                    "options => options.map(option => option.value)"
                ) == ["incremental", "full"]
                customization.get_by_text("Customize profile", exact=True).click()
                prompt_details = page.locator("#profile-prompt-details")
                prompt = page.locator("#profile-default-prompt")
                instruction_status = page.locator("#profile-instruction-status")
                assert "no saved custom instruction" in instruction_status.inner_text()
                assert page.get_by_role("button", name="Use default instructions").is_hidden()
                assert not prompt_details.evaluate("element => element.open")
                assert prompt.is_hidden()
                prompt_details.get_by_text("Default profile prompt", exact=True).click()
                assert prompt.is_visible()
                assert prompt.inner_text() == PROFILE_SYSTEM
                assert prompt.locator("textarea, input").count() == 0
                prompt_details.get_by_text("Default profile prompt", exact=True).click()
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
                assert top_level[0].startswith("Up to date")
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
                assert detail.get_by_text("[2] Provided profile details", exact=True).is_visible()
                assert detail.get_by_text("basic", exact=True).is_visible()
                assert detail.get_by_role("button", name="Inspect Profile Data").count() == 0
                assert not any("/profile-data" in call[1] for call in calls)
                assert calls[-1][1].endswith("/profile")

                page.locator("#profile-instruction").fill("Return JSON.")
                customization.get_by_text("Customize profile", exact=True).click()
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert json.loads(calls[-1][2]) == {"mode": "incremental"}
                page.locator('[data-view="profiles"]').click()
                detail.locator(".profile-content").wait_for()
                customization.get_by_text("Customize profile", exact=True).click()
                assert page.locator("#profile-instruction").input_value() == "Return JSON."
                refresh_mode.select_option("full")
                page.get_by_role("button", name="Apply and refresh", exact=True).click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert json.loads(calls[-1][2]) == {"instruction": "Return JSON.", "mode": "full"}
                refresh_mode.select_option("incremental")
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert json.loads(calls[-1][2]) == {"mode": "incremental"}
                customization.get_by_text("Customize profile", exact=True).click()

                profile_overrides.update(freshness="uncovered", uncovered_inputs=115)
                page.locator('[data-view="profiles"]').click()
                generation_details = detail.locator("details").filter(
                    has=page.get_by_text("Generation details", exact=True)
                )
                generation_details.wait_for()
                assert detail.locator(".profile-content").is_visible()
                assert "115" not in detail.inner_text()
                assert "document may miss details" not in detail.inner_text().lower()
                assert not generation_details.evaluate("element => element.open")
                generation_details.locator("summary").click()
                assert generation_details.get_by_text("115", exact=True).is_visible()
                assert generation_details.get_by_text(
                    "Insights outside this summary", exact=True
                ).is_visible()
                profile_overrides.clear()

                profile_missing = True
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text("No generated User Profile yet", exact=False).wait_for()
                assert prompt.text_content() == PROFILE_SYSTEM
                assert "no saved custom instruction" in instruction_status.text_content()
                assert summary.get_by_text("Not created", exact=True).is_visible()
                assert summary.locator("time").count() == 0
                assert detail.get_by_text("Last refreshed", exact=True).count() == 0
                assert detail.get_by_text("partial User Profile", exact=False).count() == 0
                page.locator("#refresh-profile").click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert "Create profile" in page.locator("#refresh-profile").inner_text()
                assert json.loads(calls[-1][2]) == {"mode": "incremental"}

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
                assert summary.get_by_text("Some inputs unavailable", exact=True).is_visible()
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
                page.locator('[data-view="profiles"]').click()
                detail.get_by_text(
                    "User Profile exceeds the server's display limit", exact=False
                ).wait_for()
                assert "server's display limit" in page.locator("#status").inner_text()
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
                detail.locator("pre.source-text").wait_for()
                customization.get_by_text("Customize profile", exact=True).click()
                assert "Saved custom instruction active" in instruction_status.inner_text()
                assert "supplements the default prompt" in instruction_status.inner_text()
                assert prompt.text_content() == PROFILE_SYSTEM
                rendered = detail.locator("pre").inner_text()
                assert json.loads(rendered)["occupation"] == "Engineer"
                assert detail.locator("script").count() == 0
                assert page.locator("#status").inner_text() == "User Profile loaded."
                page.get_by_role("button", name="Use default instructions", exact=True).click()
                page.locator("#status").get_by_text(
                    "creation/refresh pending", exact=False
                ).wait_for()
                assert json.loads(calls[-1][2]) == {
                    "reset_instruction": True,
                    "mode": "incremental",
                }
                assert page.locator("#profile-instruction").input_value() == ""
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('[data-view="profiles"]').click()
                detail.locator("pre.source-text").wait_for()
                assert detail.bounding_box()["y"] < 844
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.set_viewport_size({"width": 320, "height": 844})
                profile_heading = page.locator(".detail-panel .panel-heading")
                refresh_bounds = page.locator("#refresh-profile").bounding_box()
                heading_bounds = profile_heading.bounding_box()
                assert refresh_bounds["x"] + refresh_bounds["width"] <= (
                    heading_bounds["x"] + heading_bounds["width"] + 1
                )
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="Sign out", exact=True).click()
                assert page.locator("#account").is_hidden()
                assert page.locator("#scope-label").text_content() == ""
                assert prompt.text_content() == ""
                assert page.locator("#profile-instruction-status").text_content() == ""
                assert not customization.evaluate("element => element.open")
                page.locator("#language").select_option("zh-CN")
                page.reload()
                assert page.locator("html").get_attribute("lang") == "zh-CN"
                assert page.locator("#language").input_value() == "zh-CN"
                page.locator("#language").select_option("en")
                page.reload()
                assert page.locator("html").get_attribute("lang") == "en"
                assert page.locator("#language").input_value() == "en"
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("target_kind", ["memory", "insight"])
@pytest.mark.parametrize("navigate_while_removing", [False, True])
def test_unit_removal_preserves_sources_and_invalidates_pending_reads(
    tmp_path, target_kind, navigate_while_removing
):
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
    paused_deletes = []
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
        elif "/memories/" in path and route.request.method == "GET":
            data = insight if "/memories/insight-7?" in path else memory
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
        if navigate_while_removing and route.request.method == "DELETE":
            paused_deletes.append((route, data))
            return
        fulfill(route, data)

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket_url)
            context = browser.new_context(locale="en-US")
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
                with page.expect_request("**" + endpoint):
                    detail.get_by_role("button", name=action, exact=True).click()
                if navigate_while_removing:
                    page.get_by_role("button", name="Retrieve", exact=True).click()
                    fulfill(*paused_deletes[0])
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
                assert not any(path.endswith("/retrieve") for _, path, _ in calls)

                completed = page.evaluate("window.completedReads")
                for route, data in paused_reads:
                    fulfill(route, data)
                page.wait_for_function(f"window.completedReads === {completed + 2}")
                page.evaluate("() => new Promise(requestAnimationFrame)")
                assert json.loads(detail.locator("pre").inner_text()) == receipt
                assert page.locator("#item-list button").count() == (
                    0 if target_kind == "memory" or navigate_while_removing else 1
                )
                if target_kind == "insight" and not navigate_while_removing:
                    page.locator("#item-list button").first.click()
                    page.get_by_role("region", name="Source messages", exact=True).get_by_text(
                        "Please keep replies brief.", exact=True
                    ).wait_for()
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
