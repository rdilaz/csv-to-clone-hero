"""Real public UI coverage with synthetic, source-neutral imported libraries.

The Flask template, external JavaScript and CSS are real. Only provider/API
payloads are isolated fixtures; these counts are not production observations.
"""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import _assert_no_overflow, _capture, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page

pytestmark = pytest.mark.browser


class LibraryFixtures(SignalCheckFixtures):
    def __init__(self):
        super().__init__("confirm")

    def tracks(self):
        tracks = super().tracks()
        statuses = [
            ("no_match", "No suitable chart found", "No suitable chart was found in the completed search."),
            ("not_checked", "Not checked", "Run discovery to check this song for a playable chart."),
            ("in_progress", "Checking", "The chart search is still checking this song."),
            ("incomplete", "Matching incomplete", "Matching did not finish; run the search again before deciding."),
            ("not_playable", "Not playable", "This chart has no playable instrument content."),
            ("unknown", "", ""),
        ]
        for index, (state, label, summary) in enumerate(statuses):
            review = {"state": state, "label": label, "summary": summary, "selectable": False}
            # A blocked candidate is distinct from an empty completed search.
            candidates = []
            if state == "not_playable":
                blocked = deepcopy(tracks[0]["candidates"][0])
                blocked.update(result_tier="BLOCKED", selected=False, selectable=False, review=review)
                candidates = [blocked]
            tracks.append({
                "track_key": f"soundcloud:unavailable:{index}",
                "artist": "SoundCloud & Friends",
                "title": "Not checked <script>alert('preview')</script>" if state == "not_checked" else f"Status song {state}",
                "album": "<Live & unreleased>",
                "candidates": candidates,
                "selected_count": 0,
                "selected": False,
                "review": review,
                "review_state": state,
                "review_label": label,
                "review_summary": summary,
                "review_notice": "",
            })
        for index in range(24):
            track = deepcopy(tracks[2])
            track.update(track_key=f"combined:unmatched:{index}", title=f"Unmatched song {index:02}")
            tracks.append(track)
        return tracks

    def summary(self):
        summary = super().summary()
        summary["unavailable"] = len(self.tracks()) - 2
        return summary


def _catalog_route(fixtures, preflight, *, status=200):
    def route_api(route):
        if urlsplit(route.request.url).path == "/api/preflight":
            route.fulfill(status=status, json={"preflight": preflight})
        else:
            fixtures.fulfill(route)
    return route_api


def _open_catalog_details(page):
    """Inspect detailed counts through the same native disclosure a user opens."""
    playwright.expect(page.locator("#catalogCoverageBrief")).to_be_visible()
    details = page.locator(".selection-options")
    if not details.evaluate("element => element.open"):
        details.locator(":scope > summary").click()
    playwright.expect(page.locator("#catalogCoverage")).to_be_visible()


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_library_preview_lists_every_imported_song_and_honest_readonly_status(delivery_page, width):
    fixtures = LibraryFixtures()
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": 900})
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    playwright.expect(page.locator("#librarySongCount")).to_have_text("32")
    playwright.expect(page.locator("#chartsFoundCount")).to_have_text("1")
    playwright.expect(page.locator("#librarySelectedChartCount")).to_have_text("3")
    playwright.expect(page.locator('[data-review-filter="all"]')).to_have_attribute("aria-pressed", "true")
    playwright.expect(page.locator(".library-boundary")).to_have_text("Selected imports only")
    assert page.locator("#reviewCards .song-group").count() == 25
    readonly = page.locator(".song-group--readonly")
    assert readonly.count() == 23
    assert readonly.locator("button, input, select, a").count() == 0
    for label in ["No suitable chart found", "Not checked", "Checking", "Matching incomplete", "Not playable", "Chart status unavailable"]:
        assert readonly.get_by_text(label, exact=True).count() >= 1
    assert page.locator("#reviewCards script").count() == 0
    assert "<script>alert('preview')</script>" in "\n".join(readonly.all_text_contents())
    first_query = parse_qs(urlsplit(next(request.url for request in requests if urlsplit(request.url).path == "/api/workspace")).query)
    assert "tier" not in first_query and "selected" not in first_query
    _assert_no_overflow(page)
    _capture(page, f"library-all-{width}.png")
    _open_catalog_details(page)
    playwright.expect(page.locator(".library-scope")).to_be_visible()
    assert "haven't imported" in page.locator(".library-scope").inner_text()
    page.locator(".selection-options > summary").click()
    readonly.first.scroll_into_view_if_needed()
    _capture(page, f"library-status-{width}.png")
    page.locator("#reviewNext").click()
    playwright.expect(page.locator("#reviewPage")).to_have_text("2 / 2")
    assert page.locator("#reviewCards .song-group").count() == 7
    page.locator("#reviewSearch").fill("Unmatched song 23")
    playwright.expect(page.locator("#reviewCards .song-group")).to_have_count(1)
    playwright.expect(page.locator("#reviewPage")).to_have_text("1 / 1")
    playwright.expect(page.locator("#librarySongCount")).to_have_text("32")
    playwright.expect(page.locator("#reviewSummary")).to_contain_text("1 song in this view")
    assert page.locator("#reviewNext").is_disabled()
    assert not [request for request in requests if request.method == "POST"]


def test_library_total_stays_distinct_from_all_playable_recommended_review_and_selected_views(delivery_page):
    page, requests = delivery_page(LibraryFixtures())
    for filter_name, expected in [("recommended", 2), ("strong", 1), ("review", 1), ("selected", 1), ("all", 25)]:
        page.locator(f'[data-review-filter="{filter_name}"]').click()
        playwright.expect(page.locator("#reviewCards .song-group")).to_have_count(expected)
        playwright.expect(page.locator("#librarySongCount")).to_have_text("32")
        playwright.expect(page.locator("#chartsFoundCount")).to_have_text("1")
        playwright.expect(page.locator("#librarySelectedChartCount")).to_have_text("3")
    assert not [request for request in requests if request.method == "POST"]


@pytest.mark.parametrize("include_song_count", [False, True])
def test_verified_catalog_shows_indexed_records_and_eligible_counts(delivery_page, include_song_count):
    fixtures = LibraryFixtures()
    preflight = {"mode": "catalog", "catalog_available": True, "out_of": 120000, "eligible_charts": 110000, "snapshot_id": "test-snapshot-<safe>", "usable": True}
    if include_song_count:
        preflight["eligible_songs"] = 80000
    page, requests = delivery_page(fixtures, route_api=_catalog_route(fixtures, preflight))
    playwright.expect(page.locator("#catalogCoverageBrief")).to_have_text("Local catalog · 120,000 indexed charts · 110,000 eligible")
    _open_catalog_details(page)
    playwright.expect(page.locator("#catalogCoverage")).to_contain_text("120,000 indexed chart records; 110,000 eligible charts")
    playwright.expect(page.locator("#catalogSnapshot")).to_have_text("Snapshot: test-snapshot-<safe>")
    assert ("80,000 distinct eligible songs" in page.locator("#catalogCoverage").inner_text()) is include_song_count
    assert page.locator("#catalogSnapshot safe").count() == 0
    assert [(request.method, urlsplit(request.url).path) for request in requests if urlsplit(request.url).path == "/api/preflight"] == [("GET", "/api/preflight")]
    _capture(page, f"library-catalog-{include_song_count}.png")


@pytest.mark.parametrize("preflight,expected", [
    ({"mode": "live_fallback", "catalog_available": False}, "advertised chart count and its freshness are unknown"),
    ({"mode": "live_fallback", "catalog_available": False, "out_of": 95440, "success": True, "supported": True, "authoritative": True}, "95,440 chart records (freshness unknown)"),
    ({"mode": "live_fallback", "catalog_available": False, "out_of": 95440, "fresh": False, "success": True, "supported": True, "authoritative": True}, "95,440 chart records (report may be stale)"),
    ({"mode": "live_fallback", "catalog_available": False, "out_of": 95440, "fresh": True, "success": True, "supported": True, "authoritative": True}, "95,440 chart records (recent readiness report)"),
    ({"mode": "live_fallback", "catalog_available": False, "out_of": 0, "fresh": True, "success": False, "supported": False, "authoritative": False}, "advertised chart count and its freshness are unknown"),
])
def test_live_advertised_counts_are_not_claimed_as_riffs_indexed_catalog(delivery_page, preflight, expected):
    fixtures = LibraryFixtures()
    page, requests = delivery_page(fixtures, route_api=_catalog_route(fixtures, preflight))
    _open_catalog_details(page)
    playwright.expect(page.locator("#catalogCoverage")).to_contain_text(expected)
    assert "no verified local indexed catalog" in page.locator("#catalogCoverage").inner_text()
    assert "not Riff's indexed pool" in page.locator("#catalogCoverage").inner_text()
    assert page.locator("#catalogSnapshot").is_hidden()
    if not preflight.get("success"):
        assert "0 advertised" not in page.locator("#catalogCoverageBrief").inner_text()
        assert "count unknown" in page.locator("#catalogCoverageBrief").inner_text()
    assert not [request for request in requests if request.method == "POST"]


def test_unknown_library_total_and_failed_catalog_read_never_turn_into_zero(delivery_page):
    fixtures = LibraryFixtures()

    def route_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/preflight":
            route.fulfill(status=503, json={"error": "Synthetic unavailable response"})
        elif path == "/api/workspace":
            status, payload = fixtures.respond(route.request.method, route.request.url)
            del payload["summary"]["total"]
            route.fulfill(status=status, json=payload)
        else:
            fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#librarySongCount")).to_have_text("Unknown")
    playwright.expect(page.locator("#catalogCoverageBrief")).to_have_text("Chart catalog counts unknown")
    _open_catalog_details(page)
    playwright.expect(page.locator("#catalogCoverage")).to_contain_text("counts are unknown")
    assert page.locator("#stageConfirm").is_visible()
    assert page.locator("#appError").is_hidden()
    _assert_no_overflow(page)
    assert not [request for request in requests if request.method == "POST"]


@pytest.mark.parametrize("failed_field", ["success", "supported", "authoritative"])
def test_each_live_report_verification_flag_is_required_before_showing_a_count(delivery_page, failed_field):
    fixtures = LibraryFixtures()
    preflight = {"mode": "live_fallback", "catalog_available": False, "out_of": 95440, "fresh": True, "success": True, "supported": True, "authoritative": True}
    preflight[failed_field] = False
    page, requests = delivery_page(fixtures, route_api=_catalog_route(fixtures, preflight))
    playwright.expect(page.locator("#catalogCoverageBrief")).to_have_text("Live fallback · advertised chart count unknown")
    _open_catalog_details(page)
    assert "95,440" not in page.locator("#catalogCoverage").inner_text()
    assert "report is not verified" in page.locator("#catalogCoverage").inner_text()
    assert not [request for request in requests if request.method == "POST"]


@pytest.mark.parametrize("width", [320, 390])
def test_large_imported_and_chart_counts_fit_phone_metrics(delivery_page, width):
    fixtures = LibraryFixtures()

    def route_api(route):
        if urlsplit(route.request.url).path == "/api/workspace":
            status, payload = fixtures.respond(route.request.method, route.request.url)
            # Exercise large server counts without constructing a huge fixture.
            payload["summary"].update(total=50000, recommended_songs=17000, selected_charts=50000)
            route.fulfill(status=status, json=payload)
        else:
            fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api, viewport={"width": width, "height": 900})
    playwright.expect(page.locator("#librarySongCount")).to_have_text("50,000")
    playwright.expect(page.locator("#chartsFoundCount")).to_have_text("17,000")
    playwright.expect(page.locator("#librarySelectedChartCount")).to_have_text("50,000")
    _assert_no_overflow(page)
    assert page.locator(".library-totals").evaluate("element => element.scrollWidth <= element.clientWidth")
    _capture(page, f"library-large-counts-{width}.png")
    assert not [request for request in requests if request.method == "POST"]
