"""Launch the real public UI against isolated Signal Check browser fixtures."""

from __future__ import annotations

import argparse
import os
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", "landing", "add", "find", "confirm"),
        default="all",
        help="UI state to open (default: all states in tabs)",
    )
    parser.add_argument("--viewport", type=_viewport, default=_viewport("1440x900"))
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window")
    parser.add_argument("--screenshot-dir", type=Path, help="Capture viewport PNGs, then exit")
    args = parser.parse_args()

    scenarios = ["landing", "add", "find", "confirm"] if args.scenario == "all" else [args.scenario]
    with ExitStack() as stack:
        temporary = Path(stack.enter_context(TemporaryDirectory(prefix="riff-signal-check-")))
        app, cookie_name, cookie_value = _application(temporary)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        runtime = stack.enter_context(sync_playwright())
        browser = runtime.chromium.launch(headless=args.headless)
        stack.callback(browser.close)
        context = browser.new_context(viewport=args.viewport)
        stack.callback(context.close)
        context.route("https://accounts.spotify.com/**", lambda route: route.abort("blockedbyclient"))
        pages = []
        try:
            if "landing" in scenarios:
                page = context.new_page()
                page.goto(f"{base_url}/#preview-landing", wait_until="networkidle")
                page.evaluate("document.title = 'Landing | Signal Check preview'")
                pages.append(("landing", page, None))

            if any(scenario != "landing" for scenario in scenarios):
                context.add_cookies([{"name": cookie_name, "value": cookie_value, "url": base_url}])
            for scenario in (item for item in scenarios if item != "landing"):
                fixtures = SignalCheckFixtures(scenario)
                page = context.new_page()
                page.route("**/api/**", fixtures.fulfill)
                page.goto(f"{base_url}/#preview-{scenario}", wait_until="networkidle")
                page.evaluate("name => { document.title = `${name} | Signal Check preview`; }", scenario.title())
                pages.append((scenario, page, fixtures))

            if args.screenshot_dir:
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
                for scenario, page, _fixtures in pages:
                    page.emulate_media(reduced_motion="reduce")
                    page.evaluate("document.fonts.ready")
                    page.screenshot(
                        path=str(args.screenshot_dir / f"{scenario}-{args.viewport['width']}x{args.viewport['height']}.png"),
                        full_page=False,
                    )
                unexpected = [request for _scenario, _page, fixtures in pages if fixtures for request in fixtures.unexpected_requests]
                if unexpected:
                    print(f"Unexpected fixture requests: {unexpected}")
                    return 1
                return 0

            if pages:
                pages[0][1].bring_to_front()
            print(f"Signal Check preview is running at {base_url} in the opened Chromium tabs.", flush=True)
            print("The database and provider/API fixtures are temporary. Press Ctrl+C to stop.", flush=True)
            while pages and any(not page.is_closed() for _scenario, page, _fixtures in pages):
                try:
                    next(page for _scenario, page, _fixtures in pages if not page.is_closed()).wait_for_timeout(500)
                except PlaywrightError:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            thread.join(timeout=2)
            app.extensions["jobs"].shutdown(timeout=2)
            app.extensions["downloads"].close()
        unexpected = [request for _scenario, _page, fixtures in pages if fixtures for request in fixtures.unexpected_requests]
        if unexpected:
            print(f"Unexpected fixture requests: {unexpected}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
