"""Real upload boundary with synthetic surrounding journey/provider responses."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_apple_music_import import TXT
from tests.test_public_browser import _assert_no_overflow, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [390, 1440])
def test_real_export_updates_count_then_find_remains_explicit(public_server, delivery_chromium, tmp_path, monkeypatch, width):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    _app, base_url, cookie_name, cookie = public_server
    _app.extensions["jobs"].shutdown(timeout=2)
    fixture = SignalCheckFixtures("add")
    fixture.spotify_scope = {"include_liked": False, "playlist_ids": []}
    imports = []
    requests = []
    errors = []

    def route_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/sources/apple-music/import":
            imports.append(route.request)
            route.continue_()
            return
        if path in {"/api/delivery/status", "/api/journey"}:
            payload = fixture.journey()
            payload["sources"] = []
            payload["combined"] = {"tracks": 0, "artists": 0}
            route.fulfill(json=payload)
            return
        fixture.fulfill(route)

    context = delivery_chromium.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request))
    page.route("**/api/**", route_api)
    try:
        page.goto(base_url + "/", wait_until="networkidle")
        playwright.expect(page.locator("#stageAdd")).to_be_visible()
        playwright.expect(page.locator("#appleMusicCount")).to_have_text("Not added")
        assert not any(urlsplit(request.url).path == "/api/sources/apple-music/status" for request in requests)
        playwright.expect(page.locator("#soundcloudSyncStatus")).to_contain_text("not configured")
        page.get_by_text("Add an Apple Music export", exact=True).click()
        page.locator("#appleMusicFile").set_input_files({"name": "playlist.txt", "mimeType": "text/plain", "buffer": TXT})
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/sources/apple-music/import") as uploaded:
            page.locator("#appleMusicForm button[type=submit]").click()
        assert uploaded.value.status == 200
        playwright.expect(page.locator("#appleMusicCount")).to_have_text("1 unique song imported")
        playwright.expect(page.locator("#musicTally strong")).to_contain_text("1 combined unique song")
        playwright.expect(page.locator("#musicTally small")).to_contain_text("Apple Music")
        playwright.expect(page.locator("#appleMusicMessage")).to_contain_text("1 duplicate row combined; 1 row without song metadata skipped")
        assert len(imports) == 1
        assert imports[0].headers.get("x-riff-request") == "1"
        assert not [request for request in requests if request.method == "POST" and urlsplit(request.url).path == "/api/jobs"]

        # A dynamically adopted form is bound once, even after repeated mounts.
        page.evaluate("""() => {
          const form = document.querySelector('#appleMusicForm');
          const replacement = form.cloneNode(true);
          delete replacement.dataset.riffAppleMusicBound;
          form.replaceWith(replacement);
          window.RiffAppleMusic.mount(); window.RiffAppleMusic.mount();
        }""")
        page.locator("#appleMusicFile").set_input_files({"name": "playlist.txt", "mimeType": "text/plain", "buffer": TXT})
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/sources/apple-music/import") as repeated:
            page.locator("#appleMusicForm button[type=submit]").click()
        assert repeated.value.status == 200
        playwright.expect(page.locator("#appleMusicForm button[type=submit]")).to_be_enabled()
        assert len(imports) == 2
        _assert_no_overflow(page)
        with page.expect_request(lambda request: request.method == "POST" and urlsplit(request.url).path == "/api/jobs"):
            page.locator("#findChartsButton").click()
        assert [request for request in requests if request.method == "POST" and urlsplit(request.url).path == "/api/jobs"]
        assert not errors
        assert not fixture.unexpected_requests
    finally:
        context.close()


def test_unknown_upload_outcome_asks_for_reconciliation_without_automatic_retry(delivery_page):
    fixture = SignalCheckFixtures("add")
    attempts = []

    def route_api(route):
        if urlsplit(route.request.url).path == "/api/sources/apple-music/import":
            attempts.append(route.request)
            route.abort("connectionreset")
            return
        fixture.fulfill(route)

    page, requests = delivery_page(fixture, route_api=route_api)
    page.get_by_text("Add an Apple Music export", exact=True).click()
    page.locator("#appleMusicFile").set_input_files({"name": "playlist.txt", "mimeType": "text/plain", "buffer": TXT})
    page.locator("#appleMusicForm button[type=submit]").click()
    playwright.expect(page.locator("#appleMusicMessage")).to_contain_text("Reload to check saved imports before retrying")
    playwright.expect(page.locator("#appleMusicForm button[type=submit]")).to_be_enabled()
    assert len(attempts) == 1
    assert not any(request.method == "POST" and urlsplit(request.url).path == "/api/jobs" for request in requests)
