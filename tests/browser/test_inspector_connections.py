"""Navigate Insights and stored memory connections in the bundled browser UI."""

import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest
from test_inspector_ui import WEB, QuietHandler, playwright


def test_insights_entities_and_memory_connections(tmp_path):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
    except OSError:
        pytest.skip("Existing debug Chrome is unavailable")
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fact = {
        "unit_id": "unit-one",
        "memory_id": "one",
        "kind": "fact",
        "origin": "chat",
        "text": "Alice joined Atlas after its funding round.",
        "citations": [],
        "supports": [],
        "affected_insight_ids": [],
    }
    peer = {**fact, "unit_id": "unit-two", "memory_id": "two", "text": "Atlas raised funding."}
    insight = {
        **fact,
        "unit_id": "unit-insight",
        "memory_id": None,
        "insight_id": "insight-one",
        "kind": "insight",
        "insight_revision": 2,
        "subtype": "projects",
        "text": "Alice is building Atlas.",
        "supports": [{"memory_id": "one"}],
    }
    detail = {
        **fact,
        "entities": [
            {"entity_id": "alice", "name": "Alice", "mention": "Alice"},
            {"entity_id": "atlas", "name": "Atlas", "mention": "Atlas"},
        ],
        "relationships": [
            {**peer, "memory_kind": "fact", "kind": "caused_by", "direction": "outgoing"},
            {**peer, "memory_kind": "fact", "kind": "updates", "direction": "incoming"},
            {
                **peer,
                "memory_kind": "fact",
                "kind": "temporal",
                "time_basis": "event",
                "gap_seconds": 86400,
                "direction": "undirected",
            },
            {
                **peer,
                "memory_kind": "fact",
                "kind": "shared_entity",
                "entity_id": "atlas",
                "entity_name": "Atlas",
                "direction": "undirected",
            },
        ],
        "relationships_truncated": False,
    }
    pending = []
    for number in range(3, 10):
        detail["relationships"].append(
            {
                "memory_id": str(number),
                "memory_kind": "fact",
                "kind": "semantic",
                "direction": "outgoing",
                "text": f"Related event {number} describes a completed publication and its "
                "verification, including the original source references and project details.",
            }
        )
    pause_detail = False
    pause_trace = False
    pending_traces = []
    retrievals = []

    def respond(route):
        parsed = urlparse(route.request.url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/recall"):
            retrievals.append(json.loads(route.request.post_data))
            data = {
                "items": [fact],
                "context": "Alice joined Atlas [1].",
                "context_tokens": 8,
                "diagnostics": {"partial": True},
                "trace_id": "trace-one",
            }
        elif parsed.path.endswith("/debug/traces/trace-one"):
            if pause_trace:
                pending_traces.append(route)
                return
            data = {"status": "complete", "decisions": {}}
        elif parsed.path.endswith("/memories"):
            items = [insight] if query.get("memory_type") == ["insight"] else [fact, peer]
            data = {"items": items, "next_after": None}
        elif parsed.path.endswith("/memories/one"):
            if pause_detail:
                pending.append(route)
                return
            data = detail
        elif parsed.path.endswith("/memories/two"):
            data = {**peer, "entities": [], "relationships": []}
        elif parsed.path.endswith("/memories/insight-one"):
            data = {**insight, "entities": [], "relationships": []}
        elif parsed.path.endswith("/resources"):
            data = {"items": [], "next_after": None}
        else:
            raise AssertionError(f"Unexpected request: {route.request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as api:
            browser = api.chromium.connect_over_cdp(websocket_url)
            context = browser.new_context()
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_default_timeout(5000)
            page.route("**/v1/**", respond)
            try:
                page.goto(f"http://127.0.0.1:{server.server_port}/ui/")
                page.locator("#user").fill("alice")
                page.locator("#connect-form button").click()
                page.get_by_role("button", name="Insights", exact=True).click()
                page.locator("#item-list .list-item").wait_for()
                assert page.locator("#item-list .list-item").count() == 1
                page.locator("#item-list .list-item").click()
                page.locator("#detail").get_by_text(
                    "Alice is building Atlas.", exact=True
                ).wait_for()
                page.get_by_role("button", name="Open supporting memory one", exact=True).click()
                entities = page.get_by_role("region", name="Entities in this memory", exact=True)
                entities.get_by_role("button", name="Atlas", exact=True).wait_for()
                assert entities.get_by_text("Mentioned together", exact=False).is_visible()
                connections = page.get_by_role("region", name="Memory connections", exact=True)
                graph = connections.get_by_role("region", name="Connection map", exact=True)
                graph.get_by_role("button", name="Open connected memory two", exact=True).wait_for()
                assert graph.get_by_text("This memory", exact=True).is_visible()
                assert graph.locator('path[data-kind="caused_by"][marker-end]').count() == 1
                assert graph.locator('path[data-kind="updates"][marker-end]').count() == 1
                assert graph.locator('path[data-kind="temporal"][marker-end]').count() == 0
                assert (
                    graph.get_by_role("button", name="Open connected memory", exact=False).count()
                    == 6
                )
                boxes = graph.locator(".map-node").evaluate_all(
                    "nodes => nodes.map(node => { const r = node.getBoundingClientRect(); "
                    "return {x:r.x,y:r.y,right:r.right,bottom:r.bottom}; })"
                )
                for index, box in enumerate(boxes):
                    for other in boxes[index + 1 :]:
                        assert not (
                            box["x"] < other["right"]
                            and box["right"] > other["x"]
                            and box["y"] < other["bottom"]
                            and box["bottom"] > other["y"]
                        )
                graph.get_by_role("button", name="Next", exact=True).click()
                assert (
                    graph.get_by_role("button", name="Open connected memory", exact=False).count()
                    == 2
                )
                graph.get_by_role("button", name="Previous", exact=True).click()
                graph.get_by_role("button", name="Open connected memory two", exact=True).click()
                page.locator("#detail").get_by_text("No stored connections.", exact=True).wait_for()
                page.locator("#item-list .list-item").first.click()
                entities.get_by_role("button", name="Atlas", exact=True).wait_for()
                connections.get_by_role("button", name="List", exact=True).click()
                assert connections.get_by_text("Caused by", exact=True).is_visible()
                assert connections.get_by_text("Updated by", exact=True).is_visible()
                assert connections.get_by_text("Event time · 1 day apart", exact=True).is_visible()
                entities.get_by_role("button", name="Atlas", exact=True).click()
                assert connections.locator(".connection-item").count() == 1
                assert connections.get_by_text("Shared entity · Atlas", exact=True).is_visible()
                connections.get_by_role("button", name="All", exact=True).click()
                connections.get_by_role("button", name="Causal", exact=True).click()
                assert connections.locator(".connection-item").count() == 1
                connections.get_by_role("button", name="Open memory two", exact=True).click()
                page.locator("#detail").get_by_text("No stored connections.", exact=True).wait_for()
                page.locator("#item-list .list-item").first.click()
                entities.get_by_role("button", name="Atlas", exact=True).wait_for()
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.emulate_media(color_scheme="dark")
                assert entities.is_visible()
                assert not errors
                page.get_by_role("button", name="Retrieve", exact=True).click()
                page.locator("#query").fill("What connects Alice to Atlas?")
                page.get_by_label("Retrieval mode", exact=True).select_option("mid")
                page.locator("#search-form button").click()
                page.locator("#detail").get_by_text(
                    "Alice joined Atlas [1].", exact=True
                ).wait_for()
                assert retrievals == [
                    {
                        "user_id": "alice",
                        "query": "What connects Alice to Atlas?",
                        "recall_level": "mid",
                        "allow_partial": True,
                    }
                ]
                assert page.locator("#detail").get_by_text("Partial", exact=True).is_visible()
                pause_trace = True
                with page.expect_request("**/debug/traces/trace-one"):
                    page.get_by_role("button", name="Explain this search", exact=True).click()
                page.locator("#item-list .list-item").first.click()
                entities.get_by_role("button", name="Atlas", exact=True).wait_for()
                with page.expect_response("**/debug/traces/trace-one") as traced:
                    pending_traces[0].fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "complete", "decisions": {}}),
                    )
                traced.value.finished()
                page.evaluate("() => new Promise(requestAnimationFrame)")
                assert entities.get_by_role("button", name="Atlas", exact=True).is_visible()
                page.get_by_role("button", name="Retrieve", exact=True).click()
                page.locator("#detail").get_by_text(
                    "Enter a query to retrieve cited evidence.", exact=False
                ).wait_for()
                assert page.locator("#item-list .list-item").count() == 0
                page.get_by_role("button", name="Memories", exact=True).click()
                page.locator("#item-list .list-item").first.wait_for()
                pause_detail = True
                with page.expect_request("**/memories/one?memory_type=basic"):
                    page.locator("#item-list .list-item").first.click()
                page.get_by_role("button", name="Insights", exact=True).click()
                pending[0].fulfill(
                    status=200, content_type="application/json", body=json.dumps(detail)
                )
                page.locator("#list-title").get_by_text("Insights", exact=True).wait_for()
                assert entities.count() == 0
                page.get_by_role("button", name="Sign out", exact=True).click()
                assert page.locator("#detail").get_by_text("Alice", exact=False).count() == 0
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
