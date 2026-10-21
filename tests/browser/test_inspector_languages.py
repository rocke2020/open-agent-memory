"""Language selection through the bundled Inspector in the existing debug Chrome."""

import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

import pytest
from test_inspector_ui import WEB, QuietHandler, playwright

from oam.contracts.retain import RecallRequest

LANGUAGES = [
    ("en", "English", "Memories"),
    ("es", "Español", "Recuerdos"),
    ("fr", "Français", "Souvenirs"),
    ("de", "Deutsch", "Erinnerungen"),
    ("pt", "Português", "Memórias"),
    ("ja", "日本語", "メモリ"),
    ("ko", "한국어", "메모리"),
    ("zh-CN", "简体中文", "记忆"),
    ("zh-TW", "繁體中文", "記憶"),
]


@pytest.fixture
def inspector_browser(tmp_path):
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
    except OSError:
        pytest.skip("Existing debug Chrome is unavailable")
    (tmp_path / "ui").symlink_to(WEB, target_is_directory=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.connect_over_cdp(websocket_url)
            yield browser, f"http://127.0.0.1:{server.server_port}/ui/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_languages_preserve_live_content_and_selection(inspector_browser):
    browser, url = inspector_browser
    fact = {
        "unit_id": "unit-one",
        "memory_id": "one",
        "kind": "fact",
        "origin": "chat",
        "text": "Original user content <b>stays unchanged</b>.",
        "citations": [],
        "supports": [],
        "entities": [],
        "relationships": [],
    }
    calls = []
    credentials = []

    def respond(route):
        calls.append(route.request.url)
        credentials.append(route.request.headers.get("authorization"))
        if route.request.url.endswith("/recall"):
            request = RecallRequest.model_validate_json(route.request.post_data)
            assert request.query == "Original query stays unchanged"
            data = {
                "items": [fact],
                "context": "Original cited evidence [1].",
                "context_tokens": 8,
                "diagnostics": {"partial": True},
            }
        elif "/memories/one" in route.request.url:
            data = fact
        elif "/memories" in route.request.url:
            data = {"items": [fact], "next_after": None}
        elif "/resources" in route.request.url:
            data = {"items": [], "next_after": None}
        else:
            raise AssertionError(f"Unexpected request: {route.request.url}")
        route.fulfill(content_type="application/json", body=json.dumps(data))

    context = browser.new_context(locale="en-US", viewport={"width": 320, "height": 844})
    try:
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/v1/**", respond)
        page.goto(url)
        assert page.locator("#language option").all_text_contents() == [
            name for _, name, _ in LANGUAGES
        ]
        page.locator("#user").fill("alice")
        page.locator("#credential").fill("synthetic-local-only")
        page.locator("#connect-form button").click()
        page.locator("#item-list .list-item").first.click()
        page.locator("#detail").get_by_text(fact["text"], exact=True).wait_for()
        page.locator("#query").fill("Original query stays unchanged")
        request_count = len(calls)
        for locale, _, memories in LANGUAGES:
            page.locator("#language").select_option(locale)
            assert page.locator("html").get_attribute("lang") == locale
            assert page.locator('[data-view="memories"]').inner_text() == memories
            assert page.locator("#query").input_value() == "Original query stays unchanged"
            assert page.locator("#detail").get_by_text(fact["text"], exact=True).is_visible()
            assert page.locator("#credential").input_value() == ""
            assert len(calls) == request_count
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.evaluate("""() => {
                const title = document.querySelector('.site-brand h1').getBoundingClientRect();
                const controls = document.querySelector('.header-tools').getBoundingClientRect();
                return title.right <= controls.left || title.bottom <= controls.top;
            }""")
        page.locator("#language").select_option("en")
        page.locator('[data-view="retrieve"]').click()
        page.locator("#recall-level").select_option("normal")
        page.locator("#search-form button").click()
        page.locator("#detail").get_by_text("Original cited evidence [1].", exact=True).wait_for()
        assert credentials and all(value == "Bearer synthetic-local-only" for value in credentials)
        for locale, _, _ in LANGUAGES:
            page.locator("#language").select_option(locale)
            assert (
                page.locator("#detail")
                .get_by_text("Original cited evidence [1].", exact=True)
                .is_visible()
            )
            assert page.locator("#recall-level").input_value() == "normal"
            assert page.locator("#detail").get_by_text("8", exact=True).is_visible()
        page.reload()
        assert page.locator("#language").input_value() == "zh-TW"
        assert page.locator("html").get_attribute("lang") == "zh-TW"
        assert page.locator("#credential").input_value() == ""
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("preferences", "saved", "expected"),
    [
        (["es-MX"], None, "es"),
        (["fr-CA"], None, "fr"),
        (["de-AT"], None, "de"),
        (["pt-PT"], None, "pt"),
        (["ja-JP"], None, "ja"),
        (["ko-KR"], None, "ko"),
        (["zh-Hant-HK"], None, "zh-TW"),
        (["zh-HK"], None, "zh-TW"),
        (["zh-Hans-TW"], None, "zh-CN"),
        (["nl-NL", "fr-CA", "en-US"], "obsolete", "fr"),
        (["en-US"], "zh-TW", "zh-TW"),
        (["nl-NL"], None, "en"),
    ],
)
def test_language_preference_resolution(inspector_browser, preferences, saved, expected):
    browser, url = inspector_browser
    context = browser.new_context()
    try:
        context.add_init_script(
            "Object.defineProperty(navigator, 'languages', {value: "
            + json.dumps(preferences)
            + "});"
            + (
                "localStorage.setItem('oam-inspector-language', " + json.dumps(saved) + ");"
                if saved
                else "localStorage.removeItem('oam-inspector-language');"
            )
        )
        page = context.new_page()
        page.goto(url)
        assert page.locator("#language").input_value() == expected
        assert page.locator("html").get_attribute("lang") == expected
    finally:
        context.close()
