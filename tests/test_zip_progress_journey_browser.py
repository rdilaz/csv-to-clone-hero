"""Real job/status HTTP contracts with an offline, held archive builder."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
import zipfile

import pytest

from tests.test_public_browser import _assert_no_overflow, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium
from tests.test_server_web import _seed_async_download_choice
from tests.test_catalog_browser import CatalogFixtures, _play, catalog_page

pytestmark = pytest.mark.browser


def test_playing_audio_remains_mounted_and_advances_between_workflow_steps(catalog_page):
    fixtures = CatalogFixtures()
    fixtures.scenario = "add"
    fixtures.selected.clear()
    page, _fixtures = catalog_page(fixtures)
    page.locator("#catalogBrowseButton").click()
    _play(page)
    page.locator("#catalogCloseButton").click()
    page.evaluate("window.workflowOriginalAudio = document.querySelector('#catalogAudio')")
    before = page.locator("#catalogAudio").evaluate("audio => audio.currentTime")
    for stage in ("find", "confirm", "add"):
        page.locator(f'[data-navigate-step="{stage}"]').click()
        assert page.evaluate("window.workflowOriginalAudio === document.querySelector('#catalogAudio')")
        assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    page.wait_for_function("before => document.querySelector('#catalogAudio').currentTime > before", arg=before)


def test_real_job_status_contract_survives_navigation_and_finishes_in_background(
    public_server, delivery_chromium,
):
    app, base_url, cookie_name, cookie = public_server
    config = app.extensions["config"]
    client = app.test_client()
    client.set_cookie(cookie_name, cookie)
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace)
    started, release = threading.Event(), threading.Event()
    builds = []

    def builder(_config, _db, workspace_id, _sources, staging, *, progress,
                progress_details, cancellation_event):
        builds.append(workspace_id)
        progress_details({"phase": "validating", "completed": 1, "total": 1,
                          "current_item": "Offline chart.sng", "work_advanced": True})
        progress_details({"phase": "packaging", "completed": 0, "total": 1,
                          "files_completed": 0, "files_total": 1,
                          "bytes_completed": 1024 ** 2, "bytes_total": 4 * 1024 ** 2,
                          "bytes_kind": "source", "unit": "files",
                          "current_item": "Offline chart.sng", "work_advanced": True})
        started.set()
        if not release.wait(30):
            raise RuntimeError("Offline test did not release its archive builder")
        archive = staging / "offline-progress.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Offline chart.sng", b"offline browser fixture")
        return archive, {"success": True, "published": 1, "failed": 0,
                         "remaining": 0, "selected": 1, "bytes": archive.stat().st_size}

    app.extensions["jobs"].archive_builder = builder
    context = delivery_chromium.new_context(viewport={"width": 1440, "height": 1000})
    context.add_cookies([{"name": cookie_name, "value": cookie, "url": base_url}])
    page = context.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request))
    try:
        page.goto(base_url + "/#step=download", wait_until="networkidle")
        playwright.expect(page.locator("#stageDownload")).to_be_visible()
        page.locator("#prepareDownloadButton").click()
        assert started.wait(5)
        playwright.expect(page.locator("#downloadWorkProgress")).to_contain_text("1 MiB / 4 MiB")
        playwright.expect(page.locator("#downloadCurrentItem")).to_contain_text("Offline chart.sng")
        job = client.get("/api/delivery/status").get_json()["download_job"]
        assert job["progress"]["bytes_kind"] == "source"
        assert job["progress"]["last_work_at"] > 0
        job_id = job["id"]

        target = os.environ.get("RIFF_PROGRESS_EVIDENCE", "")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(target) / "real-job-packaging-1440.png"))

        page.locator('[data-navigate-step="add"]').click()
        playwright.expect(page.locator("#stageAdd")).to_be_visible()
        playwright.expect(page.locator("#preparationStrip")).to_contain_text(f"Preparation {job_id}")
        _assert_no_overflow(page)
        page.locator('[data-navigate-step="confirm"]').click()
        playwright.expect(page.locator("#stageConfirm")).to_be_visible()
        page.go_back()
        playwright.expect(page.locator("#stageAdd")).to_be_visible()
        page.go_forward()
        playwright.expect(page.locator("#stageConfirm")).to_be_visible()
        page.reload(wait_until="networkidle")
        playwright.expect(page.locator("#stageConfirm")).to_be_visible()
        playwright.expect(page.locator("#preparationStrip")).to_contain_text(f"Preparation {job_id}")

        prepare_posts = lambda: [request for request in requests
                                 if request.method == "POST" and urlsplit(request.url).path == "/api/download"]
        assert len(prepare_posts()) == len(builds) == 1
        assert client.get(f"/api/jobs/{job_id}").get_json()["job"]["status"] == "running"
        release.set()
        playwright.expect(page.locator("#preparationStripTitle")).to_contain_text("ZIP ready", timeout=10000)
        playwright.expect(page.locator("#stageConfirm")).to_be_visible()
        page.locator("#returnToPreparationButton").click()
        playwright.expect(page.locator("#stageDone")).to_be_visible()
        assert len(prepare_posts()) == len(builds) == 1
        assert not [request for request in requests
                    if urlsplit(request.url).path.startswith("/api/download/")]
        assert not errors
    finally:
        release.set()
        context.close()
