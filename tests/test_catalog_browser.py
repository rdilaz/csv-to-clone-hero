"""Persistent catalog UI with synthetic API records and real native WAV playback.

No Enchor/Spotify traffic is sent. The package reader has separate binary tests;
this suite checks native media state, shell persistence and selection boundaries.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import wave
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import _assert_no_overflow, _capture, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium

pytestmark = pytest.mark.browser

MOCK_AUDIO = """(() => {
  window.qaAudio = {loads: [], cleanups: 0};
  window.RiffCatalogAudio = {load: async (item, options) => {
    window.qaAudio.loads.push(item.catalog_key);
    if (window.qaRejectAudio) throw new Error('Synthetic chart has no complete audio mix.');
    options.onProgress('Opening synthetic WAV preview');
    const response = await fetch('/qa/audio.wav', {signal: options.signal});
    const url = URL.createObjectURL(await response.blob());
    let cleaned = false;
    return {url, mime: 'audio/wav', durationHint: 60000, previewStartMs: 0,
      cleanup: () => { if (!cleaned) { cleaned = true; window.qaAudio.cleanups++; URL.revokeObjectURL(url); } }};
  }};
})();
"""


class CatalogFixtures(SignalCheckFixtures):
    def __init__(self, total=150137):
        super().__init__("confirm")
        self.catalog_total = total
        self.catalog_selected = set()
        self.catalog_status = "ready"
        self.catalog_busy = False
        self.catalog_fail_reads = False
        self.no_preview = False
        self.catalog_fail_history = False
        self.catalog_post_headers = []
        self.catalog_restore_points = {}

    def item(self, chart_id):
        md5 = hashlib.md5(f"synthetic-{chart_id}".encode()).hexdigest()
        return {"catalog_key": f"enchor:{chart_id}", "source": "enchor", "chart_id": chart_id,
                "song_key": f"synthetic|{chart_id}", "title": f"Catalog song {chart_id}", "artist": "Synthetic artist",
                "charter": "Offline charter", "album": "Synthetic fixtures", "md5": md5,
                "song_length_ms": 60000, "preview_start_ms": 0, "has_video_background": False,
                "selectable": True, "blocked_reason": "", "selected": chart_id in self.catalog_selected,
                "candidate_id": None,
                "preview": {"source": "enchor-package", "url": f"https://files.enchor.us/{md5}.sng",
                            "available": not self.no_preview, "reason": "Audio preview unavailable" if self.no_preview else "",
                            "availability": "unverified-until-open"}}

    def selected_response(self):
        data = super().selected_response()
        data["selected"]["catalog"] = [self.item(chart_id) for chart_id in sorted(self.catalog_selected)]
        data["summary"]["catalog"] = {"songs": len(self.catalog_selected), "charts": len(self.catalog_selected)}
        data["charts"] += len(self.catalog_selected)
        data["songs"] += len(self.catalog_selected)
        data["catalog_summary"] = {"songs": len(self.catalog_selected), "charts": len(self.catalog_selected),
                                   "selected": len(self.catalog_selected), "selected_charts": len(self.catalog_selected),
                                   "selected_songs": len(self.catalog_selected)}
        data["exact_generation_id"] = self.generation_id
        return data

    def journey(self):
        data = super().journey()
        data["catalog_summary"] = self.selected_response()["catalog_summary"]
        return data

    def catalog_receipt(self):
        data = self.selected_response()
        return {"selection_revision": data["selection_revision"], "exact_generation_id": self.generation_id,
                "undo": data["undo"], "summary": {"catalog": data["summary"]["catalog"]}}

    def respond(self, method, request_url, post_data=None):
        parsed = urlsplit(request_url)
        if parsed.path == "/api/selection/history" and method == "GET" and self.catalog_fail_history:
            self.request_log.append((method, parsed.path, {}))
            return 503, {"error": "Synthetic selection read unavailable"}
        if parsed.path == "/api/catalog" and method == "GET":
            query = parse_qs(parsed.query)
            self.request_log.append((method, parsed.path, query))
            if self.catalog_fail_reads:
                return 503, {"error": "Synthetic catalog temporarily unavailable"}
            if self.catalog_status != "ready":
                return 202, {"status": self.catalog_status, "readiness": {"message": "Preparing the installed catalog"},
                             "items": [], "total_charts": None, "total_songs": None, "snapshot_token": None}
            offset = int(query.get("offset", ["0"])[0]); limit = int(query.get("limit", ["50"])[0])
            total = 0 if query.get("q", [""])[0] == "missing" else self.catalog_total
            ids = range(offset + 1, min(offset + limit, total) + 1)
            return 200, {**self.catalog_receipt(), "status": "ready", "items": [self.item(index) for index in ids],
                         "limit": limit, "offset": offset, "has_more": offset + limit < total,
                         "total_charts": self.catalog_total, "total_songs": 80000, "total_filtered_charts": total,
                         "snapshot_token": "synthetic-catalog-1", "readiness": {"status": "ready"}}
        if parsed.path.startswith("/api/catalog/enchor/") and method == "GET":
            self.request_log.append((method, parsed.path, {}))
            return 200, {**self.catalog_receipt(), "item": self.item(int(parsed.path.rsplit("/", 1)[1])),
                         "snapshot_token": "synthetic-catalog-1"}
        if parsed.path == "/api/catalog/selection" and method == "POST":
            body = json.loads(post_data or "{}"); self.request_log.append((method, parsed.path, body))
            if self.catalog_busy:
                return 409, {"error": "ZIP preparation is busy", "reload": True}
            if body["action"] != "clear":
                assert body["snapshot_token"] == "synthetic-catalog-1"
            assert body["selection_revision"] == self.selection_revision
            assert body["exact_generation_id"] == self.generation_id
            chart_id = int(body["catalog_key"].split(":")[1])
            before = set(self.catalog_selected)
            if body["action"] == "clear":
                self.catalog_selected.discard(chart_id)
            else:
                self.catalog_selected.add(chart_id)
            self.selection_revision += 1; self.operation_sequence += 1
            self.catalog_restore_points[self.operation_sequence] = before
            self.operations.append({"operation_id": self.operation_sequence, "revision": self.selection_revision,
                "generation_id": self.generation_id, "label": "Remove catalog chart" if body["action"] == "clear" else "Add catalog chart", "previous": self._snapshot()})
            return 200, {"ok": True, "changed": 1, **self.selected_response()}
        if parsed.path == "/api/selection/undo" and method == "POST":
            operation_id = self.undo()["operation_id"]
            status, response = super().respond(method, request_url, post_data)
            if status == 200 and operation_id in self.catalog_restore_points:
                self.catalog_selected = self.catalog_restore_points.pop(operation_id)
                response = self.selected_response()
            return status, response
        return super().respond(method, request_url, post_data)


class LibrarySelectedFixtures(CatalogFixtures):
    def item(self, chart_id):
        item = super().item(chart_id)
        if chart_id == 1:
            item.update(candidate_id=101, selected=True, selected_in_scope="library")
        return item


class AliasCatalogFixtures(CatalogFixtures):
    def __init__(self, mixed_cohort=False):
        super().__init__()
        self.mixed_cohort = mixed_cohort
        if mixed_cohort:
            self.catalog_selected = {1}

    def item(self, chart_id):
        item = super().item(chart_id)
        item["candidate_id"] = 9000 + chart_id
        if chart_id in {1, 2}:
            md5 = hashlib.md5(b"synthetic-1").hexdigest()
            item["md5"] = md5
            item["preview"]["url"] = f"https://files.enchor.us/{md5}.sng"
            canonical = min(self.catalog_selected.intersection({1, 2}), default=None)
            item.update(selected=canonical is not None, selected_in_scope="catalog" if canonical else None,
                        selected_catalog_key=f"enchor:{canonical}" if canonical else None)
            if self.mixed_cohort and chart_id == 2:
                item.update(candidate_id=101, selected=True, selected_in_scope="library", selected_catalog_key=None)
        return item


@pytest.fixture
def catalog_page(public_server, delivery_chromium):
    app, base_url, cookie_name, cookie = public_server
    contexts = []; errors = []; externals = []; fixtures_used = []
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * (8000 * 60))

    def open_page(fixtures=None, width=1440, path="/", route_api=None):
        fixtures = fixtures or CatalogFixtures(); fixtures_used.append(fixtures)
        context = delivery_chromium.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        contexts.append(context); context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page(); page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: fixtures.catalog_post_headers.append(request.headers) if request.method == "POST" and urlsplit(request.url).path == "/api/catalog/selection" else None)
        page.on("request", lambda request: externals.append(request.url) if urlsplit(request.url).hostname not in {None, "127.0.0.1", "localhost"} else None)
        page.route("**/api/**", route_api or fixtures.fulfill)
        page.route("**/static/catalog-audio.js?*", lambda route: route.fulfill(status=200, content_type="text/javascript", body=MOCK_AUDIO))
        page.route("**/qa/audio.wav", lambda route: route.fulfill(status=200, content_type="audio/wav", body=buffer.getvalue()))
        page.goto(base_url + path, wait_until="networkidle")
        return page, fixtures
    try:
        yield open_page
    finally:
        for context in contexts: context.close()
        assert not errors, errors
        assert not externals, externals
        assert all(not fixtures.unexpected_requests for fixtures in fixtures_used)


def _posts(fixtures, path=None):
    return [(method, request_path, body) for method, request_path, body in fixtures.request_log
            if method == "POST" and (path is None or request_path == path)]


def _browse(page):
    page.locator("#catalogBrowseButton").click()
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(50)


def _play(page, chart_id=1):
    page.locator(f'[data-catalog-key="enchor:{chart_id}"]').get_by_role("button", name="Play preview", exact=True).click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert page.locator("#catalogAudio").evaluate("audio => audio.duration") == 60


@pytest.mark.parametrize("width", [320, 390, 768, 1024, 1440])
def test_catalog_large_count_bounded_pages_and_single_mobile_dock(catalog_page, width):
    page, fixtures = catalog_page(width=width)
    assert not [entry for entry in fixtures.request_log if entry[1] == "/api/catalog"]
    assert page.evaluate("window.qaAudio.loads.length") == 0
    _browse(page)
    playwright.expect(page.locator("#catalogCounts")).to_contain_text("150,137 indexed charts")
    if width in {390, 1440}:
        _capture(page, f"catalog-browse-{width}.png")
    page.locator("#catalogNextPage").click()
    playwright.expect(page.locator('[data-catalog-key="enchor:51"]')).to_be_visible()
    reads = [body for method, path, body in fixtures.request_log if path == "/api/catalog"]
    assert len(reads) == 2 and reads[0]["limit"] == ["50"]
    assert reads[1]["offset"] == ["50"] and reads[1]["snapshot_token"] == ["synthetic-catalog-1"]
    if width == 320:
        page.locator('[data-catalog-key="enchor:100"]').scroll_into_view_if_needed()
        close = page.locator("#catalogCloseButton")
        playwright.expect(close).to_be_visible()
        assert close.bounding_box()["y"] >= page.locator("#catalogDrawer").bounding_box()["y"]
        assert close.bounding_box()["y"] + close.bounding_box()["height"] < page.locator("#catalogPlayerDock").bounding_box()["y"]
        assert close.evaluate("node => {const r=node.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===node||node.contains(hit);}")
    assert not _posts(fixtures)
    # Desktop (1200px+) keeps the selection rail sticky above the player.
    expected = "sticky" if width >= 1200 else "relative"
    assert page.locator("#selectionRail").evaluate("node => getComputedStyle(node).position") == expected
    for selector in ["#catalogVinylButton", "#catalogPlayerNext", "#catalogBrowseButton", "#catalogQueueButton", "#catalogAddButton"]:
        assert page.locator(selector).bounding_box()["height"] >= 44
    _assert_no_overflow(page)
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    playwright.expect(page.locator("#catalogBrowseButton")).to_be_focused()


def test_native_play_pause_seek_volume_queue_and_add_are_independent(catalog_page):
    page, fixtures = catalog_page()
    _browse(page); _play(page)
    audio_identity = page.evaluate("window.originalCatalogAudio = document.querySelector('#catalogAudio'); true")
    page.locator('[data-catalog-key="enchor:2"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogAddButton").evaluate("button => { button.click(); button.click(); }")
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert len(_posts(fixtures, "/api/catalog/selection")) == 1
    assert fixtures.catalog_post_headers[0].get("x-riff-request") == "1"
    assert fixtures.catalog_selected == {1}
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 1"
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert page.evaluate("window.originalCatalogAudio === document.querySelector('#catalogAudio')")
    page.locator("#catalogVinylButton").click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "paused")
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Play preview")
    page.locator("#catalogSeek").evaluate("node => {node.value='20'; node.dispatchEvent(new Event('input',{bubbles:true}));}")
    assert 19 <= page.locator("#catalogAudio").evaluate("audio => audio.currentTime") <= 21
    page.locator("#catalogVolume").evaluate("node => {node.value='0.3'; node.dispatchEvent(new Event('input',{bubbles:true}));}")
    assert page.locator("#catalogAudio").evaluate("audio => audio.volume") == pytest.approx(.3)
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 2")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    page.locator("#catalogQueueButton").click()
    playwright.expect(page.locator("#catalogQueueList li")).to_have_count(2)
    assert len(_posts(fixtures)) == 1


def test_playing_controller_survives_five_stages_and_owned_page_navigation(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page)
    page.evaluate("window.originalCatalogAudio = document.querySelector('#catalogAudio')")
    page.locator("#catalogCloseButton").click()
    for stage in ["add", "find", "confirm", "download", "done"]:
        page.evaluate("stage => {document.body.dataset.riffStage=stage; for(const node of document.querySelectorAll('.journey-stage')) node.hidden = node.id !== ({add:'stageAdd',find:'stageFind',confirm:'stageConfirm',download:'stageDownload',done:'stageDone'})[stage]; document.dispatchEvent(new CustomEvent('riff:studio-context',{detail:window.RiffStudioAPI.context()}));}", stage)
        assert page.locator("#catalogPlayerDock").is_visible()
        assert page.evaluate("window.originalCatalogAudio === document.querySelector('#catalogAudio')")
        assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.locator('.site-nav a[href="/about"]').click()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    assert urlsplit(page.url).path == "/about"
    assert page.evaluate("window.originalCatalogAudio === document.querySelector('#catalogAudio')")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.locator('#catalogSiteNav a[href="/privacy"]').click()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
    page.go_back()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    page.locator('#catalogSiteNav a[href="/"]').click()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    assert urlsplit(page.url).path == "/"
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert not _posts(fixtures)


def test_reload_restores_metadata_paused_and_direct_about_can_mount_home(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page)
    page.locator("#catalogCloseButton").click()
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 1")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    assert page.evaluate("window.qaAudio.loads") == []
    page.goto(page.url.rstrip("/") + "/about", wait_until="networkidle")
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 1")
    page.locator(".brand").click()
    playwright.expect(page.locator("#riffApp")).to_be_visible()
    playwright.expect(page.locator("#studio-panel")).to_have_attribute("data-riff-studio-mounted", "true")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")


def test_cold_and_unavailable_catalog_preserve_queue_and_only_read(catalog_page):
    fixtures = CatalogFixtures(); fixtures.catalog_status = "queued"
    page, fixtures = catalog_page(fixtures)
    page.locator("#catalogBrowseButton").click()
    playwright.expect(page.locator("#catalogRetryButton")).to_be_visible()
    playwright.expect(page.locator("#catalogCounts")).to_contain_text("await")
    fixtures.catalog_status = "ready"; page.locator("#catalogRetryButton").click()
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(50)
    page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Queue", exact=True).click()
    fixtures.catalog_fail_reads = True; page.locator("#catalogNextPage").click()
    playwright.expect(page.locator("#catalogRetryButton")).to_be_visible()
    page.locator("#catalogQueueButton").click()
    playwright.expect(page.locator("#catalogQueueList li")).to_have_count(1)
    assert not _posts(fixtures) and page.evaluate("window.qaAudio.loads") == []


def test_backend_busy_rejects_add_but_playing_and_skip_remain_available(catalog_page):
    fixtures = CatalogFixtures(); fixtures.catalog_busy = True
    page, fixtures = catalog_page(fixtures); _browse(page); _play(page)
    page.locator('[data-catalog-key="enchor:2"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("busy or changed")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 2")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert len(_posts(fixtures, "/api/catalog/selection")) == 1 and not fixtures.catalog_selected


def test_audio_unavailable_never_claims_playing_and_add_still_works(catalog_page):
    page, fixtures = catalog_page(); page.evaluate("window.qaRejectAudio=true"); _browse(page)
    page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Play preview", exact=True).click()
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("no complete audio mix")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert fixtures.catalog_selected == {1}


def test_shuffle_search_keyboard_and_reduced_motion_keep_clear_boundaries(catalog_page):
    page, fixtures = catalog_page(width=390); _browse(page)
    page.locator("#catalogShuffleButton").click()
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(50)
    reads = [body for method, path, body in fixtures.request_log if path == "/api/catalog"]
    assert reads[-1]["order"] == ["shuffle"] and len(reads[-1]["seed"][0]) == 16
    page.locator("#catalogSearch").fill("missing"); page.locator("#catalogSearch").press("Enter")
    playwright.expect(page.locator("#catalogBrowseStatus")).to_have_text("No charts match this search.")
    assert not _posts(fixtures)
    page.locator("#catalogSearch").fill(""); page.locator("#catalogSearch").press("Enter")
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(50)
    _play(page)
    assert page.locator(".catalog-vinyl-record").evaluate("node => getComputedStyle(node).animationName") == "none"
    page.locator("#catalogSearch").focus(); page.keyboard.press("a")
    assert not _posts(fixtures)


def test_library_selected_chart_remains_selected_without_catalog_duplicate(catalog_page):
    page, fixtures = catalog_page(LibrarySelectedFixtures()); _browse(page)
    card = page.locator('[data-catalog-key="enchor:1"]')
    playwright.expect(card.get_by_role("button", name="In ZIP selection")).to_be_disabled()
    _play(page)
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    playwright.expect(page.locator("#catalogAddButton")).to_be_disabled()
    page.evaluate("receipt => document.dispatchEvent(new CustomEvent('riff:selection-committed',{detail:receipt}))", fixtures.selected_response())
    playwright.expect(page.locator("#catalogAddButton")).to_be_disabled()
    assert not fixtures.catalog_selected and not _posts(fixtures)
    fixtures.selected.remove(101); fixtures.selection_revision += 1
    page.evaluate("receipt => document.dispatchEvent(new CustomEvent('riff:selection-committed',{detail:receipt}))", fixtures.selected_response())
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("Add to ZIP")
    playwright.expect(page.locator("#catalogAddButton")).to_be_enabled()
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")


def test_add_prefers_current_discovery_generation_over_older_catalog_receipt(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page)
    fixtures.generation_id = 2
    page.evaluate("window.qaOldContext=window.RiffStudioAPI.context; window.RiffStudioAPI={...window.RiffStudioAPI,context:()=>({...window.qaOldContext(),exact_generation_id:2})}")
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert _posts(fixtures, "/api/catalog/selection")[0][2]["exact_generation_id"] == 2


def test_noop_context_refresh_preserves_catalog_card_and_keyboard_focus(catalog_page):
    page, fixtures = catalog_page(); _browse(page)
    action = page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Queue", exact=True)
    action.focus()
    page.evaluate("window.qaOriginalCard=document.querySelector('[data-catalog-key=\"enchor:1\"]'); for(let index=0;index<10;index++) document.dispatchEvent(new CustomEvent('riff:selection-state'))")
    playwright.expect(action).to_be_focused()
    assert page.evaluate("window.qaOriginalCard === document.querySelector('[data-catalog-key=\"enchor:1\"]')")
    assert not _posts(fixtures)


def test_phone_audio_controls_have_44px_targets_and_motion_off_preserves_audio(catalog_page):
    page, fixtures = catalog_page(width=320); _browse(page); _play(page)
    page.locator("#catalogQueueButton").click()
    for selector in ["#catalogSeek", "#catalogVolume"]:
        assert page.locator(selector).bounding_box()["height"] >= 44
    assert page.locator('label[for="catalogSeek"]').is_visible()
    assert page.locator('label[for="catalogVolume"]').is_visible()
    _assert_no_overflow(page)
    _capture(page, "catalog-queue-playing-320.png")
    page.locator("#catalogCloseButton").click()
    page.emulate_media(reduced_motion="no-preference")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-motion", "on")
    page.locator("#riff-motion-toggle").click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-motion", "off")
    assert page.locator(".catalog-vinyl-record").evaluate("node => getComputedStyle(node).animationName") == "none"
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")


def test_native_ended_queue_stops_after_three_unavailable_next_previews(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page)
    for chart_id in [2, 3, 4, 5]:
        page.locator(f'[data-catalog-key="enchor:{chart_id}"]').get_by_role("button", name="Queue", exact=True).click()
    page.evaluate("window.qaRejectAudio=true; document.querySelector('#catalogAudio').currentTime=59.9")
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 4")
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("Three previews could not be played")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1", "enchor:2", "enchor:3", "enchor:4"]
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    assert not _posts(fixtures)


def test_direct_about_review_zip_mounts_home_before_review_without_remounting_audio(catalog_page):
    page, fixtures = catalog_page(path="/about"); _browse(page); _play(page)
    page.evaluate("window.originalCatalogAudio=document.querySelector('#catalogAudio')")
    page.locator("#catalogQueueButton").click()
    page.locator("#catalogReviewZipButton").click()
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    assert page.locator("#riffApp").get_attribute("data-riff-mounted") == "true"
    assert page.evaluate("window.originalCatalogAudio === document.querySelector('#catalogAudio')")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert not _posts(fixtures)


def test_actual_find_transition_preserves_audio_and_only_disables_zip_add(catalog_page):
    fixtures = CatalogFixtures(); fixtures.discovery_complete_after_polls = None
    page, fixtures = catalog_page(fixtures); _browse(page); _play(page)
    page.locator('[data-catalog-key="enchor:2"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogCloseButton").click()
    page.locator("#searchAgainButton").click()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    page.locator("#findChartsButton").click()
    playwright.expect(page.locator("#stageFind")).to_be_visible()
    playwright.expect(page.locator("#catalogAddButton")).to_be_disabled()
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 2")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert not _posts(fixtures, "/api/catalog/selection")


def test_lost_add_and_failed_reconciliation_require_read_only_check_before_retry(catalog_page):
    fixtures = CatalogFixtures()

    def route_api(route):
        if route.request.method == "POST" and urlsplit(route.request.url).path == "/api/catalog/selection":
            fixtures.respond("POST", route.request.url, route.request.post_data)
            route.abort("failed")
            return
        fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api); _browse(page); _play(page)
    fixtures.catalog_fail_history = True
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("selection status is unavailable")
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("Check choices")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert len(_posts(fixtures, "/api/catalog/selection")) == 1
    fixtures.catalog_fail_history = False
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert fixtures.catalog_selected == {1}
    assert len(_posts(fixtures, "/api/catalog/selection")) == 1


def test_catalog_add_remove_and_undo_preserve_native_audio_and_listening_queue(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page)
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    page.locator("#catalogQueueButton").click()
    page.locator("#catalogRemoveButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("Add to ZIP")
    assert not fixtures.catalog_selected
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.locator("#catalogCloseButton").click()
    page.locator("#selectionUndoButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert fixtures.catalog_selected == {1}
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    posts = _posts(fixtures, "/api/catalog/selection")
    assert [entry[2]["action"] for entry in posts] == ["select", "clear"]
    assert "snapshot_token" not in posts[1][2]
    assert len(_posts(fixtures, "/api/selection/undo")) == 1


def test_catalog_artifact_alias_remove_uses_owned_canonical_key_and_preserves_playback(catalog_page):
    page, fixtures = catalog_page(AliasCatalogFixtures()); _browse(page); _play(page, 1)
    page.locator("#catalogAddButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    alias = page.locator('[data-catalog-key="enchor:2"]')
    playwright.expect(alias.get_by_role("button", name="In ZIP selection")).to_be_disabled()
    playwright.expect(alias.get_by_role("button", name="Remove from ZIP")).to_be_enabled()
    assert fixtures.catalog_selected == {1}
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    _play(page, 2)
    fixtures.catalog_fail_reads = True
    page.locator("#catalogQueueButton").click()
    page.locator("#catalogRemoveButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("Add to ZIP")
    assert fixtures.catalog_selected == set()
    posts = _posts(fixtures, "/api/catalog/selection")
    assert posts[-1][2]["action"] == "clear" and posts[-1][2]["catalog_key"] == "enchor:1"
    assert "snapshot_token" not in posts[-1][2]
    assert fixtures.selected == {101, 102, 103}
    page.locator("#catalogCloseButton").click()
    page.locator("#selectionUndoButton").click()
    playwright.expect(page.locator("#catalogAddButton")).to_have_text("In ZIP selection")
    assert fixtures.catalog_selected == {1}
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 2"
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1", "enchor:2"]

    # An artifact alias already owned through imported choices retains that
    # cohort's controls even if another catalog reference shares its package.
    other_page, other = catalog_page(AliasCatalogFixtures(mixed_cohort=True)); _browse(other_page)
    imported_alias = other_page.locator('[data-catalog-key="enchor:2"]')
    playwright.expect(imported_alias.get_by_role("button", name="In ZIP selection")).to_be_disabled()
    assert imported_alias.get_by_role("button", name="Remove from ZIP").count() == 0
    assert other.selected == {101, 102, 103} and not _posts(other)


def test_catalog_continuation_captures_order_and_queued_picks_take_priority(catalog_page):
    page, fixtures = catalog_page(); _browse(page)
    page.locator("#catalogSearch").fill("captured artist")
    page.locator("#catalogSearchForm").get_by_role("button", name="Search", exact=True).click()
    playwright.expect(page.locator("#catalogCounts")).to_contain_text("matching charts")
    page.locator("#catalogShuffleButton").click()
    page.wait_for_function("() => document.querySelector('#catalogResults').getAttribute('aria-busy') === 'false'")
    seed = [body for _, path, body in fixtures.request_log if path == "/api/catalog"][-1]["seed"][0]
    _play(page, 50)
    page.locator('[data-catalog-key="enchor:9"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogSearch").fill("missing")
    page.locator("#catalogSearchForm").get_by_role("button", name="Search", exact=True).click()
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(0)
    page.locator("#catalogAudio").evaluate("audio => {audio.currentTime=59.9;}")
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 9")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert not [body for _, path, body in fixtures.request_log if path == "/api/catalog" and body.get("limit") == ["10"]]
    page.locator("#catalogAudio").evaluate("audio => {audio.currentTime=59.9;}")
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 51")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    reads = [body for _, path, body in fixtures.request_log if path == "/api/catalog" and body.get("limit") == ["10"]]
    assert len(reads) == 1
    assert reads[0]["offset"] == ["50"] and reads[0]["q"] == ["captured artist"]
    assert reads[0]["order"] == ["shuffle"] and reads[0]["seed"] == [seed]
    assert reads[0]["snapshot_token"] == ["synthetic-catalog-1"]
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50", "enchor:9", "enchor:51"]
    # New explicit picks jump ahead of the upcoming catalog metadata batch.
    page.locator("#catalogSearch").fill("another artist")
    page.locator("#catalogSearchForm").get_by_role("button", name="Search", exact=True).click()
    playwright.expect(page.locator('[data-catalog-key="enchor:7"]')).to_be_visible()
    page.locator('[data-catalog-key="enchor:7"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 7")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 52")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert not _posts(fixtures)


def test_catalog_continuation_pause_next_and_reload_do_not_autoplay(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page, 50)
    page.locator("#catalogVinylButton").click()
    page.locator("#catalogAudio").evaluate("audio => audio.dispatchEvent(new Event('ended'))")
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("Press Play or Next")
    assert not [body for _, path, body in fixtures.request_log if path == "/api/catalog" and body.get("limit") == ["10"]]
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 51")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "paused")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50"]
    page.locator("#catalogQueueButton").click()
    playwright.expect(page.locator("#catalogContinueCatalog")).to_be_checked()
    page.locator("#catalogContinueCatalog").uncheck()
    page.reload(wait_until="networkidle")
    assert page.evaluate("window.qaAudio.loads") == []
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    page.locator("#catalogQueueButton").click()
    playwright.expect(page.locator("#catalogContinueCatalog")).not_to_be_checked()
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 51"
    assert not _posts(fixtures)


def test_catalog_continuation_cancel_aborts_pending_metadata_and_ignores_late_response(catalog_page):
    fixtures = CatalogFixtures(); pending = []

    def route_api(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if urlsplit(route.request.url).path == "/api/catalog" and query.get("limit") == ["10"]:
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api); _browse(page); _play(page, 50)
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Cancel preview loading")
    assert len(pending) == 1
    page.locator("#catalogVinylButton").click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    fixtures.fulfill(pending.pop())
    page.wait_for_timeout(150)
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50"]
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 50"
    assert page.locator("#catalogDockQueueCount").inner_text() == "1"
    assert not _posts(fixtures)


def test_catalog_continuation_prunes_consumed_metadata_and_crosses_resident_cap(catalog_page):
    page, fixtures = catalog_page()
    storage_key = "riff:catalog-queue:1:" + (page.locator("body").get_attribute("data-riff-owner") or "guest")
    # Seed before the reload's scripts; pagehide intentionally saves real state.
    page.add_init_script("""(() => {
      const key = """ + json.dumps(storage_key) + """;
      sessionStorage.setItem(key, JSON.stringify({version:1,index:199,continueCatalog:true,
        feed:{q:'',seed:'',offset:200,snapshot:'synthetic-catalog-1',exhausted:false},
        queue:Array.from({length:200},(_,i)=>({catalog_key:'enchor:'+(i+1),title:'Catalog song '+(i+1),artist:'Synthetic artist',charter:'Offline charter'}))}));
    })();""")
    page.reload(wait_until="networkidle")
    assert page.evaluate("window.qaAudio.loads") == []
    page.locator("#catalogVinylButton").click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 201")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert page.locator("#catalogDockQueueCount").inner_text() == "200"
    page.locator("#catalogQueueButton").click()
    playwright.expect(page.locator("#catalogQueueList li")).to_have_count(200)
    assert page.locator('[data-queue-key="enchor:1"]').count() == 0
    assert page.locator('[data-queue-key="enchor:11"]').count() == 1
    page.locator("#catalogCloseButton").click()
    for chart_id in range(202, 212):
        page.locator("#catalogPlayerNext").click()
        playwright.expect(page.locator("#catalogNowTitle")).to_have_text(f"Catalog song {chart_id}")
        playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    reads = [body for _, path, body in fixtures.request_log if path == "/api/catalog"]
    assert [body["offset"] for body in reads] == [["200"], ["210"]]
    assert page.locator("#catalogDockQueueCount").inner_text() == "200"
    assert len(page.evaluate("JSON.parse(sessionStorage.getItem('riff:catalog-queue:1:'+(document.body.dataset.riffOwner||'guest'))).queue")) == 200
    assert not _posts(fixtures)


def test_catalog_continuation_stops_after_three_unavailable_auto_previews(catalog_page):
    page, fixtures = catalog_page(); _browse(page); _play(page, 50)
    page.evaluate("window.qaRejectAudio=true")
    page.locator("#catalogAudio").evaluate("audio => {audio.currentTime=59.9;}")
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("Three previews could not be played")
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 53"
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50", "enchor:51", "enchor:52", "enchor:53"]
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    reads = [body for _, path, body in fixtures.request_log if path == "/api/catalog" and body.get("limit") == ["10"]]
    assert len(reads) == 1 and reads[0]["offset"] == ["50"]
    assert not _posts(fixtures)


@pytest.mark.parametrize("edit", ["reorder", "remove-earlier"])
def test_pending_queue_play_resolves_identity_after_edits_and_cancelled_current_stays_stopped(catalog_page, edit):
    fixtures = CatalogFixtures(); pending = []

    def route_api(route):
        path = urlsplit(route.request.url).path
        if route.request.method == "GET" and path in {"/api/catalog/enchor/2", "/api/catalog/enchor/3"}:
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api); _browse(page); _play(page)
    for chart_id in [2, 3]:
        page.locator(f'[data-catalog-key="enchor:{chart_id}"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogQueueButton").click()
    page.locator('[data-queue-key="enchor:2"]').get_by_role("button", name="Play queued chart Catalog song 2", exact=True).click()
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Cancel preview loading")
    assert len(pending) == 1
    if edit == "reorder":
        page.get_by_role("button", name="Move Catalog song 2 earlier", exact=True).click()
        expected = ["enchor:2", "enchor:1", "enchor:3"]
    else:
        page.get_by_role("button", name="Remove Catalog song 1 from listening queue", exact=True).click()
        expected = ["enchor:2", "enchor:3"]
    fixtures.fulfill(pending.pop())
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 2"
    assert page.locator("#catalogQueueList li").evaluate_all("rows => rows.map(row => row.dataset.queueKey)") == expected
    assert page.locator("#catalogQueueList li[data-current='true']").get_attribute("data-queue-key") == "enchor:2"
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1", "enchor:2"]
    page.locator('[data-queue-key="enchor:3"]').get_by_role("button", name="Play queued chart Catalog song 3", exact=True).click()
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Cancel preview loading")
    page.get_by_role("button", name="Remove Catalog song 3 from listening queue", exact=True).click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    fixtures.fulfill(pending.pop())
    page.wait_for_timeout(150)
    assert page.locator('[data-queue-key="enchor:3"]').count() == 0
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1", "enchor:2"]
    assert not _posts(fixtures)


@pytest.mark.parametrize("intent", ["pause", "clear", "new-play", "next"])
def test_studio_bridge_pending_lookup_is_cancellable_and_latest_player_intent_wins(catalog_page, intent):
    fixtures = CatalogFixtures(); pending = []

    def route_api(route):
        if route.request.method == "GET" and urlsplit(route.request.url).path == "/api/catalog/enchor/99":
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api)
    if intent in {"new-play", "next"}:
        _browse(page)
    if intent == "next":
        _play(page, 1)
        page.locator('[data-catalog-key="enchor:2"]').get_by_role("button", name="Queue", exact=True).click()
    page.evaluate("() => {window.qaBridgeResult=null;window.RiffCatalog.playKey('enchor:99').then(result=>window.qaBridgeResult=result);}")
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Cancel preview loading")
    playwright.expect(page.locator("#catalogVinylButton")).to_be_enabled()
    assert len(pending) == 1
    if intent == "pause":
        page.locator("#catalogVinylButton").click()
    elif intent == "clear":
        page.locator("#catalogQueueButton").click()
        playwright.expect(page.locator("#catalogClearQueue")).to_be_enabled()
        page.locator("#catalogClearQueue").click()
    elif intent == "new-play":
        page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Play preview", exact=True).click()
    else:
        page.locator("#catalogPlayerNext").click()
    page.wait_for_function("() => window.qaBridgeResult===false")
    fixtures.fulfill(pending.pop())
    page.wait_for_timeout(150)
    assert "enchor:99" not in page.evaluate("window.qaAudio.loads")
    if intent in {"pause", "clear"}:
        assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
        assert page.locator("#catalogDockQueueCount").inner_text() == "0"
        assert page.evaluate("window.qaAudio.loads") == []
    else:
        playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
        assert page.locator("#catalogNowTitle").inner_text() == ("Catalog song 2" if intent == "next" else "Catalog song 1")
        assert page.evaluate("window.qaAudio.loads") == (["enchor:1", "enchor:2"] if intent == "next" else ["enchor:1"])
    assert not _posts(fixtures)


@pytest.mark.parametrize("intent", ["pause", "new-play"])
def test_studio_initial_deck_lookup_cannot_override_newer_player_intent(catalog_page, intent):
    fixtures = CatalogFixtures(); pending = []

    def route_api(route):
        if urlsplit(route.request.url).path == "/api/listening-deck":
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api)
    if intent == "pause":
        _browse(page); _play(page, 1)
        page.locator("#catalogCloseButton").click()
    page.locator("#studio-player-toggle").click()
    playwright.expect(page.locator("#studio-panel")).to_have_attribute("aria-busy", "true")
    assert len(pending) == 1
    if intent == "pause":
        page.locator("#catalogVinylButton").click()
    else:
        _browse(page); _play(page, 1)
    pending.pop().fulfill(status=200, content_type="application/json", body=json.dumps({
        "items": [{"key": "library:studio-track-0", "scope": "library", "track_key": "studio-track-0",
                   "candidate_id": 1000, "title": "Studio song 0", "artist": "Synthetic artist", "charter": "Offline",
                   "selected": False, "song_selected": False, "selectable": True, "catalog_key": "enchor:1000"}],
        "total": 1, "offset": 0, "limit": 100, "has_more": False, "generation_token": "synthetic-studio-generation-1"}))
    playwright.expect(page.locator("#studio-panel")).to_have_attribute("aria-busy", "false")
    page.wait_for_timeout(150)
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert not [entry for entry in fixtures.request_log if entry[1] == "/api/catalog/enchor/1000"]
    assert page.locator("#catalogNowTitle").inner_text() == "Catalog song 1"
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused") == (intent == "pause")
    assert not _posts(fixtures)


def test_catalog_continuation_unavailable_then_exhausted_pool_stops_honestly(catalog_page):
    page, fixtures = catalog_page(CatalogFixtures(total=50)); _browse(page); _play(page, 50)
    fixtures.catalog_fail_reads = True
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("Catalog continuation paused")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50"]
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    fixtures.catalog_fail_reads = False
    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogPlayerStatus")).to_contain_text("This catalog order has ended")
    playwright.expect(page.locator("#catalogPlayerNext")).to_be_disabled()
    assert page.locator("#catalogDockQueueCount").inner_text() == "1"
    assert page.evaluate("window.qaAudio.loads") == ["enchor:50"]
    assert not _posts(fixtures)
