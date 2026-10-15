"""Visible Listening deck behavior with real native media and offline fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_catalog_browser import CatalogFixtures, _play, _posts, catalog_page
from tests.test_public_browser import _assert_no_overflow, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium

pytestmark = pytest.mark.browser


def _add_fixtures():
    fixtures = CatalogFixtures()
    fixtures.scenario = "add"
    fixtures.selected.clear()
    return fixtures


def _capture(page, name, tmp_path):
    target = Path(os.environ.get("RIFF_TURNTABLE_EVIDENCE", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / name), full_page=True)
    page.locator("#stageAdd [data-riff-listening-panel]").screenshot(path=str(target / name.replace(".png", "-panel.png")))


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_idle_deck_is_useful_reachable_and_fits_the_add_music_screen(catalog_page, width, tmp_path):
    page, fixtures = catalog_page(_add_fixtures(), width=width)
    panel = page.locator("#stageAdd [data-riff-listening-panel]")
    panel.scroll_into_view_if_needed()
    playwright.expect(panel).to_be_visible()
    playwright.expect(panel).to_have_attribute("data-playback-state", "idle")
    playwright.expect(panel.locator("[data-listening-state]")).to_have_text("STANDBY")
    playwright.expect(panel.locator("[data-listening-note]")).to_contain_text("ZIP choices stay separate")
    canvas = panel.locator("canvas")
    playwright.expect(canvas).to_have_attribute("data-renderer", "webgl", timeout=10000)
    playwright.expect(canvas).to_have_attribute("data-record-spinning", "false")
    action = panel.locator("[data-listening-action]")
    box = action.bounding_box()
    assert box and box["height"] >= 44
    assert action.evaluate("node => {const b=node.getBoundingClientRect();const hit=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);return hit===node||node.contains(hit);}")
    _assert_no_overflow(page)
    _capture(page, f"listening-deck-add-{width}.png", tmp_path)
    action.focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#catalogResults .catalog-card")).to_have_count(50)
    page.keyboard.press("Escape")
    playwright.expect(action).to_be_focused()
    assert not page.locator("#catalogAudio").get_attribute("src")
    assert not _posts(fixtures)


def test_standby_animates_scene_without_rotating_a_silent_record(catalog_page):
    page, _ = catalog_page(_add_fixtures())
    canvas = page.locator("#stageAdd canvas")
    canvas.scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-renderer", "webgl", timeout=10000)
    page.emulate_media(reduced_motion="no-preference")
    playwright.expect(canvas).to_have_attribute("data-animating", "true")
    before = canvas.get_attribute("data-record-angle")
    frames = int(canvas.get_attribute("data-frames"))
    page.wait_for_function("before => Number(document.querySelector('#stageAdd canvas').dataset.frames) > before + 2", arg=frames)
    assert canvas.get_attribute("data-record-angle") == before
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    assert page.locator(".listening-panel-state").evaluate("node => getComputedStyle(node,'::before').animationName") == "riff-standby-light"
    page.locator("#riff-motion-toggle").click()
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    assert page.locator(".listening-panel-state").evaluate("node => getComputedStyle(node,'::before').animationName") == "none"
    page.emulate_media(reduced_motion="reduce")
    playwright.expect(canvas).to_have_attribute("data-record-spinning", "false")


def test_panel_controls_native_playback_and_shows_the_current_record(catalog_page, tmp_path):
    page, fixtures = catalog_page(_add_fixtures())
    page.emulate_media(reduced_motion="no-preference")
    panel = page.locator("#stageAdd [data-riff-listening-panel]")
    panel.locator("[data-listening-action]").click()
    _play(page)
    page.locator("#catalogCloseButton").click()
    panel.scroll_into_view_if_needed()
    playwright.expect(panel).to_have_attribute("data-playback-state", "playing")
    playwright.expect(panel.locator("[data-listening-title]")).to_have_text("Catalog song 1")
    playwright.expect(panel.locator("[data-listening-artist]")).to_contain_text("Synthetic artist")
    canvas = panel.locator("canvas")
    playwright.expect(canvas).to_have_attribute("data-record-spinning", "true")
    angle = canvas.get_attribute("data-record-angle")
    page.wait_for_function("angle => document.querySelector('#stageAdd canvas').dataset.recordAngle !== angle", arg=angle)
    _capture(page, "listening-deck-playing-1440.png", tmp_path)
    panel.get_by_role("button", name="Pause preview").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "paused")
    playwright.expect(canvas).to_have_attribute("data-record-spinning", "false")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    panel.get_by_role("button", name="Play preview").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "playing")
    page.locator("#catalogMuteButton").click()
    playwright.expect(panel.locator("[data-listening-state]")).to_have_text("PLAYING · MUTED")
    page.emulate_media(reduced_motion="reduce")
    playwright.expect(canvas).to_have_attribute("data-record-spinning", "false")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused && audio.muted")
    assert not _posts(fixtures)


def test_panel_reports_real_preview_failure_and_clears_it_on_retry(catalog_page):
    page, fixtures = catalog_page(_add_fixtures())
    panel = page.locator("#stageAdd [data-riff-listening-panel]")
    panel.locator("[data-listening-action]").click()
    page.evaluate("window.qaRejectAudio = true")
    page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Play preview", exact=True).click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-preview-error", "true")
    page.locator("#catalogCloseButton").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "error")
    playwright.expect(panel.locator("[data-listening-state]")).to_have_text("PREVIEW UNAVAILABLE")
    playwright.expect(panel.locator("[data-listening-note]")).to_have_text("Synthetic chart has no complete audio mix.")
    page.evaluate("window.qaRejectAudio = false")
    panel.get_by_role("button", name="Play preview").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "playing")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-preview-error", "false")
    assert not _posts(fixtures)


def test_panel_loading_action_cancels_without_late_audio(catalog_page):
    fixtures = _add_fixtures()
    pending = []

    def route_api(route):
        if urlsplit(route.request.url).path == "/api/catalog/enchor/1":
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, _ = catalog_page(fixtures, route_api=route_api)
    panel = page.locator("#stageAdd [data-riff-listening-panel]")
    panel.locator("[data-listening-action]").click()
    page.locator('[data-catalog-key="enchor:1"]').get_by_role("button", name="Play preview", exact=True).click()
    page.locator("#catalogCloseButton").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "loading")
    panel.get_by_role("button", name="Cancel loading").click()
    fixtures.fulfill(pending.pop())
    playwright.expect(panel).to_have_attribute("data-playback-state", "paused")
    assert page.evaluate("window.qaAudio.loads") == []
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused && !audio.getAttribute('src')")
    assert not _posts(fixtures)


def test_home_adoption_mounts_a_live_deck_without_replacing_audio_or_double_binding(catalog_page, tmp_path):
    page, fixtures = catalog_page(_add_fixtures(), path="/about")
    assert page.locator("[data-riff-listening-panel]").count() == 0
    page.emulate_media(reduced_motion="no-preference")
    page.locator("#catalogBrowseButton").click()
    _play(page)
    page.evaluate("window.originalListeningAudio = document.querySelector('#catalogAudio')")
    page.locator("#catalogCloseButton").click()
    page.locator(".brand").click()
    panel = page.locator("#stageAdd [data-riff-listening-panel]")
    panel.scroll_into_view_if_needed()
    playwright.expect(panel).to_be_visible()
    playwright.expect(panel.locator("canvas")).to_have_attribute("data-renderer", "webgl", timeout=10000)
    playwright.expect(panel.locator("[data-listening-title]")).to_have_text("Catalog song 1")
    playwright.expect(panel).to_have_attribute("data-playback-state", "playing")
    playwright.expect(panel.locator("canvas")).to_have_attribute("data-record-spinning", "true")
    assert page.locator("#catalogAudio").count() == 1
    assert page.evaluate("window.originalListeningAudio === document.querySelector('#catalogAudio')")
    page.evaluate("window.RiffScene.mount(document.querySelector('#riffApp')); window.RiffScene.mount()")
    panel.get_by_role("button", name="Pause preview").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    panel.get_by_role("button", name="Play preview").click()
    playwright.expect(panel).to_have_attribute("data-playback-state", "playing")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert page.evaluate("window.originalListeningAudio === document.querySelector('#catalogAudio')")
    _assert_no_overflow(page)
    _capture(page, "listening-deck-after-home-mount-1440.png", tmp_path)
    assert not _posts(fixtures)
