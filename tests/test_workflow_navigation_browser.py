"""Navigation and truthful preparation feedback on the real page, offline API fixtures."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import playwright, public_server, _assert_no_overflow
from tests.test_public_delivery_browser import delivery_chromium, delivery_page, _posts, NOW
from tests.test_catalog_browser import CatalogFixtures, catalog_page, _browse, _play

pytestmark = pytest.mark.browser


class ProgressFixtures(SignalCheckFixtures):
    def __init__(self):
        super().__init__("confirm")
        self.async_download = True
        self.download_complete_after_polls = None
        self.phase = "packaging"
        self.progress = {"selection_count": 5909, "completed": 2, "total": 5856,
                         "files_completed": 2, "files_total": 5856,
                         "bytes_completed": 1024 * 1024, "bytes_total": 1024 ** 3,
                         "bytes_kind": "source", "current_item": "Example Artist / Chart 3.sng",
                         "last_work_at": NOW.timestamp() - 120}
        self.heartbeat_at = NOW.timestamp() - 2

    def poll_download_job(self):
        status, result = super().poll_download_job()
        if self.download_job and self.download_job["status"] in {"queued", "running"}:
            self.download_job.update(phase=self.phase, progress=deepcopy(self.progress), heartbeat_at=self.heartbeat_at)
            result["job"] = deepcopy(self.download_job)
        return status, result


def _prepare(page, fixtures):
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadCurrentItem")).to_contain_text("Chart 3.sng")
    return fixtures.download_job["id"]


@pytest.mark.parametrize("width,height", [(320, 800), (390, 844), (1440, 1000)])
def test_steps_history_reload_and_job_identity_are_preserved(delivery_page, tmp_path, width, height):
    fixtures = ProgressFixtures()
    page, requests = delivery_page(fixtures, viewport={"width": width, "height": height})
    job_id = _prepare(page, fixtures)
    selected = set(fixtures.selected)
    revision = fixtures.selection_revision
    page.evaluate("window.stepResetCount = 0; document.addEventListener('riff:studio-reset', () => window.stepResetCount++)")
    playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("2 / 5,856 files packed")
    playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("1 MiB / 1 GiB source data packed")
    playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("over 90 seconds")
    playwright.expect(page.locator("#downloadActivityTimes")).to_contain_text("Last work recorded")
    playwright.expect(page.locator("#downloadLastCheck")).to_contain_text("does not mean work advanced")
    _assert_no_overflow(page)
    page.locator('[data-navigate-step="add"]').click()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    playwright.expect(page.locator("#preparationStripTitle")).to_contain_text(f"Preparation {job_id}")
    playwright.expect(page.locator("#includeLiked")).to_be_disabled()
    page.locator("#refreshSpotifyButton").click()
    playwright.expect(page.locator("#includeLiked")).to_be_disabled()
    page.locator("#soundcloudForm").dispatch_event("submit")
    page.locator("#appleMusicForm").dispatch_event("submit")
    page.locator('[data-navigate-step="confirm"]').click()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    playwright.expect(page.locator("#selectAllButton")).to_be_disabled()
    page.locator("#selectAllButton").dispatch_event("click")
    assert page.evaluate("window.RiffStudioAPI.begin()") is False
    page.go_back()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    page.go_forward()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert page.evaluate("window.stepResetCount") == 0
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    playwright.expect(page.locator("#preparationStripTitle")).to_contain_text(f"Preparation {job_id}")
    page.locator("#returnToPreparationButton").click()
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(page.locator("#downloadCurrentItem")).to_contain_text("Chart 3.sng")
    assert fixtures.download_job["id"] == job_id
    assert fixtures.selected == selected and fixtures.selection_revision == revision
    assert len(_posts(requests, "/api/download")) == 1
    assert len(_posts(requests)) == 1
    _assert_no_overflow(page)
    destination = Path(__file__).resolve().parents[2] / "progress-evidence" / "navigation"
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / f"packaging-{width}.png"), full_page=False)


def test_legacy_packaging_does_not_reuse_validation_count_as_zip_percent(delivery_page):
    fixtures = ProgressFixtures()
    fixtures.progress = {"selection_count": 5909, "completed": 5856, "total": 5856}
    page, requests = delivery_page(fixtures)
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("not reported by this worker")
    assert page.locator("#downloadWorkProgress progress").count() == 0
    assert "100%" not in page.locator("#downloadWorkProgress").inner_text()
    assert len(_posts(requests)) == 1


def test_finalizing_and_failed_packaging_remain_truthful(delivery_page):
    fixtures = ProgressFixtures()
    page, requests = delivery_page(fixtures)
    _prepare(page, fixtures)
    fixtures.phase = "finalizing"
    fixtures.progress.update(bytes_completed=fixtures.progress["bytes_total"])
    page.locator("#checkActiveJobButton").click()
    playwright.expect(page.locator("#downloadTitle")).to_have_text("Finalizing ZIP archive")
    assert page.locator("#downloadWorkProgress progress").count() == 0
    playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("not ready until its link is confirmed")
    fixtures.download_job.update(status="failed", phase="packaging", error="Not enough staging space")
    page.locator("#checkActiveJobButton").click()
    playwright.expect(page.locator("#preparationStripTitle")).to_contain_text("Preparation failed")
    playwright.expect(page.locator("#preparationStripDetail")).to_contain_text("Not enough staging space")
    playwright.expect(page.locator("#downloadTechnicalError")).to_contain_text("Not enough staging space")
    assert len(_posts(requests)) == 1


def test_browser_back_forward_after_finished_job_does_not_discard_forward_history(delivery_page):
    fixtures = ProgressFixtures()
    page, requests = delivery_page(fixtures)
    _prepare(page, fixtures)
    fixtures.download_complete_after_polls = fixtures.download_job_polls + 1
    page.locator("#checkActiveJobButton").click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    page.locator('[data-navigate-step="add"]').click()
    page.locator('[data-navigate-step="confirm"]').click()
    page.go_back()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    page.go_back()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    page.go_forward()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    page.go_forward()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert len(_posts(requests)) == 1


def test_status_check_cannot_steal_a_new_step_navigation(delivery_page):
    fixtures = SignalCheckFixtures("confirm")
    held = []
    hold = False

    def route_api(route):
        if hold and urlsplit(route.request.url).path == "/api/delivery/status":
            held.append(route)
            return
        fixtures.fulfill(route)

    page, requests = delivery_page(fixtures, route_api=route_api)
    hold = True
    page.locator('[data-navigate-step="download"]').click()
    page.wait_for_function("document.querySelector('#downloadTitle').textContent.includes('Checking')")
    page.locator('[data-navigate-step="add"]').click()
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    assert len(held) == 1
    fixtures.fulfill(held.pop())
    playwright.expect(page.locator("#stageAdd")).to_be_visible()
    assert _posts(requests) == []


def test_new_archive_finishing_while_browsing_cannot_offer_old_archive(delivery_page):
    fixtures = ProgressFixtures()
    _status, old = fixtures.prepare_download()
    old_token = old["download"]["token"]
    page, requests = delivery_page(fixtures)
    page.locator('[data-navigate-step="confirm"]').click()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    _prepare(page, fixtures)
    page.locator('[data-navigate-step="confirm"]').click()
    playwright.expect(page.locator('[data-navigate-step="done"]')).to_be_disabled()
    fixtures.download_complete_after_polls = fixtures.download_job_polls + 1
    page.locator("#checkActiveJobButton").click()
    playwright.expect(page.locator("#preparationStripTitle")).to_contain_text("ZIP ready")
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    page.locator('[data-navigate-step="done"]').click()
    playwright.expect(page.locator("#stageDone")).to_be_visible()
    new_token = fixtures.download["token"]
    assert new_token != old_token
    playwright.expect(page.locator("#downloadLibraryLink")).to_have_attribute("href", f"/api/download/{new_token}")
    assert not any(urlsplit(request.url).path.startswith("/api/download/") for request in requests)
    assert len(_posts(requests)) == 1


def test_native_player_survives_actual_step_clicks_and_history_during_packaging(catalog_page):
    fixtures = CatalogFixtures()
    fixtures.async_download = True
    fixtures.download_complete_after_polls = None
    page, fixtures = catalog_page(fixtures)
    _browse(page)
    _play(page)
    page.evaluate("window.originalCatalogAudio = document.querySelector('#catalogAudio')")
    page.locator("#catalogCloseButton").click()
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("leave")
    job_id = fixtures.download_job["id"]
    for step, section in [("add", "stageAdd"), ("find", "stageFind"), ("confirm", "stageConfirm")]:
        page.locator(f'[data-navigate-step="{step}"]').click()
        playwright.expect(page.locator(f"#{section}")).to_be_visible()
        assert page.evaluate("window.originalCatalogAudio === document.querySelector('#catalogAudio')")
        assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
        playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 1")
    before = page.locator("#catalogAudio").evaluate("audio => audio.currentTime")
    page.go_back()
    playwright.expect(page.locator("#stageFind")).to_be_visible()
    page.go_forward()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.currentTime") >= before
    page.locator("#catalogBrowseButton").click()
    page.locator("#catalogCloseButton").click()
    page.locator("#returnToPreparationButton").click()
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    assert fixtures.download_job["id"] == job_id
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert len([method for method, path, _ in fixtures.request_log if method == "POST"]) == 1
