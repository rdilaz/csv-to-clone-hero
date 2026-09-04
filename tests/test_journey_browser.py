"""Optional real-browser coverage for the primary journey route.

Install with ``pip install -e .[browser-e2e]`` and
``python -m playwright install chromium``. This file never runs a frontend
build and remains skipped when Playwright is not installed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderPlaylist, ProviderTrack
from csv_to_clonehero.webapp import create_app

from .conftest import FakeSource

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")


def _track(provider: str, identifier: str, artist: str, title: str) -> ProviderTrack:
    return ProviderTrack(provider=provider, provider_id=identifier, artist=artist, title=title)


def _is_focused(locator) -> bool:
    return bool(locator.evaluate("element => document.activeElement === element"))


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
            first = _track("spotify", "one", "Artist", "One")
            first.playlist_ids = ["playlist-1"]
            first.playlist_names = ["Road trip"]
            db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [
                first,
                _track("spotify", "two", "Artist", "Two"),
            ], [ProviderPlaylist("spotify", "playlist-1", "Road trip")]))
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
    assert _is_focused(page.get_by_role("button", name="Close source sheet"))
    page.keyboard.press("Escape")
    assert _is_focused(page.get_by_role("button", name="Open Spotify source"))


@pytest.mark.music_ready
@pytest.mark.parametrize("browser_page", [(1440, 900)], indirect=True)
def test_music_ready_counts_and_format_chooser(browser_page):
    page = browser_page
    assert page.get_by_role("heading", name="2 songs").is_visible()
    assert page.get_by_text("Spotify", exact=True).count() >= 1
    assert page.get_by_text("songs, 1 playlist", exact=True).is_visible()
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
    assert _is_focused(page.get_by_role("button", name="Manage music sources"))


@pytest.fixture
def matching_browser_page(config, request, monkeypatch):
    """Serve real journey assets against a seeded durable matching state."""
    from werkzeug.serving import make_server

    state = request.param
    config.input_path.mkdir(parents=True, exist_ok=True)
    source = FakeSource()
    app = create_app(config, source_factory=lambda cfg: [source])
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
            mobile = bool(request.node.get_closest_marker("mobile"))
            page = browser.new_page(
                viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 900},
                has_touch=mobile,
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#main-content", timeout=5000)
            yield page
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _route_review_concerns(page, concerns):
    """Replace only the rendered candidate concerns in the real review payload."""

    def handler(route):
        response = route.fetch()
        payload = response.json()
        for card in payload.get("tracks", []):
            for candidate in card.get("candidates", []):
                candidate.setdefault("review", {})["concerns"] = list(concerns)
        route.fulfill(
            status=response.status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/review?*", handler)


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
    page.locator("[data-test='charts-found']").wait_for()
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
    page.get_by_role("heading", name="A few representative charts").wait_for()
    checkbox = page.get_by_role("checkbox", name="Use Song by Artist")
    assert checkbox.is_checked()

    checkbox.uncheck()
    page.get_by_text("Nothing selected yet", exact=True).wait_for()
    assert page.get_by_role("checkbox", name="Use Song by Artist").is_checked() is False
    page.get_by_role("checkbox", name="Use Song by Artist").check()
    page.get_by_text("1 selected", exact=True).wait_for()
    assert page.get_by_role("checkbox", name="Use Song by Artist").is_checked()

    all_recommendations.click()
    page.locator(".cohort-card.is-selected").filter(has_text="recommendations").wait_for()
    page.get_by_role("button", name="Use 1 best matches").click()
    page.locator(".cohort-card.is-selected").filter(has_text="Best matches").wait_for()

    page.reload(wait_until="domcontentloaded")
    page.get_by_text("1 selected", exact=True).wait_for()
    best_card = page.locator(".cohort-card").filter(has_text="Best matches")
    assert "is-selected" in (best_card.get_attribute("class") or "").split()
    assert page.get_by_role("checkbox", name="Use Song by Artist").is_checked()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert "/advanced" not in page.content()


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_chart_concern_indicator_requires_a_reported_reason(matching_browser_page):
    page = matching_browser_page
    _route_review_concerns(page, ["", "   ", None])

    page.get_by_role("button", name="Use 1 best matches").click()
    page.get_by_role("heading", name="A few representative charts").wait_for()
    assert page.locator(".chart-concern").count() == 0
    assert page.locator(".concern-trigger").count() == 0


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_chart_concerns_expose_every_reason_on_hover_and_focus(matching_browser_page):
    page = matching_browser_page
    concerns = ["Possible live-version mismatch.", "Only one instrument is available."]
    _route_review_concerns(page, concerns)

    page.get_by_role("button", name="Use 1 best matches").click()
    trigger = page.get_by_role("button", name="2 chart issues", exact=True)
    trigger.wait_for()
    popover = page.locator("#" + trigger.get_attribute("aria-describedby"))
    assert popover.get_attribute("role") == "tooltip"
    assert popover.is_visible() is False

    trigger.hover()
    popover.wait_for(state="visible")
    for concern in concerns:
        assert popover.get_by_text(concern, exact=True).is_visible()
    page.keyboard.press("Escape")
    popover.wait_for(state="hidden")

    page.locator(".found-head h1").hover()
    page.get_by_role("checkbox", name="Use Song by Artist").focus()
    page.keyboard.press("Tab")
    popover.wait_for(state="visible")
    assert _is_focused(trigger)
    assert trigger.evaluate("element => getComputedStyle(element).outlineStyle !== 'none'")
    page.keyboard.press("Escape")
    popover.wait_for(state="hidden")
    assert _is_focused(trigger)
    page.keyboard.press("Enter")
    popover.wait_for(state="visible")
    assert "is-open" in (trigger.locator("..").get_attribute("class") or "").split()
    page.keyboard.press("Enter")
    popover.wait_for(state="hidden")
    page.keyboard.press("Enter")
    popover.wait_for(state="visible")
    page.keyboard.press("Tab")
    popover.wait_for(state="hidden")


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_chart_concern_popover_opens_by_touch_without_mobile_overflow(matching_browser_page):
    page = matching_browser_page
    assert page.viewport_size == {"width": 390, "height": 844}
    concerns = ["Possible live-version mismatch.", "Only one instrument is available."]
    _route_review_concerns(page, concerns)

    page.get_by_role("button", name="Use 1 best matches").click()
    trigger = page.get_by_role("button", name="2 chart issues", exact=True)
    trigger.tap()
    popover = page.locator("#" + trigger.get_attribute("aria-controls"))
    popover.wait_for(state="visible")
    assert "is-open" in (trigger.locator("..").get_attribute("class") or "").split()
    bounds = popover.bounding_box()
    assert bounds is not None
    assert bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= 390
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    page.locator(".found-head h1").tap()
    popover.wait_for(state="hidden")
    assert "is-open" not in (trigger.locator("..").get_attribute("class") or "").split()


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


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_failed_row_mutation_rehydrates_saved_checkbox(matching_browser_page):
    page = matching_browser_page
    page.get_by_role("button", name="Use 1 best matches").click()
    checkbox = page.get_by_role("checkbox", name="Use Song by Artist")
    checkbox.wait_for()
    assert checkbox.is_checked()
    page.route(
        "**/api/discovery/select",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":"Temporary row failure"}',
        ),
    )

    checkbox.click()
    alert = page.get_by_role("alert")
    alert.wait_for()
    assert "saved choice was restored" in alert.inner_text()
    assert page.get_by_role("checkbox", name="Use Song by Artist").is_checked()
    assert page.get_by_text("1 selected", exact=True).is_visible()


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_continue_uses_real_preflight_and_changed_folder(matching_browser_page):
    page = matching_browser_page
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    assert page.locator("[data-test='continue-download']").count() == 0

    page.get_by_role("button", name="Use 1 best matches").click()
    continue_button = page.get_by_role("button", name="Continue with 1 charts")
    continue_button.wait_for()
    with page.expect_response("**/api/export/preview") as preview_info:
        continue_button.click()
    preview = preview_info.value.json()
    assert preview["ok"] is True
    page.get_by_role("heading", name="Ready to add 1 song").wait_for()
    assert page.locator("[data-test='preflight-selected']").inner_text() == str(preview["selected_chart_count"])
    assert page.locator("[data-test='preflight-reusable']").inner_text() == str(preview["reusable_count"])
    assert page.locator("[data-test='preflight-required']").inner_text() == str(preview["download_required_count"])
    assert page.locator("[data-test='preflight-destination']").inner_text() == preview["destination"]
    assert page.get_by_role("button", name="Download library").is_visible()

    chosen = str(Path(preview["destination"]).parent / "Dedicated Test Songs")
    page.route(
        "**/api/paths/pick",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"selected": True, "path": chosen, "resolved": chosen}),
        ),
    )
    with page.expect_response("**/api/export/preview") as changed_info:
        page.get_by_role("button", name="Change folder").click()
    changed = changed_info.value.json()
    assert changed["ok"] is True
    assert changed["destination"] == str(Path(chosen).resolve())
    assert page.locator("[data-test='preflight-destination']").inner_text() == changed["destination"]
    assert not any("/api/spotify/export/start" in url for url in requests)

    page.get_by_role("button", name="Change chart choices").click()
    assert page.locator("[data-test='charts-found']").is_visible()
    assert page.get_by_text("1 selected", exact=True).is_visible()
    assert page.get_by_role("button", name="Continue with 1 charts").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_pending_destination_visual_is_neutral(matching_browser_page):
    page = matching_browser_page
    page.get_by_role("button", name="Use 1 best matches").click()
    preview = {
        "ok": True,
        "selected_chart_count": 1,
        "eligible_chart_count": 1,
        "reusable_count": 0,
        "download_required_count": 1,
        "invalid_count": 0,
        "estimated_bytes": 1024,
        "available_bytes": 1024 * 1024,
        "destination": "C:\\Test\\Songs",
        "errors": [],
        "warnings": [],
    }
    page.evaluate(
        """payload => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, options) => {
            if (!String(input).includes('/api/export/preview')) return originalFetch(input, options);
            return new Promise(resolve => {
              window.__finishPreview = () => resolve(new Response(JSON.stringify(payload), {
                status: 200,
                headers: {'Content-Type': 'application/json'}
              }));
            });
          };
        }""",
        preview,
    )

    page.get_by_role("button", name="Continue with 1 charts").click()
    page.locator("[data-test='download-preflight']").wait_for()
    folder = page.locator("[data-test='destination-visual'] .folder-card")
    assert "needs-attention" not in (folder.get_attribute("class") or "").split()
    assert folder.locator(".folder-check").count() == 0
    assert "!" not in folder.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    page.evaluate("window.__finishPreview()")
    page.locator("[data-test='download-ready']").wait_for()
    assert "is-ready" in (page.locator(".folder-card").get_attribute("class") or "").split()


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_preflight_failure_is_not_ready_and_preserves_selection(matching_browser_page):
    page = matching_browser_page
    page.get_by_role("button", name="Use 1 best matches").click()
    page.route(
        "**/api/export/preview",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": False,
                "selected_chart_count": 1,
                "eligible_chart_count": 0,
                "reusable_count": 0,
                "download_required_count": 0,
                "invalid_count": 1,
                "estimated_bytes": 0,
                "available_bytes": 1024,
                "destination": "C:\\Test\\Songs",
                "errors": ["A persisted selection is no longer current."],
                "warnings": [],
            }),
        ),
    )

    page.get_by_role("button", name="Continue with 1 charts").click()
    preflight_error = page.locator("[data-test='download-preflight-error']")
    page.get_by_role("heading", name="Your library needs one more check").wait_for()
    assert preflight_error.get_by_text("A persisted selection is no longer current.").is_visible()
    assert page.get_by_text("Ready for download").count() == 0
    assert page.locator("[data-test='preflight-selected']").inner_text() == "1"
    folder = page.locator("[data-test='destination-visual'] .folder-card")
    assert "needs-attention" in (folder.get_attribute("class") or "").split()
    assert folder.locator(".folder-check").inner_text() == "!"
    page.get_by_role("button", name="Change chart choices").click()
    assert page.get_by_text("1 selected", exact=True).is_visible()


def _reach_download_ready(page):
    page.get_by_role("button", name="Use 1 best matches").click()
    page.get_by_role("button", name="Continue with 1 charts").click()
    page.locator("[data-test='download-ready']").wait_for()


def _running_export_status(destination, *, done=0, current="Checking download sizes", failed=0, invalid=0):
    item_status = "failed" if failed else "invalid" if invalid else "validated" if done else "pending"
    return {
        "running": True,
        "status": "running",
        "workspace_id": 1,
        "destination": destination,
        "done": done,
        "total": 1,
        "current": current,
        "started_at": time.time() - 2,
        "items": {
            "selected": 1,
            "completed": 0,
            "downloading": 0,
            "retrying": 0,
            "failed": failed,
            "invalid": invalid,
            "cancelled": 0,
            "remaining": 0 if failed or invalid else 1,
            "items": [{"status": item_status, "result": item_status if failed or invalid else ""}],
        },
    }


def _complete_export_status(destination):
    return {
        "running": False,
        "status": "complete",
        "workspace_id": 1,
        "destination": destination,
        "items": {
            "selected": 1,
            "completed": 1,
            "failed": 0,
            "invalid": 0,
            "cancelled": 0,
            "remaining": 0,
            "items": [{"status": "published", "result": "downloaded"}],
        },
        "result": {
            "success": True,
            "status": "success",
            "selected": 1,
            "ready": 1,
            "added": 1,
            "already_present": 0,
            "failed": 0,
            "invalid": 0,
            "cancelled_items": 0,
            "remaining": 0,
            "destination": destination,
        },
    }


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_export_start_rejection_stays_download_ready(matching_browser_page):
    page = matching_browser_page
    _reach_download_ready(page)
    page.route(
        "**/api/spotify/export/start",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"error": "Another library build is already running."}),
        ),
    )

    page.get_by_role("button", name="Download library").click()
    alert = page.get_by_role("alert")
    alert.wait_for()
    assert "already running" in alert.inner_text()
    assert page.locator("[data-test='download-ready']").is_visible()
    assert page.locator("[data-test='export-running']").count() == 0


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_building_polls_keep_flow_dom_and_ready_progress_truthful(matching_browser_page):
    page = matching_browser_page
    assert page.viewport_size == {"width": 390, "height": 844}
    _reach_download_ready(page)
    destination = page.locator("[data-test='preflight-destination']").inner_text()
    state = {"polls": 0, "allow_second": False, "terminal": False}

    def start_handler(route):
        payload = json.loads(route.request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "started": True,
                "kind": "folder",
                "workspace_id": payload["workspace_id"],
                "destination": payload["path"],
            }),
        )

    def status_handler(route):
        state["polls"] += 1
        if state["terminal"]:
            payload = _complete_export_status(destination)
        elif state["polls"] == 1 or not state["allow_second"]:
            payload = _running_export_status(destination)
        else:
            payload = _running_export_status(
                destination,
                done=1,
                current="Writing files",
            )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/spotify/export/start", start_handler)
    page.route("**/api/export/status?*", status_handler)
    page.get_by_role("button", name="Download library").click()
    page.locator("[data-test='export-running']").wait_for()
    page.evaluate("""() => {
      window.__persistentFlowNode = document.querySelector('.flow-node-one');
      window.__flowAnimationTime = window.__persistentFlowNode.getAnimations()[0].currentTime;
    }""")
    state["allow_second"] = True

    page.get_by_role("heading", name="Adding charts to your Songs folder").wait_for()
    assert state["polls"] >= 2
    assert page.evaluate("window.__persistentFlowNode === document.querySelector('.flow-node-one')")
    assert page.evaluate("document.querySelector('.flow-node-one').getAnimations()[0].currentTime > window.__flowAnimationTime")
    assert page.get_by_text("0%", exact=True).is_visible()
    assert page.get_by_text("ready", exact=True).count() >= 1
    assert page.get_by_text("100%", exact=True).count() == 0
    assert "processed" not in page.locator("[data-test='export-running']").inner_text().casefold()
    progress = page.locator("[data-export-progress]")
    assert progress.get_attribute("aria-label") == "Charts ready"
    assert progress.get_attribute("aria-valuenow") == "0"
    assert progress.get_attribute("aria-valuemax") == "1"
    assert page.locator("[data-export-warning]:visible").count() == 0
    assert "!" not in page.locator(".songs-folder-target").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    state["terminal"] = True
    page.locator("[data-test='export-complete']").wait_for()
    assert page.get_by_role("heading", name="1 song is ready to play").is_visible()
    assert page.locator("[data-test='export-running']").count() == 0


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_ready_percentage_stays_below_100_until_every_chart_is_ready(matching_browser_page):
    page = matching_browser_page
    selections = {
        "items": [
            {"kind": "exact", "track_key": f"artist|song-{index}"}
            for index in range(200)
        ]
    }
    status = {
        "running": True,
        "status": "running",
        "workspace_id": 1,
        "destination": "C:\\Test\\Songs",
        "done": 200,
        "total": 200,
        "current": "Writing files",
        "started_at": time.time() - 2,
        "items": {
            "selected": 200,
            "completed": 199,
            "downloading": 0,
            "retrying": 0,
            "failed": 0,
            "invalid": 0,
            "cancelled": 0,
            "remaining": 1,
            "items": [],
        },
    }
    page.route(
        "**/api/discovery/selections?*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(selections)),
    )
    page.route(
        "**/api/export/status?*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(status)),
    )

    page.reload(wait_until="domcontentloaded")
    page.locator("[data-test='export-running']").wait_for()
    assert page.get_by_text("199 / 200 ready", exact=True).is_visible()
    assert page.get_by_text("99%", exact=True).is_visible()
    assert page.get_by_text("100%", exact=True).count() == 0
    assert page.locator("[data-export-progress]").get_attribute("aria-valuetext") == "99% ready, 199 of 200 charts ready"


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_download_warning_appears_only_for_real_item_failures(matching_browser_page):
    page = matching_browser_page
    _reach_download_ready(page)
    destination = page.locator("[data-test='preflight-destination']").inner_text()

    page.route(
        "**/api/spotify/export/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"started": True, "kind": "folder", "workspace_id": 1, "destination": destination}),
        ),
    )
    page.route(
        "**/api/export/status?*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_running_export_status(destination, done=1, failed=1)),
        ),
    )
    page.get_by_role("button", name="Download library").click()
    warning = page.locator("[data-export-warning]")
    warning.wait_for(state="visible")
    assert " ".join(warning.inner_text().split()) == "! 1 couldn’t be added"
    assert warning.get_attribute("aria-label") == "Warning: 1 chart couldn’t be added"
    assert warning.locator(".counter-warning-mark").is_visible()
    assert "!" not in page.locator(".songs-folder-target").inner_text()
    assert page.get_by_text("Some charts couldn’t be added.", exact=True).is_visible()
    assert "is-warning" in (page.locator("[data-export-reassurance-mark]").get_attribute("class") or "").split()

    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.locator(".flow-node-one").evaluate("element => getComputedStyle(element).animationIterationCount") == "1"


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_terminal_failure_after_outputs_are_ready_exposes_the_error(matching_browser_page):
    page = matching_browser_page
    _reach_download_ready(page)
    destination = page.locator("[data-test='preflight-destination']").inner_text()
    failed = _complete_export_status(destination)
    failed.update({
        "status": "failed",
        "error": "The library manifest could not be written.",
    })
    failed["result"].update({
        "success": False,
        "status": "failed",
        "errors": ["The library manifest could not be written."],
    })
    terminal = {"payload": failed}
    page.route(
        "**/api/spotify/export/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"started": True, "kind": "folder", "workspace_id": 1, "destination": destination}),
        ),
    )
    page.route(
        "**/api/export/status?*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(terminal["payload"])),
    )

    page.get_by_role("button", name="Download library").click()
    page.locator("[data-test='export-failed']").wait_for()
    assert page.get_by_role("heading", name="1 song is ready, but finishing failed").is_visible()
    assert page.get_by_text("The library manifest could not be written.").is_visible()
    assert page.get_by_text("Completed chart files remain in your Songs folder.").is_visible()
    assert page.get_by_text("0 couldn’t be added", exact=True).count() == 0

    partial = _complete_export_status(destination)
    partial["status"] = "partial"
    partial["result"].update({"status": "partial", "failed": 1})
    terminal["payload"] = partial
    page.reload(wait_until="domcontentloaded")
    page.locator("[data-test='export-complete']").wait_for()
    assert page.get_by_role("heading", name="1 song is ready to play").is_visible()
    assert page.get_by_text("0 couldn’t be added", exact=True).count() == 0


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
@pytest.mark.mobile
def test_browser_export_cancel_resume_and_complete_use_live_status(matching_browser_page):
    page = matching_browser_page
    _reach_download_ready(page)
    destination = page.locator("[data-test='preflight-destination']").inner_text()
    state = {"starts": 0, "phase": "idle", "cancel_polls": 0, "resume_polls": 0, "payloads": []}

    def start_handler(route):
        payload = json.loads(route.request.post_data or "{}")
        state["payloads"].append(payload)
        state["starts"] += 1
        state["phase"] = "running" if state["starts"] == 1 else "resumed"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "started": True,
                "kind": "folder",
                "workspace_id": payload["workspace_id"],
                "destination": payload["path"],
            }),
        )

    def status_handler(route):
        workspace = state["payloads"][-1]["workspace_id"] if state["payloads"] else 1
        if state["phase"] == "cancelling":
            state["cancel_polls"] += 1
            if state["cancel_polls"] >= 2:
                state["phase"] = "cancelled"
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "running": True, "status": "cancelling", "workspace_id": workspace,
                    "destination": destination, "done": 0, "total": 1,
                    "current": "Cancellation requested; finishing the active chart safely",
                    "started_at": time.time() - 2,
                    "items": {"selected": 1, "completed": 0, "downloading": 1, "retrying": 0, "failed": 0, "invalid": 0, "cancelled": 0, "remaining": 1, "items": []},
                }))
                return
        if state["phase"] == "cancelled":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "running": False, "status": "cancelled", "workspace_id": workspace,
                "destination": destination, "done": 1, "total": 1,
                "items": {"selected": 1, "completed": 0, "failed": 0, "invalid": 0, "cancelled": 1, "remaining": 1, "items": []},
                "result": {"success": False, "status": "cancelled", "selected": 1, "ready": 0, "added": 0, "already_present": 0, "failed": 0, "invalid": 0, "cancelled_items": 1, "remaining": 1, "destination": destination},
            }))
            return
        if state["phase"] == "resumed":
            state["resume_polls"] += 1
            if state["resume_polls"] >= 2:
                state["phase"] = "complete"
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "running": True, "status": "running", "workspace_id": workspace,
                    "destination": destination, "done": 0, "total": 1,
                    "current": "Downloading Artist - Song", "started_at": time.time() - 1,
                    "items": {"selected": 1, "completed": 0, "downloading": 1, "retrying": 0, "failed": 0, "invalid": 0, "cancelled": 0, "remaining": 1, "items": []},
                }))
                return
        if state["phase"] == "complete":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "running": False, "status": "complete", "workspace_id": workspace,
                "destination": destination,
                "items": {"selected": 1, "completed": 1, "failed": 0, "invalid": 0, "cancelled": 0, "remaining": 0, "items": []},
                "result": {"success": True, "status": "success", "selected": 1, "ready": 1, "added": 0, "already_present": 1, "failed": 0, "invalid": 0, "cancelled_items": 0, "remaining": 0, "destination": destination},
            }))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "running": True, "status": "running", "workspace_id": workspace,
            "destination": destination, "done": 0, "total": 1,
            "current": "Downloading Artist - Song", "started_at": time.time() - 1,
            "items": {"selected": 1, "completed": 0, "downloading": 1, "retrying": 0, "failed": 0, "invalid": 0, "cancelled": 0, "remaining": 1, "items": []},
        }))

    def cancel_handler(route):
        state["phase"] = "cancelling"
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "cancelled": True, "status": "cancelling"}))

    page.route("**/api/spotify/export/start", start_handler)
    page.route("**/api/export/status?*", status_handler)
    page.route("**/api/spotify/export/cancel", cancel_handler)

    page.get_by_role("button", name="Download library").click()
    page.locator("[data-test='export-running']").wait_for()
    assert page.get_by_text("0 / 1 ready", exact=True).is_visible()
    assert state["payloads"][0]["kind"] == "folder"
    assert state["payloads"][0]["path"] == destination
    page.get_by_role("button", name="Stop safely").click()
    page.locator("[data-test='export-cancelling']").wait_for()
    assert page.get_by_text("Completed songs are being kept").is_visible()
    page.get_by_role("button", name="Resume download").wait_for()
    assert page.get_by_role("heading", name="0 charts are already ready").is_visible()
    assert state["cancel_polls"] >= 2
    page.get_by_role("button", name="Resume download").click()
    page.locator("[data-test='export-complete']").wait_for()
    assert page.get_by_role("heading", name="1 song is ready to play").is_visible()
    assert page.get_by_text("1 already available", exact=True).is_visible()
    assert state["starts"] == 2
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert "/advanced" not in page.content()


@pytest.mark.parametrize("matching_browser_page", ["charts_found"], indirect=True)
def test_browser_unknown_progress_partial_retry_and_failed_states(matching_browser_page):
    page = matching_browser_page
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    _reach_download_ready(page)
    destination = page.locator("[data-test='preflight-destination']").inner_text()
    state = {"starts": 0, "phase": "unknown"}

    def start_handler(route):
        payload = json.loads(route.request.post_data or "{}")
        state["starts"] += 1
        state["phase"] = "unknown" if state["starts"] == 1 else "failed"
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"started": True, "kind": "folder", "workspace_id": payload["workspace_id"], "destination": payload["path"]}))

    def status_handler(route):
        if state["phase"] == "partial":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "running": False, "status": "partial", "workspace_id": 1, "destination": destination,
                "items": {"selected": 1, "completed": 0, "failed": 1, "invalid": 0, "cancelled": 0, "remaining": 0, "items": []},
                "result": {"success": True, "status": "partial", "selected": 1, "ready": 0, "added": 0, "already_present": 0, "failed": 1, "invalid": 0, "cancelled_items": 0, "remaining": 0, "destination": destination},
            }))
        elif state["phase"] == "failed":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "running": False, "status": "failed", "workspace_id": 1, "destination": destination, "error": "The source could not provide this chart.",
                "items": {"selected": 1, "completed": 0, "failed": 1, "invalid": 0, "cancelled": 0, "remaining": 0, "items": []},
                "result": {"success": False, "status": "failed", "selected": 1, "ready": 0, "added": 0, "already_present": 0, "failed": 1, "invalid": 0, "cancelled_items": 0, "remaining": 0, "destination": destination, "errors": ["The source could not provide this chart."]},
            }))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "running": True, "status": "running", "workspace_id": 1, "destination": destination,
                "done": 0, "total": 0, "current": "Checking download sizes", "started_at": time.time() - 1,
                "items": {"selected": 0, "completed": 0, "downloading": 0, "retrying": 0, "failed": 0, "invalid": 0, "cancelled": 0, "remaining": 0, "items": []},
            }))

    page.route("**/api/spotify/export/start", start_handler)
    page.route("**/api/export/status?*", status_handler)
    page.get_by_role("button", name="Download library").click()
    page.locator("[data-test='export-running']").wait_for()
    assert page.get_by_text("Preparing", exact=True).is_visible()
    assert page.get_by_text("100%", exact=True).count() == 0
    progress = page.locator(".building-progress[role='progressbar']")
    assert progress.get_attribute("aria-label") == "Preparing chart progress"
    assert progress.get_attribute("aria-valuenow") is None
    assert "is-indeterminate" in (progress.get_attribute("class") or "").split()
    assert "ETA" not in page.locator("[data-test='export-running']").inner_text()

    state["phase"] = "partial"
    page.locator("[data-test='export-partial']").wait_for()
    assert page.get_by_text("1 couldn’t be added", exact=True).is_visible()
    page.get_by_role("button", name="Retry remaining").click()
    page.locator("[data-test='export-failed']").wait_for()
    assert page.get_by_role("heading", name="Build failed").is_visible()
    assert page.get_by_text("The source could not provide this chart.").is_visible()
    assert page.locator("[data-test='export-complete']").count() == 0
    assert state["starts"] == 2
    page.wait_for_timeout(100)
    assert sum("/api/export/preview" in url for url in requests) >= 2


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
