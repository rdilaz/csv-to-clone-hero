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

from tests.public_signal_fixtures import SignalCheckFixtures


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
    monkeypatch.delenv("GOFUNDME_URL", raising=False)
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
    result = page.evaluate("""() => ({
      fits: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      offenders: [...document.querySelectorAll('body *')]
        .filter(element => {
          const box = element.getBoundingClientRect();
          return box.left < -0.5 || box.right > document.documentElement.clientWidth + 0.5;
        })
        .slice(0, 20)
        .map(element => ({tag: element.tagName, id: element.id, className: element.className, box: element.getBoundingClientRect().toJSON()})),
      internalOverflow: [...document.querySelectorAll('body *')]
        .filter(element => element.scrollWidth > element.clientWidth + 1)
        .slice(0, 20)
        .map(element => ({tag: element.tagName, id: element.id, className: element.className, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})),
    })""")
    assert result["fits"], json.dumps(result, sort_keys=True)


def _assert_in_viewport(page, selector):
    box = page.locator(selector).bounding_box()
    assert box is not None
    assert box["y"] >= 0
    assert box["y"] + box["height"] <= page.evaluate("window.innerHeight")


def _capture(page, name):
    root = os.environ.get("RIFF_BROWSER_EVIDENCE_DIR", "").strip()
    if not root:
        return
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(destination / name), full_page=False)


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
                assert page.get_by_role("heading", name="Music you love. Ready to play.").is_visible()
                login = page.get_by_role("link", name="Connect Spotify")
                assert login.is_visible()
                assert login.bounding_box()["height"] >= 44
                support = page.get_by_role(
                    "link", name="Support the creator of Clone Hero on GoFundMe"
                )
                assert support.is_visible()
                assert support.get_attribute("href") == (
                    "https://www.gofundme.com/f/"
                    "cancer-deudas-y-a-punto-de-perder-nuestro-hogar-apoyanos"
                )
                assert support.get_attribute("target") == "_blank"
                assert support.get_attribute("rel") == "noopener noreferrer"
                support.focus()
                assert support.evaluate("element => document.activeElement === element")
                assert page.get_by_text("Your music", exact=True).is_visible()
                assert page.get_by_text("Community charts", exact=True).is_visible()
                _assert_no_overflow(page)
                assert not [error for error in errors if "Content Security Policy" in error]
                _capture(page, f"landing-{viewport['width']}x{viewport['height']}.png")
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize(
    "viewport",
    [{"width": 390, "height": 844}, {"width": 1440, "height": 900}],
)
def test_running_find_eta_restores_without_overflow(public_server, viewport):
    _app, base_url, cookie_name, cookie = public_server
    job_polls = {"count": 0}

    def route_api(route):
        path = route.request.url.split(base_url, 1)[-1]
        if path == "/api/journey":
            payload = {
                "workspace_id": 1,
                "workspace_status": "running",
                "workspace": {"id": 1, "status": "running", "phase": "finding_charts"},
                "sources": [],
                "combined": {"tracks": 100, "artists": 20},
                "job": {"id": 9, "status": "running", "phase": "finding_charts"},
                "review": {"strong": 0, "review": 0, "unavailable": 0, "selected": 0, "total": 0},
                "download": None,
            }
        elif path == "/api/jobs/9":
            job_polls["count"] += 1
            early = job_polls["count"] == 1
            payload = {"job": {
                "id": 9,
                "status": "running",
                "phase": "finding_charts",
                "progress": {
                    "total_tracks": 100,
                    "processed_tracks": 1 if early else 10,
                    "total_work": 100,
                    "completed_work": 80 if early else 90,
                    "elapsed_seconds": 2 if early else 20,
                },
            }}
        else:
            payload = {"error": f"unexpected mocked request: {path}"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport=viewport)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.get_by_text("1 of 100 songs checked", exact=True)).to_be_visible()
        assert page.locator("#progressEta").inner_text() == "Estimating..."
        playwright.expect(page.get_by_text("10 of 100 songs checked", exact=True)).to_be_visible(timeout=3000)
        assert page.locator("#progressEta").inner_text() == "About 2-4 minutes"
        _assert_in_viewport(page, "#progressEta")
        _assert_no_overflow(page)

        _capture(page, f"find-{viewport['width']}x{viewport['height']}.png")
        page.reload(wait_until="networkidle")
        playwright.expect(page.get_by_text("10 of 100 songs checked", exact=True)).to_be_visible()
        assert page.locator("#progressEta").inner_text() == "About 2-4 minutes"
        _assert_in_viewport(page, "#progressEta")
        _assert_no_overflow(page)
        browser.close()


@pytest.mark.parametrize(
    "viewport",
    [{"width": 390, "height": 844}, {"width": 1440, "height": 900}],
)
def test_signal_check_confirm_keeps_multi_chart_selection_truthful(
    public_server, viewport
):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport=viewport, has_touch=viewport["width"] < 720)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", fixtures.fulfill)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.get_by_role("heading", name="Choose your charts")).to_be_visible()
        playwright.expect(page.locator("#selectedCount")).to_have_text("3")
        playwright.expect(page.locator("#exactSelectedCount")).to_have_text("3")
        playwright.expect(page.locator("#selectedSongCount")).to_have_text("1")
        playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("0")
        playwright.expect(page.locator('[data-candidate="101"][data-selection="clear"]')).to_be_visible()
        assert page.locator('[data-candidate="101"]').get_attribute("aria-pressed") is None
        assert page.get_by_role(
            "button",
            name="Selected Misery Business chart by Atlas Charts. Clear this choice.",
        ).is_visible()
        same_recording = page.locator('details[data-disclosure-key$="SAME_RECORDING_CHART_VARIANT"]')
        alternate = page.locator('details[data-disclosure-key$="ALTERNATE_RECORDING"]')
        assert same_recording.get_attribute("open") is not None
        assert alternate.get_attribute("open") is not None
        assert page.get_by_text("Guitar difficulty 0", exact=True).is_visible()
        assert page.get_by_text("Different live recording.", exact=True).is_visible()
        _assert_no_overflow(page)

        if viewport["width"] <= 1000:
            group_box = page.locator(".song-group").first.bounding_box()
            action_box = page.locator('[data-candidate="101"]').bounding_box()
            band_box = page.locator("#selectionRail").bounding_box()
            assert group_box is not None and action_box is not None and band_box is not None
            if viewport["width"] == 390:
                banner_box = page.locator(".cause-bar").bounding_box()
                assert banner_box is not None and banner_box["height"] <= 44
                assert group_box["y"] <= 380 + banner_box["height"]
            assert "is-in-flow" not in (page.locator("#selectionRail").get_attribute("class") or "")
            assert action_box["y"] + action_box["height"] + 7 <= band_box["y"]
        else:
            rail_box = page.locator("#selectionRail").bounding_box()
            continue_box = page.locator("#continueDownloadButton").bounding_box()
            assert rail_box is not None and continue_box is not None
            assert rail_box["height"] < viewport["height"] * 0.75
            assert continue_box["y"] + continue_box["height"] <= viewport["height"]
        _capture(page, f"confirm-{viewport['width']}x{viewport['height']}.png")

        sibling = page.locator('[data-candidate="102"]')
        page.locator('details[data-disclosure-key$=":102:concerns"] > summary').click()
        sibling.focus()
        sibling.click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("2")
        replacement = page.locator('[data-candidate="102"][data-selection="select"]')
        playwright.expect(replacement).to_be_focused()
        assert page.locator('[data-candidate="101"][data-selection="clear"]').count() == 1
        assert page.locator('[data-candidate="103"][data-selection="clear"]').count() == 1
        assert same_recording.get_attribute("open") is not None
        assert page.locator('details[data-disclosure-key$=":102:concerns"]').get_attribute("open") is not None

        page.reload(wait_until="networkidle")
        playwright.expect(page.locator("#selectedCount")).to_have_text("2")
        assert page.locator('[data-candidate="101"][data-selection="clear"]').count() == 1
        assert page.locator('[data-candidate="103"][data-selection="clear"]').count() == 1
        page.locator('details[data-disclosure-key$="SAME_RECORDING_CHART_VARIANT"] > summary').click()
        page.locator('[data-candidate="102"][data-selection="select"]').click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("3")

        page.get_by_role("button", name="Possible matches").click()
        possible = page.locator('[data-candidate="201"][data-selection="select"]')
        playwright.expect(possible).to_be_visible()
        assert page.get_by_text("Guitar difficulty not provided", exact=True).is_visible()
        posts_before = len([
            body for method, path, body in fixtures.request_log
            if method == "POST" and path == "/api/selection" and body.get("candidate_id") == 201
        ])
        page.once("dialog", lambda dialog: dialog.dismiss())
        possible.click()
        posts_after_cancel = len([
            body for method, path, body in fixtures.request_log
            if method == "POST" and path == "/api/selection" and body.get("candidate_id") == 201
        ])
        assert posts_after_cancel == posts_before

        page.once("dialog", lambda dialog: dialog.accept())
        possible.click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("4")
        possible_post = [
            body for method, path, body in fixtures.request_log
            if method == "POST" and path == "/api/selection" and body.get("candidate_id") == 201
        ][-1]
        assert possible_post["confirm_possible"] is True

        fixtures.fail_next_selection = True
        page.locator('[data-review-filter="selected"]').click()
        page.locator('[data-candidate="201"][data-selection="clear"]').click()
        playwright.expect(page.locator("#selectionError")).to_have_text(
            "Selection service is temporarily unavailable."
        )
        playwright.expect(page.locator("#selectedCount")).to_have_text("4")
        assert page.locator('[data-candidate="201"][data-selection="clear"]').count() == 1
        page.locator('[data-candidate="201"][data-selection="clear"]').click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("3")
        playwright.expect(page.locator("#reviewSearch")).to_be_focused()
        _assert_no_overflow(page)
        assert fixtures.unexpected_requests == []
        browser.close()


def test_signal_check_retries_a_failed_post_mutation_recount(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")
    mutation_saved = {"value": False}
    recount_failed = {"value": False}
    pending_mutation = []

    def route_api(route):
        path = route.request.url.split(base_url, 1)[-1]
        if route.request.method == "POST" and path == "/api/selection":
            pending_mutation.append(route)
            return
        if path.startswith("/api/workspace?") and mutation_saved["value"] and not recount_failed["value"]:
            recount_failed["value"] = True
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"error": "Selection summary is temporarily unavailable."}),
            )
            return
        fixtures.fulfill(route)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        page.locator('[data-candidate="102"][data-selection="clear"]').click()
        page.wait_for_timeout(50)
        assert pending_mutation
        playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
        assert all(
            page.locator('[data-selection]').nth(index).is_disabled()
            for index in range(page.locator('[data-selection]').count())
        )
        mutation_saved["value"] = True
        fixtures.fulfill(pending_mutation.pop())
        playwright.expect(page.locator("#selectionError")).to_contain_text(
            "Chart choice cleared. Riff could not refresh the selection summary."
        )
        playwright.expect(page.locator("#selectedCount")).to_have_text("...")
        playwright.expect(page.locator("#exactSelectedCount")).to_have_text("3")
        playwright.expect(page.locator('[data-candidate="102"]')).to_be_disabled()
        playwright.expect(page.locator('[data-candidate="102"]')).to_contain_text("Selected")
        playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
        retry = page.locator("#retrySelectionButton")
        playwright.expect(retry).to_be_visible()

        retry.click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("2")
        playwright.expect(page.locator('[data-candidate="102"][data-selection="select"]')).to_be_enabled()
        assert retry.is_hidden()
        assert fixtures.unexpected_requests == []
        browser.close()


def test_signal_check_keeps_combined_count_unknown_when_expansion_recount_fails(
    public_server,
):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")

    def route_api(route):
        if "/api/expansion/results" in route.request.url:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"error": "Expansion summary is temporarily unavailable."}),
            )
            return
        fixtures.fulfill(route)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.locator("#exactSelectedCount")).to_have_text("3")
        playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("...")
        playwright.expect(page.locator("#selectedCount")).to_have_text("...")
        playwright.expect(page.locator("#selectionMode")).to_have_text(
            "Updating the combined selection total..."
        )
        playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()
        assert page.locator("#continueDownloadButton").get_attribute("aria-label") == (
            "Continue with the currently confirmed chart selections"
        )
        browser.close()


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 768, "height": 1024},
        {"width": 960, "height": 900},
        {"width": 1024, "height": 900},
    ],
)
def test_signal_check_selection_summary_reflows_at_tablet_widths(
    public_server, viewport
):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport=viewport)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", fixtures.fulfill)
        page.goto(base_url + "/", wait_until="networkidle")

        rail = page.locator("#selectionRail")
        rail_box = rail.bounding_box()
        continue_box = page.locator("#continueDownloadButton").bounding_box()
        source_box = page.locator(".song-group > .candidate-row .candidate-source").first.bounding_box()
        assert rail_box is not None and continue_box is not None and source_box is not None
        if viewport["width"] <= 1000:
            assert rail.evaluate("element => getComputedStyle(element).position") == "fixed"
            assert rail_box["height"] <= viewport["height"] * 0.2
            action_box = page.locator('[data-candidate="101"]').bounding_box()
            assert action_box is not None
            assert action_box["y"] + action_box["height"] + 7 <= rail_box["y"]
        else:
            assert rail.evaluate("element => getComputedStyle(element).position") == "sticky"
            assert rail_box["height"] < viewport["height"] * 0.75
            assert continue_box["y"] + continue_box["height"] <= viewport["height"]
        _assert_no_overflow(page)
        if viewport["width"] in {768, 1024}:
            page.evaluate("document.documentElement.style.fontSize = '200%'; window.dispatchEvent(new Event('resize'))")
            page.wait_for_timeout(50)
            _assert_no_overflow(page)
            if viewport["width"] == 1024:
                page.locator("#continueDownloadButton").scroll_into_view_if_needed()
                _assert_in_viewport(page, "#continueDownloadButton")
        browser.close()


def test_signal_check_add_refresh_failure_keeps_cached_choices(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("add")
    fail_refresh = {"value": False}

    def route_api(route):
        if route.request.method == "GET" and "/api/spotify/scope" in route.request.url and fail_refresh["value"]:
            route.fulfill(
                status=502,
                content_type="application/json",
                body=json.dumps({"error": "Spotify refresh is temporarily unavailable."}),
            )
            return
        fixtures.fulfill(route)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        page.get_by_label("Heavy rotation").check()
        fail_refresh["value"] = True
        page.get_by_role("button", name="Refresh Spotify").click()
        playwright.expect(page.locator("#appError")).to_have_text(
            "Spotify refresh is temporarily unavailable."
        )
        playwright.expect(page.locator("#appError")).to_be_focused()
        assert page.get_by_label("Heavy rotation").is_checked()
        assert page.get_by_label("Road trip").is_checked()
        assert page.locator("#spotifyFreshness").inner_text() == (
            "Spotify could not be refreshed. Saved choices are still shown."
        )
        _assert_no_overflow(page)
        browser.close()


def test_signal_check_unknown_progress_never_invents_percentage(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("find")

    def route_api(route):
        if route.request.method == "GET" and route.request.url.endswith("/api/jobs/41"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"job": {
                    "id": 41,
                    "kind": "discovery",
                    "status": "running",
                    "phase": "catalog_prepare",
                    "message": "Preparing the chart catalog",
                    "progress": {"current_song": None},
                }}),
            )
            return
        fixtures.fulfill(route)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.locator("#progressPercent")).to_have_text("Working...")
        assert page.locator("#progressDisc").get_attribute("aria-valuenow") is None
        assert "is-indeterminate" in (page.locator("#progressDisc").get_attribute("class") or "")
        playwright.expect(page.locator("#progressCount")).to_have_text("Preparing your music")
        _assert_no_overflow(page)
        browser.close()


def test_signal_check_forced_colors_keeps_progress_meter_distinct(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("find")

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(
            viewport={"width": 390, "height": 844}, forced_colors="active"
        )
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", fixtures.fulfill)
        page.goto(base_url + "/", wait_until="networkidle")

        styles = page.locator("#progressDisc").evaluate("""element => {
          const track = getComputedStyle(element);
          const fill = getComputedStyle(element.firstElementChild);
          return {
            borderStyle: track.borderTopStyle,
            borderWidth: track.borderTopWidth,
            track: track.backgroundColor,
            fill: fill.backgroundColor,
          };
        }""")
        assert styles["borderStyle"] == "solid"
        assert styles["borderWidth"] != "0px"
        assert styles["track"] != styles["fill"]
        browser.close()


def test_signal_check_ignores_out_of_order_review_reads(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")
    pending = []
    workspace_reads = {"count": 0}

    def route_api(route):
        if route.request.method == "GET" and "/api/workspace?" in route.request.url:
            workspace_reads["count"] += 1
            if workspace_reads["count"] > 1:
                pending.append(route)
                return
        fixtures.fulfill(route)

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        search = page.locator("#reviewSearch")
        search.fill("radiohead")
        page.wait_for_timeout(350)
        assert len(pending) == 1
        search.fill("paramore")
        page.wait_for_timeout(350)
        assert len(pending) == 2

        newest = pending.pop()
        fixtures.fulfill(newest)
        playwright.expect(page.locator(".song-group__identity h3")).to_have_text("Misery Business")
        assert page.get_by_role("heading", name="Weird Fishes / Arpeggi").count() == 0

        oldest = pending.pop()
        fixtures.fulfill(oldest)
        page.wait_for_timeout(100)
        assert page.locator(".song-group__identity h3").inner_text() == "Misery Business"
        assert page.get_by_role("heading", name="Weird Fishes / Arpeggi").count() == 0
        _assert_no_overflow(page)
        browser.close()


def test_signal_check_reflows_at_320_and_respects_reduced_motion(public_server):
    _app, base_url, cookie_name, cookie = public_server
    fixtures = SignalCheckFixtures("confirm")

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(
            viewport={"width": 320, "height": 800},
            has_touch=True,
            reduced_motion="reduce",
        )
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", fixtures.fulfill)
        page.goto(base_url + "/", wait_until="networkidle")

        _assert_no_overflow(page)
        assert page.locator(".journey-steps li").count() == 5
        assert all(
            box and box["x"] >= 0 and box["x"] + box["width"] <= 320
            for box in [page.locator(".journey-steps li").nth(index).bounding_box() for index in range(5)]
        )
        assert page.locator(".selection-options").get_attribute("open") is None
        for selector in [".cause-bar__link", ".brand", ".selection-options > summary", ".concern-details > summary"]:
            box = page.locator(selector).first.bounding_box()
            assert box is not None and box["height"] >= 44
        identity_style = page.locator(".candidate-identity h4").first.evaluate(
            "element => ({family: getComputedStyle(element).fontFamily, size: parseFloat(getComputedStyle(element).fontSize)})"
        )
        evidence_size = page.locator(".candidate-evidence small").first.evaluate(
            "element => parseFloat(getComputedStyle(element).fontSize)"
        )
        assert "IBM Plex Sans Riff" in identity_style["family"]
        assert identity_style["size"] >= 16
        assert evidence_size >= 14
        page.locator(".selection-options > summary").focus()
        focus_style = page.locator(".selection-options > summary").evaluate(
            "element => ({outline: getComputedStyle(element).outlineColor, shadow: getComputedStyle(element).boxShadow})"
        )
        assert focus_style["shadow"] != "none"
        rail = page.locator("#selectionRail")
        action_box = page.locator('[data-candidate="101"]').bounding_box()
        rail_box = rail.bounding_box()
        assert action_box is not None and rail_box is not None
        if "is-in-flow" not in (rail.get_attribute("class") or ""):
            assert action_box["y"] + action_box["height"] + 7 <= rail_box["y"]
            assert rail_box["height"] <= 800 * 0.2
        animation_duration = page.locator(".loading-line span").evaluate(
            "element => getComputedStyle(element).animationDuration"
        )
        assert animation_duration in {"0s", "1e-05s", "0.001ms", "0.01ms"}
        page.evaluate("document.documentElement.style.fontSize = '200%'; window.dispatchEvent(new Event('resize'))")
        page.wait_for_timeout(50)
        _assert_no_overflow(page)
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
    pending_refresh = []

    def journey_payload():
        complete = job_polls["count"] > 0
        saved_scope = (
            {"include_liked": True, "playlist_ids": ["playlist-unknown"]}
            if scope_saved["value"]
            else {"include_liked": False, "playlist_ids": []}
        )
        imported_scope = (
            {"known": True, "include_liked": True, "playlist_ids": ["playlist-unknown"]}
            if complete
            else {"known": True, "include_liked": False, "playlist_ids": ["playlist-1"]}
        )
        return {
            "workspace_id": 1,
            "workspace_status": "complete" if complete else "created",
            "workspace": {"id": 1, "status": "complete" if complete else "created", "phase": "complete" if complete else "source_setup", "updated_at": time.time()},
            "spotify_scope": saved_scope,
            "spotify_library": {
                "playlists": [
                    {"id": "playlist-1", "name": "Road trip", "track_count": 12, "owner_name": "Browser Player"},
                    {"id": "playlist-unknown", "name": "Current API playlist", "track_count": None, "owner_name": "Browser Player"},
                ],
                "liked_count": 1,
                "workspace": {"id": 1, "exists": True, "scope": saved_scope, "soundcloud_scope": {}},
                "cache_available": True,
                "cached": True,
                "cache_source": "spotify",
                "last_refreshed_at": 1788552000,
                "last_imported_at": 1788551000,
                "known_unique_songs": 2,
                "imported_scope": imported_scope,
            },
            "soundcloud_library": {
                "status": "active",
                "known_unique_songs": 1,
                "last_imported_at": 1788551000,
                "current_scope": {"include_liked": False, "playlist_ids": []},
                "imported_scope": {"known": True, "include_liked": False, "playlist_ids": []},
            },
            "sources": [
                {"provider": "spotify", "tracks": 2, "unique_tracks": 2, "playlists": 1, "raw_items": 3, "liked": 1, "playlist_only": 1},
                {"provider": "soundcloud", "tracks": 1, "unique_tracks": 1, "playlists": 1, "raw_items": 1, "liked": 0, "playlist_only": 1},
            ],
            "combined": {"tracks": 2, "artists": 2},
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
            "result_tier": "RECOMMENDED_EXACT",
            "result_tier_reasons": ["safe same-song recommendation"],
            "selectable": True,
            "review": {
                "state": "strong",
                "label": "Strong match",
                "summary": "The song and artist identities agree.",
                "concerns": ["This chart is an alternate album version."],
                "selectable": True,
                "action": "Select chart",
            },
        }
        alternate = {
            "candidate_id": 42,
            "source": "enchor",
            "chart_id": 420,
            "artist": "Paramore",
            "title": "Misery Business (Live)",
            "charter": "Live Charter",
            "pack_name": "Live set",
            "instruments": ["guitar", "drums"],
            "diff_guitar": 4,
            "selected": False,
            "best": False,
            "result_tier": "ALTERNATE_RECORDING",
            "result_tier_reasons": ["recording duration differs"],
            "selectable": True,
            "review": {
                "state": "needs_review",
                "label": "Alternate recording",
                "summary": "The recording evidence differs.",
                "concerns": ["Recording duration differs from the imported track."],
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
                "candidates": [candidate, alternate],
            }],
            "total": 1,
            "offset": 0,
            "limit": 25,
            "summary": {"strong": 1, "review": 0, "unavailable": 0, "selected": int(selected["value"]), "selected_strong": int(selected["value"]), "selected_review": 0, "selection_count": int(selected["value"]), "recommended_songs": 1, "additional_playable_charts": 1, "selectable_charts": 2, "all_safe_charts": 1, "maximum_playable_charts": 2, "total": 1},
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
                    "cache_available": True,
                    "cached": False,
                    "cache_source": "spotify",
                    "last_refreshed_at": time.time(),
                    "last_imported_at": 1788551000,
                    "known_unique_songs": 2,
                    "imported_scope": {"known": True, "include_liked": False, "playlist_ids": ["playlist-1"]},
                }
                pending_refresh.append((route, payload))
                return
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
            if job_polls["count"] <= 2:
                payload = {"job": {"id": 9, "status": "running", "phase": "finding_charts", "message": "Matching", "progress": {"total_tracks": 200, "completed_tracks": 50, "total_work": 180, "completed_work": 140, "elapsed_seconds": 100}}}
            else:
                payload = {"job": {"id": 9, "status": "complete", "phase": "complete", "message": "Matching complete", "progress": {"total_tracks": 1, "processed_tracks": 1}}}
            status = 200
        elif path.startswith("/api/workspace?"):
            payload, status = workspace_payload(), 200
        elif path == "/api/expansion/artists":
            payload, status = {"available": False, "reason": "Expansion is not enabled for this fixture.", "artists": [], "job": None}, 200
        elif path.startswith("/api/expansion/results"):
            payload, status = {"songs": [], "total": 0, "offset": 0, "limit": 25, "has_more": False, "summary": {"selected": 0}}, 200
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

        page.get_by_text("Road trip", exact=True).wait_for()
        assert sum(
            method == "GET" and path == "/api/journey"
            for method, path, _body in api_calls
        ) == 1
        assert not [
            call for call in api_calls
            if call[0] == "GET" and call[1] == "/api/spotify/scope"
        ]
        playwright.expect(page.get_by_text("Road trip", exact=True)).to_be_visible()
        playwright.expect(
            page.get_by_text("Showing saved Spotify data.", exact=False)
        ).to_be_visible()
        source_cards = page.locator(".source-card")
        spotify_card = source_cards.nth(0).bounding_box()
        soundcloud_card = source_cards.nth(1).bounding_box()
        assert spotify_card is not None and soundcloud_card is not None
        soundcloud_title = page.locator(".source-card--soundcloud h2")
        assert soundcloud_title.is_visible()
        assert page.locator(".source-card--soundcloud .source-icon").is_visible()
        assert page.get_by_text("CSV or JSON export", exact=True).is_visible()
        assert page.locator("#soundcloudCount").is_visible()
        page.locator("#soundcloudCount").evaluate(
            "element => { element.textContent = '2,354 unique songs, 53 playlists'; }"
        )
        assert soundcloud_title.evaluate(
            "element => element.scrollWidth <= element.clientWidth "
            "&& element.getBoundingClientRect().height < 30"
        )
        title_box = soundcloud_title.bounding_box()
        badge_box = page.locator("#soundcloudCount").bounding_box()
        assert title_box is not None and badge_box is not None
        assert title_box["y"] + title_box["height"] <= badge_box["y"]
        if viewport["width"] > 820:
            assert abs(spotify_card["y"] - soundcloud_card["y"]) < 2
            assert spotify_card["width"] > soundcloud_card["width"]
            assert 0.35 < soundcloud_card["width"] / spotify_card["width"] < 0.6
        else:
            assert soundcloud_card["y"] >= spotify_card["y"] + spotify_card["height"]
        _capture(page, f"add-music-{viewport['width']}x{viewport['height']}.png")
        page.locator("#soundcloudCount").evaluate(
            "element => { element.textContent = '1 unique song, 1 playlist'; }"
        )

        page.get_by_role("button", name="Refresh Spotify").click()
        playwright.expect(
            page.get_by_role("button", name="Refreshing...")
        ).to_be_disabled()
        page.wait_for_timeout(50)
        assert pending_refresh
        page.locator('input[value="playlist-1"]').check()
        refresh_route, refresh_payload = pending_refresh.pop()
        refresh_route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(refresh_payload),
        )
        playwright.expect(
            page.get_by_text("Updated from Spotify just now.", exact=True)
        ).to_be_visible()
        assert page.locator('input[value="playlist-1"]').is_checked()
        assert sum(
            method == "GET" and path == "/api/spotify/scope"
            for method, path, _body in api_calls
        ) == 1
        playwright.expect(page.get_by_text("Count unavailable", exact=True)).to_be_visible()
        playwright.expect(page.get_by_text(
            "1 playlist; 3 selected source entries became 2 unique songs "
            "(1 liked, 1 playlist-only).",
            exact=True,
        )).to_be_visible()
        assert page.locator("#soundcloudCount").inner_text() == "1 unique song, 1 playlist"
        page.get_by_role("button", name="Select all playlists").click()
        assert page.locator(".spotify-playlist:checked").count() == 2
        assert not page.get_by_label("Liked Songs").is_checked()
        page.get_by_role("button", name="Clear playlist checks").click()
        assert page.locator(".spotify-playlist:checked").count() == 0
        assert not page.get_by_label("Liked Songs").is_checked()
        page.locator('input[value="playlist-unknown"]').check()
        page.get_by_label("Liked Songs").check()
        playwright.expect(
            page.get_by_text("At least 1 selected Spotify entry", exact=True)
        ).to_be_visible()
        playwright.expect(page.get_by_text(
            "Playlist memberships and Liked Songs can overlap. The exact unique "
            "total will be determined after import and cross-provider deduplication. "
            "1 unique SoundCloud song is already imported.",
            exact=True,
        )).to_be_visible()
        add_music_text = page.locator("#stageAdd").inner_text()
        assert "unique Spotify tracks" not in add_music_text
        assert "imported SoundCloud songs" not in add_music_text
        page.get_by_role("button", name="Find charts").click()
        playwright.expect(page.get_by_text("50 of 200 songs checked", exact=True)).to_be_visible()
        assert page.locator("#progressPercent").inner_text() == "25%"
        assert page.locator("#progressEta").inner_text() == "About 4-6 minutes"
        _assert_in_viewport(page, "#progressEta")
        _assert_no_overflow(page)
        page.get_by_role("heading", name="Choose your charts").wait_for()
        workspace_requests = [
            path for method, path, _body in api_calls
            if method == "GET" and path.startswith("/api/workspace?")
        ]
        assert workspace_requests
        assert "tier=RECOMMENDED_EXACT%2CSAME_RECORDING_CHART_VARIANT%2CALTERNATE_RECORDING%2CPOSSIBLE_MATCH" in workspace_requests[-1]
        page.get_by_text("Alternate versions (1)", exact=True).click()
        playwright.expect(
            page.get_by_text("Different live recording.", exact=True)
        ).to_be_visible()
        page.locator('details[data-disclosure-key$="ALTERNATE_RECORDING"] .concern-details > summary').click()
        assert page.get_by_text(
            "Recording duration differs from the imported track.", exact=True
        ).is_visible()
        scope_posts = [json.loads(body or "{}") for method, path, body in api_calls if method == "POST" and path == "/api/spotify/scope"]
        assert scope_posts and scope_posts[-1]["playlist_ids"] == ["playlist-unknown"]
        assert page.locator("[data-step=confirm]").get_attribute("aria-current") == "step"
        _assert_no_overflow(page)
        _capture(page, f"journey-confirm-{viewport['width']}x{viewport['height']}.png")

        page.get_by_role("button", name="Search again").click()
        playwright.expect(page.get_by_label("Liked Songs")).to_be_visible()
        assert sum(
            method == "GET" and path == "/api/journey"
            for method, path, _body in api_calls
        ) == 2
        assert sum(
            method == "GET" and path == "/api/spotify/scope"
            for method, path, _body in api_calls
        ) == 1
        playwright.expect(
            page.get_by_text("2 combined unique songs in the latest import", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Find charts").click()
        page.get_by_role("heading", name="Choose your charts").wait_for()

        concern = page.locator(".song-group > .candidate-row .concern-details > summary")
        if has_touch:
            concern.click()
            assert page.get_by_text("This chart is an alternate album version.").is_visible()
            concern.click()
        concern.focus()
        page.keyboard.press("Enter")
        assert page.get_by_text("This chart is an alternate album version.").is_visible()
        _assert_no_overflow(page)

        if has_touch:
            page.locator(".selection-options > summary").click()
        page.locator("#selectBestButton").click()
        page.locator("#continueDownloadButton").wait_for(state="visible")
        try:
            playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()
        except AssertionError:
            pytest.fail(f"selection did not settle: calls={api_calls!r}, error={page.locator('#appError').inner_text()!r}")
        assert page.locator("#selectionMode").inner_text() == "Last action: selected recommended charts."
        assert page.get_by_role("button", name="Selected Misery Business chart by Community Charter. Clear this choice.").is_visible()
        assert page.locator("#continueDownloadButton").inner_text() == "Continue"
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


def test_partial_source_counts_never_claim_exact_combined_total(public_server):
    _app, base_url, cookie_name, cookie = public_server

    def route_api(route):
        path = route.request.url.split(base_url, 1)[-1]
        if path == "/api/journey":
            payload, status = {
                "workspace_id": 1,
                "workspace_status": "partial",
                "workspace": {"id": 1, "status": "partial", "phase": "source_setup"},
                "spotify_scope": {"include_liked": True, "playlist_ids": []},
                "spotify_library": {
                    "playlists": [],
                    "liked_count": None,
                    "workspace": {"id": 1, "exists": True, "scope": {"include_liked": True, "playlist_ids": []}, "soundcloud_scope": {}},
                    "cache_available": True,
                    "cached": True,
                    "cache_source": "latest_import",
                    "known_unique_songs": 1,
                    "import_status": "partial",
                    "imported_scope": {"known": False, "include_liked": True, "playlist_ids": []},
                },
                "soundcloud_library": {
                    "status": "partial",
                    "known_unique_songs": 2,
                    "current_scope": {"include_liked": False, "playlist_ids": []},
                    "imported_scope": {"known": False, "include_liked": False, "playlist_ids": []},
                },
                "sources": [
                    {"provider": "spotify", "status": "partial", "tracks": 1, "unique_tracks": 1, "playlists": 0, "raw_items": 2, "liked": 1, "playlist_only": 0},
                    {"provider": "soundcloud", "status": "partial", "tracks": 2, "unique_tracks": 2, "playlists": 1, "raw_items": 3, "liked": 0, "playlist_only": 2},
                ],
                "combined": {"tracks": 3, "artists": 3},
                "job": None,
                "review": {"strong": 0, "review": 0, "unavailable": 0, "selected": 0, "total": 0},
                "download": None,
            }, 200
        elif path == "/api/auth/soundcloud/status":
            payload, status = {"configured": False, "authenticated": False}, 200
        else:
            payload, status = {"error": f"unexpected mocked request: {path}"}, 500
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        assert "Partial import:" in page.locator("#spotifyImportedSummary").inner_text()
        assert page.locator("#soundcloudCount").inner_text().startswith("Partial:")
        playwright.expect(
            page.get_by_text("Spotify sources selected", exact=True)
        ).to_be_visible()
        assert "combined unique songs in the latest import" not in page.locator("#musicTally").inner_text()
        assert "exact unique total will be determined after import" in page.locator("#musicTally").inner_text()
        _assert_no_overflow(page)
        browser.close()


def test_unimported_soundcloud_scope_invalidates_combined_count(public_server):
    _app, base_url, cookie_name, cookie = public_server

    def route_api(route):
        path = route.request.url.split(base_url, 1)[-1]
        if path == "/api/journey":
            payload, status = {
                "workspace_id": 1,
                "workspace_status": "complete",
                "workspace": {"id": 1, "status": "complete", "phase": "source_setup"},
                "spotify_scope": {"include_liked": True, "playlist_ids": []},
                "spotify_library": {
                    "playlists": [],
                    "liked_count": 10,
                    "workspace": {"id": 1, "exists": True, "scope": {"include_liked": True, "playlist_ids": []}, "soundcloud_scope": {"playlist_ids": ["new-set"], "include_liked": False}},
                    "cache_available": True,
                    "cached": True,
                    "cache_source": "spotify",
                    "known_unique_songs": 3,
                    "import_status": "active",
                    "imported_scope": {"known": True, "include_liked": True, "playlist_ids": []},
                },
                "soundcloud_library": {
                    "status": "",
                    "known_unique_songs": None,
                    "current_scope": {"include_liked": False, "playlist_ids": ["new-set"]},
                    "imported_scope": {"known": False, "include_liked": False, "playlist_ids": []},
                },
                "sources": [{"provider": "spotify", "status": "active", "tracks": 3, "unique_tracks": 3, "playlists": 0, "raw_items": 3, "liked": 3, "playlist_only": 0}],
                "combined": {"tracks": 3, "artists": 3},
                "job": None,
                "review": {"strong": 0, "review": 0, "unavailable": 0, "selected": 0, "total": 0},
                "download": None,
            }, 200
        elif path == "/api/auth/soundcloud/status":
            payload, status = {"configured": False, "authenticated": False}, 200
        else:
            payload, status = {"error": f"unexpected mocked request: {path}"}, 500
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(
            page.get_by_text("10 selected Spotify entries", exact=True)
        ).to_be_visible()
        tally = page.locator("#musicTally").inner_text()
        assert "3 combined unique songs in the latest import" not in tally
        assert "exact unique total will be determined after import" in tally
        browser.close()


def test_public_expansion_confirm_and_reload_in_browser(public_server):
    _app, base_url, cookie_name, cookie = public_server
    expansion_started = {"value": False}
    expansion_selected = {"value": False}
    expansion_job_polls = {"count": 0}
    fail_expansion_recount = {"value": False}

    def route_api(route):
        request = route.request
        path = request.url.split(base_url, 1)[-1]
        method = request.method
        if path == "/api/journey":
            job = (
                {"id": 12, "kind": "expansion", "status": "complete", "phase": "complete"}
                if expansion_started["value"]
                else {"id": 9, "kind": "discovery", "status": "complete", "phase": "complete"}
            )
            payload, status = {
                "workspace_id": 1,
                "workspace_status": "complete",
                "workspace": {"id": 1, "status": "complete", "phase": "complete"},
                "sources": [],
                "combined": {"tracks": 1, "artists": 1},
                "job": job,
                "review": {"strong": 0, "review": 0, "unavailable": 1, "selected": 0, "total": 1},
                "download": None,
            }, 200
        elif path.startswith("/api/workspace?"):
            payload, status = {
                "workspace": 1, "tracks": [], "total": 0, "offset": 0, "limit": 25,
                "summary": {"strong": 0, "review": 0, "unavailable": 1, "selected": 0, "selection_count": 0, "total": 1},
            }, 200
        elif path == "/api/expansion/artists":
            payload, status = {
                "available": True,
                "max_artists": 25,
                "artists": [{
                    "artist": "Paramore", "artist_norm": "paramore",
                    "eligible_songs": 8, "eligible_charts": 10,
                    "imported_songs": 2, "cap": 25,
                    "selected": expansion_started["value"],
                }],
                "job": ({"id": 12, "kind": "expansion", "status": "complete"} if expansion_started["value"] else None),
            }, 200
        elif path.startswith("/api/expansion/results?"):
            if expansion_selected["value"] and fail_expansion_recount["value"]:
                fail_expansion_recount["value"] = False
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "Expansion summary is temporarily unavailable."}))
                return
            candidate = {
                "candidate_id": 51, "source": "enchor", "chart_id": 510,
                "artist": "Paramore", "title": "Decode", "charter": "Community Charter",
                "pack_name": "Full band", "artist_norm": "paramore", "kind": "expansion",
                "instruments": ["guitar", "drums"], "diff_guitar": 4,
                "result_tier": "RECOMMENDED_EXACT", "result_tier_reasons": [],
                "score": 92, "quality": 20, "selected": expansion_selected["value"],
                "selectable": True, "best": True,
                "review": {"state": "strong", "label": "Recommended expansion", "concerns": [], "selectable": True},
            }
            payload, status = {
                "songs": [{
                    "song_key": "paramore|decode", "artist": "Paramore", "title": "Decode",
                    "artist_norm": "paramore", "selected": expansion_selected["value"],
                    "candidates": [candidate],
                }],
                "total": 1,
                "summary": {"total": 1, "selected": int(expansion_selected["value"])},
            }, 200
        elif path == "/api/expansion" and method == "POST":
            assert json.loads(request.post_data or "{}")["artists"] == ["paramore"]
            expansion_started["value"] = True
            payload, status = {"job": {"id": 12, "kind": "expansion", "status": "queued"}}, 202
        elif path == "/api/jobs/12":
            expansion_job_polls["count"] += 1
            payload, status = {"job": {
                "id": 12, "kind": "expansion", "status": "complete", "phase": "complete",
                "message": "Artist recommendations are ready.",
                "progress": {"completed_artists": 1, "total_artists": 1, "network_requests": 0},
            }}, 200
        elif path == "/api/selection" and method == "POST":
            body = json.loads(request.post_data or "{}")
            assert body["kind"] == "expansion"
            expansion_selected["value"] = body["action"] == "select"
            payload, status = {"ok": True}, 200
        else:
            payload, status = {"error": f"unexpected mocked request: {method} {path}"}, 500
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.route("**/api/**", route_api)
        page.goto(base_url + "/", wait_until="networkidle")

        playwright.expect(page.get_by_role("heading", name="Add more songs by artists you already love.")).to_be_visible()
        page.get_by_label("Paramore", exact=False).check()
        page.get_by_role("button", name="Find more songs by selected artists").click()
        playwright.expect(page.locator(".review-card--expansion")).to_contain_text("Decode")
        assert expansion_job_polls["count"] == 1
        playwright.expect(page.locator(".review-card--expansion")).to_contain_text("guitar, drums")
        playwright.expect(page.locator(".review-card--expansion")).to_contain_text("Guitar difficulty 4")
        fail_expansion_recount["value"] = True
        page.locator('[data-expansion-selection="select"]').click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("...")
        playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
        playwright.expect(page.locator("#retrySelectionButton")).to_be_visible()
        playwright.expect(page.locator('[data-expansion-selection="select"]')).to_be_disabled()
        page.locator("#retrySelectionButton").click()
        playwright.expect(page.locator("#selectedCount")).to_have_text("1")
        assert page.get_by_role("button", name="Continue with 1 selected chart").is_enabled()
        _assert_no_overflow(page)

        page.reload(wait_until="networkidle")
        assert expansion_job_polls["count"] == 1
        assert page.get_by_label("Paramore", exact=False).is_checked()
        playwright.expect(page.locator('[data-expansion-selection="clear"]')).to_be_visible()
        _assert_no_overflow(page)
        browser.close()
