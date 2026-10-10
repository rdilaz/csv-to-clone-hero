"""V2 recovery uses real templates/browser code and isolated provider fixtures."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import public_server, playwright, _assert_no_overflow, _capture

pytestmark = pytest.mark.browser


def _browser(runtime):
    try:
        return runtime.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_v2_brand_and_assets_identify_the_new_design(public_server, width):
    _app, base_url, _name, _cookie = public_server
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.goto(base_url + "/", wait_until="networkidle")
            assert page.locator("body").get_attribute("data-riff-release") == "riff-v2-20261008"
            assert page.locator(".release-badge").inner_text() == "V2"
            assert page.locator(".brand__wordmark").is_visible()
            assert page.locator("body").get_attribute("data-riff-ui") == "riff-ux-20261010"
            for stylesheet in ("public.css", "studio.css"):
                asset = page.locator(f'link[rel="stylesheet"][href*="/{stylesheet}"]')
                playwright.expect(asset).to_have_count(1)
                assert "v=riff-ux-20261010" in asset.get_attribute("href")
            assert "v=riff-ux-20261010" in page.locator('script[src*="public.js"]').get_attribute("src")
            _assert_no_overflow(page)
            if width in {390, 1440}:
                _capture(page, f"v2-landing-{width}.png")
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_v2_busy_readiness_keeps_choices_and_retries_to_real_review(public_server, width):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("add")
    busy = {"value": True}
    calls = []

    def route_api(route):
        request = route.request
        path = urlsplit(request.url).path
        calls.append((request.method, path))
        if path == "/api/preflight" and busy["value"]:
            status = 503 if request.method == "POST" else 200
            payload = {"preflight": {"usable": False, "mode": "live_fallback", "status": "busy", "busy": True, "retryable": True, "message": "The chart service is busy. Your music choices are kept. Try again in a moment."}}
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        else:
            fixture.fulfill(route)

    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": width, "height": 900})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#findChartsButton").wait_for(state="visible")
            page.locator("#includeLiked").uncheck()
            page.locator('.spotify-playlist[value="playlist-road"]').uncheck()
            page.locator('.spotify-playlist[value="playlist-heavy"]').check()
            original_scope = {"include_liked": False, "playlist_ids": ["playlist-heavy"]}
            page.locator("#findChartsButton").click()
            page.locator("#searchRecovery").wait_for(state="visible")
            assert "busy" in page.locator("#searchRecoveryMessage").inner_text()
            assert page.locator("#stageConfirm").is_hidden()
            assert page.locator("#progressDisc").is_hidden()
            assert not any(method == "POST" and path == "/api/jobs" for method, path in calls)
            assert fixture.spotify_scope == original_scope
            _assert_no_overflow(page)
            _capture(page, f"v2-search-recovery-{width}.png")
            busy["value"] = False
            page.locator("#retrySearchButton").click()
            page.locator("#stageConfirm").wait_for(state="visible", timeout=15000)
            assert page.locator("#reviewCards .candidate-row").count() > 0
            assert fixture.spotify_scope == original_scope
            assert not fixture.unexpected_requests
        finally:
            browser.close()


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "partial"])
def test_v2_terminal_failure_never_labels_old_results_as_current(public_server, status):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("confirm")
    calls = []

    def route_api(route):
        request = route.request
        path = urlsplit(request.url).path
        calls.append(path)
        if path == "/api/journey":
            payload = fixture.journey()
            payload["job"] = {"id": 99, "kind": "discovery", "status": status, "result": {}, "error": "internal provider detail"}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        else:
            fixture.fulfill(route)

    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#searchRecovery").wait_for(state="visible")
            assert page.locator("#stageConfirm").is_hidden()
            assert "/api/workspace" not in calls
            assert "internal provider detail" not in page.locator("body").inner_text()
            assert page.locator("#savedSearchResultsButton").is_visible()
            page.locator("#savedSearchResultsButton").click()
            page.locator("#stageConfirm").wait_for(state="visible")
            playwright.expect(page.locator("#appError")).to_contain_text("earlier search")
            assert page.locator("#reviewCards .candidate-row").count() > 0
        finally:
            browser.close()


def test_v2_polled_search_failure_stops_before_confirm(public_server):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("find")

    def route_api(route):
        request = route.request
        if urlsplit(request.url).path == "/api/jobs/41":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"job": {"id": 41, "kind": "discovery", "status": "failed", "result": {}, "error": "private path"}}))
        else:
            fixture.fulfill(route)

    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#searchRecovery").wait_for(state="visible")
            assert page.locator("#stageConfirm").is_hidden()
            assert page.locator("#cancelJobButton").is_hidden()
            assert "private path" not in page.locator("body").inner_text()
            _assert_no_overflow(page)
        finally:
            browser.close()

@pytest.mark.parametrize("result", [
    {"partial": True},
    {"query_errors": 1},
    {"failed_queries": 1},
    {"yield_histogram": {"incomplete_evidence": 1, "recommended_strong": 1}},
])
def test_v2_reload_preserves_incomplete_search_warning(public_server, result):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("confirm")

    def route_api(route):
        request = route.request
        if urlsplit(request.url).path == "/api/journey":
            payload = fixture.journey()
            payload["job"]["result"] = result
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        else:
            fixture.fulfill(route)

    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#stageConfirm").wait_for(state="visible")
            playwright.expect(page.locator("#appError")).to_contain_text("Some songs could not be checked")
            assert page.locator("#reviewCards .candidate-row").count() > 0
        finally:
            browser.close()


def test_v2_stalled_response_body_times_out_without_posting_a_probe(public_server):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("add")
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            context.add_init_script("""(() => {
                const fetchOriginal = window.fetch.bind(window);
                const timeoutOriginal = window.setTimeout.bind(window);
                window.setTimeout = (fn, ms, ...args) => timeoutOriginal(fn, ms === 15000 ? 100 : ms, ...args);
                window.fetch = (url, options) => {
                    if (url === '/api/preflight' && !options.method) {
                        return Promise.resolve(new Response(new ReadableStream({
                            start(controller) {
                                controller.enqueue(new TextEncoder().encode('{"preflight":'));
                                options.signal.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')));
                            }
                        }), {status: 200, headers: {'Content-Type': 'application/json'}}));
                    }
                    return fetchOriginal(url, options);
                };
            })();""")
            page = context.new_page()
            page.route("**/api/**", fixture.fulfill)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#findChartsButton").wait_for(state="visible")
            page.locator("#findChartsButton").click()
            page.locator("#searchRecovery").wait_for(state="visible")
            assert "too long to respond" in page.locator("#searchRecoveryMessage").inner_text()
            assert not any(method == "POST" and path in {"/api/preflight", "/api/jobs"} for method, path, _body in fixture.request_log)
            assert page.locator("#stageConfirm").is_hidden()
        finally:
            browser.close()


@pytest.mark.parametrize("result", [
    {"total_queries": 4, "failed_queries": 4},
    {"yield_histogram": {"incomplete_evidence": 4, "recommended_strong": 0}},
])
def test_v2_all_failed_checks_require_explicit_access_to_older_results(public_server, result):
    _app, base_url, name, cookie = public_server
    fixture = SignalCheckFixtures("confirm")

    def route_api(route):
        if urlsplit(route.request.url).path == "/api/journey":
            payload = fixture.journey()
            payload["job"]["result"] = result
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        else:
            fixture.fulfill(route)

    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": name, "value": cookie, "url": base_url}])
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.goto(base_url + "/", wait_until="networkidle")
            page.locator("#searchRecovery").wait_for(state="visible")
            assert page.locator("#stageConfirm").is_hidden()
            assert page.locator("#savedSearchResultsButton").is_visible()
        finally:
            browser.close()
