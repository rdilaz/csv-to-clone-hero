"""Offline real-browser geometry, motion, and Studio layout checks.

Library responses are synthetic; screenshots are design evidence, not live counts.
"""
from __future__ import annotations

import pytest

from tests.public_signal_fixtures import SignalCheckFixtures
from tests.test_public_browser import _assert_no_overflow, _capture, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium, delivery_page

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_studio_selection_controls_fit_every_screen(delivery_page, width):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"), viewport={"width": width, "height": 900})
    playwright.expect(page.locator("#studio-panel")).to_be_visible()
    assert page.locator("#studio-select-scope").input_value() == "both"
    assert page.locator("#studio-select-policy").input_value() == "one_per_song"
    primary = page.locator("#studio-select-all").bounding_box()
    assert primary and primary["height"] >= 44
    if width < 720:
        rail = page.locator("#selectionRail").bounding_box()
        assert rail and primary["y"] + primary["height"] < rail["y"]
    _assert_no_overflow(page)
    _capture(page, f"studio-setlist-{width}.png")
    page.locator("#studio-deck").scroll_into_view_if_needed()
    _assert_no_overflow(page)
    _capture(page, f"studio-deck-{width}.png")


@pytest.mark.parametrize("scenario", ["add", "find"])
def test_studio_source_and_search_surfaces_keep_the_journey(delivery_page, scenario):
    page, _requests = delivery_page(SignalCheckFixtures(scenario), viewport={"width": 390, "height": 844})
    stage = "#stageAdd" if scenario == "add" else "#stageFind"
    playwright.expect(page.locator(stage)).to_be_visible()
    _assert_no_overflow(page)
    _capture(page, f"studio-{scenario}-390.png")


def test_scene_has_real_geometry_and_respects_reduced_motion(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    canvas = page.locator("#studio-deck canvas")
    canvas.scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-renderer", "webgl", timeout=10000)
    assert int(canvas.get_attribute("data-vertices")) > 10000
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    first = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(250)
    assert int(canvas.get_attribute("data-frames")) == first
    playwright.expect(page.locator("#riff-motion-toggle")).to_have_attribute("aria-pressed", "false")
    _capture(page, "studio-3d-static-1440.png")


def test_scene_motion_is_bounded_and_can_be_paused(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    page.emulate_media(reduced_motion="no-preference")
    canvas = page.locator("#studio-deck canvas")
    canvas.scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-renderer", "webgl", timeout=10000)
    playwright.expect(canvas).to_have_attribute("data-animating", "true")
    before = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(400)
    after = int(canvas.get_attribute("data-frames"))
    assert 1 <= after - before <= 14
    page.locator("#riff-motion-toggle").click()
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    paused = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(180)
    assert int(canvas.get_attribute("data-frames")) == paused
    assert page.evaluate("localStorage.getItem('riff-motion')") == "off"
    # The hidden Add Music scene has never allocated a WebGL context.
    assert page.locator("#stageAdd canvas").get_attribute("data-renderer") is None
    page.locator("#riff-motion-toggle").click()
    page.locator("#studio-deck").scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-animating", "true")
    page.locator("#continueDownloadButton").click()
    playwright.expect(page.locator("#stageDownload")).to_be_visible()
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    hidden_frames = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(180)
    assert int(canvas.get_attribute("data-frames")) == hidden_frames


def test_reduced_motion_defaults_static_but_allows_explicit_session_opt_in(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    canvas = page.locator("#studio-deck canvas")
    canvas.scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-renderer", "webgl", timeout=10000)
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    first = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(180)
    assert int(canvas.get_attribute("data-frames")) == first
    page.locator("#riff-motion-toggle").click()
    playwright.expect(canvas).to_have_attribute("data-animating", "true")
    before = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(250)
    assert int(canvas.get_attribute("data-frames")) > before
    page.locator("#riff-motion-toggle").click()
    playwright.expect(canvas).to_have_attribute("data-animating", "false")
    paused = int(canvas.get_attribute("data-frames"))
    page.wait_for_timeout(180)
    assert int(canvas.get_attribute("data-frames")) == paused
    # A changed device preference clears the one-visit override.
    page.locator("#riff-motion-toggle").click()
    page.emulate_media(reduced_motion="no-preference")
    # Chromium coalesces media flips in the same frame; allow its change event.
    page.wait_for_timeout(60)
    page.emulate_media(reduced_motion="reduce")
    playwright.expect(canvas).to_have_attribute("data-animating", "false")


def test_scene_fallback_preserves_controls_without_webgl(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    page.add_init_script("""const originalContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(kind, ...options) {
          return kind === 'webgl' ? null : originalContext.call(this, kind, ...options);
        };""")
    page.reload(wait_until="networkidle")
    canvas = page.locator("#studio-deck canvas")
    canvas.scroll_into_view_if_needed()
    playwright.expect(canvas).to_have_attribute("data-renderer", "css-fallback")
    playwright.expect(page.locator("#studio-deck .scene-fallback")).to_be_visible()
    playwright.expect(page.locator("#studio-select-all")).to_be_visible()
    _assert_no_overflow(page)


def test_landing_3d_and_source_entry_stay_clear(public_server, delivery_chromium):
    _app, url, _cookie_name, _cookie = public_server
    for width in (390, 1440):
        page = delivery_chromium.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        try:
            page.goto(url + "/", wait_until="networkidle")
            playwright.expect(page.get_by_role("heading", name="Find your next favorite riff.")).to_be_visible()
            entry = page.locator('a[href="/login"]').bounding_box()
            assert entry and entry["height"] >= 44
            page.locator("#riff-scene").scroll_into_view_if_needed()
            playwright.expect(page.locator("#riff-scene")).to_have_attribute("data-renderer", "webgl", timeout=10000)
            _assert_no_overflow(page)
            _capture(page, f"studio-landing-{width}.png")
        finally:
            page.close()
