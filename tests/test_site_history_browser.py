"""Owned-page history preserves the mounted landing page, studio and player."""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from tests.test_public_browser import playwright, public_server
from tests.test_public_delivery_browser import delivery_chromium
from tests.test_catalog_browser import CatalogFixtures, catalog_page, _browse, _play

pytestmark = pytest.mark.browser


@pytest.fixture
def anonymous_page(public_server, delivery_chromium):
    app, base_url, _cookie_name, _cookie = public_server
    contexts = []
    errors = []
    requests = []

    def open_page(path="/", *, width=1440):
        context = delivery_chromium.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request))
        page.goto(base_url + path, wait_until="networkidle")
        return page, app, base_url, requests

    yield open_page
    for context in contexts:
        context.close()
    assert not errors, errors
    assert not [request.url for request in requests if request.method != "GET"]


def _identity(page):
    page.evaluate("""() => {
      window.baseMainChild = document.querySelector('#main-content').firstElementChild;
      window.baseAudio = document.querySelector('#catalogAudio');
    }""")


def _retained(page):
    assert page.evaluate("window.baseMainChild === document.querySelector('#main-content').firstElementChild")
    assert page.evaluate("window.baseAudio === document.querySelector('#catalogAudio')")
    assert page.locator("#main-content").evaluate("node => !node.inert")


@pytest.mark.parametrize("width", [390, 1440])
def test_anonymous_about_back_forward_restores_landing_without_refetch(anonymous_page, width):
    page, _app, _base_url, requests = anonymous_page(width=width)
    _identity(page)
    page.locator('.site-nav a[href="/about"]').click()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    assert urlsplit(page.url).path == "/about"
    page.go_back()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    playwright.expect(page.locator("#landing-title")).to_be_visible()
    assert urlsplit(page.url).path == "/"
    _retained(page)
    assert len([request for request in requests if urlsplit(request.url).path == "/"]) == 1
    page.go_forward()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    playwright.expect(page.locator("#catalogSitePage h1")).to_have_text("Our mission")
    assert urlsplit(page.url).path == "/about"
    page.go_back()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    _retained(page)


def test_initial_about_base_is_restored_by_close_and_history(anonymous_page):
    page, _app, _base_url, _requests = anonymous_page("/about")
    _identity(page)
    page.locator('.site-footer a[href="/privacy"]').click()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
    page.locator("#catalogCloseButton").click()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    assert urlsplit(page.url).path == "/about"
    playwright.expect(page.locator("#main-content h1")).to_have_text("Our mission")
    _retained(page)
    page.go_back()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
    page.go_back()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    _retained(page)
    page.go_forward()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
    page.go_forward()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    _retained(page)


def test_anonymous_about_deep_link_can_open_home_and_return(anonymous_page):
    page, _app, _base_url, _requests = anonymous_page("/about")
    page.evaluate("window.baseAudio = document.querySelector('#catalogAudio')")
    page.locator('.site-header a[href="/"]').click()
    playwright.expect(page.locator("#landing-title")).to_be_visible()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    assert page.evaluate("window.baseAudio === document.querySelector('#catalogAudio')")
    page.go_back()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    page.go_forward()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    playwright.expect(page.locator("#landing-title")).to_be_visible()
    assert page.evaluate("window.baseAudio === document.querySelector('#catalogAudio')")


@pytest.mark.parametrize("destination,status", [("home", 200), ("privacy", 500)])
def test_late_about_response_cannot_reopen_or_overwrite_newer_page(anonymous_page, destination, status):
    page, app, base_url, _requests = anonymous_page()
    _identity(page)
    held = []
    about_html = app.test_client().get("/about").data
    page.route(base_url + "/about", lambda route: held.append(route))
    # Let the intentionally late response resolve despite AbortController, to
    # exercise the navigation identity guard independently of browser aborts.
    page.evaluate("""() => {
      const original = window.fetch.bind(window);
      window.fetch = (path, options) => {
        if (path !== '/about') return original(path, options);
        return original(path, {...options, signal: undefined}).then(response => {
          window.lateAboutResolved = true;
          return response;
        });
      };
    }""")
    page.locator('.site-nav a[href="/about"]').click()
    playwright.expect(page).to_have_url(base_url + "/about")
    assert len(held) == 1
    page.go_back()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    _retained(page)
    if destination == "privacy":
        page.locator('.site-footer a[href="/privacy"]').click()
        playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
    held.pop().fulfill(status=status, content_type="text/html", body=about_html)
    page.wait_for_function("window.lateAboutResolved === true")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    if destination == "home":
        playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
        _retained(page)
    else:
        playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Privacy Policy")
        playwright.expect(page.locator("#catalogSitePage h1")).to_have_text("Privacy Policy")


def test_authenticated_about_history_keeps_workflow_hash_audio_and_zip_job(catalog_page):
    fixtures = CatalogFixtures()
    fixtures.async_download = True
    fixtures.download_complete_after_polls = None
    page, fixtures = catalog_page(fixtures)
    _browse(page)
    _play(page)
    page.locator("#catalogCloseButton").click()
    page.locator("#continueDownloadButton").click()
    page.locator("#prepareDownloadButton").click()
    playwright.expect(page.locator("#downloadStatus")).to_contain_text("leave")
    job_id = fixtures.download_job["id"]
    selected = set(fixtures.selected)
    revision = fixtures.selection_revision
    page.locator('[data-navigate-step="confirm"]').click()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert urlsplit(page.url).fragment == "step=confirm"
    _identity(page)
    before = page.locator("#catalogAudio").evaluate("audio => audio.currentTime")
    page.locator('.site-nav a[href="/about"]').click()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    page.go_back()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert urlsplit(page.url).fragment == "step=confirm"
    assert page.evaluate("history.state.riffStage") == "confirm"
    _retained(page)
    page.go_forward()
    playwright.expect(page.locator("#catalogDrawerTitle")).to_have_text("Mission")
    page.locator("#catalogCloseButton").click()
    playwright.expect(page.locator("#catalogDrawer")).to_be_hidden()
    assert urlsplit(page.url).fragment == "step=confirm"
    assert page.evaluate("history.state.riffStage") == "confirm"
    playwright.expect(page.locator("#stageConfirm")).to_be_visible()
    assert page.locator("#catalogAudio").evaluate("audio => !audio.paused")
    assert page.locator("#catalogAudio").evaluate("audio => audio.currentTime") >= before
    assert fixtures.download_job["id"] == job_id
    assert fixtures.selected == selected and fixtures.selection_revision == revision
    assert page.evaluate("window.qaAudio.loads") == ["enchor:1"]
    posts = [(method, path) for method, path, _ in fixtures.request_log if method != "GET"]
    assert posts == [("POST", "/api/download")]
