"""Optional real-browser coverage for the temporary Slice 1 route.

Install with ``pip install -e .[browser-e2e]`` and
``python -m playwright install chromium``. This file never runs a frontend
build and remains skipped when Playwright is not installed.
"""

from __future__ import annotations

import threading

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.webapp import create_app

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")


def _track(provider: str, identifier: str, artist: str, title: str) -> ProviderTrack:
    return ProviderTrack(provider=provider, provider_id=identifier, artist=artist, title=title)


@pytest.fixture
def browser_page(config, request):
    from werkzeug.serving import make_server

    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        workspace = client.get("/api/journey").get_json()["workspace_id"]
    if request.node.get_closest_marker("music_ready"):
        db = Database(config.database_path)
        try:
            db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [
                _track("spotify", "one", "Artist", "One"),
                _track("spotify", "two", "Artist", "Two"),
            ]))
        finally:
            db.close()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # browser binary is optional test tooling
                pytest.skip(f"Chromium is not installed: {exc}")
            page = browser.new_page(viewport={"width": request.param[0], "height": request.param[1]})
            page.goto(f"http://127.0.0.1:{server.server_port}/journey", wait_until="networkidle")
            yield page
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize("browser_page", [(1440, 900)], indirect=True)
def test_no_source_drawer_and_coming_soon_tiles(browser_page):
    page = browser_page
    assert page.get_by_role("heading", name="Start with the music you already love.").is_visible()
    assert page.get_by_role("button", name="Open Spotify source").is_visible()
    assert page.get_by_role("article", name="Apple Music / iTunes, coming soon").is_visible()
    assert page.locator(".source-coming").count() == 4
    page.get_by_role("button", name="Open Spotify source").focus()
    page.keyboard.press("Enter")
    assert page.get_by_role("dialog", name="Spotify").is_visible()
    assert page.get_by_role("button", name="Close source sheet").is_focused()
    page.keyboard.press("Escape")
    assert page.get_by_role("button", name="Open Spotify source").is_focused()


@pytest.mark.music_ready
@pytest.mark.parametrize("browser_page", [(1440, 900)], indirect=True)
def test_music_ready_counts_and_format_chooser(browser_page):
    page = browser_page
    assert page.get_by_role("heading", name="2 songs").is_visible()
    assert page.get_by_text("Spotify", exact=True).count() >= 1
    page.get_by_role("button", name="Download my song list").click()
    assert page.get_by_role("dialog", name="Choose a format").is_visible()
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Artist - Song").click()
    assert download_info.value.suggested_filename.endswith(".txt")
    assert "/advanced" not in page.content()


@pytest.mark.music_ready
@pytest.mark.parametrize("browser_page", [(390, 844)], indirect=True)
def test_mobile_music_ready_has_no_horizontal_overflow_and_sheet(browser_page):
    page = browser_page
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.get_by_role("button", name="Manage music sources").click()
    assert page.get_by_role("dialog", name="Spotify").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.keyboard.press("Escape")
    assert page.get_by_role("button", name="Manage music sources").is_focused()
