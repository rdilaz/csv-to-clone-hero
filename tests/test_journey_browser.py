"""Optional real-browser coverage for the primary journey route.

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
            if request.node.get_closest_marker("api_failure"):
                page.route("**/api/journey", lambda route: route.abort())
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#main-content", timeout=5000)
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


@pytest.fixture
def matching_browser_page(config, request, monkeypatch):
    """Serve real journey assets against a seeded durable matching state."""
    from werkzeug.serving import make_server

    state = request.param
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        workspace = client.get("/api/journey").get_json()["workspace_id"]
        if state in {"charts_found", "interrupted"}:
            from tests.test_journey_slice2 import seed_completed
            seed_completed(client, config, monkeypatch)
        else:
            db = Database(config.database_path)
            try:
                db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
                    _track("soundcloud", "one", "Artist", "Song"),
                ]))
                db.initialize_discovery_song_progress(workspace, [type("T", (), {"track_key": "artist|song", "artist_full": "Artist", "title": "Song"})()])
                db.update_workspace_progress(workspace, status="running", phase="finding_charts", total_tracks=1, completed_tracks=0, metadata={"scan_key": "catalog", "run_id": "browser-run"})
            finally:
                db.close()
            job = app.extensions["spotify_job"]
            job.begin(workspace, workspace_key="local", scan_key="catalog", run_id="browser-run")
            job.phase = "restoring" if state == "matching_restoring" else "finding_charts"
            if state == "matching_interrupted":
                db = Database(config.database_path)
                try:
                    db.create_discovery_scan(workspace, scan_key="browser-interrupted", kind="exact", status="interrupted", metadata={"workflow": "combined_catalog"})
                finally:
                    db.close()
        if state == "interrupted":
            db = Database(config.database_path)
            try:
                db.create_discovery_scan(workspace, scan_key="browser-interrupted", kind="exact", status="interrupted", metadata={"workflow": "combined_catalog"})
                db.update_workspace(workspace, status="interrupted", phase="interrupted")
            finally:
                db.close()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"Chromium is not installed: {exc}")
            page = browser.new_page(viewport={"width": 390 if request.node.get_closest_marker("mobile") else 1440, "height": 844 if request.node.get_closest_marker("mobile") else 900})
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#main-content", timeout=5000)
            yield page
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize("matching_browser_page", ["matching_restoring"], indirect=True)
def test_browser_restoring_state_is_indeterminate(matching_browser_page):
    page = matching_browser_page
    assert page.locator("[data-test='matching_restoring']").is_visible()
    assert page.get_by_text("Checking saved match results...").is_visible()
    assert page.locator(".progress-ring.is-indeterminate").is_visible()


@pytest.mark.parametrize("matching_browser_page", ["matching_running"], indirect=True)
@pytest.mark.mobile
def test_browser_matching_running_is_composed_on_mobile(matching_browser_page):
    page = matching_browser_page
    assert page.locator("[data-test='matching_running']").is_visible()
    assert page.get_by_text("0 / 1").is_visible()
    assert page.get_by_text("Currently checking").is_visible() or page.get_by_text("Preparing your songs...").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_charts_found_selects_and_rehydrates_best_cohort(matching_browser_page):
    page = matching_browser_page
    assert page.locator("[data-test='charts-found']").is_visible()
    assert page.get_by_text("We found charts for 1 of your songs.").is_visible()
    assert page.get_by_role("heading", name="1 Best matches").is_visible()
    assert page.get_by_role("heading", name="1 recommendations").is_visible()
    best = page.get_by_role("button", name="Use 1 best matches")
    all_recommendations = page.get_by_role("button", name="Use all 1 recommendations")
    assert best.is_enabled()
    assert all_recommendations.is_enabled()
    assert page.get_by_text("Nothing selected yet", exact=True).is_visible()

    best.click()
    page.get_by_text("1 selected", exact=True).wait_for()
    best_card = page.locator(".cohort-card").filter(has_text="Best matches")
    assert "is-selected" in (best_card.get_attribute("class") or "").split()

    all_recommendations.click()
    page.locator(".cohort-card.is-selected").filter(has_text="recommendations").wait_for()
    page.get_by_role("button", name="Use 1 best matches").click()
    page.locator(".cohort-card.is-selected").filter(has_text="Best matches").wait_for()

    page.reload(wait_until="domcontentloaded")
    page.get_by_text("1 selected", exact=True).wait_for()
    best_card = page.locator(".cohort-card").filter(has_text="Best matches")
    assert "is-selected" in (best_card.get_attribute("class") or "").split()
    assert "/advanced" not in page.content()


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_failed_cohort_mutation_keeps_truthful_state(matching_browser_page):
    page = matching_browser_page
    page.route(
        "**/api/discovery/select-all",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":"Temporary selection failure"}',
        ),
    )

    page.get_by_role("button", name="Use 1 best matches").click()
    alert = page.get_by_role("alert")
    alert.wait_for()
    assert "could not update your chart choices" in alert.inner_text()
    assert page.get_by_text("Nothing selected yet", exact=True).is_visible()
    assert page.locator(".cohort-card.is-selected").count() == 0
    assert page.get_by_role("button", name="Use 1 best matches").is_enabled()


@pytest.mark.parametrize("matching_browser_page", ["interrupted"], indirect=True)
@pytest.mark.mobile
def test_browser_interrupted_state_offers_explicit_retry(matching_browser_page):
    page = matching_browser_page
    assert page.locator("[data-test='matching_interrupted']").is_visible()
    assert page.get_by_text("Your completed saved work can be reused when you try again.").is_visible()
    assert page.locator("[data-test='retry-matching']").is_visible()


@pytest.fixture
def matching_start_page(config, monkeypatch):
    """Hold the real local start worker in its restoring phase for the click test."""
    from werkzeug.serving import make_server
    import csv_to_clonehero.combined_pipeline as pipeline

    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        workspace = client.get("/api/journey").get_json()["workspace_id"]
        db = Database(config.database_path)
        try:
            db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [_track("soundcloud", "one", "Artist", "Song")]))
        finally:
            db.close()
    entered = threading.Event()
    release = threading.Event()
    original_fast = pipeline.completed_scan_fast_path

    def blocked_fast(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_fast(*args, **kwargs)

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", blocked_fast)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"Chromium is not installed: {exc}")
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector("[data-test='music-ready']", timeout=5000)
            yield page, entered, release
            browser.close()
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=2)


def test_browser_find_charts_click_enters_restoring(matching_start_page):
    page, entered, release = matching_start_page
    page.locator("[data-test='find-charts']").click()
    assert entered.wait(5)
    page.wait_for_selector("[data-test='matching_restoring']", timeout=5000)
    assert page.get_by_text("Checking saved match results...").is_visible()
    release.set()


@pytest.mark.api_failure
@pytest.mark.parametrize("browser_page", [(1440, 900)], indirect=True)
def test_browser_api_failure_shows_accepted_retry_state(browser_page):
    page = browser_page
    page.wait_for_selector("[data-test='journey-error']", timeout=5000)
    assert page.get_by_role("button", name="Retry").is_visible()
    assert "WHAT'S HAPPENING" not in page.content()
    assert "/advanced" not in page.content()
