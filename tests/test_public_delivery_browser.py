"""Offline Chromium regressions for the public Download and Done transitions.

The real Flask page, CSS, and JavaScript run unchanged. Only API traffic is
synthetic, including held and job based ZIP preparation responses.
"""

from __future__ import annotations

import re
import socket
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import (
    _assert_in_viewport,
    _assert_no_overflow,
    _capture,
    playwright,
    public_server,
)


pytestmark = pytest.mark.browser
NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def delivery_chromium():
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def delivery_page(public_server, delivery_chromium):
    _app, base_url, cookie_name, cookie = public_server
    contexts = []
    errors = []
    used_fixtures = []

    def open_page(fixtures, *, route_api=None, viewport=None):
        fixtures.clock = lambda: NOW.timestamp()
        if fixtures not in used_fixtures:
            fixtures.attach_downloads(_app)
            used_fixtures.append(fixtures)
        viewport = viewport or {"width": 1440, "height": 900}
        context = delivery_chromium.new_context(
            viewport=viewport,
            has_touch=viewport["width"] < 720,
            reduced_motion="reduce",
            accept_downloads=True,
        )
        contexts.append(context)
        context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        page.on("pageerror", lambda error: errors.append(str(error)))
        requests = []
        page.on("request", lambda request: requests.append(request))
        page.route("**/api/**", route_api or fixtures.fulfill)
        page.goto(base_url + "/", wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        return page, requests

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()
        assert not errors, errors
        assert all(not fixtures.unexpected_requests for fixtures in used_fixtures)


def _posts(requests, path=None):
    return [
        request for request in requests
        if request.method == "POST"
        and (path is None or urlsplit(request.url).path == path)
    ]


def _done_count(page, label):
    return page.locator("#doneCounts > div").filter(
        has=page.get_by_text(label, exact=True)
    ).locator("dd")


def _assert_download_counts(page, *, exact=3, songs=1, expansion=0):
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator('[data-step="download"]')).to_have_attribute(
        "aria-current", "step"
    )
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text(
        str(exact + expansion)
    )
    playwright.expect(page.locator("#downloadExactSummary")).to_have_text(
        f"{exact} {'chart' if exact == 1 else 'charts'} for "
        f"{songs} imported {'song' if songs == 1 else 'songs'}"
    )
    playwright.expect(page.locator("#downloadExpansionSummary")).to_have_text(
        re.compile(rf"^{expansion} {'chart' if expansion == 1 else 'charts'}\b")
    )


def _assert_no_delivery_progress(page):
    stage = page.locator("#stageDownload")
    playwright.expect(stage).to_be_visible()
    assert stage.locator('[role="progressbar"], progress, [aria-valuenow]').count() == 0
    assert page.get_by_role("progressbar").count() == 0
    assert not re.search(r"\d+(?:\.\d+)?\s*%", stage.inner_text())
    assert not re.search(r"\b\d+\s+(?:seconds?|minutes?|hours?)\b", stage.inner_text())


def test_async_download_job_survives_reload_without_duplicate_export(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = None
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadBusyIndicator")).to_be_visible()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    page.locator("#prepareDownloadButton").dispatch_event("click")
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("leave")
    assert len(_posts(requests, "/api/download")) == 1
    assert fixtures.download_attempts == 0
    assert fixtures.download_job["kind"] == "download"
    assert fixtures.start_download_job()[1]["created"] is False
    assert fixtures.download_attempts == 0

    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadBusyIndicator")).to_be_visible()
    assert len(_posts(requests, "/api/download")) == 1
    fixtures.download_complete_after_polls = fixtures.download_job_polls + 1
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("3")
    assert fixtures.download_attempts == 1
    assert len(_posts(requests, "/api/download")) == 1
    assert "token" not in fixtures.download_job["result"]
    assert "path" not in fixtures.download_job["result"]
    with page.expect_download() as event:
        page.locator("#downloadLibraryLink").click()
    assert event.value.failure() is None
    with ZipFile(event.value.path()) as archive:
        assert len(archive.namelist()) == 3


def test_async_download_failure_reconciles_before_retry(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    fixtures.fail_next_download = "Chart provider unavailable."
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Preparation did not finish")
    playwright.expect(page.locator("#downloadTechnicalError")).to_have_text("Chart provider unavailable.")
    playwright.expect(page.locator("#downloadTechnicalError")).to_be_hidden()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    assert fixtures.download_job["status"] == "failed"
    assert len(_posts(requests, "/api/download")) == 1
    page.locator("#refreshDownloadButton").click()
    _assert_download_counts(page)
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    assert len(_posts(requests, "/api/download")) == 2
    assert fixtures.download_attempts == 2


def test_async_download_restart_does_not_claim_stale_link(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    page, requests = delivery_page(fixtures)
    status, prior = fixtures.prepare_download()
    assert status == 200
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    latest = fixtures.download
    assert latest["token"] != prior["download"]["token"]
    # Clock equality must not let the prior ZIP satisfy the completed job.
    assert latest["created_at"] == fixtures.download_job["result"]["archive_created_at"]
    assert latest["created_at"] == fixtures._downloads[prior["download"]["token"]]["created_at"]
    assert latest["archive_id"] != prior["download"]["archive_id"]
    assert fixtures.download_job["result"]["archive_id"] == latest["archive_id"]
    with fixtures._download_lock:
        fixtures._downloads.pop(latest["token"])
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("single-use ZIP link is unavailable")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    playwright.expect(page.locator("#previousDownloadButton")).to_be_visible()
    assert page.locator("#downloadLibraryLink").is_hidden()
    assert len(_posts(requests, "/api/download")) == 1
    assert not any(request.method == "GET" and urlsplit(request.url).path.startswith("/api/download/") for request in requests)


def test_async_download_restore_without_archive_identity_fails_closed(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = 1
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    fixtures.download_job["result"].pop("archive_id")

    page.reload(wait_until="networkidle")

    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("single-use ZIP link is unavailable")
    assert page.locator("#downloadLibraryLink").is_hidden()
    assert len(_posts(requests, "/api/download")) == 1
    assert not any(request.method == "GET" and urlsplit(request.url).path.startswith("/api/download/") for request in requests)


def test_async_download_can_be_cancelled_without_losing_choices(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = None
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    cancel = page.locator("#cancelDownloadButton")
    playwright.expect(cancel).to_be_visible()
    cancel.click()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("Preparation cancelled")
    playwright.expect(cancel).to_be_hidden()
    assert fixtures.download_attempts == 0
    assert len(_posts(requests, "/api/download")) == 1
    page.locator("#refreshDownloadButton").click()
    _assert_download_counts(page)
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    fixtures.download_complete_after_polls = 1
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    assert len(_posts(requests, "/api/download")) == 2
    assert fixtures.download_attempts == 1


def test_async_download_poll_error_requires_refresh_before_retry(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.async_download = True
    fixtures.download_complete_after_polls = None
    fail_poll = {"once": True}

    def route_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/jobs/77" and route.request.method == "GET" and fail_poll["once"]:
            fail_poll["once"] = False
            route.fulfill(status=503, json={"error": "Status temporarily unavailable."})
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("outcome is unknown")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    assert len(_posts(requests, "/api/download")) == 1
    fixtures.download_complete_after_polls = 1
    page.locator("#refreshDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=10000)
    assert len(_posts(requests, "/api/download")) == 1


@pytest.mark.parametrize("songs", [1, 2], ids=["one-song", "multiple-songs"])
@pytest.mark.parametrize("expansion_mode", ["disabled", "empty", "multi-chart"])
def test_delivery_counts_survive_confirm_done_and_reprepare(
    public_server, delivery_page, songs, expansion_mode
):
    fixtures = SignalCheckFixtures("confirm")
    if songs == 2:
        fixtures.selected.add(201)
    expansion = 0
    if expansion_mode == "disabled":
        public_server[0].extensions["config"].server.enable_expansion = False
    elif expansion_mode == "multi-chart":
        fixtures.expansion_groups = [
            fixtures.expansion_group("Paramore", "That's What You Get", [301, 302, 303]),
            fixtures.expansion_group("Radiohead", "Bodysnatchers", [401]),
        ]
        fixtures.expansion_selected = {301, 302, 303}
        expansion = 3
    exact = 3 if songs == 1 else 4
    total = exact + expansion
    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#selectedCount")).to_have_text(str(total))
    playwright.expect(page.locator("#selectedSongCount")).to_have_text(str(songs))
    playwright.expect(page.locator("#exactSelectedCount")).to_have_text(str(exact))
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text(str(expansion))

    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page, exact=exact, songs=songs, expansion=expansion)
    playwright.expect(page.locator("#downloadTitle")).to_be_focused()
    assert _posts(requests) == []
    page.locator("#deliveryDetails > summary").click()
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(total)
    playwright.expect(page.locator(".delivery-song")).to_have_count(
        songs + int(expansion > 0)
    )
    assert "Bodysnatchers" not in page.locator("#deliverySelectionList").inner_text()
    assert page.locator("#deliverySelectionList [data-selection]").count() == 0
    _assert_no_delivery_progress(page)

    for attempt in (1, 2):
        page.locator("#prepareDownloadButton").click()
        playwright.expect(page.locator("#stageDone")).to_be_visible()
        playwright.expect(page.locator('[data-step="done"]')).to_have_attribute(
            "aria-current", "step"
        )
        playwright.expect(page.locator("#doneTitle")).to_be_focused()
        playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text(str(total))
        playwright.expect(_done_count(page, "Selections represented")).to_have_text(
            f"{total} of {total} processed"
        )
        playwright.expect(_done_count(page, "Selected before preparation")).to_have_text(
            f"{exact} charts from your music / {songs} imported "
            f"{'song' if songs == 1 else 'songs'}; {expansion} charts from artist expansion; 0 charts from catalog browsing"
        )
        assert len(_posts(requests, "/api/download")) == attempt
        assert not [request for request in requests if "/api/download/" in request.url]
        if attempt == 1:
            page.locator("#prepareAgainButton").click()
            _assert_download_counts(page, exact=exact, songs=songs, expansion=expansion)
            assert len(_posts(requests)) == 1
    assert [urlsplit(request.url).path for request in _posts(requests)] == [
        "/api/download", "/api/download"
    ]
    if expansion_mode == "disabled":
        assert page.locator("#expansionPanel").count() == 0
        assert not [request for request in requests if "/api/expansion" in request.url]
    else:
        expansion_reads = [
            request for request in requests if "/api/expansion/results" in request.url
        ]
        assert expansion_reads
        assert all("selected" not in parse_qs(urlsplit(request.url).query) for request in expansion_reads)


def test_delivery_distinguishes_represented_selections_from_actual_zip_files(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_duplicates = {102: 101}
    fixtures.export_already_present = {103}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("3 of 3 processed")
    playwright.expect(_done_count(page, "Duplicate selections collapsed")).to_have_text("1")
    playwright.expect(page.locator("#doneSummary")).to_contain_text("2 validated chart artifacts")
    playwright.expect(page.locator("#doneWarnings")).to_have_text(
        "No failed or remaining selections reported."
    )
    playwright.expect(page.locator("#downloadHandoff")).to_contain_text(
        "not confirmation that your browser saved the ZIP"
    )
    assert fixtures.last_export_summary["already_present"] == 1
    assert not [request for request in requests if "/api/download/" in request.url]
    with page.expect_download() as event:
        page.locator("#downloadLibraryLink").click()
    download = event.value
    assert download.suggested_filename == "riff-library.zip"
    assert download.failure() is None
    with ZipFile(download.path()) as archive:
        assert sorted(archive.namelist()) == ["synthetic/chart-101.sng", "synthetic/chart-103.sng"]
    playwright.expect(page.locator("#downloadHandoff")).to_contain_text("Riff cannot confirm saving")
    assert _posts(requests, "/api/selection") == []


@pytest.mark.parametrize("omitted", ["summary", "published", "ready", "selected", "failed", "remaining"])
def test_delivery_missing_report_fields_never_borrow_other_counts(delivery_page, omitted):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_duplicates = {102: 101}

    def route_api(route):
        if route.request.method == "POST" and urlsplit(route.request.url).path == "/api/download":
            status, payload = fixtures.respond("POST", route.request.url, route.request.post_data)
            # A missing projection stays unknown, even with current selections and a token.
            if omitted == "summary":
                payload.pop("summary")
            else:
                payload["summary"].pop(omitted)
            route.fulfill(status=status, json=payload)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    if omitted in {"summary", "published"}:
        assert _done_count(page, "Packaged artifacts").count() == 0
        playwright.expect(page.locator("#doneSummary")).to_contain_text("artifact count")
        assert "validated chart" not in page.locator("#doneSummary").inner_text()
    else:
        playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
    if omitted in {"summary", "ready"}:
        assert _done_count(page, "Selections represented").count() == 0
        assert _done_count(page, "Duplicate selections collapsed").count() == 0
    else:
        playwright.expect(_done_count(page, "Selections represented")).to_have_text(
            "3" if omitted == "selected" else "3 of 3 processed"
        )
    if omitted in {"summary", "failed", "remaining"}:
        playwright.expect(page.locator("#doneWarnings")).to_contain_text(
            "Failure and remaining-selection counts are unavailable."
        )
        assert "no failed or remaining" not in page.locator("#doneWarnings").inner_text()
    assert len(_posts(requests)) == 1


def test_delivery_authoritative_zero_is_not_replaced_by_saved_selection(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_summary_overrides = {"published": 0, "ready": 0, "selected": 0}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(_done_count(page, "Last packaged artifacts")).to_have_text("0")
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("0 of 0 processed")
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_hidden()
    assert page.locator("#downloadLibraryLink").get_attribute("href") is None
    playwright.expect(page.locator("#doneTitle")).to_have_text("This download is not available.")
    playwright.expect(page.locator("#doneWarnings")).to_contain_text("server processed 0")
    assert len(_posts(requests)) == 1


def test_delivery_revalidation_difference_is_not_claimed_as_delivered(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_duplicates = {102: 101}
    fixtures.export_summary_overrides = {"selected": 2, "ready": 2}
    page, _requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("2 of 2 processed")
    playwright.expect(page.locator("#doneWarnings")).to_contain_text(
        "The tray had 3 selected chart entries; the server processed 2"
    )
    playwright.expect(page.locator("#doneWarnings")).to_contain_text(
        "The difference is not claimed as delivered."
    )
    playwright.expect(page.locator("#doneTitle")).to_have_text("Your ZIP is ready, with exceptions.")


def test_delivery_partial_report_keeps_failure_categories_inclusive(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.selected.add(201)
    fixtures.expansion_groups = [fixtures.expansion_group("Paramore", "Decode", [301, 302, 303])]
    fixtures.expansion_selected = {301, 302, 303}
    fixtures.export_failures = {102: "Provider temporarily unavailable"}
    fixtures.export_invalid = {103: "No playable chart content <not-markup>"}
    fixtures.export_remaining = {201}
    fixtures.export_duplicates = {302: 301, 303: 301}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page, exact=4, songs=2, expansion=3)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#doneTitle")).to_have_text("Your ZIP is ready, with exceptions.")
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("4 of 7 processed")
    playwright.expect(_done_count(page, "Duplicate selections collapsed")).to_have_text("2")
    playwright.expect(page.locator("#doneWarnings")).to_contain_text(
        "2 failed selections; 1 remaining selections were not delivered."
    )
    page.locator('#doneWarnings details > summary').click()
    playwright.expect(page.locator("#doneWarnings")).to_contain_text(
        "categories are included in the failure total, not additional failures"
    )
    playwright.expect(page.locator("#doneWarnings")).to_contain_text(
        "Preparing again rechecks all current selections, not just failed files"
    )
    playwright.expect(page.locator("#doneWarnings li")).to_have_count(2)
    playwright.expect(page.locator("#doneWarnings li").last).to_contain_text("<not-markup>")
    assert page.locator("#doneWarnings not-markup").count() == 0
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_visible()
    assert len(_posts(requests)) == 1


@pytest.mark.parametrize("restore_mode", ["receipt", "fresh-tab", "different-token"])
def test_delivery_done_restore_uses_only_a_matching_optional_receipt(delivery_page, restore_mode):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_duplicates = {102: 101}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
    metadata = fixtures.journey()["download"]
    assert {"token", "url", "expires_at"} <= metadata.keys()
    assert "summary" not in metadata
    assert "summary" not in fixtures.journey()

    if restore_mode == "fresh-tab":
        page, requests = delivery_page(fixtures)
    else:
        if restore_mode == "different-token":
            status, response = fixtures.prepare_download()
            assert status == 200 and response["download"]["token"] != metadata["token"]
        page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_visible()
    if restore_mode == "receipt":
        playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
        playwright.expect(_done_count(page, "Selections represented")).to_have_text("3 of 3 processed")
    else:
        playwright.expect(page.locator("#doneCounts")).to_be_hidden()
        playwright.expect(page.locator("#doneSummary")).to_contain_text(
            "Riff cannot confirm how many of your choices it contains"
        )
        playwright.expect(page.locator("#doneWarnings")).to_contain_text("counts are unavailable")
    assert len(_posts(requests)) == (0 if restore_mode == "fresh-tab" else 1)
    assert not [request for request in requests if "/api/download/" in request.url]
    page.locator("#prepareAgainButton").click()
    _assert_download_counts(page)
    assert _posts(requests, "/api/selection") == []


@pytest.mark.parametrize(
    "outcome",
    ["unconfirmed", "completed", "earlier-zip", "completed-after-earlier", "older-zip-resurfaced"],
)
def test_delivery_pending_post_reload_reconciles_without_automatic_duplicate(delivery_page, outcome):
    fixtures = SignalCheckFixtures("confirm")
    pending = []
    earlier_archives = 2 if outcome == "older-zip-resurfaced" else int(
        outcome in {"earlier-zip", "completed-after-earlier"}
    )
    hold = {"value": not earlier_archives}

    def route_api(route):
        if hold["value"] and route.request.method == "POST" and urlsplit(route.request.url).path == "/api/download":
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    archives = []
    for archive_index in range(earlier_archives):
        fixtures.clock = lambda index=archive_index: NOW.timestamp() + index * 10
        page.locator("#prepareDownloadButton").click()
        playwright.expect(page.locator("#stageDone")).to_be_visible()
        archives.append(fixtures.download)
        page.locator("#prepareAgainButton").click()
        _assert_download_counts(page)
    hold["value"] = True
    fixtures.clock = lambda: NOW.timestamp() + 20
    page.clock.set_fixed_time(datetime.fromtimestamp(NOW.timestamp() + 20, timezone.utc))
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#prepareDownloadButton")).to_have_attribute("aria-busy", "true")
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("Starting preparation")
    assert len(pending) == 1
    _assert_no_delivery_progress(page)
    before = len(_posts(requests))
    assert page.evaluate("JSON.parse(sessionStorage.getItem('riff:delivery:1')).pending") is True
    if outcome in {"completed", "completed-after-earlier"}:
        # The server can finish after the document loses the POST response.
        fixtures.clock = lambda: NOW.timestamp() + 30
        status, _response = fixtures.prepare_download()
        assert status == 200
    elif outcome == "older-zip-resurfaced":
        # Another tab consumes the latest token, exposing an older unconsumed ZIP.
        latest = fixtures.download
        status, _body = fixtures.respond("GET", latest["url"])
        assert status == 200 and latest["token"] == archives[1]["token"]
        assert fixtures.download["token"] == archives[0]["token"]
        assert fixtures.download["created_at"] < latest["created_at"]
    page.reload(wait_until="networkidle")
    if outcome in {"completed", "completed-after-earlier"}:
        playwright.expect(page.locator("#stageDone")).to_be_visible()
        playwright.expect(page.locator("#doneCounts")).to_be_hidden()
        playwright.expect(page.locator("#doneWarnings")).to_contain_text("counts are unavailable")
    else:
        _assert_download_counts(page)
        playwright.expect(page.locator("#downloadStatus")).to_contain_text("No new ZIP is confirmed yet")
        playwright.expect(page.locator("#prepareDownloadButton")).to_have_text("Retry preparation")
        page.locator("#refreshDownloadButton").click()
        playwright.expect(page.locator("#refreshDownloadButton")).to_be_enabled()
        _assert_download_counts(page)
        assert page.evaluate("JSON.parse(sessionStorage.getItem('riff:delivery:1')).pending") is True
        if outcome in {"earlier-zip", "older-zip-resurfaced"}:
            playwright.expect(page.locator("#previousDownloadButton")).to_be_visible()
        if outcome == "earlier-zip":
            page.locator("#previousDownloadButton").click()
            playwright.expect(page.locator("#stageDone")).to_be_visible()
        elif outcome == "older-zip-resurfaced":
            # Only C, created after B and the pending request, resolves this build.
            fixtures.clock = lambda: NOW.timestamp() + 30
            status, completed = fixtures.prepare_download()
            assert status == 200
            assert completed["download"]["token"] not in {archive["token"] for archive in archives}
            page.locator("#refreshDownloadButton").click()
            playwright.expect(page.locator("#stageDone")).to_be_visible()
            playwright.expect(page.locator("#doneCounts")).to_be_hidden()
            playwright.expect(page.locator("#downloadLibraryLink")).to_have_attribute(
                "href", f"/api/download/{completed['download']['token']}"
            )
            assert page.evaluate("JSON.parse(sessionStorage.getItem('riff:delivery:1')).pending") is False
    assert len(_posts(requests)) == before
    assert len(pending) == 1
    assert not [request for request in requests if "/api/jobs" in request.url]
    assert _posts(requests, "/api/selection") == []


def test_delivery_failed_post_requires_reconciliation_before_explicit_retry(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.fail_next_download = "Could not create your download right now; try again in a moment."
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadTechnicalError")).to_have_text(
        "Could not create your download right now; try again in a moment."
    )
    playwright.expect(page.locator("#downloadTechnicalError")).to_be_hidden()
    playwright.expect(page.locator("#appError")).to_be_hidden()
    assert fixtures.last_export_summary is None
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("Awaiting confirmation")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Preparation request unconfirmed")
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("Check status before retrying")
    assert len(_posts(requests)) == 1

    fixtures.selected.remove(103)
    page.locator("#refreshDownloadButton").click()
    _assert_download_counts(page, exact=2)
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    assert len(_posts(requests)) == 1
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("2")
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("2 of 2 processed")
    assert len(_posts(requests, "/api/download")) == 2
    assert _posts(requests, "/api/selection") == []


@pytest.mark.parametrize("unknown", ["exact", "expansion", "read-error"])
def test_delivery_recount_unknown_never_uses_confirm_cache(delivery_page, unknown):
    fixtures = SignalCheckFixtures("confirm")
    invalidate = {"value": False}

    def route_api(route):
        path = urlsplit(route.request.url).path
        if invalidate["value"] and path == "/api/delivery/status":
            status, payload = fixtures.respond(route.request.method, route.request.url)
            if unknown == "read-error":
                route.fulfill(status=503, json={"error": "Selection report temporarily unavailable."})
                return
            payload["selection_summary"]["expansion" if unknown == "expansion" else "library"].pop("charts")
            route.fulfill(status=status, json=payload)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    invalidate["value"] = True
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#appError")).to_be_hidden()
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text("Awaiting confirmation")
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Preparation status unavailable")
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_visible()
    assert _posts(requests) == []
    invalidate["value"] = False
    page.locator("#refreshDownloadButton").click()
    _assert_download_counts(page)
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_enabled()
    assert _posts(requests) == []


def test_delivery_recount_authoritative_zero_blocks_preparation(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    fixtures.selected.clear()
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page, exact=0, songs=0)
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("No charts are selected")
    page.locator("#backToChartsButton").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("0")
    assert _posts(requests) == []


def test_delivery_stale_confirm_read_cannot_overwrite_reconciled_counts(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    pending = []
    hold = {"value": False}

    def route_api(route):
        parsed = urlsplit(route.request.url)
        if hold["value"] and parsed.path == "/api/workspace" and "selected" not in parse_qs(parsed.query):
            status, payload = fixtures.respond(route.request.method, route.request.url)
            pending.append((route, status, deepcopy(payload)))
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    hold["value"] = True
    page.locator('[data-review-filter="strong"]').click()
    playwright.expect(page.locator("#reviewCards")).to_have_attribute("aria-busy", "true")
    assert len(pending) == 1
    fixtures.selected = {101}
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page, exact=1)
    route, status, stale = pending.pop()
    assert stale["summary"]["selected_charts"] == 3
    with page.expect_response(route.request.url):
        route.fulfill(status=status, json=stale)
    # A subsequent real action observes the settled JS state, not just old DOM text.
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Selected before preparation")).to_contain_text(
        "1 chart from your music / 1 imported song"
    )
    playwright.expect(_done_count(page, "Selections represented")).to_have_text("1 of 1 processed")
    assert len(_posts(requests)) == 1


def test_delivery_expansion_details_page_song_groups_and_retry_without_changing_choices(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.expansion_groups = [
        fixtures.expansion_group("Paramore", f"A song {index:02}", [800 + index])
        for index in range(25)
    ] + [fixtures.expansion_group("Paramore", "Z selected song", [301, 302])]
    fixtures.expansion_selected = {301, 302}
    pending = []

    def route_api(route):
        parsed = urlsplit(route.request.url)
        if parsed.path == "/api/expansion/results" and parse_qs(parsed.query).get("offset") == ["25"]:
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#selectedCount")).to_have_text("5")
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page, expansion=2)
    page.locator("#deliveryDetails > summary").click()
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(3)
    more = page.locator("#deliverySelectionMore")
    playwright.expect(more).to_be_visible()
    more.click()
    playwright.expect(more).to_be_disabled()
    more.dispatch_event("click")
    assert len(pending) == 1
    pending.pop().fulfill(status=503, json={"error": "Chart detail page temporarily unavailable."})
    playwright.expect(page.locator("#deliverySelectionStatus")).to_contain_text(
        "Chart detail page temporarily unavailable."
    )
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(3)
    playwright.expect(more).to_be_enabled()
    more.click()
    playwright.expect(more).to_be_disabled()
    assert len(pending) == 1
    fixtures.fulfill(pending.pop())
    playwright.expect(page.locator("#deliverySelectionList li")).to_have_count(5)
    playwright.expect(page.locator(".delivery-song")).to_have_count(2)
    playwright.expect(more).to_be_hidden()
    playwright.expect(page.locator("#deliverySelectionStatus")).to_have_text(
        "All currently selected chart details are shown."
    )
    assert "A song" not in page.locator("#deliverySelectionList").inner_text()
    assert all(
        "selected" not in parse_qs(urlsplit(request.url).query)
        for request in requests if "/api/expansion/results" in request.url
    )
    _assert_download_counts(page, expansion=2)
    assert _posts(requests) == []


def test_delivery_stale_link_reconciliation_cannot_replace_reprepare_stage(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    pending = []
    hold = {"value": False}

    def route_api(route):
        if hold["value"] and urlsplit(route.request.url).path == "/api/delivery/status":
            hold["value"] = False
            status, payload = fixtures.respond("GET", route.request.url)
            pending.append((route, status, payload))
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    with page.expect_download() as event:
        page.locator("#downloadLibraryLink").click()
    assert event.value.failure() is None
    hold["value"] = True
    page.locator("#refreshLinkButton").click()
    playwright.expect(page.locator("#refreshLinkButton")).to_be_disabled()
    assert len(pending) == 1
    page.locator("#prepareAgainButton").click()
    _assert_download_counts(page)
    route, status, stale = pending.pop()
    assert stale["download"] is None
    with page.expect_response(route.request.url):
        route.fulfill(status=status, json=stale)
    playwright.expect(page.locator("#refreshLinkButton")).to_be_enabled()
    _assert_download_counts(page)
    playwright.expect(page.locator("#stageDone")).to_be_hidden()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(_done_count(page, "Packaged artifacts")).to_have_text("3")
    assert len(_posts(requests)) == 2


def test_delivery_metadata_only_restore_handles_disappearing_token_without_receipt(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    preparing_page, _preparing_requests = delivery_page(fixtures)
    preparing_page.locator("#continueDownloadButton").click()
    preparing_page.locator("#prepareDownloadButton").click()
    playwright.expect(preparing_page.locator("#stageDone")).to_be_visible()
    metadata = fixtures.download

    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(page.locator("#doneCounts")).to_be_hidden()
    assert page.evaluate("JSON.parse(sessionStorage.getItem('riff:delivery:1')).receipt ?? null") is None
    status, _body = fixtures.respond("GET", metadata["url"])
    assert status == 200 and fixtures.download is None

    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    playwright.expect(page.locator("#appError")).to_be_hidden()
    playwright.expect(page.locator("#doneCounts")).to_be_hidden()
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_hidden()
    assert page.locator("#downloadLibraryLink").get_attribute("href") is None
    playwright.expect(page.locator("#doneTitle")).to_contain_text(
        re.compile(r"not available|unavailable|expired", re.IGNORECASE)
    )
    page.locator("#refreshLinkButton").click()
    playwright.expect(page.locator("#refreshLinkButton")).to_be_enabled()
    playwright.expect(page.locator("#appError")).to_be_hidden()
    playwright.expect(page.locator("#downloadLibraryLink")).to_be_hidden()
    playwright.expect(page.locator("#prepareAgainButton")).to_be_enabled()
    assert page.evaluate("JSON.parse(sessionStorage.getItem('riff:delivery:1')).receipt ?? null") is None
    assert _posts(requests) == []
    assert fixtures.download_attempts == 1


def test_delivery_aborted_native_get_can_retry_same_token_after_authoritative_check(
    public_server, delivery_page, monkeypatch
):
    fixtures = SignalCheckFixtures("confirm")
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    metadata = fixtures.download
    link = page.locator("#downloadLibraryLink")
    app = public_server[0]
    original = app.wsgi_app
    disconnect = {"value": True}
    aborted_gets = []

    def abort_before_token_handler(environ, start_response):
        if disconnect["value"] and environ.get("REQUEST_METHOD") == "GET" and environ.get("PATH_INFO") == metadata["url"]:
            # Native downloads bypass both Playwright routing and offline mode.
            # Close the test HTTP connection before the token can be consumed.
            aborted_gets.append(environ["PATH_INFO"])
            start_response("503 Service Unavailable", [("Content-Length", "0")])
            environ["werkzeug.socket"].shutdown(socket.SHUT_RDWR)
            return []
        return original(environ, start_response)

    monkeypatch.setattr(app, "wsgi_app", abort_before_token_handler)
    with page.expect_download(timeout=10000) as event:
        link.click()
    assert event.value.failure() is not None
    disconnect["value"] = False
    assert aborted_gets
    assert fixtures.download["token"] == metadata["token"]
    assert not [
        path for method, path, _body in fixtures.request_log
        if method == "GET" and path == metadata["url"]
    ]
    playwright.expect(link).to_have_attribute("aria-disabled", "true")
    page.wait_for_function("!document.querySelector('#downloadLibraryLink').hasAttribute('href')")
    page.locator("#refreshLinkButton").click()
    playwright.expect(link).to_have_attribute("href", metadata["url"])
    playwright.expect(link).to_have_attribute("aria-disabled", "false")
    playwright.expect(link).to_be_focused()
    assert fixtures.download_attempts == 1
    assert len(_posts(requests)) == 1
    with page.expect_download() as retry:
        page.keyboard.press("Enter")
    assert retry.value.failure() is None
    assert retry.value.url.endswith(metadata["url"])
    assert fixtures.download is None
    assert len([
        path for method, path, _body in fixtures.request_log
        if method == "GET" and path == metadata["url"]
    ]) == 1
    assert len(_posts(requests)) == 1


def test_delivery_duplicate_prepare_and_native_link_actions_are_guarded(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    pending = []

    def route_api(route):
        if route.request.method == "POST" and urlsplit(route.request.url).path == "/api/download":
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_disabled()
    # Dispatch even through disabled controls to exercise the JS reentry guard.
    page.locator("#prepareDownloadButton").dispatch_event("click")
    page.locator("#backToChartsButton").dispatch_event("click")
    playwright.expect(page.locator("#backToChartsButton")).to_be_disabled()
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_disabled()
    _assert_no_delivery_progress(page)
    assert len(pending) == 1
    assert len(_posts(requests)) == 1
    fixtures.fulfill(pending.pop())
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    link = page.locator("#downloadLibraryLink")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    with page.expect_download() as event:
        link.evaluate("element => { element.click(); element.click(); }")
    assert event.value.failure() is None
    playwright.expect(link).to_have_attribute("aria-disabled", "true")
    assert link.get_attribute("href") is None
    link.dispatch_event("click")
    playwright.expect(page.locator("#downloadHandoff")).to_contain_text("Download requested")
    page.locator("#prepareAgainButton").click()
    _assert_download_counts(page)
    assert len(downloads) == 1
    assert len([
        path for method, path, _body in fixtures.request_log
        if method == "GET" and path.startswith("/api/download/")
    ]) == 1
    assert len(_posts(requests)) == 1
    assert _posts(requests, "/api/selection") == []


def test_delivery_keyboard_choices_and_stage_focus_do_not_write_implicit_selections(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.expansion_groups = [fixtures.expansion_group("Paramore", "Decode", [301, 302])]
    fixtures.expansion_selected = {301, 302}
    page, requests = delivery_page(fixtures)
    concern = page.locator('details[data-disclosure-key$=":102:concerns"]')
    concern.locator("summary").focus()
    page.keyboard.press("Enter")
    playwright.expect(concern).to_have_attribute("open", "")
    page.locator('[data-candidate="102"]').focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#selectedCount")).to_have_text("4")
    playwright.expect(page.locator('[data-candidate="102"][data-selection="select"]')).to_be_focused()
    playwright.expect(concern).to_have_attribute("open", "")
    page.locator('[data-candidate="302"]').focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    playwright.expect(page.locator('[data-candidate="302"][data-expansion-selection="select"]')).to_be_focused()

    page.locator("#continueDownloadButton").focus()
    page.keyboard.press("Enter")
    _assert_download_counts(page, exact=2, expansion=1)
    playwright.expect(page.locator("#downloadTitle")).to_be_focused()
    page.locator("#deliveryDetails > summary").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#deliveryDetails > summary")).to_be_focused()
    page.locator("#prepareDownloadButton").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#doneTitle")).to_be_focused()
    page.locator("#prepareAgainButton").focus()
    page.keyboard.press("Enter")
    _assert_download_counts(page, exact=2, expansion=1)
    playwright.expect(page.locator("#downloadTitle")).to_be_focused()
    page.locator("#backToChartsButton").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#confirmTitle")).to_be_focused()
    playwright.expect(concern).to_have_attribute("open", "")
    choices = [request.post_data_json for request in _posts(requests, "/api/selection")]
    assert [(choice["candidate_id"], choice["action"]) for choice in choices] == [
        (102, "clear"), (302, "clear")
    ]
    assert choices[1]["kind"] == "expansion"
    assert fixtures.selected == {101, 103}
    assert fixtures.expansion_selected == {301}
    assert _posts(requests, "/api/selection/bulk") == []


@pytest.mark.parametrize("failed_read", ["artists", "results", "both"])
def test_delivery_ambiguous_expansion_post_gates_choices_until_authoritative_refresh(
    delivery_page, failed_read
):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.expansion_groups = deepcopy(fixtures.expansion_catalog)
    fixtures.expansion_selected = {301, 302}
    fixtures.expansion_artists_selected = {"paramore"}
    fixtures.expansion_complete_after_polls = None
    lost = {"value": False}
    fail_reads = {"value": True}
    observed_polls = []

    def route_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/expansion" and route.request.method == "POST":
            status, _response = fixtures.respond("POST", route.request.url, route.request.post_data)
            assert status == 202
            lost["value"] = True
            route.abort("failed")
            return
        if lost["value"] and fail_reads["value"] and path in {
            "/api/expansion/artists", "/api/expansion/results"
        } and (failed_read == "both" or path.endswith("/" + failed_read)):
            route.fulfill(status=503, json={"error": "Expansion read temporarily unavailable."})
            return
        if path == "/api/jobs/42":
            observed_polls.append(route.request)
            # Verify the gate at the request boundary, before authoritative completion.
            playwright.expect(page.locator("#continueDownloadButton")).to_be_disabled()
            playwright.expect(page.locator('[data-candidate="101"]')).to_be_disabled()
            fixtures.expansion_complete_after_polls = 1
            fixtures.fulfill(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#selectedCount")).to_have_text("5")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#startExpansionButton").click()
    playwright.expect(page.locator("#retryExpansionButton")).to_be_visible()
    playwright.expect(page.locator("#appError")).to_be_visible()
    playwright.expect(page.locator("#selectedCount")).to_have_text("Unknown")
    playwright.expect(page.locator("#exactSelectedCount")).to_have_text("3")
    controls = page.locator(
        "[data-selection], [data-expansion-selection], #selectBestButton, "
        "#selectAllButton, #selectMaximumButton, #clearSelectionButton, "
        "#startExpansionButton, #continueDownloadButton"
    )
    for control in controls.all():
        playwright.expect(control).to_be_disabled()
    page.locator('[data-candidate="101"]').dispatch_event("click")
    page.locator("#continueDownloadButton").dispatch_event("click")
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert _posts(requests, "/api/selection") == []
    assert fixtures.selected == {101, 102, 103}
    assert fixtures.expansion_selected == {301, 302}

    fail_reads["value"] = False
    page.locator("#retryExpansionButton").click()
    playwright.expect(page.locator("#retryExpansionButton")).to_be_hidden()
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    assert len(observed_polls) == 1
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("0")
    playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()
    playwright.expect(page.locator('[data-candidate="101"]')).to_be_enabled()
    assert fixtures.selected == {101, 102, 103}
    assert fixtures.expansion_selected == set()
    assert len(_posts(requests, "/api/expansion")) == 1
    assert _posts(requests, "/api/selection") == []
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    assert len(_posts(requests)) == 1


@pytest.mark.parametrize("kind", ["exact", "expansion"])
def test_delivery_expansion_start_recomputes_after_selection_mutation(delivery_page, kind):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.expansion_groups = deepcopy(fixtures.expansion_catalog)
    fixtures.expansion_selected = {301}
    fixtures.expansion_artists_selected = {"paramore"}
    fixtures.expansion_complete_after_polls = 1
    pending = []

    def route_api(route):
        if route.request.method == "POST" and urlsplit(route.request.url).path == "/api/selection":
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    artist = page.locator('#expansionChoices input[value="paramore"]')
    start = page.locator("#startExpansionButton")
    playwright.expect(artist).to_be_checked()
    playwright.expect(start).to_be_enabled()
    candidate = 101 if kind == "exact" else 301
    page.locator(f'[data-candidate="{candidate}"]').click()
    playwright.expect(start).to_be_disabled()
    assert len(pending) == 1
    fixtures.fulfill(pending.pop())
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    playwright.expect(artist).to_be_checked()
    playwright.expect(start).to_be_enabled()
    artist.uncheck()
    playwright.expect(start).to_be_disabled()
    artist.check()
    playwright.expect(start).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    start.focus()
    page.keyboard.press("Enter")
    expected_exact = 2 if kind == "exact" else 3
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("0")
    playwright.expect(page.locator("#selectedCount")).to_have_text(str(expected_exact))
    playwright.expect(start).to_be_enabled()
    playwright.expect(start).to_be_focused()
    selection_posts = _posts(requests, "/api/selection")
    assert len(selection_posts) == 1
    assert selection_posts[0].post_data_json["candidate_id"] == candidate
    assert selection_posts[0].post_data_json["action"] == "clear"
    expansion_posts = _posts(requests, "/api/expansion")
    assert len(expansion_posts) == 1
    assert expansion_posts[0].post_data_json == {
        "artists": ["paramore"], "all_artists": False, "coverage_mode": "recommended",
    }


@pytest.mark.parametrize(
    "viewport", [{"width": 390, "height": 844}, {"width": 1440, "height": 900}],
    ids=["mobile", "desktop"],
)
def test_delivery_restored_nonbest_expansion_clear_keeps_focus_visible(delivery_page, viewport):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.expansion_groups = [fixtures.expansion_group("Paramore", "Decode", [301, 302])]
    fixtures.expansion_selected = {302}
    page, requests = delivery_page(fixtures, viewport=viewport)
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#selectedCount")).to_have_text("4")
    selected = page.locator('[data-candidate="302"][data-expansion-selection="clear"]')
    playwright.expect(selected).to_be_visible()
    selected.focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("0")
    replacement = page.locator('[data-candidate="302"][data-expansion-selection="select"]')
    playwright.expect(replacement).to_be_visible()
    playwright.expect(replacement).to_be_focused()
    # A saved alternate is promoted to the primary row on restore. If it is
    # inside a disclosure, clearing must leave that disclosure open for focus.
    assert replacement.evaluate("element => !element.closest('details') || element.closest('details').open") is True
    _assert_no_overflow(page)
    assert fixtures.selected == {101, 102, 103}
    assert fixtures.expansion_selected == set()
    selection_posts = _posts(requests, "/api/selection")
    assert len(selection_posts) == 1
    assert selection_posts[0].post_data_json["candidate_id"] == 302
    assert selection_posts[0].post_data_json["kind"] == "expansion"
    assert selection_posts[0].post_data_json["action"] == "clear"


@pytest.mark.parametrize("disclosure", ["selection", "errors"])
def test_delivery_disclosures_restore_after_reload(delivery_page, disclosure):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_failures = {102: "Temporary provider failure"}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    if disclosure == "errors":
        page.locator("#prepareDownloadButton").click()
        playwright.expect(page.locator("#stageDone")).to_be_visible()
        selector = '#doneWarnings details[data-disclosure-key="delivery:errors"]'
    else:
        selector = "#deliveryDetails"
    details = page.locator(selector)
    details.locator("summary").focus()
    page.keyboard.press("Enter")
    playwright.expect(details).to_have_attribute("open", "")
    page.wait_for_function(
        "key => JSON.parse(sessionStorage.getItem('riff:disclosures:1') || '{}')[key] === true",
        arg=f"delivery:{disclosure}",
    )
    page.reload(wait_until="networkidle")
    playwright.expect(details).to_have_attribute("open", "")
    details.locator("summary").focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "key => JSON.parse(sessionStorage.getItem('riff:disclosures:1') || '{}')[key] === false",
        arg=f"delivery:{disclosure}",
    )
    page.reload(wait_until="networkidle")
    assert details.get_attribute("open") is None
    assert len(_posts(requests)) == int(disclosure == "errors")


def test_delivery_keyboard_refresh_restores_focus_after_disabling_control(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    pending = []
    hold = {"value": False}

    def route_api(route):
        if hold["value"] and urlsplit(route.request.url).path == "/api/delivery/status":
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, _requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    refresh = page.locator("#refreshDownloadButton")
    refresh.focus()
    hold["value"] = True
    page.keyboard.press("Enter")
    playwright.expect(refresh).to_be_disabled()
    assert len(pending) == 1
    fixtures.fulfill(pending.pop())
    playwright.expect(refresh).to_be_enabled()
    playwright.expect(refresh).to_be_focused()


@pytest.mark.parametrize(
    "viewport",
    [{"width": 320, "height": 800}, {"width": 390, "height": 844}, {"width": 1440, "height": 900}],
    ids=["320x800", "390x844", "1440x900"],
)
def test_delivery_download_and_done_reflow_with_touch_controls(delivery_page, viewport):
    fixtures = SignalCheckFixtures("confirm")
    fixtures.export_failures = {102: "Provider unavailable: " + "long-artifact-reference-" * 15}
    pending = []

    def route_api(route):
        if route.request.method == "POST" and urlsplit(route.request.url).path == "/api/download":
            pending.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api, viewport=viewport)
    page.locator("#continueDownloadButton").click()
    _assert_download_counts(page)
    page.locator("#deliveryDetails > summary").click()
    for selector in ["#prepareDownloadButton", "#refreshDownloadButton", "#backToChartsButton", "#deliveryDetails > summary"]:
        control = page.locator(selector)
        control.evaluate("element => element.scrollIntoView({block: 'center', behavior: 'instant'})")
        _assert_in_viewport(page, selector)
        box = control.bounding_box()
        assert box and box["height"] >= 44 and box["width"] >= 44, (selector, box)
        assert control.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    if viewport["width"] < 720:
        total = page.locator(".delivery-total").bounding_box()
        counts = page.locator("#stageDownload .delivery-counts").bounding_box()
        assert total and counts and total["y"] + total["height"] <= counts["y"]
    _assert_no_overflow(page)
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadBusyIndicator")).to_be_visible()
    _assert_no_delivery_progress(page)
    _assert_no_overflow(page)
    _capture(page, f"delivery-pending-{viewport['width']}x{viewport['height']}.png")
    assert len(pending) == 1
    fixtures.fulfill(pending.pop())
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    page.locator("#doneWarnings details > summary").click()
    _assert_no_overflow(page)
    for selector in ["#downloadLibraryLink", "#prepareAgainButton", "#doneReviewButton", "#doneWarnings details > summary"]:
        control = page.locator(selector)
        control.evaluate("element => element.scrollIntoView({block: 'center', behavior: 'instant'})")
        _assert_in_viewport(page, selector)
        box = control.bounding_box()
        assert box and box["height"] >= 44 and box["width"] >= 44, (selector, box)
        assert control.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    _capture(page, f"delivery-done-{viewport['width']}x{viewport['height']}.png")
    page.evaluate("document.documentElement.style.fontSize = '200%'; window.dispatchEvent(new Event('resize'))")
    _assert_no_overflow(page)
    page.locator("#prepareAgainButton").click()
    _assert_download_counts(page)
    _assert_no_overflow(page)
    assert len(_posts(requests)) == 1
