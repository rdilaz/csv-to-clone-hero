"""Real public UI with synthetic difficulty-inspection and expansion responses.

No provider calls, real user data, chart downloads or production-count claims.
Backend inspection/preset authority has separate API and note-parser coverage.
"""

from __future__ import annotations

import json
from copy import deepcopy
from urllib.parse import urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import _assert_no_overflow, _capture, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page

pytestmark = pytest.mark.browser


def _coverage(parts, *, evidence="inspected_notes", note_count=123):
    return {
        "evidence": evidence,
        "instruments": parts,
        "guitar_difficulties": parts.get("guitar", []),
        "expert_only": bool(parts) and all(levels == ["expert"] for levels in parts.values()),
        "playable_note_count": note_count,
    }


class YieldFixtures(SignalCheckFixtures):
    def __init__(self):
        super().__init__("confirm")
        self.coverages = {
            101: _coverage({"guitar": ["expert"], "bass": ["expert"]}),
            102: _coverage({"guitar": ["easy", "medium", "hard", "expert"]}),
            103: _coverage({"guitar": ["expert"], "bass": ["easy", "expert"]}),
            201: {"evidence": "unknown", "instruments": {}, "guitar_difficulties": [], "expert_only": None, "playable_note_count": None},
            301: _coverage({"guitar": ["expert"]}),
            302: _coverage({"guitar": ["easy", "medium", "hard", "expert"]}),
            401: _coverage({"guitar": ["expert"], "bass": ["easy", "expert"]}),
        }
        self.expansion_groups = deepcopy(self.expansion_catalog)
        self.support_new_modes = True
        self.requested_mode = "all"
        self.requested_all_artists = False
        self.recommended_counts = {}
        self.fail_bulk_recount = False

    def tracks(self):
        tracks = super().tracks()
        for track in tracks:
            for candidate in track["candidates"]:
                candidate["difficulty_coverage"] = deepcopy(self.coverages[candidate["candidate_id"]])
                if candidate["candidate_id"] == 201:
                    candidate["diff_guitar"] = 6
        return tracks

    def expansion_songs(self):
        songs = super().expansion_songs()
        for song in songs:
            for candidate in song["candidates"]:
                candidate["difficulty_coverage"] = deepcopy(self.coverages.get(candidate["candidate_id"], self.coverages[201]))
        return songs

    def expansion_artists(self):
        payload = super().expansion_artists()
        if self.support_new_modes:
            payload.update(coverage_modes=["all", "recommended"], supports_all_artists=True, supports_bulk_selection=True, eligible_artist_count=len(payload["artists"]))
        for artist in payload["artists"]:
            if artist["artist_norm"] == "paramore":
                artist.update(eligible_songs=123, eligible_charts=234, cap=10)
            artist["recommended_eligible_songs"] = self.recommended_counts.get(artist["artist_norm"], artist["eligible_songs"])
        return payload

    def expansion_job(self):
        job = super().expansion_job()
        if job:
            job["progress"].update(coverage_mode=self.requested_mode, all_artists=self.requested_all_artists)
        return job

    def respond(self, method, request_url, post_data=None):
        path = urlsplit(request_url).path
        body = json.loads(post_data or "{}")
        if path == "/api/expansion/results" and method == "GET" and self.fail_bulk_recount and self.expansion_selected:
            self.request_log.append((method, path, {}))
            return 503, {"error": "Additional-song summary is temporarily unavailable."}
        if path == "/api/expansion" and method == "POST" and self.support_new_modes:
            self.request_log.append((method, path, body))
            self.requested_mode = body.get("coverage_mode")
            self.requested_all_artists = body.get("all_artists") is True
            artists = [artist["artist_norm"] for artist in self.expansion_artists()["artists"]] if self.requested_all_artists else body.get("artists", [])
            self.expansion_artists_selected = set(artists)
            self._expansion_status = "running"
            return 202, {"job": self.expansion_job()}
        if path == "/api/expansion/selection/bulk" and method == "POST":
            self.request_log.append((method, path, body))
            if body != {"preset": "one_per_song", "confirm_possible": True}:
                return 400, {"error": "Explicit expansion-review confirmation is required."}
            # Model the separately tested server preset: one candidate per song,
            # known multiple guitar levels first; exact choices are untouched.
            self.expansion_selected = {302, 401}
            return 200, {"ok": True, "songs": 2, "charts": 2, "changed": 2, "scope": "expansion"}
        return super().respond(method, request_url, post_data)


def _posts(requests, path):
    return [request for request in requests if request.method == "POST" and urlsplit(request.url).path == path]


@pytest.mark.parametrize("width", [390, 1440])
def test_expert_only_is_labeled_and_selectable_in_exact_and_expansion_choices(delivery_page, width):
    fixtures = YieldFixtures()
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": 844 if width == 390 else 900})
    row = page.locator('.candidate-row').filter(has=page.locator('[data-candidate="101"]'))
    playwright.expect(row.locator("h4")).to_have_text("Misery Business (Expert only)")
    playwright.expect(row.locator('[data-candidate="101"]')).to_be_enabled()
    assert "Expert only" in row.locator('[data-candidate="101"]').get_attribute("aria-label")
    assert "Inspected note tracks · Guitar: Expert; Bass: Expert" in row.inner_text()
    if width == 390:
        action = row.locator('[data-candidate="101"]').bounding_box()
        rail = page.locator("#selectionRail").bounding_box()
        assert action and rail and action["y"] + action["height"] + 7 <= rail["y"]
    expansion = page.locator('.review-card--expansion').filter(has=page.locator('[data-candidate="301"]'))
    playwright.expect(expansion.locator(".candidate-summary > h4").first).to_have_text("Paramore - That's What You Get (Expert only)")
    playwright.expect(expansion.locator('[data-candidate="301"]')).to_be_enabled()
    assert "Expert only" in expansion.locator('[data-candidate="301"]').get_attribute("aria-label")
    _assert_no_overflow(page)
    _capture(page, f"yield-choice-{width}.png")
    assert not [request for request in requests if request.method == "POST"]


def test_multiple_mixed_and_unknown_levels_do_not_become_blanket_expert_only(delivery_page):
    page, _requests = delivery_page(YieldFixtures())
    multiple = page.locator('.candidate-row').filter(has=page.locator('[data-candidate="102"]'))
    assert "Guitar: Easy, Medium, Hard, Expert" in multiple.inner_text()
    assert "Expert only" not in multiple.locator("h4").inner_text()
    mixed = page.locator('.candidate-row').filter(has=page.locator('[data-candidate="103"]'))
    assert mixed.locator("h4").inner_text().endswith("(Guitar: Expert only)")
    assert "Bass: Easy, Expert" in mixed.inner_text()
    unknown = page.locator('.candidate-row').filter(has=page.locator('[data-candidate="201"]'))
    assert "Guitar intensity rating 6" in unknown.inner_text()
    assert "Difficulty levels unknown" in unknown.inner_text()
    assert "Expert only" not in unknown.locator("h4").inner_text()


@pytest.mark.parametrize("coverage", [
    _coverage({"guitar": ["expert"]}, evidence="unknown"),
    _coverage({"guitar": ["expert"]}, evidence="provider_metadata"),
    _coverage({"guitar": ["expert"]}, note_count=0),
])
def test_unverified_or_empty_note_evidence_cannot_label_a_chart_expert_only(delivery_page, coverage):
    fixtures = YieldFixtures()
    fixtures.coverages[101] = coverage
    page, _requests = delivery_page(fixtures)
    row = page.locator('.candidate-row').filter(has=page.locator('[data-candidate="101"]'))
    assert "Expert only" not in row.locator("h4").inner_text()
    assert "Difficulty levels unknown" in row.inner_text()
    assert row.locator('[data-candidate="101"]').is_enabled()


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_expansion_jump_and_all_mode_are_explicit_without_automatic_jobs(delivery_page, width):
    fixtures = YieldFixtures()
    fixtures.expansion_groups = []
    fixtures.expansion_complete_after_polls = None
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": 900})
    assert not _posts(requests, "/api/expansion")
    page.locator(".expansion-jump").click()
    playwright.expect(page.locator("#expansionTitle")).to_be_focused()
    playwright.expect(page.locator('[name="expansionCoverage"][value="all"]')).to_be_checked()
    assert "include all 123 songs" in page.locator('#expansionChoices label').filter(has_text="Paramore").inner_text()
    page.locator('#expansionChoices input[value="paramore"]').check()
    _assert_no_overflow(page)
    _capture(page, f"yield-expansion-{width}.png")
    page.locator("#startExpansionButton").click()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    posts = _posts(requests, "/api/expansion")
    assert len(posts) == 1
    assert posts[0].post_data_json == {"artists": ["paramore"], "all_artists": False, "coverage_mode": "all"}
    assert page.locator('[name="expansionCoverage"][value="recommended"]').is_disabled()


def test_recommended_amount_uses_cap_without_excluding_expert_only(delivery_page):
    fixtures = YieldFixtures()
    fixtures.expansion_complete_after_polls = None
    page, requests = delivery_page(fixtures)
    page.locator('[name="expansionCoverage"][value="recommended"]').check()
    assert "recommend up to 10 songs" in page.locator('#expansionChoices label').filter(has_text="Paramore").inner_text()
    assert "Expert-only charts remain eligible" in page.locator("#expansionCoverageExplanation").inner_text()
    page.locator('#expansionChoices input[value="paramore"]').check()
    page.locator("#startExpansionButton").click()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    assert _posts(requests, "/api/expansion")[0].post_data_json["coverage_mode"] == "recommended"


def test_recommended_scope_uses_quality_qualified_counts_and_disables_zero_count_artists(delivery_page):
    fixtures = YieldFixtures()
    fixtures.recommended_counts = {"paramore": 0, "radiohead": 1}
    page, requests = delivery_page(fixtures)
    paramore = page.locator('#expansionChoices input[value="paramore"]')
    paramore.check()
    page.locator('[name="expansionCoverage"][value="recommended"]').check()
    playwright.expect(paramore).to_be_disabled()
    playwright.expect(paramore).not_to_be_checked()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    assert "recommend up to 0 songs" in page.locator('#expansionChoices label').filter(has_text="Paramore").inner_text()
    assert "recommend up to 1 song" in page.locator('#expansionChoices label').filter(has_text="Radiohead").inner_text()
    assert "1 eligible artists" in page.locator("#allExpansionArtistCount").inner_text()
    page.locator('[name="expansionCoverage"][value="all"]').check()
    playwright.expect(paramore).to_be_enabled()
    assert "include all 123 songs" in page.locator('#expansionChoices label').filter(has_text="Paramore").inner_text()
    assert not _posts(requests, "/api/expansion")


def test_all_artists_is_an_opt_in_server_scope_that_bypasses_the_individual_artist_limit(delivery_page):
    fixtures = YieldFixtures()
    fixtures.expansion_catalog = [fixtures.expansion_group(f"Artist {index}", f"Additional song {index}", [500 + index]) for index in range(30)]
    fixtures.expansion_complete_after_polls = None
    page, requests = delivery_page(fixtures)
    assert not page.locator("#allExpansionArtists").is_checked()
    assert page.locator("#startExpansionButton").is_disabled()
    assert "30 eligible artists" in page.locator("#allExpansionArtistCount").inner_text()
    page.locator("#allExpansionArtists").check()
    assert page.locator("#startExpansionButton").is_enabled()
    assert page.locator('#expansionChoices input:enabled').count() == 0
    page.locator("#startExpansionButton").click()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    assert _posts(requests, "/api/expansion")[0].post_data_json == {"artists": [], "all_artists": True, "coverage_mode": "all"}


def test_reload_restores_running_all_artist_scope_without_posting_a_second_job(delivery_page):
    fixtures = YieldFixtures()
    fixtures._expansion_status = "running"
    fixtures.requested_mode = "all"
    fixtures.requested_all_artists = True
    fixtures.expansion_artists_selected = {"paramore", "radiohead"}
    fixtures.expansion_complete_after_polls = None
    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#allExpansionArtists")).to_be_checked()
    playwright.expect(page.locator("#allExpansionArtists")).to_be_disabled()
    playwright.expect(page.locator('[name="expansionCoverage"][value="all"]')).to_be_checked()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#allExpansionArtists")).to_be_checked()
    playwright.expect(page.locator("#startExpansionButton")).to_be_disabled()
    assert not _posts(requests, "/api/expansion")


def test_bulk_expansion_choice_needs_confirmation_and_keeps_exact_choices(delivery_page):
    fixtures = YieldFixtures()
    page, requests = delivery_page(fixtures)
    button = page.locator("#selectExpansionSongsButton")
    assert button.is_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    button.click()
    assert not _posts(requests, "/api/expansion/selection/bulk")
    assert not fixtures.expansion_selected
    dialogs = []
    page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    button.click()
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("2")
    playwright.expect(page.locator("#exactSelectedCount")).to_have_text("3")
    assert fixtures.selected == {101, 102, 103}
    assert fixtures.expansion_selected == {302, 401}
    assert len(_posts(requests, "/api/expansion/selection/bulk")) == 1
    assert not _posts(requests, "/api/selection/bulk")
    assert "Replace your additional-song choices" in dialogs[0]
    assert "marked for review" in dialogs[0] and "Unknown difficulty levels" in dialogs[0]
    assert "imported-song choices stay unchanged" in dialogs[0]


def test_saved_bulk_choices_with_failed_recount_block_download_until_reconciled(delivery_page):
    fixtures = YieldFixtures()
    fixtures.fail_bulk_recount = True
    page, _requests = delivery_page(fixtures)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#selectExpansionSongsButton").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("...")
    playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
    playwright.expect(page.locator("#selectExpansionSongsButton")).to_be_disabled()
    playwright.expect(page.locator("#retrySelectionButton")).to_be_visible()
    assert fixtures.expansion_selected == {302, 401}
    assert fixtures.selected == {101, 102, 103}
    fixtures.fail_bulk_recount = False
    page.locator("#retrySelectionButton").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("5")
    playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()


def test_legacy_expansion_api_does_not_promise_unsupported_all_or_bulk_modes(delivery_page):
    fixtures = YieldFixtures()
    fixtures.support_new_modes = False
    page, requests = delivery_page(fixtures)
    assert page.locator('[name="expansionCoverage"][value="all"]').is_disabled()
    assert page.locator('[name="expansionCoverage"][value="recommended"]').is_checked()
    assert page.locator("#allExpansionArtists").is_disabled()
    assert page.locator("#selectExpansionSongsButton").is_disabled()
    assert not [request for request in requests if request.method == "POST"]
