"""Offline catalog/ZIP journey integration, including real Flask receipts.

Synthetic provider packages are generated locally. No Enchor or Spotify request
is allowed; signed ZIP links are used only through native browser download.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from csv_to_clonehero.database import Database
from tests.conftest import FakeSource, make_chart, valid_sng_bytes
from tests.test_catalog import write_snapshot
from tests.test_catalog_browser import CatalogFixtures, _browse, _posts, catalog_page
from tests.test_public_browser import playwright, public_server
from tests.test_public_delivery_browser import _done_count, delivery_chromium

pytestmark = pytest.mark.browser


class CatalogJourneyFixtures(CatalogFixtures):
    """Include selected catalog choices in the synthetic archive and read page."""

    @property
    def selected_candidates(self):
        return super().selected_candidates + [
            {**self.item(chart_id), "candidate_id": 10000 + chart_id}
            for chart_id in sorted(self.catalog_selected)
        ]

    def fulfill(self, route):
        parsed = urlsplit(route.request.url)
        if parsed.path == "/api/catalog/selections" and route.request.method == "GET":
            query = parse_qs(parsed.query)
            self.request_log.append(("GET", parsed.path, query))
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["25"])[0])
            items = [self.item(index) for index in sorted(self.catalog_selected)]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                **self.selected_response(), "items": items[offset:offset + limit],
                "total": len(items), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(items),
                "snapshot_token": "synthetic-catalog-1",
            }))
            return
        super().fulfill(route)


def _done_with_catalog(catalog_page):
    fixtures = CatalogJourneyFixtures()
    fixtures.catalog_selected = {1, 2}
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    page, fixtures = catalog_page(fixtures)
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("5")
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    return page, fixtures


def test_done_receipt_includes_catalog_selection_at_preparation(catalog_page):
    page, fixtures = _done_with_catalog(catalog_page)
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("5")
    playwright.expect(_done_count(page, "Selected before preparation")).to_have_text(
        "3 charts from your music / 1 imported song; 0 charts from artist expansion; "
        "2 charts from catalog browsing"
    )
    assert fixtures.last_export_summary["selected"] == 5
    assert len(_posts(fixtures, "/api/download")) == 1


@pytest.mark.parametrize("control", ["prepare-again", "review-zip"])
def test_catalog_added_after_done_reviews_current_choices_without_repreparing(catalog_page, control):
    page, fixtures = _done_with_catalog(catalog_page)
    initial_archive = fixtures.download["archive_id"]
    _browse(page)
    page.locator('[data-catalog-key="enchor:3"]').get_by_role("button", name="Add to ZIP", exact=True).click()
    playwright.expect(page.locator('[data-catalog-key="enchor:3"]')).to_contain_text("In ZIP selection")
    assert fixtures.catalog_selected == {1, 2, 3}
    page.locator("#catalogCloseButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(_done_count(page, "Selected before preparation")).to_contain_text("2 charts from catalog browsing")
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("5")
    if control == "review-zip":
        page.locator("#catalogQueueButton").click()
    page.locator("#prepareAgainButton" if control == "prepare-again" else "#catalogReviewZipButton").click()
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("6")
    playwright.expect(page.locator("#downloadCatalogSummary")).to_contain_text("3 charts")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    assert fixtures.download["archive_id"] == initial_archive
    assert fixtures.download_attempts == 1
    assert len(_posts(fixtures, "/api/download")) == 1


def test_older_catalog_response_cannot_overwrite_newer_journey_selection(catalog_page):
    page, fixtures = catalog_page()
    stale = fixtures.selected_response()
    fixtures.selected = {101}
    fixtures.selection_revision += 1
    newer = fixtures.selected_response()
    page.evaluate("receipt => document.dispatchEvent(new CustomEvent('riff:catalog-selection', {detail:receipt}))", newer)
    assert page.evaluate("window.RiffStudioAPI.context().charts") == 1
    page.evaluate("receipt => document.dispatchEvent(new CustomEvent('riff:catalog-selection', {detail:receipt}))", stale)
    context = page.evaluate("window.RiffStudioAPI.context()")
    assert context["selection_revision"] == newer["selection_revision"]
    assert context["charts"] == 1


def _installed_catalog(app, count=51):
    config = app.extensions["config"]
    jobs = app.extensions["jobs"]
    jobs.shutdown()
    rows = []
    files = {}
    for chart_id in range(1, count + 1):
        name = f"Offline song {chart_id:03}"
        package = valid_sng_bytes(name)
        chart = make_chart(name, "Offline artist", chart_id=chart_id, md5=hashlib.md5(package).hexdigest())
        chart.source = "enchor"
        rows.append(chart)
        files[chart.md5] = package
    config.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    write_snapshot(config.catalog_path, rows)
    jobs.catalog_browser.request()
    jobs.catalog_browser.prepare()
    source = FakeSource(files=files)
    source.name = "enchor"
    return config, rows, source


def _real_page(public_server, delivery_chromium, path):
    app, base_url, cookie_name, cookie = public_server
    context = delivery_chromium.new_context(reduced_motion="reduce", accept_downloads=True)
    context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
    page = context.new_page()
    errors, external = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: external.append(request.url)
            if urlsplit(request.url).hostname not in {None, "127.0.0.1", "localhost"} else None)
    # Stop and fail any accidental external provider request at the boundary.
    page.route("**/*", lambda route: route.abort() if urlsplit(route.request.url).hostname not in
               {None, "127.0.0.1", "localhost"} else route.continue_())
    page.goto(base_url + path, wait_until="networkidle")
    return context, page, errors, external


def test_real_partial_catalog_receipts_keep_review_zip_enabled_on_about(public_server, delivery_chromium):
    app, _base_url, _cookie_name, _cookie = public_server
    _installed_catalog(app)
    context, page, errors, external = _real_page(public_server, delivery_chromium, "/about")
    try:
        _browse(page)
        page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Add to ZIP", exact=True).click()
        playwright.expect(page.locator("#catalogReviewZipButton")).to_be_enabled()
        page.locator("#catalogNextPage").click()
        playwright.expect(page.locator('[data-catalog-key="enchor:51"]')).to_be_visible()
        # This GET carries revision/catalog counts but no complete selected arrays
        # or combined `charts`, exactly as the real Flask endpoint emits them.
        page.locator("#catalogQueueButton").click()
        playwright.expect(page.locator("#catalogReviewZipButton")).to_be_visible()
        playwright.expect(page.locator("#catalogReviewZipButton")).to_be_enabled()
        receipt = page.evaluate("fetch('/api/selection/history').then(response=>response.json())")
        assert receipt["charts"] == receipt["summary"]["catalog"]["charts"] == 1
        assert receipt["exact_generation_id"] is None
    finally:
        context.close()
    assert not errors, errors
    assert not external, external


def test_real_catalog_only_generation_zero_prepares_and_downloads_before_import(public_server, delivery_chromium, monkeypatch):
    app, base_url, _cookie_name, _cookie = public_server
    config, _rows, source = _installed_catalog(app, count=1)
    config.server.enforce_secret_key = True
    config.server.base_url = base_url
    monkeypatch.setattr("csv_to_clonehero.sources.build_sources", lambda _: [source])
    monkeypatch.setattr("csv_to_clonehero.server.web._client_for", lambda *_: SimpleNamespace(close=lambda: None))
    app.extensions["jobs"].start()
    context, page, errors, external = _real_page(public_server, delivery_chromium, "/")
    posts = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
    try:
        page.locator("#catalogBrowseButton").click()
        playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(1)
        page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Add to ZIP", exact=True).click()
        playwright.expect(page.locator("#catalogReviewZipButton")).to_be_enabled()
        selection_posts = [request for request in posts if urlsplit(request.url).path == "/api/catalog/selection"]
        assert len(selection_posts) == 1
        assert selection_posts[0].headers["x-riff-request"] == "1"
        assert selection_posts[0].headers["origin"] == base_url
        assert json.loads(selection_posts[0].post_data)["exact_generation_id"] == 0
        page.locator("#catalogQueueButton").click()
        page.locator("#catalogReviewZipButton").click()
        playwright.expect(page.locator("#stageDownload")).to_be_visible()
        playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("1")
        playwright.expect(page.locator("#downloadExactSummary")).to_have_text("0 charts for 0 imported songs")
        playwright.expect(page.locator("#downloadCatalogSummary")).to_contain_text("1 chart")
        page.locator("#prepareDownloadButton").click()
        playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=15000)
        playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("1")
        playwright.expect(_done_count(page, "Selected before preparation")).to_contain_text("1 chart from catalog browsing")
        with page.expect_download() as event:
            page.locator("#downloadLibraryLink").click()
        assert event.value.failure() is None
        with ZipFile(event.value.path()) as archive:
            assert any(name.endswith(".sng") for name in archive.namelist())
        assert source.downloads and len(source.downloads) == 1
        assert len([request for request in posts if urlsplit(request.url).path == "/api/download"]) == 1
        assert not [request for request in posts if urlsplit(request.url).path in {"/api/discover", "/api/expansion"}]
        db = Database(config.database_path)
        try:
            workspace = db.get_workspace(Database.workspace_key_for_user("browser-user"))["id"]
            assert not db.get_workspace_library_track_ids(workspace)
        finally:
            db.close()
    finally:
        context.close()
    assert not errors, errors
    assert not external, external
