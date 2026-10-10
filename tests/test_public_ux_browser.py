"""Offline Chromium coverage for the audited public recovery/selection journey.

The real templates, controls and scripts run against synthetic API DTOs. These
checks establish browser interaction behavior, not provider or hosted acceptance.
Backend ownership, generation and transaction authority have separate API tests.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_public_browser import _assert_no_overflow, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page
from tests.test_studio_browser import StudioFixtures
from tests.public_signal_fixtures import SignalCheckFixtures


pytestmark = pytest.mark.browser


def _requests(requests, path, method="GET"):
    return [request for request in requests
            if request.method == method and urlsplit(request.url).path == path]


def _delivery_probes(requests):
    return [request for request in requests
            if urlsplit(request.url).path.startswith("/api/download/")]


def _signed_fetches(fixtures):
    # Native download-attribute navigation can bypass page request events. The
    # synthetic WSGI boundary records it as well as routed API fetches.
    return [path for method, path, _body in fixtures.request_log
            if method == "GET" and path.startswith("/api/download/")]


class UXFixtures(StudioFixtures):
    """A known uncertain identity exercises the explicit inclusion checkbox."""

    def __init__(self, count=5, versions=2):
        super().__init__(count=count, versions=versions)

    def tracks(self):
        tracks = super().tracks()
        if len(tracks) > 1:
            for candidate in tracks[1]["candidates"]:
                candidate["result_tier"] = "POSSIBLE_MATCH"
        return tracks


class LimitedUndoFixtures(UXFixtures):
    """Exercise the server's bounded-history DTO without a giant test library."""

    undo_unavailable = False
    undo_reason = "This selection exceeded the restore history size limit; its earlier choices cannot be restored."

    def undo(self):
        receipt = super().undo()
        if self.undo_unavailable:
            receipt.update(available=False, operation_id=None, label="", reason=self.undo_reason)
        return receipt

    def _commit(self, previous, label):
        super()._commit(previous, label)
        self.operations.clear()
        self.undo_unavailable = True

    def respond(self, method, request_url, post_data=None):
        status, response = super().respond(method, request_url, post_data)
        if method == "GET" and urlsplit(request_url).path == "/api/selection/all-preview":
            response["undo_supported"] = False
        return status, response


class PreflightFixtures(SignalCheckFixtures):
    """Separate cached readiness from matching and from its durable check job."""

    def __init__(self, *, restored=None, usable_on_completion=True):
        super().__init__("add")
        self.readiness_job = None
        self.readiness_id = 90
        self.readiness_polls = 0
        self.readiness_complete_after = 2
        self.readiness_usable = False
        self.usable_on_completion = usable_on_completion
        if restored:
            self.readiness_job = self._job(restored)

    def _job(self, status):
        return {"id": self.readiness_id, "kind": "preflight", "status": status,
                "phase": "catalog_prepare" if status == "running" else status,
                "message": "Preparing the community chart catalog…" if status == "running" else "Chart-readiness check is queued.",
                "progress": {}}

    def journey(self):
        response = super().journey()
        response["preflight_job"] = deepcopy(self.readiness_job)
        return response

    def respond(self, method, request_url, post_data=None):
        path = urlsplit(request_url).path
        if path == "/api/preflight":
            self.request_log.append((method, path, json.loads(post_data or "{}")))
            if method == "POST":
                self.readiness_id += 1
                self.readiness_polls = 0
                self.readiness_job = self._job("queued")
            active = self.readiness_job and self.readiness_job["status"] in {"queued", "running"}
            result = {"usable": self.readiness_usable, "authoritative": True, "mode": "catalog",
                      "busy": bool(active), "status": self.readiness_job["status"] if active else "ready" if self.readiness_usable else "unavailable",
                      "catalog_available": self.readiness_usable,
                      "message": "Ready" if self.readiness_usable else "The community chart library is unavailable right now."}
            return (202 if method == "POST" else 200), {"preflight": result, "job": deepcopy(self.readiness_job)}
        if (method == "GET" and self.readiness_job is not None
                and path == f"/api/jobs/{self.readiness_id}"):
            self.request_log.append((method, path, {}))
            self.readiness_polls += 1
            status = "complete" if (self.readiness_complete_after is not None
                                    and self.readiness_polls >= self.readiness_complete_after) else "running"
            self.readiness_job = self._job(status)
            if status == "complete":
                self.readiness_usable = self.usable_on_completion
                self.readiness_job["result"] = {"preflight": {"usable": self.readiness_usable}}
            return 200, {"job": deepcopy(self.readiness_job)}
        return super().respond(method, request_url, post_data)


class ExpansionUndoFixtures(UXFixtures):
    def __init__(self):
        super().__init__(count=3, versions=2)
        self.restored_groups = deepcopy(self.expansion_catalog[:1])
        self.expansion_groups = deepcopy(self.expansion_catalog[1:])
        self.selected = {1000}
        self.expansion_selected = {401}
        self.selection_revision = 2
        self.operations = [{"operation_id": 1, "revision": 2, "generation_id": 1,
                            "label": "Replace artist expansion", "previous": ({1001}, {302})}]

    def respond(self, method, request_url, post_data=None):
        status, response = super().respond(method, request_url, post_data)
        if (method == "POST" and urlsplit(request_url).path == "/api/selection/undo"
                and status == 200):
            self.expansion_groups = deepcopy(self.restored_groups)
            response = self.selected_response()
        return status, response


def _delivery_fixtures():
    fixtures = UXFixtures(count=3, versions=1)
    fixtures.selected = {1000, 1001, 1002}
    fixtures.async_download = True
    return fixtures


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_poll_network_failure_keeps_stale_ledger_and_reconciles_same_job(delivery_page, width):
    fixtures = _delivery_fixtures()
    fixtures.download_complete_after_polls = None
    failures = {"polls": 0, "blocked": True}

    def route_api(route):
        if (route.request.method == "GET"
                and urlsplit(route.request.url).path == f"/api/jobs/{fixtures._download_job_id}"
                and fixtures.download_job is not None):
            failures["polls"] += 1
            if failures["blocked"] and failures["polls"] > 1:
                route.abort("failed")
                return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api,
                                   viewport={"width": width, "height": 900})
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadTitle")).to_have_text(
        "Preparation status unavailable", timeout=15000)
    playwright.expect(page.locator("#downloadSelectionCount")).to_have_text(
        "Awaiting confirmation")
    playwright.expect(page.locator("#downloadSelectionFreshness")).to_contain_text(
        re.compile(r"last (?:confirmed|displayed)", re.I))
    playwright.expect(page.locator("#downloadLastCheck")).to_contain_text(
        re.compile(r"last|stale", re.I))
    playwright.expect(page.locator("#prepareDownloadButton")).to_be_hidden()
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_visible()
    playwright.expect(page.locator("#downloadTechnicalError")).to_be_hidden()
    assert page.locator("#appError").is_hidden()
    assert len(_requests(requests, "/api/download", "POST")) == 1
    assert not _delivery_probes(requests)
    _assert_no_overflow(page)

    failures["blocked"] = False
    fixtures.download_complete_after_polls = fixtures.download_job_polls + 1
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_enabled(timeout=15000)
    page.locator("#refreshDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=15000)
    assert len(_requests(requests, "/api/download", "POST")) == 1
    assert not _delivery_probes(requests)


def test_lost_prepare_response_recovers_owner_job_without_second_prepare(delivery_page):
    fixtures = _delivery_fixtures()
    fixtures.download_complete_after_polls = None
    dropped = []

    def route_api(route):
        if (route.request.method == "POST"
                and urlsplit(route.request.url).path == "/api/download" and not dropped):
            fixtures.respond("POST", route.request.url, route.request.post_data)
            dropped.append(True)
            route.abort("failed")
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_visible()
    assert fixtures.download_job is not None
    assert len(_requests(requests, "/api/download", "POST")) == 1
    fixtures.download_complete_after_polls = 1
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_enabled()
    page.locator("#refreshDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=15000)
    assert len(_requests(requests, "/api/download", "POST")) == 1
    assert not _delivery_probes(requests)


def test_terminal_receipt_network_failure_stops_after_three_read_only_retries(delivery_page):
    fixtures = _delivery_fixtures()
    fixtures.download_complete_after_polls = 1
    receipt_failures = []

    def route_api(route):
        if (route.request.method == "GET" and urlsplit(route.request.url).path == "/api/journey"
                and fixtures.download_job is not None):
            receipt_failures.append(route.request.url)
            route.abort("failed")
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    # Speed only the already-scheduled retry timers. Every API response remains
    # real asynchronous browser I/O; the simulated job is terminal throughout.
    for _ in range(4):
        playwright.expect(page.locator("#downloadTitle")).to_have_text("Preparation status unavailable")
        page.clock.run_for(7000)
    playwright.expect(page.locator("#refreshDownloadButton")).to_be_enabled()
    assert len(receipt_failures) == 4
    assert len(_requests(requests, f"/api/jobs/{fixtures._download_job_id}")) == 4
    assert len(_requests(requests, "/api/download", "POST")) == 1
    assert not _delivery_probes(requests)
    page.clock.run_for(20000)
    assert len(receipt_failures) == 4


@pytest.mark.parametrize("width", [390, 1440])
def test_bulk_settings_preview_precede_action_and_uncertainty_is_explicit(delivery_page, width):
    fixtures = UXFixtures()
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-select-scope").select_option("library")
    playwright.expect(page.locator("#studio-selection-preview")).to_contain_text(
        re.compile(r"song|chart", re.I))
    assert not page.locator("#studio-include-uncertain").is_checked()
    assert page.evaluate("""() => ['studio-select-scope', 'studio-select-policy',
      'studio-include-uncertain', 'studio-selection-preview'].every(id =>
      Boolean(document.getElementById(id).compareDocumentPosition(
        document.getElementById('studio-select-all')) & Node.DOCUMENT_POSITION_FOLLOWING))""")
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("4")
    first = _requests(requests, "/api/selection/all", "POST")[0].post_data_json
    assert first["confirm_possible"] is False
    assert first["policy"] == "one_per_song"
    assert fixtures.selected == {1000, 1004, 1006, 1008}

    page.locator("#studio-include-uncertain").check()
    playwright.expect(page.locator("#studio-select-all")).to_be_enabled()
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("5")
    assert _requests(requests, "/api/selection/all", "POST")[1].post_data_json["confirm_possible"] is True
    previews = _requests(requests, "/api/selection/all-preview")
    assert any(parse_qs(urlsplit(request.url).query).get("include_possible") == ["false"] for request in previews)
    assert any(parse_qs(urlsplit(request.url).query).get("include_possible") == ["true"] for request in previews)
    _assert_no_overflow(page)


def test_operation_undo_restores_customized_versions_and_expansion(delivery_page):
    fixtures = UXFixtures()
    fixtures.selected = {1001, 1005}
    fixtures.expansion_groups = deepcopy(fixtures.expansion_catalog)
    fixtures.expansion_selected = {302}
    original = fixtures._snapshot()
    page, requests = delivery_page(fixtures)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectionUndoButton")).to_be_enabled()
    operation = fixtures.undo()
    page.locator("#selectionUndoButton").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("3")
    assert fixtures._snapshot() == original
    payload = _requests(requests, "/api/selection/undo", "POST")[0].post_data_json
    assert payload == {"operation_id": operation["operation_id"],
                       "selection_revision": operation["revision"]}
    playwright.expect(page.locator("#selectionUndoButton")).to_be_disabled()
    page.locator("#selectionUndoButton").dispatch_event("click")
    assert len(_requests(requests, "/api/selection/undo", "POST")) == 1


def test_size_limited_restore_history_warns_before_apply_and_keeps_selection_usable(delivery_page):
    fixtures = LimitedUndoFixtures()
    fixtures.selected = {1001}
    page, requests = delivery_page(fixtures)
    preview = page.locator("#studio-selection-preview")
    playwright.expect(preview).to_contain_text("too large to keep for undo")
    playwright.expect(preview).to_contain_text("without an undo receipt")
    playwright.expect(page.locator("#studio-select-all")).to_be_enabled()
    assert not _requests(requests, "/api/selection/all", "POST")
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectedCount")).to_have_text("4")
    playwright.expect(page.locator("#selectionUndoButton")).to_be_disabled()
    playwright.expect(page.locator("#selectionUndoStatus")).to_have_text(fixtures.undo_reason)
    playwright.expect(page.locator("#continueDownloadButton")).to_be_enabled()
    assert fixtures.selected == {1000, 1004, 1006, 1008}
    assert len(_requests(requests, "/api/selection/all", "POST")) == 1
    assert not _requests(requests, "/api/selection/undo", "POST")


def test_stale_undo_reconciles_without_overwriting_newer_server_choices(delivery_page):
    fixtures = UXFixtures()
    page, requests = delivery_page(fixtures)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#studio-select-all").click()
    playwright.expect(page.locator("#selectionUndoButton")).to_be_enabled()
    fixtures.selected.add(1001)
    fixtures.selection_revision += 1
    current = fixtures._snapshot()
    page.locator("#selectionUndoButton").click()
    playwright.expect(page.locator("#selectionUndoButton")).to_be_disabled()
    assert fixtures._snapshot() == current
    assert len(_requests(requests, "/api/selection/undo", "POST")) == 1
    playwright.expect(page.locator("#selectedCount")).to_have_text(str(len(fixtures.selected)))


def test_expansion_operation_undo_reloads_restored_options_and_current_totals(delivery_page):
    fixtures = ExpansionUndoFixtures()
    page, requests = delivery_page(fixtures)
    assert page.locator('[data-candidate="401"]').count() == 1
    assert page.locator('[data-candidate="302"]').count() == 0
    expansion_reads = len(_requests(requests, "/api/expansion/results"))
    workspace_reads = len(_requests(requests, "/api/workspace"))
    history_reads = len(_requests(requests, "/api/selection/history"))
    page.locator("#selectionUndoButton").click()
    playwright.expect(page.locator('[data-candidate="302"]')).to_be_visible()
    playwright.expect(page.locator('[data-candidate="401"]')).to_have_count(0)
    playwright.expect(page.locator("#selectedCount")).to_have_text("2")
    playwright.expect(page.locator("#expansionSelectedCount")).to_have_text("1")
    assert fixtures.selected == {1001}
    assert fixtures.expansion_selected == {302}
    assert len(_requests(requests, "/api/expansion/results")) > expansion_reads
    assert len(_requests(requests, "/api/workspace")) > workspace_reads
    assert len(_requests(requests, "/api/selection/history")) > history_reads
    assert len(_requests(requests, "/api/selection/undo", "POST")) == 1
    playwright.expect(page.locator("#selectionUndoButton")).to_be_disabled()


def test_done_is_zip_ready_then_request_receipt_without_consuming_probe(delivery_page):
    fixtures = _delivery_fixtures()
    fixtures.download_complete_after_polls = 1
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible(timeout=15000)
    playwright.expect(page.locator("#doneTitle")).to_have_text("Your ZIP is ready.")
    assert not _delivery_probes(requests)
    token = fixtures.download["token"]
    assert token in fixtures._downloads
    with page.expect_download() as event:
        page.locator("#downloadLibraryLink").click()
    assert event.value.failure() is None
    playwright.expect(page.locator("#doneTitle")).to_have_text("Download requested.")
    playwright.expect(page.locator("#downloadHandoff")).to_contain_text(
        re.compile(r"cannot confirm saving|cannot confirm.*saved", re.I))
    assert token not in fixtures._downloads
    assert len(_signed_fetches(fixtures)) == 1
    page.locator("#refreshLinkButton").click()
    playwright.expect(page.locator("#downloadLibraryLink")).not_to_have_attribute("href", re.compile(".+"))
    assert len(_signed_fetches(fixtures)) == 1
    assert len(_requests(requests, "/api/download", "POST")) == 1


def test_async_readiness_shows_queue_and_catalog_preparation_before_one_discovery(delivery_page):
    fixtures = PreflightFixtures()
    fixtures.readiness_complete_after = None
    page, requests = delivery_page(fixtures)
    page.locator("#findChartsButton").click()
    playwright.expect(page.locator("#progressMessage")).to_have_text("The chart-library check is queued.")
    assert len(_requests(requests, "/api/preflight", "POST")) == 1
    assert not _requests(requests, "/api/jobs", "POST")
    playwright.expect(page.locator("#progressMessage")).to_contain_text("Preparing the community chart catalog", timeout=15000)
    playwright.expect(page.locator("#progressCount")).to_contain_text("song matching has not started")
    assert not page.locator("#progressDisc").get_attribute("aria-valuenow")
    assert not _requests(requests, "/api/jobs", "POST")
    fixtures.readiness_complete_after = fixtures.readiness_polls + 1
    playwright.expect(page.locator("#cancelJobButton")).to_be_visible(timeout=15000)
    assert len(_requests(requests, "/api/jobs", "POST")) == 1


def test_completed_unusable_readiness_keeps_saved_choices_and_never_starts_matching(delivery_page):
    fixtures = PreflightFixtures(usable_on_completion=False)
    fixtures.readiness_complete_after = 1
    page, requests = delivery_page(fixtures)
    page.locator("#findChartsButton").click()
    playwright.expect(page.locator("#searchRecovery")).to_be_visible(timeout=15000)
    playwright.expect(page.locator("#retrySearchButton")).to_be_enabled()
    playwright.expect(page.locator("#searchRecoveryMessage")).to_contain_text("unavailable")
    assert fixtures.spotify_scope == {"include_liked": True, "playlist_ids": ["playlist-road"]}
    assert len(_requests(requests, "/api/preflight", "POST")) == 1
    assert not _requests(requests, "/api/jobs", "POST")


def test_restored_active_readiness_only_reads_until_explicit_search_request(delivery_page):
    fixtures = PreflightFixtures(restored="running")
    fixtures.readiness_complete_after = 1
    page, requests = delivery_page(fixtures)
    playwright.expect(page.locator("#searchRecovery")).to_be_visible(timeout=15000)
    assert _requests(requests, f"/api/jobs/{fixtures.readiness_id}")
    assert not [request for request in requests if request.method == "POST"]
    page.locator("#retrySearchButton").click()
    playwright.expect(page.locator("#cancelJobButton")).to_be_visible(timeout=15000)
    assert len(_requests(requests, "/api/jobs", "POST")) == 1
    assert not _requests(requests, "/api/preflight", "POST")


def test_prior_terminal_unusable_readiness_does_not_prevent_an_explicit_new_check(delivery_page):
    fixtures = PreflightFixtures(restored="complete")
    fixtures.readiness_complete_after = 1
    prior_id = fixtures.readiness_id
    page, requests = delivery_page(fixtures)
    assert not [request for request in requests if request.method == "POST"]
    page.locator("#findChartsButton").click()
    playwright.expect(page.locator("#cancelJobButton")).to_be_visible(timeout=15000)
    assert len(_requests(requests, "/api/preflight", "POST")) == 1
    assert fixtures.readiness_id != prior_id
    assert _requests(requests, f"/api/jobs/{fixtures.readiness_id}")
    assert len(_requests(requests, "/api/jobs", "POST")) == 1


def test_find_network_loss_rechecks_same_job_without_restarting_search(delivery_page):
    fixtures = SignalCheckFixtures("find")
    polls = []
    blocked = [True]

    def route_api(route):
        if route.request.method == "GET" and urlsplit(route.request.url).path == "/api/jobs/41":
            polls.append(route.request.url)
            if blocked[0] and len(polls) > 1:
                route.abort("failed")
                return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    playwright.expect(page.locator("#retryJobButton")).to_be_visible(timeout=15000)
    playwright.expect(page.locator("#searchRecoveryMessage")).to_contain_text("may still be running")
    playwright.expect(page.locator("#retrySearchButton")).to_be_hidden()
    playwright.expect(page.locator("#searchTechnicalError")).to_be_hidden()
    assert page.locator("#appError").is_hidden()
    assert page.locator("body").get_attribute("data-riff-search-state") == "blocked"
    assert not _requests(requests, "/api/jobs", "POST")
    failed_poll_count = len(polls)
    blocked[0] = False
    page.locator("#retryJobButton").click()
    playwright.expect(page.locator("#cancelJobButton")).to_be_visible()
    playwright.expect(page.locator("#searchRecovery")).to_be_hidden()
    assert len(polls) > failed_poll_count
    assert not _requests(requests, "/api/jobs", "POST")
