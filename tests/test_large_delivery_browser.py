"""ZIP controls must stay usable without loading the complete review pool."""
from urllib.parse import urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import public_server, playwright
from tests.test_public_delivery_browser import NOW, delivery_chromium, delivery_page

pytestmark = pytest.mark.browser


def _completed_download(status, *, age_seconds=0):
    """Prepare a tiny synthetic archive before opening a fresh browser tab."""
    fixtures = SignalCheckFixtures("confirm")
    fixtures.clock = lambda: NOW.timestamp() - age_seconds
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    if status == "partial":
        fixtures.export_failures[103] = "Synthetic unavailable artifact"
    assert fixtures.start_download_job()[0] == 202
    assert fixtures.poll_download_job()[0] == 200
    fixtures.download_job["status"] = status
    assert fixtures.download_job["result"]["archive_id"]
    return fixtures


def _lightweight_routes(fixtures):
    def routes(route):
        if urlsplit(route.request.url).path in {
            "/api/journey", "/api/workspace", "/api/expansion/results", "/api/catalog/selections"
        }:
            route.fulfill(status=503, json={"error": "Full review must not be needed for delivery recovery"})
        else:
            fixtures.fulfill(route)
    return routes


def _assert_read_only_lightweight_restore(requests):
    assert not [request for request in requests if request.method == "POST"]
    assert not [request for request in requests if urlsplit(request.url).path in {
        "/api/journey", "/api/workspace", "/api/expansion/results", "/api/catalog/selections"
    }]
    assert not [request for request in requests if urlsplit(request.url).path.startswith("/api/download/")]


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_fresh_tab_restores_completed_archive_without_local_receipt_or_review(delivery_page, status):
    fixtures = _completed_download(status)
    published = fixtures.download_job["result"]["summary"]["published"]
    page, requests = delivery_page(fixtures, route_api=_lightweight_routes(fixtures))
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(page.locator("#downloadLibraryLink")).to_have_attribute("href", fixtures.download["url"])
    playwright.expect(page.locator("#doneSummary")).to_contain_text(f"{published} validated chart artifacts are packaged")
    if status == "partial":
        playwright.expect(page.locator("#doneTitle")).to_have_text("Your ZIP is ready, with exceptions.")
        playwright.expect(page.locator("#doneWarnings")).to_contain_text("1 failed selections")
    else:
        playwright.expect(page.locator("#doneTitle")).to_have_text("Your ZIP is ready.")
    assert fixtures.download_job["result"]["archive_id"] == fixtures.download["archive_id"]
    assert fixtures.selected == {101, 102, 103}
    _assert_read_only_lightweight_restore(requests)


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_fresh_tab_restores_expired_preparation_to_lightweight_retry(delivery_page, status):
    fixtures = _completed_download(status, age_seconds=1000)
    page, requests = delivery_page(fixtures, route_api=_lightweight_routes(fixtures))
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Prepare a new ZIP")
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("3")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    playwright.expect(page.locator("#stageDone")).to_be_hidden()
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_hidden()
    assert not page.locator("#downloadLibraryLink").get_attribute("href")
    assert fixtures.download is None
    assert fixtures.selected == {101, 102, 103}
    _assert_read_only_lightweight_restore(requests)


def test_fresh_tab_never_publishes_mismatched_completed_archive(delivery_page):
    fixtures = _completed_download("complete")
    token = fixtures.download["token"]
    fixtures._downloads[token]["metadata"]["archive_id"] = "synthetic-other-archive"
    page, requests = delivery_page(fixtures, route_api=_lightweight_routes(fixtures))
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Prepare a new ZIP")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    playwright.expect(page.locator("#stageDone")).to_be_hidden()
    assert not page.locator("#downloadLibraryLink").get_attribute("href")
    assert fixtures.download["archive_id"] != fixtures.download_job["result"]["archive_id"]
    _assert_read_only_lightweight_restore(requests)


@pytest.mark.parametrize("stage", ["add", "confirm", "find"])
def test_completed_archive_restore_respects_explicit_music_review_stage(delivery_page, stage):
    fixtures = _completed_download("complete")
    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    page.evaluate("stage => sessionStorage.setItem('riff:delivery:1', JSON.stringify({stage}))", stage)
    before = len(requests)
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageAdd" if stage == "add" else "#stageConfirm")).to_be_visible()
    playwright.expect(page.locator("#stageDone")).to_be_hidden()
    assert any(urlsplit(request.url).path == "/api/journey" for request in requests[before:])
    assert not [request for request in requests[before:] if request.method == "POST"]


def test_large_saved_counts_allow_preparation_when_optional_details_fail(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    block_details = {"value": False}
    counts = {"library": {"songs": 513, "charts": 513},
              "expansion": {"songs": 5396, "charts": 5396},
              "catalog": {"songs": 0, "charts": 0}}

    def routes(route):
        path = urlsplit(route.request.url).path
        if path == "/api/delivery/status":
            status, data = fixtures.respond("GET", route.request.url)
            data.update(selection_summary=counts, selection_count=5909)
            route.fulfill(status=status, json=data)
        elif block_details["value"] and path in {"/api/workspace", "/api/expansion/results"}:
            route.fulfill(status=503, json={"error": "Optional detail service unavailable"})
        else:
            fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=routes)
    block_details["value"] = True
    before = len(requests)
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("5,909")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    assert not [request for request in requests[before:] if urlsplit(request.url).path in {
        "/api/journey", "/api/workspace", "/api/expansion/results", "/api/catalog/selections"}]
    page.locator("#deliveryDetails > summary").click()
    playwright.expect(page.locator("#deliverySelectionStatus")).to_contain_text("Saved counts and ZIP preparation are still available")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("5,909")
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    assert len([request for request in requests if request.method == "POST" and urlsplit(request.url).path == "/api/download"]) == 1


def test_download_restore_avoids_full_journey_and_review_queries(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    before = len(requests)
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    assert not [request for request in requests[before:] if urlsplit(request.url).path in {
        "/api/journey", "/api/workspace", "/api/expansion/results"}]


def test_ready_restore_does_not_load_hidden_saved_open_details(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    page.locator("#deliveryDetails > summary").click()
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(3)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    before = len(requests)
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    assert not [request for request in requests[before:] if urlsplit(request.url).path in {
        "/api/journey", "/api/workspace", "/api/expansion/results", "/api/catalog/selections"}]


def test_late_detail_page_does_not_render_after_selection_revision_changes(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    hold = {"value": False}
    pending = []

    def routes(route):
        if hold["value"] and urlsplit(route.request.url).path == "/api/workspace":
            status, data = fixtures.respond("GET", route.request.url)
            pending.append((route, status, data))
        else:
            fixtures.fulfill(route)

    page, _requests = delivery_page(fixtures, route_api=routes)
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    hold["value"] = True
    page.locator("#deliveryDetails > summary").click()
    page.wait_for_function("document.querySelector('#deliveryDetails').open")
    assert len(pending) == 1
    fixtures.selection_revision += 1
    page.evaluate("receipt => document.dispatchEvent(new CustomEvent('riff:catalog-selection', {detail:receipt}))", fixtures.selected_response())
    route, status, data = pending.pop()
    with page.expect_response(route.request.url):
        route.fulfill(status=status, json=data)
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(0)
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("Awaiting confirmation")
