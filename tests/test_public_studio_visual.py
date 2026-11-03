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
    disclosure = page.locator("#studioDisclosure")
    if width < 768:
        # Phones keep the first song in view; bulk selection waits one tap away.
        assert disclosure.get_attribute("open") is None
        disclosure.locator("summary").click()
    assert disclosure.get_attribute("open") is not None
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


def _knot(page, selector):
    return page.locator(selector).evaluate("""wrap => {
      const art = getComputedStyle(wrap, '::after');
      return {image: art.backgroundImage, name: art.animationName, duration: art.animationDuration};
    }""")


def test_confirm_ledger_has_no_knot_and_never_starts_webgl(delivery_page):
    """Signal Check spec 4.5 and 6: no knot in the ledger, no WebGL engine."""
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    page.locator("#studio-deck").scroll_into_view_if_needed()
    playwright.expect(page.locator("#studio-deck-art")).to_be_hidden()
    playwright.expect(page.locator("#studio-select-all")).to_be_visible()
    page.wait_for_timeout(250)
    assert page.locator("canvas[data-renderer]").count() == 0
    _assert_no_overflow(page)
    _capture(page, "studio-deck-no-knot-1440.png")


def test_knot_motion_follows_the_toggle_and_reduced_motion(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    knot = "#stageAdd .riff-scene-wrap"
    assert "signal-knot.svg" in _knot(page, knot)["image"]
    # Reduced motion is the default here: the knot stays still.
    playwright.expect(page.locator("#riff-motion-toggle")).to_have_attribute("aria-pressed", "false")
    assert page.evaluate("document.documentElement.dataset.riffMotion") == "off"
    assert _knot(page, knot)["name"] == "none"
    # An explicit opt-in for this visit lets it float.
    page.locator("#riff-motion-toggle").click()
    assert page.evaluate("document.documentElement.dataset.riffMotion") == "on"
    assert _knot(page, knot)["name"] == "riff-knot-suspend"
    page.locator("#riff-motion-toggle").click()
    assert _knot(page, knot)["name"] == "none"
    # A changed device preference clears the one-visit override.
    page.locator("#riff-motion-toggle").click()
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_timeout(60)
    page.emulate_media(reduced_motion="reduce")
    playwright.expect(page.locator("html")).to_have_attribute("data-riff-motion", "off")
    assert _knot(page, knot)["name"] == "none"


def test_knot_needs_no_webgl(delivery_page):
    page, _requests = delivery_page(SignalCheckFixtures("confirm"))
    page.add_init_script("""const originalContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(kind, ...options) {
          window.qaWebglRequests = (window.qaWebglRequests || 0) + (kind === 'webgl' ? 1 : 0);
          return kind === 'webgl' ? null : originalContext.call(this, kind, ...options);
        };""")
    page.reload(wait_until="networkidle")
    assert "signal-knot.svg" in _knot(page, "#stageAdd .riff-scene-wrap")["image"]
    assert page.evaluate("window.qaWebglRequests || 0") == 0
    playwright.expect(page.locator("#studio-select-all")).to_be_visible()
    _assert_no_overflow(page)


def test_landing_knot_and_source_entry_stay_clear(public_server, delivery_chromium):
    _app, url, _cookie_name, _cookie = public_server
    for width in (390, 1440):
        page = delivery_chromium.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        try:
            page.goto(url + "/", wait_until="networkidle")
            playwright.expect(page.get_by_role("heading", name="Find your next favorite riff.")).to_be_visible()
            entry = page.locator('a[href="/login"]').bounding_box()
            assert entry and entry["height"] >= 44
            wrap = page.locator(".riff-scene-wrap:has(#riff-scene)")
            wrap.scroll_into_view_if_needed()
            art = _knot(page, ".riff-scene-wrap:has(#riff-scene)")
            assert "signal-knot.svg" in art["image"] and art["name"] == "none"
            playwright.expect(page.locator("#riff-scene")).to_be_hidden()
            _assert_no_overflow(page)
            _capture(page, f"studio-landing-{width}.png")
        finally:
            page.close()
