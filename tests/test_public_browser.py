"""Optional real-browser acceptance for the public Riff journey.

Install with ``pip install -e .[browser-e2e]`` and
``python -m playwright install chromium``. Provider traffic is always mocked.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def public_server(tmp_path, monkeypatch):
    from csv_to_clonehero.config import AppConfig
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.web import create_app
    from werkzeug.serving import make_server

    config = AppConfig()
    config.base_dir = tmp_path
    config.paths.database = "state/public.db"
    config.paths.state_dir = "state"
    config.server.enforce_secret_key = False
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "browser-test")
    app = create_app(config, source_factory=lambda _config: [])

    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="browser-user", display_name="Browser Player")
        signed = db.create_session(int(user["id"]), expires_at=time.time() + 3600)
    finally:
        db.close()
    flask_client = app.test_client()
    with flask_client.session_transaction() as session:
        session["sid"] = signed["session_id"]
    cookie = flask_client.get_cookie(config.sessions.cookie_name)

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, f"http://127.0.0.1:{server.server_port}", config.sessions.cookie_name, cookie.value
    finally:
        server.shutdown()
        thread.join(timeout=2)
        app.extensions["jobs"].shutdown(timeout=2)
        app.extensions["downloads"].close()


def _assert_no_overflow(page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def _capture(page, name):
    root = os.environ.get("RIFF_BROWSER_EVIDENCE_DIR", "").strip()
    if not root:
        return
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / name), full_page=True)


def test_public_landing_mobile_and_desktop(public_server):
    _app, base_url, _cookie_name, _cookie = public_server
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            for viewport in ({"width": 390, "height": 844}, {"width": 1440, "height": 900}):
                page = browser.new_page(viewport=viewport)
                errors = []
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.goto(base_url + "/", wait_until="networkidle")
                assert page.get_by_role("heading", name="Turn the music you love into a Clone Hero library.").is_visible()
                login = page.get_by_role("link", name="Continue with Spotify")
                assert login.is_visible()
                assert login.bounding_box()["height"] >= 44
                assert page.get_by_text("Your music", exact=True).is_visible()
                assert page.get_by_text("Community charts", exact=True).is_visible()
                _assert_no_overflow(page)
                assert not [error for error in errors if "Content Security Policy" in error]
                _capture(page, f"landing-{viewport['width']}x{viewport['height']}.png")
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("viewport", "has_touch"),
    [({"width": 390, "height": 844}, True), ({"width": 1440, "height": 900}, False)],
)
def test_public_authenticated_journey_to_download_ready(
    public_server, viewport, has_touch
):
    _app, base_url, cookie_name, cookie = public_server
    selected = {"value": False}
    scope_saved = {"value": False}
    job_polls = {"count": 0}
    api_calls = []

    def journey_payload():
        complete = job_polls["count"] > 0
        return {
            "workspace_id": 1,
            "workspace_status": "complete" if complete else "created",
            "workspace": {"id": 1, "status": "complete" if complete else "created", "phase": "complete" if complete else "source_setup", "updated_at": time.time()},
            "spotify_scope": None,
            "sources": [],
            "combined": {"tracks": 0, "artists": 0},
            "job": {"id": 9, "status": "complete", "phase": "complete", "message": "Complete", "progress": {"total_tracks": 1, "processed_tracks": 1}} if complete else None,
            "review": {"strong": 1 if complete else 0, "review": 0, "unavailable": 0, "selected": int(selected["value"]), "total": 1 if complete else 0},
            "download": None,
        }

    def workspace_payload():
        candidate = {
            "candidate_id": 41,
            "source": "enchor",
            "chart_id": 410,
            "artist": "Paramore",
            "title": "Misery Business",
            "charter": "Community Charter",
            "pack_name": "Full band",
            "selected": selected["value"],
            "best": True,
            "review": {
                "state": "strong",
                "label": "Strong match",
                "summary": "The song and artist identities agree.",
                "concerns": ["This chart is an alternate album version."],
                "selectable": True,
                "action": "Select chart",
            },
        }
        return {
            "workspace": 1,
            "minimum_score": 50,
            "tracks": [{
                "track_key": "spotify:track-1",
                "artist": "Paramore",
                "title": "Misery Business",
                "album": "Riot!",
                "review_state": "strong",
                "selected": selected["value"],
                "candidates": [candidate],
            }],
            "total": 1,
            "offset": 0,
            "limit": 25,
            "summary": {"strong": 1, "review": 0, "unavailable": 0, "selected": int(selected["value"]), "selected_strong": int(selected["value"]), "selected_review": 0, "selection_count": int(selected["value"]), "total": 1},
        }

    def route_api(route):
        request = route.request
        path = request.url.split(base_url, 1)[-1]
        method = request.method
        api_calls.append((method, path, request.post_data))
        if path == "/api/journey":
            payload, status = journey_payload(), 200
        elif path == "/api/spotify/scope":
            if method == "GET":
                payload = {
                    "liked_count": 1,
                    "playlists": [
                        {"id": "playlist-1", "name": "Road trip", "track_count": 12, "owner_name": "Browser Player"},
                        {"id": "playlist-unknown", "name": "Current API playlist", "track_count": None, "owner_name": "Browser Player"},
                    ],
                    "workspace": {"id": 1, "exists": True, "scope": {"include_liked": True, "playlist_ids": []} if scope_saved["value"] else None, "soundcloud_scope": {}},
                }
            else:
                scope_saved["value"] = True
                payload = {"ok": True, "workspace": {"scope": {"include_liked": True}}}
            status = 200
        elif path == "/api/auth/soundcloud/status":
            payload, status = {"configured": False, "authenticated": False}, 200
        elif path == "/api/preflight":
            payload, status = {"preflight": {"usable": True, "authoritative": True, "message": "Ready"}}, 200
        elif path == "/api/jobs" and method == "POST":
            payload, status = {"job": {"id": 9, "status": "queued", "phase": "queued"}}, 202
        elif path == "/api/jobs/9":
            job_polls["count"] += 1
            if job_polls["count"] == 1:
                payload = {"job": {"id": 9, "status": "running", "phase": "finding_charts", "message": "Matching", "progress": {"total_tracks": 133, "completed_tracks": 133, "total_work": 180, "completed_work": 140}}}
            else:
                payload = {"job": {"id": 9, "status": "complete", "phase": "complete", "message": "Matching complete", "progress": {"total_tracks": 1, "processed_tracks": 1}}}
            status = 200
        elif path.startswith("/api/workspace?"):
            payload, status = workspace_payload(), 200
        elif path == "/api/selection/bulk":
            selected["value"] = json.loads(request.post_data or "{}").get("action") != "clear"
            payload, status = {"ok": True, "changed": 1, "scope": "strong", "minimum_score": 50}, 200
        elif path == "/api/download":
            payload, status = {"download": {"token": "single.use.token", "expires_at": time.time() + 900, "ttl_seconds": 900, "size_bytes": 1234}, "summary": {"ready": 1, "published": 1, "success": True}}, 200
        else:
            payload, status = {"error": f"unexpected mocked request: {method} {path}"}, 500
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport=viewport, has_touch=has_touch)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.get_by_text("Count unavailable", exact=True)).to_be_visible()
        page.get_by_role("button", name="Select all playlists").click()
        assert page.locator(".spotify-playlist:checked").count() == 2
        assert not page.get_by_label("Liked Songs").is_checked()
        page.get_by_role("button", name="Clear all playlists").click()
        assert page.locator(".spotify-playlist:checked").count() == 0
        assert not page.get_by_label("Liked Songs").is_checked()
        page.locator('input[value="playlist-unknown"]').check()
        page.get_by_label("Liked Songs").check()
        page.get_by_role("button", name="Find Clone Hero charts").click()
        playwright.expect(page.get_by_text("140 of 180 checks complete", exact=True)).to_be_visible()
        assert page.locator("#progressPercent").inner_text() == "78%"
        page.get_by_role("heading", name="1 song has a recommendation.").wait_for()
        scope_posts = [json.loads(body or "{}") for method, path, body in api_calls if method == "POST" and path == "/api/spotify/scope"]
        assert scope_posts and scope_posts[-1]["playlist_ids"] == ["playlist-unknown"]
        assert page.locator("[data-step=confirm]").get_attribute("aria-current") == "step"
        _assert_no_overflow(page)
        _capture(page, f"confirm-{viewport['width']}x{viewport['height']}.png")

        page.get_by_role("button", name="Search again").click()
        playwright.expect(page.get_by_label("Liked Songs")).to_be_visible()
        page.get_by_role("button", name="Find Clone Hero charts").click()
        page.get_by_role("heading", name="1 song has a recommendation.").wait_for()

        concern = page.get_by_text("1 chart concern", exact=True)
        if has_touch:
            concern.click()
            assert page.get_by_text("This chart is an alternate album version.").is_visible()
            concern.click()
        concern.focus()
        page.keyboard.press("Enter")
        assert page.get_by_text("This chart is an alternate album version.").is_visible()
        _assert_no_overflow(page)

        page.locator("#selectBestButton").click()
        page.locator("#continueDownloadButton").wait_for(state="visible")
        try:
            playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()
        except AssertionError:
            pytest.fail(f"selection did not settle: calls={api_calls!r}, error={page.locator('#appError').inner_text()!r}")
        assert page.locator("#selectBestButton").get_attribute("aria-pressed") == "true"
        assert page.get_by_text("Best Matches preset", exact=True).is_visible()
        assert page.get_by_text("Selected chart", exact=True).is_visible()
        assert page.locator("#continueDownloadButton").inner_text() == "Continue with 1 selected chart"
        page.get_by_role("button", name="Continue with 1 selected chart").click()
        assert page.get_by_role("heading", name="Prepare your Riff library.").is_visible()
        assert "Songs folder" not in page.locator("#stageDownload").inner_text()
        page.get_by_role("button", name="Prepare my Riff library").click()
        page.get_by_role("heading", name="Your Riff library is ready.").wait_for()
        download = page.get_by_role("link", name="Download library")
        assert download.is_visible()
        assert download.bounding_box()["height"] >= 44
        assert page.get_by_text("Scan Songs.", exact=True).is_visible()
        _assert_no_overflow(page)
        _capture(page, f"done-{viewport['width']}x{viewport['height']}.png")
        browser.close()
