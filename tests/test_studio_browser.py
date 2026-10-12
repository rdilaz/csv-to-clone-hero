"""Real Chromium interactions with synthetic songs and the persistent preview bridge."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_public_browser import _assert_no_overflow, _capture, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page
from tests.test_public_yield_browser import YieldFixtures, _coverage

pytestmark = pytest.mark.browser


class StudioFixtures(YieldFixtures):
    def __init__(self, count=1000, versions=1):
        super().__init__()
        self.count = count
        self.versions = versions
        self.selected = set()
        self.expansion_selected = set()
        self.expansion_groups = []
        self.token = "synthetic-studio-generation-1"
        self.fail_recount = False
        self.omit_save_summary = False
        self.change_generation_on_page = False
        self.invalid_spotify_id = False
        self.invalid_catalog_key = False
        self.reported_deck_total = None
        self.invalid_deck_offset = False

    def tracks(self):
        template = super().tracks()[0]
        tracks = []
        for index in range(self.count):
            track = deepcopy(template)
            track.update(track_key=f"studio-track-{index}", title=f"Studio song {index}", artist="Synthetic artist", spotify_id=str(index).zfill(22))
            candidates = []
            for version in range(self.versions):
                candidate = deepcopy(template["candidates"][0])
                candidate_id = 1000 + index * self.versions + version
                candidate.update(candidate_id=candidate_id, title=track["title"], artist=track["artist"], selected=candidate_id in self.selected,
                    result_tier="RECOMMENDED_EXACT" if version == 0 else "SAME_RECORDING_CHART_VARIANT", best=version == 0, selectable=True, difficulty_coverage=_coverage({"guitar": ["expert"] if index % 2 == 0 else ["easy", "hard", "expert"]}))
                candidates.append(candidate)
            track.update(candidates=candidates, selected=any(item["selected"] for item in candidates), selected_count=sum(item["selected"] for item in candidates))
            tracks.append(track)
        return tracks

    def selected_response(self):
        return super().selected_response()

    def deck(self, query):
        selected_scope = query.get("scope", ["both"])[0]
        items = []
        if selected_scope in {"library", "both"}:
            for track in self.tracks():
                candidate = track["candidates"][0]
                items.append({"key": f"library:{track['track_key']}", "scope": "library", "track_key": track["track_key"], "candidate_id": candidate["candidate_id"],
                    "title": track["title"], "artist": track["artist"], "chart_title": candidate["title"], "chart_artist": candidate["artist"], "album": track.get("album"),
                    "selected": candidate["selected"], "song_selected": track["selected"], "selectable": True, "result_tier": candidate["result_tier"], "difficulty_coverage": candidate["difficulty_coverage"],
                    "spotify_id": "not-a-confirmed-id" if self.invalid_spotify_id else track["spotify_id"], "spotify_url": None, "artist_norm": None,
                    "catalog_key": None if self.invalid_catalog_key else f"enchor:{candidate['candidate_id']}"})
        if selected_scope in {"expansion", "both"}:
            for song in self.expansion_songs():
                candidate = song["candidates"][0]
                items.append({"key": f"expansion:{song['song_key']}", "scope": "expansion", "track_key": None, "candidate_id": candidate["candidate_id"],
                    "title": song["title"], "artist": song["artist"], "chart_title": candidate["title"], "chart_artist": candidate["artist"], "album": None,
                    "selected": candidate["selected"], "song_key": song["song_key"], "song_selected": any(item["selected"] for item in song["candidates"]), "selectable": True, "result_tier": "POSSIBLE_MATCH", "difficulty_coverage": candidate["difficulty_coverage"],
                    "spotify_id": None, "spotify_url": None, "artist_norm": candidate["artist_norm"], "catalog_key": f"enchor:{candidate['candidate_id']}"})
        seed = query.get("seed", [""])[0]
        if query.get("order", [""])[0] == "shuffle":
            items.sort(key=lambda item: hashlib.sha256((seed + item["key"]).encode()).digest())
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        total = len(items) if self.reported_deck_total is None else self.reported_deck_total
        return {"items": items[offset:offset + limit], "total": total, "offset": offset + int(self.invalid_deck_offset), "limit": limit, "has_more": offset + limit < total,
            "generation_token": self.token, "exact_generation_id": 1, "capabilities": {"shuffle": True}}

    def respond(self, method, request_url, post_data=None):
        path = urlsplit(request_url).path
        query = parse_qs(urlsplit(request_url).query)
        body = json.loads(post_data or "{}")
        if path == "/api/listening-deck" and method == "GET":
            self.request_log.append((method, path, query))
            if self.change_generation_on_page and int(query.get("offset", ["0"])[0]) > 0:
                return 409, {"error": "Your chart results changed. Start browsing again."}
            return 200, self.deck(query)
        if self.fail_recount and self.selected and path in {"/api/workspace", "/api/expansion/results"}:
            self.request_log.append((method, path, {}))
            return 503, {"error": "Synthetic summary unavailable"}
        response_status, response = super().respond(method, request_url, post_data)
        if path == "/api/selection" and method == "POST" and response_status == 200 and not self.omit_save_summary:
            return 200, self.selected_response()
        return response_status, response


def _requests(requests, path, method="GET"):
    return [request for request in requests if request.method == method and urlsplit(request.url).path == path]


@pytest.mark.parametrize("width", [390, 1440])
def test_all_page_one_song_selection_is_one_post_with_authoritative_totals(delivery_page, width):
    fixtures = StudioFixtures()
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": 844 if width == 390 else 900})
    assert page.locator("#studio-select-status").inner_text() == "Ready · selections include every page."
    assert "results changed" not in page.locator("#studio-deck-status").inner_text().lower()
    page.evaluate("window.originalStudioSong = document.querySelector('#reviewCards > article')")
    baseline = len(_requests(requests, "/api/workspace"))
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1,000")
    assert len(_requests(requests, "/api/selection/all", "POST")) == 1
    assert len(_requests(requests, "/api/workspace")) == baseline
    assert page.evaluate("window.originalStudioSong === document.querySelector('#reviewCards > article')")
    assert len(fixtures.selected) == 1000
    assert "All pages included" in page.locator("#studio-select-status").inner_text()
    _assert_no_overflow(page)
    _capture(page, f"studio-all-selected-{width}.png")


def test_all_versions_and_scope_have_a_preview_before_one_explicit_save(delivery_page):
    fixtures = StudioFixtures(count=120, versions=2)
    page, requests = delivery_page(fixtures)
    page.locator("#studio-select-scope").select_option("library")
    page.locator("#studio-select-policy").select_option("all_versions")
    playwright.expect(page.locator("#studio-selection-preview")).to_contain_text("240")
    assert not page.locator("#studio-include-uncertain").is_checked()
    assert not _requests(requests, "/api/selection/all", "POST")
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("240")
    payload = _requests(requests, "/api/selection/all", "POST")[0].post_data_json
    assert payload == {"scope": "library", "policy": "all_versions", "confirm_possible": False,
                       "selection_revision": 1, "exact_generation_id": 1}


def test_add_advances_skip_added_and_undo_preserves_previous_choices_without_refetch(delivery_page):
    fixtures = StudioFixtures(count=20)
    fixtures.selected = {1000}
    page, requests = delivery_page(fixtures)
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 1")
    baseline = len(_requests(requests, "/api/workspace"))
    page.locator("#studio-add").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("2")
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 2 (Expert only)")
    playwright.expect(page.locator("#studio-undo")).to_be_enabled()
    page.locator("#studio-undo").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    assert fixtures.selected == {1000}
    assert len(_requests(requests, "/api/selection", "POST")) == 1
    assert len(_requests(requests, "/api/selection/undo", "POST")) == 1
    assert len(_requests(requests, "/api/workspace")) == baseline


def test_shuffle_fetches_one_page_of_1000_songs_and_is_stable_until_next_page(delivery_page):
    page, requests = delivery_page(StudioFixtures())
    assert not _requests(requests, "/api/listening-deck")
    page.locator("#studio-shuffle").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    deck_requests = _requests(requests, "/api/listening-deck")
    assert len(deck_requests) == 1
    query = parse_qs(urlsplit(deck_requests[0].url).query)
    assert query["order"] == ["shuffle"] and len(query["seed"][0]) == 16
    assert query["limit"] == ["100"]
    assert "of 1,000" in page.locator("#studio-deck-status").inner_text()
    titles = [page.locator("#studio-now-title").inner_text()]
    for _ in range(15):
        page.locator("#studio-next").click()
        titles.append(page.locator("#studio-now-title").inner_text())
    assert len(set(titles)) == 16
    assert len(_requests(requests, "/api/listening-deck")) == 1


def test_shuffle_large_total_loads_only_first_100_without_an_arbitrary_pool_cap(delivery_page):
    fixtures = StudioFixtures(count=120)
    fixtures.reported_deck_total = 100001
    page, requests = delivery_page(fixtures)
    page.locator("#studio-shuffle").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    assert "of 100,001" in page.locator("#studio-deck-status").inner_text()
    assert len(_requests(requests, "/api/listening-deck")) == 1


def test_wrong_page_offset_is_rejected_without_enabling_a_chart_add(delivery_page):
    fixtures = StudioFixtures(count=120)
    fixtures.invalid_deck_offset = True
    page, requests = delivery_page(fixtures)
    page.locator("#studio-shuffle").click()
    playwright.expect(page.locator("#studio-deck-status")).to_contain_text("could not be confirmed")
    playwright.expect(page.locator("#studio-add")).to_be_disabled()
    assert not _requests(requests, "/api/selection", "POST")


def test_seeded_shuffle_visits_all_pages_once_before_repeating(delivery_page):
    fixtures = StudioFixtures(count=125)
    page, requests = delivery_page(fixtures)
    page.locator("#studio-shuffle").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    titles = [page.locator("#studio-now-title").inner_text()]
    for _ in range(124):
        page.locator("#studio-next").click()
        playwright.expect(page.locator("#studio-now-title")).not_to_have_text(titles[-1])
        titles.append(page.locator("#studio-now-title").inner_text())
    assert len(set(titles)) == 125
    deck_requests = _requests(requests, "/api/listening-deck")
    assert len(deck_requests) == 2
    first, second = [parse_qs(urlsplit(request.url).query) for request in deck_requests]
    assert first["seed"] == second["seed"]
    assert second["offset"] == ["100"] and second["generation_token"] == [fixtures.token]
    page.locator("#studio-next").click()
    assert page.locator("#studio-now-title").inner_text() == titles[0]


def test_chart_preview_bridge_is_lazy_and_never_creates_spotify_embed(delivery_page):
    fixtures = StudioFixtures(count=3)
    page, requests = delivery_page(fixtures)
    spotify = []
    page.route("https://open.spotify.com/**", lambda route: (spotify.append(route.request.url), route.fulfill(status=200, body="<html>Offline synthetic Spotify embed</html>", content_type="text/html")))
    page.evaluate("window.qaPreviewKeys=[]; window.RiffCatalog={...window.RiffCatalog,playKey:async key=>window.qaPreviewKeys.push(key)}")
    assert not spotify and page.locator("#studio-player-host iframe").count() == 0
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    assert not spotify
    page.locator("#studio-player-toggle").click()
    page.wait_for_function("() => window.qaPreviewKeys.length===1")
    assert page.evaluate("window.qaPreviewKeys") == ["enchor:1000"]
    assert not spotify and page.locator("#studio-player-host iframe").count() == 0
    page.locator("#studio-next").click()
    assert page.evaluate("window.qaPreviewKeys") == ["enchor:1000"]
    page.locator("#studio-player-toggle").click()
    assert page.evaluate("window.qaPreviewKeys") == ["enchor:1000", "enchor:1001"]
    assert not _requests(requests, "/api/selection", "POST")


def test_chart_preview_can_be_first_explicit_action(delivery_page):
    page, _requests_log = delivery_page(StudioFixtures(count=3))
    page.route("https://open.spotify.com/**", lambda route: route.fulfill(status=200, body="<html>Offline synthetic embed</html>", content_type="text/html"))
    page.evaluate("window.qaPreviewKeys=[]; window.RiffCatalog={...window.RiffCatalog,playKey:async key=>window.qaPreviewKeys.push(key)}")
    page.locator("#studio-player-toggle").click()
    page.wait_for_function("() => window.qaPreviewKeys.length===1")
    assert page.evaluate("window.qaPreviewKeys") == ["enchor:1000"]
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 0 (Expert only)")
    assert page.locator("#catalogPlayerDock").is_visible()
    assert page.locator("#studio-player-host iframe").count() == 0


def test_cancelled_chart_preview_bridge_does_not_announce_playback_started(delivery_page):
    page, requests = delivery_page(StudioFixtures(count=3))
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    previous_status = page.locator("#studio-deck-status").inner_text()
    page.evaluate("window.qaCancelledPreview=false;window.RiffCatalog={...window.RiffCatalog,playKey:async()=>{window.qaCancelledPreview=true;return false;}}")
    page.locator("#studio-player-toggle").click()
    page.wait_for_function("() => window.qaCancelledPreview")
    assert page.locator("#studio-deck-status").inner_text() == previous_status
    assert page.locator("#catalogAudio").evaluate("audio=>audio.paused")
    assert not _requests(requests, "/api/selection", "POST")


def test_unconfirmed_spotify_id_never_embeds(delivery_page):
    fixtures = StudioFixtures(count=2)
    fixtures.invalid_spotify_id = True
    page, _requests_log = delivery_page(fixtures)
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    playwright.expect(page.locator("#studio-player-toggle")).to_be_enabled()
    playwright.expect(page.locator("#studio-now-link")).to_be_hidden()
    assert page.locator("iframe[src*=spotify]").count() == 0


def test_unconfirmed_catalog_identity_never_opens_audio_preview(delivery_page):
    fixtures = StudioFixtures(count=2)
    fixtures.invalid_catalog_key = True
    page, _requests_log = delivery_page(fixtures)
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-player-toggle")).to_be_disabled()
    assert not page.locator("#catalogAudio").get_attribute("src")


def test_expansion_uses_catalog_identity_and_add_requires_explicit_review(delivery_page):
    fixtures = StudioFixtures(count=2)
    fixtures.expansion_groups = deepcopy(fixtures.expansion_catalog)
    page, requests = delivery_page(fixtures)
    page.locator("#studio-select-scope").select_option("expansion")
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    playwright.expect(page.locator("#studio-player-toggle")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#studio-add").click()
    assert not _requests(requests, "/api/selection", "POST")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-add").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    payload = _requests(requests, "/api/selection", "POST")[0].post_data_json
    assert payload["kind"] == "expansion" and payload["confirm_possible"] is True
    assert "track_key" not in payload


def test_generation_changes_between_pages_reset_instead_of_mixing_results(delivery_page):
    fixtures = StudioFixtures(count=125)
    fixtures.change_generation_on_page = True
    page, _requests_log = delivery_page(fixtures)
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    for _ in range(100):
        page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_disabled()
    assert "results changed" in page.locator("#studio-deck-status").inner_text().lower()


def test_unknown_recount_keeps_download_blocked(delivery_page):
    fixtures = StudioFixtures(count=3)
    fixtures.omit_save_summary = True
    fixtures.fail_recount = True
    page, _requests_log = delivery_page(fixtures)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
    playwright.expect(page.locator("#retrySelectionButton")).to_be_visible()
    assert "not confirmed" in page.locator("#studio-select-status").inner_text().lower()


def test_repeated_add_taps_make_one_mutation(delivery_page):
    page, requests = delivery_page(StudioFixtures(count=3))
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    page.evaluate("document.querySelector('#studio-add').click(); document.querySelector('#studio-add').click();")
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    assert len(_requests(requests, "/api/selection", "POST")) == 1


def test_keyboard_deck_actions_do_not_steal_input_keys(delivery_page):
    page, requests = delivery_page(StudioFixtures(count=5))
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    page.locator("#studio-deck").focus()
    page.keyboard.press("ArrowRight")
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 1")
    page.keyboard.press("a")
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 2 (Expert only)")
    page.locator("#reviewSearch").focus()
    page.keyboard.press("ArrowRight")
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 2 (Expert only)")
    assert len(_requests(requests, "/api/selection", "POST")) == 1


def test_selected_filter_refreshes_after_removal_with_authoritative_count(delivery_page):
    fixtures = StudioFixtures(count=3)
    fixtures.selected = {1000, 1001}
    page, _requests_log = delivery_page(fixtures)
    page.locator('[data-review-filter="selected"]').click()
    playwright.expect(page.locator("#reviewCards > article")).to_have_count(2)
    page.locator('[data-candidate="1000"][data-selection="clear"]').click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    playwright.expect(page.locator("#reviewCards > article")).to_have_count(1)
    assert page.locator('[data-candidate="1001"][data-selection="clear"]').count() == 1


def test_hidden_page_pauses_job_requests_and_resumes_when_visible(delivery_page):
    fixtures = YieldFixtures()
    fixtures.scenario = "find"
    fixtures._discovery_job_id = 41
    page, requests = delivery_page(fixtures)
    page.clock.install()
    page.reload(wait_until="networkidle")
    page.evaluate("window.riffHiddenForTest = true; Object.defineProperty(document, 'hidden', {configurable:true, get:() => window.riffHiddenForTest});")
    baseline = len(_requests(requests, "/api/jobs/41"))
    page.clock.run_for(20000)
    assert len(_requests(requests, "/api/jobs/41")) == baseline
    with page.expect_request("**/api/jobs/41"):
        page.evaluate("window.riffHiddenForTest = false; document.dispatchEvent(new Event('visibilitychange'));")
    assert len(_requests(requests, "/api/jobs/41")) == baseline + 1


def test_skip_added_song_uses_library_song_identity_when_another_version_is_selected(delivery_page):
    page, _requests_log = delivery_page(StudioFixtures(count=3, versions=2))
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 0 (Expert only)")
    # The queue's suggested version for song 1 is 1002; select version 1003
    # through the regular chart cards after the queue has already been loaded.
    version_group = page.locator('.song-group').filter(has=page.locator('[data-candidate="1003"]')).locator('details.alternatives')
    if version_group.get_attribute("open") is None:
        version_group.locator(":scope > summary").click()
    page.locator('[data-candidate="1003"][data-selection="select"]').click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 2 (Expert only)")
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    page.locator("#studio-skip-selected").uncheck()
    page.locator("#studio-next").click()
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Studio song 1")
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    page.locator("#studio-add").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("2")


def test_skip_added_expansion_song_uses_song_key_for_another_chart_version(delivery_page):
    fixtures = StudioFixtures(count=2)
    fixtures.expansion_groups = [
        fixtures.expansion_group("Paramore", "That's What You Get", [301, 302]),
        fixtures.expansion_group("Radiohead", "Bodysnatchers", [401, 402]),
        fixtures.expansion_group("Radiohead", "Synthetic third song", [501]),
    ]
    page, _requests_log = delivery_page(fixtures)
    page.locator("#studio-select-scope").select_option("expansion")
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-add")).to_be_enabled()
    alternatives = page.locator('.review-card--expansion').filter(has=page.locator('[data-candidate="402"]')).locator("details.alternatives")
    if alternatives.get_attribute("open") is None:
        alternatives.locator(":scope > summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('[data-candidate="402"][data-expansion-selection="select"]').click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("1")
    page.locator("#studio-next").click()
    playwright.expect(page.locator("#studio-now-title")).to_have_text("Synthetic third song")
