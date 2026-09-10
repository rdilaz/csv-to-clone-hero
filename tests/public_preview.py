"""Launch the real public UI against isolated Signal Check browser fixtures."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from tests.public_signal_fixtures import SignalCheckFixtures


def _viewport(value: str) -> dict[str, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("viewport must look like 1440x900") from exc
    if width < 320 or height < 480:
        raise argparse.ArgumentTypeError("viewport must be at least 320x480")
    return {"width": width, "height": height}


def _application(root: Path):
    # An editable install can point at a different Agent Manager worktree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from csv_to_clonehero.config import AppConfig
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.web import create_app

    config = AppConfig()
    config.base_dir = root
    config.paths.database = "state/signal-check-preview.db"
    config.paths.state_dir = "state"
    config.server.enforce_secret_key = False
    config.monetization.gofundme_banner = False
    os.environ["SPOTIFY_CLIENT_ID"] = "signal-check-preview"
    app = create_app(config, source_factory=lambda _config: [])

    db = Database(config.database_path)
    try:
        user = db.upsert_user(
            spotify_user_id="signal-check-preview",
            display_name="Signal Player",
        )
        signed = db.create_session(int(user["id"]), expires_at=time.time() + 86400)
    finally:
        db.close()

    client = app.test_client()
    with client.session_transaction() as session:
        session["sid"] = signed["session_id"]
    cookie = client.get_cookie(config.sessions.cookie_name)
    return app, config.sessions.cookie_name, cookie.value


def _wait_for_surface(page, surface: str, fixtures: SignalCheckFixtures | None) -> None:
    if surface == "landing":
        expect(page.locator("#landing-title")).to_be_visible()
    else:
        expect(page.locator(f"#stage{surface.title()}")).to_be_visible(timeout=15000)
        expect(page.locator("body")).to_have_attribute("data-riff-stage", surface)
        expect(page.locator("#stageLoading")).to_be_hidden()
        if surface == "add":
            expect(page.locator("#spotifyChoices")).to_have_attribute("aria-busy", "false")
            expect(page.locator("#findChartsButton")).to_be_enabled()
        elif surface == "find":
            expect(page.locator("#progressCount")).to_contain_text("songs checked")
        elif surface == "confirm":
            expect(page.locator("#reviewCards")).to_have_attribute("aria-busy", "false")
            expect(page.locator("#selectedCount")).to_have_text(f"{len(fixtures.selected_candidates):,}")
            expect(page.locator("#continueDownloadButton")).to_be_enabled()
        elif surface == "download":
            expect(page.locator("#downloadSelectionCount")).to_have_text(f"{len(fixtures.selected_candidates):,}")
            expect(page.locator("#downloadExactSummary")).not_to_be_empty()
            expect(page.locator("#downloadExpansionSummary")).not_to_be_empty()
            expect(page.locator("#prepareDownloadButton")).to_be_enabled()
        elif surface == "done":
            expect(page.locator("#downloadLibraryLink")).to_have_attribute("href", fixtures.download["url"])
            expect(page.locator("#doneSummary")).not_to_be_empty()
            expect(page.locator("#downloadExpiry")).not_to_be_empty()
    page.evaluate("document.fonts.ready")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _preview_errors(pages, errors: list[str]) -> list[str]:
    return errors + [
        f"{scenario}: unexpected fixture request: {request}"
        for scenario, _page, fixtures in pages if fixtures
        for request in fixtures.unexpected_requests
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", "landing", "add", "find", "confirm", "download", "done", "confirm-expansion", "download-expansion", "done-partial"),
        default="all",
        help="UI state to open (all: the six main surfaces in tabs)",
    )
    parser.add_argument(
        "--viewport", type=_viewport, default=_viewport("1440x900"),
        help="WIDTHxHEIGHT, for example 1440x900, 390x844, or 320x800",
    )
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window")
    parser.add_argument("--screenshot-dir", type=Path, help="Capture viewport PNGs, then exit")
    args = parser.parse_args()

    scenarios = ["landing", "add", "find", "confirm", "download", "done"] if args.scenario == "all" else [args.scenario]
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(TemporaryDirectory(prefix="riff-signal-check-")))
        app, cookie_name, cookie_value = _application(temporary)
        stack.callback(app.extensions["downloads"].close)
        stack.callback(app.extensions["jobs"].shutdown, timeout=2)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        stack.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, timeout=2)
        stack.callback(server.shutdown)
        base_url = f"http://127.0.0.1:{server.server_port}"
        runtime = stack.enter_context(sync_playwright())
        browser = runtime.chromium.launch(headless=args.headless)
        stack.callback(browser.close)
        context = browser.new_context(
            viewport=args.viewport,
            has_touch=args.viewport["width"] < 720,
            reduced_motion="reduce" if args.screenshot_dir else "no-preference",
            accept_downloads=True,
            service_workers="block",
            locale="en-US",
        )
        stack.callback(context.close)
        errors: list[str] = []

        def local_only(route):
            parsed = urlsplit(route.request.url)
            if f"{parsed.scheme}://{parsed.netloc}" != base_url:
                errors.append(f"Blocked external request: {route.request.method} {route.request.url}")
                route.abort("blockedbyclient")
            elif parsed.path.startswith("/api/"):
                # Every API request must hit a page fixture, never the real worker.
                errors.append(f"Unmocked API request: {route.request.method} {parsed.path}")
                route.fulfill(status=500, content_type="application/json", body='{"error":"Unmocked preview API request"}')
            else:
                route.continue_()

        context.route("**/*", local_only)
        pages = []
        try:
            if "landing" in scenarios:
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(f"landing: page error: {error}"))
                pages.append(("landing", page, None))
                page.goto(f"{base_url}/#preview-landing", wait_until="networkidle")
                page.evaluate("document.title = 'Landing | Signal Check preview'")
                _wait_for_surface(page, "landing", None)

            if any(scenario != "landing" for scenario in scenarios):
                context.add_cookies([{"name": cookie_name, "value": cookie_value, "url": base_url}])
            for scenario in (item for item in scenarios if item != "landing"):
                surface = scenario.split("-", 1)[0]
                fixtures = SignalCheckFixtures(surface)
                fixtures.attach_downloads(app)
                if scenario in {"confirm-expansion", "download-expansion", "done-partial"}:
                    fixtures.expansion_groups = deepcopy(fixtures.expansion_catalog)
                    fixtures.expansion_artists_selected = {"paramore", "radiohead"}
                    fixtures.expansion_selected = {301, 302, 401}
                if scenario == "done-partial":
                    fixtures.export_duplicates = {102: 101}
                    fixtures.export_failures = {103: "Synthetic fixture: chart download temporarily unavailable."}
                    fixtures.export_invalid = {401: "Synthetic fixture: no playable chart content."}
                page = context.new_page()
                page.on("pageerror", lambda error, name=scenario: errors.append(f"{name}: page error: {error}"))
                pages.append((scenario, page, fixtures))
                page.route("**/api/**", fixtures.fulfill)
                page.goto(f"{base_url}/#preview-{scenario}", wait_until="networkidle")
                page.evaluate("name => { document.title = `${name} | Signal Check preview`; }", scenario.title())
                _wait_for_surface(page, "confirm" if surface in {"download", "done"} else surface, fixtures)
                if surface in {"download", "done"}:
                    page.locator("#continueDownloadButton").click()
                    _wait_for_surface(page, "download", fixtures)
                if surface == "done":
                    page.locator("#prepareDownloadButton").click()
                    _wait_for_surface(page, "done", fixtures)
                if scenario == "done-partial":
                    expect(page.locator("#doneWarnings")).to_be_visible()
                if scenario == "confirm-expansion":
                    page.locator("#expansionPanel").scroll_into_view_if_needed()

            if args.screenshot_dir:
                failures = _preview_errors(pages, errors)
                if failures:
                    raise AssertionError("\n".join(failures))
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
                for scenario, page, _fixtures in pages:
                    _wait_for_surface(page, scenario.split("-", 1)[0], _fixtures)
                    page.screenshot(
                        path=str(args.screenshot_dir / f"{scenario}-{args.viewport['width']}x{args.viewport['height']}.png"),
                        full_page=False,
                    )
            else:
                if pages:
                    pages[0][1].bring_to_front()
                print(f"Signal Check preview is running at {base_url} in the opened Chromium tabs.", flush=True)
                print("The database and provider/API fixtures are temporary. Add-started searches finish automatically; the Find checkpoint stays stationary.", flush=True)
                print("Press Ctrl+C to stop.", flush=True)
                while pages and any(not page.is_closed() for _scenario, page, _fixtures in pages):
                    if _preview_errors(pages, errors):
                        break
                    try:
                        next(page for _scenario, page, _fixtures in pages if not page.is_closed()).wait_for_timeout(500)
                    except PlaywrightError:
                        if browser.is_connected() and any(not page.is_closed() for _scenario, page, _fixtures in pages):
                            raise
                        break
        except KeyboardInterrupt:
            pass
        except (PlaywrightError, AssertionError) as exc:
            errors.append(f"Preview failed: {exc}")
        failures = _preview_errors(pages, errors)
        if failures:
            print("\n".join(failures))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
