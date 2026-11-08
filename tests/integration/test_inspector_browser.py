"""Use the bundled Inspector against real HTTP and an isolated PostgreSQL database."""

import json
import socket
from threading import Thread
from urllib.request import urlopen

import pytest
import uvicorn
from api_test_app import create_api_only_app
from test_core_http import ROOT, SyntheticIndexModels
from test_core_http import core_database as core_database
from test_memory_removal import INSIGHT, REMOVED_MEMORY, SURVIVING_MEMORY, USER, _content
from test_memory_removal import removal_database as removal_database

pytestmark = pytest.mark.integration
playwright = pytest.importorskip("playwright.sync_api")


def test_browser_reads_insights_connections_and_retrieval_from_public_api(
    removal_database, tmp_path
):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
    except OSError:
        pytest.skip("Existing debug Chrome is unavailable")
    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,%s,%s,%s,'caused_by',1)",
            (database.tenant, USER, REMOVED_MEMORY, SURVIVING_MEMORY),
        )
    app = create_api_only_app(ROOT / "configs/oam.yaml")
    models = SyntheticIndexModels()
    app.state.runtime.models = models
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            with urlopen(address + "/ui/", timeout=10) as response:
                assert "Memory Inspector" in response.read().decode()
            with playwright.sync_playwright() as api:
                browser = api.chromium.connect_over_cdp(websocket_url)
                context = browser.new_context(
                    locale="en-US", viewport={"width": 1440, "height": 1000}
                )
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(address + "/ui/")
                    page.locator("#tenant").fill(database.tenant)
                    page.locator("#user").fill(USER)
                    page.locator("#credential").fill(
                        database.headers["Authorization"].removeprefix("Bearer ")
                    )
                    page.locator("#connect-form button").click()
                    page.get_by_role("button", name="Insights", exact=True).click()
                    page.locator("#item-list .list-item").first.wait_for()
                    assert page.locator("#item-list .list-item").count() == 3
                    page.locator("#item-list .list-item").filter(
                        has_text=f"Synthetic synthesis {INSIGHT} revision 2."
                    ).click()
                    page.get_by_role(
                        "button", name=f"Open supporting memory {REMOVED_MEMORY}", exact=True
                    ).click()
                    connections = page.get_by_role("region", name="Memory connections", exact=True)
                    graph = connections.get_by_role("region", name="Connection map", exact=True)
                    graph.get_by_role(
                        "button", name=f"Open connected memory {SURVIVING_MEMORY}", exact=True
                    ).wait_for()
                    assert graph.locator('path[data-kind="caused_by"][marker-end]').count() == 1
                    sources = page.get_by_role("region", name="Source messages", exact=True)
                    sources.get_by_text("Synthetic learned preferences.", exact=True).wait_for()
                    detail = page.locator("#detail")
                    assert sources.bounding_box()["x"] > detail.bounding_box()["x"]
                    connections.get_by_role("button", name="List", exact=True).click()
                    connections.get_by_text("Caused by", exact=True).wait_for()
                    assert connections.get_by_text(
                        f"Synthetic claim {SURVIVING_MEMORY}.", exact=True
                    ).is_visible()
                    page.screenshot(path=str(tmp_path / "inspector-desktop.png"), full_page=True)
                    connections.get_by_role(
                        "button", name=f"Open memory {SURVIVING_MEMORY}", exact=True
                    ).click()
                    connections.get_by_role("button", name="List", exact=True).click()
                    connections.get_by_text("Led to", exact=True).wait_for()
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.emulate_media(color_scheme="dark")
                    page.locator("#language").select_option("zh-CN")
                    assert page.get_by_role("button", name="检索", exact=True).is_visible()
                    assert page.get_by_role("heading", name="来源消息", exact=True).is_visible()
                    assert (
                        page.locator("#source-panel").bounding_box()["y"]
                        > detail.bounding_box()["y"]
                    )
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(tmp_path / "inspector-mobile.png"), full_page=True)
                    page.locator("#language").select_option("en")
                    page.get_by_role("button", name="Retrieve", exact=True).click()
                    query = f"Synthetic claim {SURVIVING_MEMORY}"
                    page.locator("#query").fill(query)
                    with page.expect_response("**/v1/recall") as recalled:
                        page.locator("#search-form button").click()
                    response = recalled.value
                    assert response.status == 200, response.text()
                    result = response.json()
                    (tmp_path / "recall-result.json").write_text(json.dumps(result, indent=2))
                    assert any(
                        item.get("memory_id") == SURVIVING_MEMORY for item in result["items"]
                    )
                    assert f"Synthetic claim {SURVIVING_MEMORY}." in result["context"]
                    detail.locator("pre").wait_for()
                    assert detail.locator("pre").inner_text() == result["context"]
                    assert sources.is_hidden()
                    assert (query,) in models.embedding_batches
                    page.locator("#item-list .list-item").filter(
                        has_text=f"Synthetic claim {SURVIVING_MEMORY}."
                    ).click()
                    sources.get_by_text("Synthetic learned preferences.", exact=True).wait_for()
                    assert detail.locator(".source-text").inner_text() == (
                        f"Synthetic claim {SURVIVING_MEMORY}."
                    )
                    (tmp_path / "retrieval.json").write_text(
                        json.dumps(result, indent=2), encoding="utf-8"
                    )
                    page.get_by_role("button", name="Profile Data", exact=True).click()
                    page.get_by_role("button", name="Add Profile Data", exact=True).click()
                    page.get_by_label("Profile Data ID", exact=True).fill("browser-profile")
                    content = {
                        "date_of_birth": "1990-05-10",
                        "interests": ["<img src=x onerror=alert(1)>"],
                    }
                    page.get_by_label("Structured values (JSON)", exact=True).fill(
                        json.dumps(content)
                    )
                    page.get_by_label("Source revision", exact=True).fill("owner-v1")
                    page.get_by_label("As of", exact=True).fill("2026-11-01T00:00:00Z")
                    with page.expect_response("**/profile-data") as created:
                        page.get_by_role("button", name="Create Profile Data", exact=True).click()
                    assert created.value.status == 202
                    assert created.value.request.post_data_json["content"] == content
                    page.locator("#item-list .list-item").filter(has_text="browser-profile").click()
                    detail.get_by_text("basic-profile/v1", exact=True).wait_for()
                    detail.get_by_role("button", name="Inspect original", exact=True).click()
                    detail.locator(".original-preview").wait_for()
                    assert "owner-v1" in detail.locator(".original-preview").inner_text()
                    assert detail.locator("img").count() == 0
                    detail.get_by_role("button", name="Revise", exact=True).click()
                    values = page.get_by_label("Structured values (JSON)", exact=True)
                    assert json.loads(values.input_value()) == content
                    assert (
                        page.get_by_label("Source revision", exact=True).input_value() == "owner-v1"
                    )
                    assert (
                        page.get_by_label("As of", exact=True).input_value()
                        == "2026-11-01T00:00:00Z"
                    )
                    values.fill(json.dumps({"date_of_birth": "1991-06-11"}))
                    with page.expect_response("**/profile-data/browser-profile") as revised:
                        page.get_by_role(
                            "button", name="Create immutable revision", exact=True
                        ).click()
                    assert revised.value.status == 202
                    assert revised.value.request.headers["if-match"] == "1"
                    page.locator("#item-list .list-item").filter(has_text="browser-profile").click()
                    detail.get_by_role("button", name="Inspect original", exact=True).click()
                    detail.locator(".original-preview").wait_for()
                    assert "1991-06-11" in detail.locator(".original-preview").inner_text()
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response("**/profile-data/browser-profile") as removed:
                        detail.get_by_role("button", name="Delete Profile Data", exact=True).click()
                    assert removed.value.status == 202
                    assert removed.value.json()["excluded_versions"] == [1, 2]
                    page.locator("#item-list .list-item").filter(
                        has_text="browser-profile"
                    ).wait_for(state="hidden")
                    assert errors == []
                    page.get_by_role("button", name="Sign out", exact=True).click()
                    assert page.locator("#credential").input_value() == ""
                finally:
                    context.close()
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
