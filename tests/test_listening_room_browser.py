"""Listening-room controls with synthetic metadata and native offline WAV audio.

These regressions never fetch chart packages or write to a real song library.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_catalog_browser import CatalogFixtures, _browse, _play, _posts, catalog_page
from tests.test_public_browser import _assert_no_overflow, playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390])
def test_compact_transport_targets_are_reachable_at_phone_widths(catalog_page, width):
    page, fixtures = catalog_page(width=width)
    for selector in ["#catalogPlayerShuffle", "#catalogPlayerPrevious", "#catalogPlayerNext", "#catalogMuteButton"]:
        control = page.locator(selector)
        box = control.bounding_box()
        assert box is not None and box["width"] >= 44 and box["height"] >= 44, (selector, box)
        assert control.evaluate("node => {const box=node.getBoundingClientRect();const hit=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2);return hit===node || node.contains(hit);}")
    _assert_no_overflow(page)
    assert not _posts(fixtures)


def test_mute_survives_next_shuffle_and_unmute_without_selection(catalog_page):
    page, fixtures = catalog_page()
    _browse(page)
    _play(page)
    page.locator('[data-catalog-key="enchor:2"]').get_by_role("button", name="Queue", exact=True).click()
    page.locator("#catalogVolume").evaluate("node => {node.value='0.3'; node.dispatchEvent(new Event('input',{bubbles:true}));}")
    page.locator("#catalogMuteButton").click()
    playwright.expect(page.locator("#catalogMuteButton")).to_have_attribute("aria-pressed", "true")
    assert page.locator("#catalogAudio").evaluate("audio => audio.muted && audio.volume === .3 && !audio.paused")

    page.locator("#catalogPlayerNext").click()
    playwright.expect(page.locator("#catalogNowTitle")).to_have_text("Catalog song 2")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert page.locator("#catalogAudio").evaluate("audio => audio.muted && audio.volume === .3")

    loads = len(page.evaluate("window.qaAudio.loads"))
    page.locator("#catalogPlayerShuffle").click()
    page.wait_for_function("previous => window.qaAudio.loads.length > previous", arg=loads)
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    assert page.locator("#catalogAudio").evaluate("audio => audio.muted && audio.volume === .3")
    reads = [body for method, path, body in fixtures.request_log if path == "/api/catalog"]
    assert reads[-1]["order"] == ["shuffle"]
    page.locator("#catalogMuteButton").click()
    playwright.expect(page.locator("#catalogMuteButton")).to_have_attribute("aria-pressed", "false")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.muted && audio.volume === .3 && !audio.paused")
    assert not _posts(fixtures)


def test_reduced_motion_and_typing_do_not_interrupt_native_playback(catalog_page):
    page, fixtures = catalog_page(width=390)
    _browse(page)
    _play(page)
    record = page.locator(".catalog-vinyl-record")
    playwright.expect(record).to_have_attribute("data-animating", "false")
    page.locator("#catalogSearch").focus()
    page.keyboard.type(" mnas ")
    page.keyboard.press("ArrowRight")
    assert page.locator("#catalogSearch").input_value() == " mnas "
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused && !audio.muted")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert not _posts(fixtures)

    page.emulate_media(reduced_motion="no-preference")
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-motion", "on")
    playwright.expect(record).to_have_attribute("data-animating", "true")
    page.wait_for_function("() => Number(document.querySelector('.catalog-vinyl-record').dataset.angle) > 5")
    page.locator("#catalogVinylButton").click()
    playwright.expect(record).to_have_attribute("data-animating", "false")
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    angle = record.get_attribute("data-angle")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert record.get_attribute("data-angle") == angle
    page.locator("#catalogVinylButton").click()
    playwright.expect(page.locator("#catalogPlayerDock")).to_have_attribute("data-playback-state", "playing")
    page.emulate_media(reduced_motion="reduce")
    playwright.expect(record).to_have_attribute("data-animating", "false")
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    _assert_no_overflow(page)


def test_shuffle_loading_can_be_cancelled_without_late_playback(catalog_page):
    fixtures = CatalogFixtures()
    pending = []

    def route_api(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if urlsplit(route.request.url).path == "/api/catalog" and query.get("order") == ["shuffle"]:
            pending.append(route)
        else:
            fixtures.fulfill(route)

    page, fixtures = catalog_page(fixtures, route_api=route_api)
    _browse(page)
    _play(page)
    page.locator("#catalogPlayerShuffle").click()
    playwright.expect(page.locator("#catalogVinylButton")).to_have_attribute("aria-label", "Cancel preview loading")
    assert len(pending) == 1
    page.locator("#catalogVinylButton").click()
    fixtures.fulfill(pending.pop())
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    assert page.locator("#catalogAudio").evaluate("audio => audio.paused")
    assert not _posts(fixtures)
