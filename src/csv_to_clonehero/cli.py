"""Command-line interface.

Commands: scan, search, expand, plan, build, report, doctor, reset-cache.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import shutil
import sys
from pathlib import Path

from rich.console import Console

console = Console()


def _setup_logging(level: str) -> None:
    from rich.logging import RichHandler

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    # httpx logs every request at INFO - too noisy for normal use.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load(config_arg: str | None):
    from csv_to_clonehero.config import load_config

    try:
        return load_config(config_arg)
    except Exception as exc:
        console.print(f"[red]Failed to load configuration: {exc}[/red]")
        raise SystemExit(2)


def _open_db(config):
    from csv_to_clonehero.database import Database

    return Database(config.database_path)


def _open_sources(config):
    from csv_to_clonehero.sources import build_sources

    return build_sources(config)


def _open_overrides(config):
    from csv_to_clonehero.overrides import load_overrides

    return load_overrides(config.base_dir)


def cmd_scan(config, _args) -> int:
    from csv_to_clonehero.pipeline import run_scan

    db = _open_db(config)
    try:
        stats = run_scan(config, db, _open_overrides(config))
    finally:
        db.close()
    console.print(
        f"[green]{stats.unique_tracks} unique tracks[/green] from "
        f"{stats.csv_files} playlist files ({stats.total_rows} rows), "
        f"{stats.primary_artists} primary artists"
    )
    return 0


def cmd_search(config, _args) -> int:
    from csv_to_clonehero.pipeline import run_search

    db = _open_db(config)
    sources = _open_sources(config)
    try:
        stats = run_search(
            config,
            db,
            sources,
            _open_overrides(config),
            include_expansion=not getattr(_args, "exact_only", False),
        )
    finally:
        for s in sources:
            s.close()
        db.close()
    console.print(
        f"Exact: [green]{stats.auto} auto[/green], [yellow]{stats.review} review[/yellow], "
        f"[red]{stats.no_match} no match[/red] | "
        f"Expansion: {stats.expansion_candidates} candidates"
    )
    return 0


def cmd_expand(config, args) -> int:
    from csv_to_clonehero.pipeline import run_build

    dry_run = args.dry_run or config.build.dry_run
    db = _open_db(config)
    sources = _open_sources(config)
    try:
        result = run_build(
            config,
            db,
            sources,
            _open_overrides(config),
            dry_run=dry_run,
            include_expansion=True,
        )
    finally:
        for s in sources:
            s.close()
        db.close()
    console.print(
        f"Expansion pass: {result.search.expansion_candidates} quality candidates, "
        f"{result.plan.expansion_selected} selected"
    )
    return 0


def cmd_plan(config, _args) -> int:
    from csv_to_clonehero.pipeline import run_plan

    db = _open_db(config)
    sources = _open_sources(config)
    try:
        stats = run_plan(
            config,
            db,
            sources,
            include_expansion=not getattr(_args, "exact_only", False),
        )
    finally:
        for s in sources:
            s.close()
        db.close()
    console.print(
        f"Plan: {stats.exact_selected} exact + {stats.expansion_selected} expansion, "
        f"{stats.planned_bytes / (1024**3):.1f} GiB estimated, "
        f"{stats.skipped_for_space} skipped for space"
    )
    return 0


def cmd_build(config, args) -> int:
    from csv_to_clonehero.pipeline import run_build

    dry_run = args.dry_run or config.build.dry_run
    db = _open_db(config)
    sources = _open_sources(config)
    try:
        result = run_build(
            config,
            db,
            sources,
            _open_overrides(config),
            dry_run=dry_run,
            include_expansion=not getattr(args, "exact_only", False),
        )
    finally:
        for s in sources:
            s.close()
        db.close()

    if result.search.review:
        console.print(
            f"[yellow]{result.search.review} songs need manual review - "
            f"see reports/review_needed.csv[/yellow]"
        )
    return 0


def cmd_report(config, _args) -> int:
    from csv_to_clonehero.reporting import generate_reports, generate_spotify_reports

    db = _open_db(config)
    try:
        paths = generate_reports(config, db)
        workspace = db.get_workspace("spotify")
        if workspace is not None:
            paths.extend(generate_spotify_reports(config, db, workspace["id"]))
    finally:
        db.close()
    console.print(f"[green]{len(paths)} reports written to {config.reports_path}[/green]")
    return 0


def cmd_reset_cache(config, _args) -> int:
    db = _open_db(config)
    try:
        count = db.reset_search_cache()
    finally:
        db.close()
    console.print(f"[green]Cleared {count} cached searches[/green] (downloads were kept)")
    return 0


def cmd_keys(config, _args) -> int:
    """Generate ephemeral secrets for production deployment."""
    from csv_to_clonehero.server.security import generate_fernet_key, generate_secret_key

    console.print("[bold]Production secrets (set these in env / secret manager)[/bold]")
    console.print(f"[dim]SECRET_KEY            =[/dim] {generate_secret_key()}")
    console.print(f"[dim]TOKEN_ENCRYPTION_KEY =[/dim] {generate_fernet_key()}")
    console.print("[dim]Note: also set APP_BASE_URL and SPOTIFY_CLIENT_ID.[/dim]")
    return 0


def cmd_serve(config, args) -> int:
    """Run the production multi-user web app (waitress; never the dev server)."""
    try:
        config.server.enforce_secret_key = True
    except Exception:
        pass

    def import_app():
        from csv_to_clonehero.server.web import create_app

        return create_app(config)

    if args.wsgi:
        # Print nothing; let a real WSGI server import `wsgi:app`.
        console.print("Use: waitress-serve --port 8000 wsgi:app  (see wsgi.py)")
        return 0

    from waitress import serve

    app = import_app()
    console.print(f"[green bold]Production web app on {config.public_base_url or 'http://127.0.0.1'}:{args.port}[/green bold]")
    serve(app, host=args.host, port=args.port, threads=getattr(args, "threads", 8))
    return 0


def cmd_doctor(config, _args) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "[green]OK[/green]" if good else "[red]FAIL[/red]"
        console.print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

    def warn(label: str, detail: str) -> None:
        console.print(f"  [[yellow]WARN[/yellow]] {label} - {detail}")

    console.print("[bold]Checking setup...[/bold]")

    version = sys.version_info
    check(f"Python {version.major}.{version.minor}.{version.micro}", version >= (3, 11),
          "3.11+ required" if version < (3, 11) else "")

    for package in ("httpx", "rapidfuzz", "pydantic", "yaml", "rich", "tenacity"):
        try:
            __import__(package)
            check(f"dependency {package}", True)
        except ImportError:
            check(f"dependency {package}", False, "run: pip install -e .")

    input_path = config.input_path
    from csv_to_clonehero.csv_import import SUPPORTED_EXTENSIONS

    file_count = (
        sum(
            1
            for p in input_path.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if input_path.exists()
        else 0
    )
    check(f"input folder {input_path}", input_path.exists(),
          f"{file_count} playlist files (.csv/.txt/.xml)" if input_path.exists()
          else "create it and add playlist exports")

    library_root = config.library_root
    try:
        library_root.mkdir(parents=True, exist_ok=True)
        probe = library_root / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        check(f"library folder {library_root} writable", True)
    except OSError as exc:
        check(f"library folder {library_root} writable", False, str(exc))

    # Clone Hero / YARG scan recursively, so deep roots risk exceeding
    # Windows MAX_PATH once artist/album folders are appended.
    if len(str(library_root)) > 60:
        warn(
            f"library folder path is {len(str(library_root))} characters",
            "deep roots plus artist/album folders can exceed the Windows 260-char "
            "path limit; prefer a shorter root",
        )
    try:
        from csv_to_clonehero.filesystem import is_within

        if is_within(config.reports_path, library_root):
            warn(
                "reports folder is inside the library root",
                "Clone Hero/YARG scan the library recursively; keep reports outside "
                "it so only .sng files live there",
            )
    except OSError:
        pass

    try:
        usage = shutil.disk_usage(library_root)
        free_gib = usage.free / (1024**3)
        check(
            f"free disk space {free_gib:.1f} GiB",
            free_gib >= 1,
            f"budget is {config.storage.max_library_gib:.0f} GiB",
        )
    except OSError as exc:
        check("free disk space", False, str(exc))

    try:
        import httpx

        response = httpx.get("https://api.enchor.us", timeout=10, follow_redirects=True)
        # 404 on / is fine - we only need reachability.
        check("Enchor API reachable", response.status_code < 500,
              f"HTTP {response.status_code}")
    except Exception as exc:
        check("Enchor API reachable", False, str(exc))

    try:
        import httpx

        response = httpx.head("https://files.enchor.us", timeout=10, follow_redirects=True)
        check("Enchor download host reachable", response.status_code < 500,
              f"HTTP {response.status_code}")
    except Exception as exc:
        check("Enchor download host reachable", False, str(exc))

    if ok:
        console.print("[green bold]All checks passed.[/green bold]")
        return 0
    console.print("[red bold]Some checks failed - see above.[/red bold]")
    return 1


def cmd_web(config, args) -> int:
    import webbrowser

    from csv_to_clonehero.webapp import create_app

    if not _ensure_spotify_client_id(config):
        return 2
    public_url = str(getattr(args, "public_url", "") or "").strip().rstrip("/")
    if public_url:
        config.spotify.public_base_url = public_url
    public_mode = bool(getattr(config.spotify, "public_base_url", "").strip())
    host = "0.0.0.0" if args.lan or public_mode else args.host
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback_hosts and not (args.lan or public_mode):
        console.print("[red]Non-loopback hosts require --lan or --public-url.[/red]")
        return 2
    access_token = ""
    if args.lan or public_mode:
        access_token = (
            args.access_token
            or os.environ.get("CSV_TO_CLONEHERO_LAN_TOKEN")
            or secrets.token_urlsafe(32)
        )
        # Werkzeug's reloader starts a child process. Keep the bearer token
        # stable across that handoff instead of generating a second URL.
        os.environ["CSV_TO_CLONEHERO_LAN_TOKEN"] = access_token
    app = create_app(config, lan_access_token=access_token or None)
    local_url = f"http://127.0.0.1:{args.port}"
    console.print(f"[green bold]Web interface running locally at {local_url}[/green bold]")
    if args.lan:
        addresses = set()
        try:
            addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        addresses.discard("127.0.0.1")
        if not addresses:
            addresses.add("<host-ip>")
        console.print("[yellow]LAN mode is enabled. Complete Spotify sign-in on this host first.[/yellow]")
        console.print("[yellow]Share this token URL only with trusted devices on your Wi-Fi:[/yellow]")
        for address in sorted(addresses):
            console.print(f"  http://{address}:{args.port}/?access_token={access_token}")
        console.print("[dim]LAN requests require the token; localhost remains available without it.[/dim]")
    if public_mode:
        callback = config.spotify_redirect_uri
        if not callback:
            console.print("[red]--public-url must be a valid HTTPS URL.[/red]")
            return 2
        console.print(f"[yellow]Public mode is enabled at {public_url}[/yellow]")
        console.print(f"[yellow]Register this exact Spotify callback first: {callback}[/yellow]")
        console.print(f"[yellow]Open this private link on any device: {public_url}/?access_token={access_token}[/yellow]")
        console.print("[dim]Use Download ZIP in the browser; it saves to the device you are using.[/dim]")
    console.print("Press Ctrl+C to stop.")
    # Flask's reloader starts the command twice. Open the browser only in the
    # reloader child so one launcher invocation creates one tab.
    reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if not args.no_browser and (not getattr(args, "reload", False) or reloader_child):
        webbrowser.open(local_url)
    app.run(
        host=host,
        port=args.port,
        debug=bool(getattr(args, "reload", False)),
        use_reloader=bool(getattr(args, "reload", False)),
    )
    return 0


def _ensure_spotify_client_id(config) -> bool:
    """Prompt for a session-only Spotify client ID when none is configured."""
    if getattr(config, "spotify_client_id", ""):
        return True

    console.print(
        "[yellow]Spotify client ID is not configured.[/yellow]\n"
        "Create one at https://developer.spotify.com/dashboard and add the "
        "redirect URI shown below."
    )
    console.print(f"Redirect URI: {getattr(config, 'spotify_redirect_uri', '')}")
    try:
        client_id = console.input("Spotify client ID (input is not saved): ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]No Spotify client ID entered; web server not started.[/yellow]")
        return False
    if not client_id:
        console.print("[red]A Spotify client ID is required to start the web interface.[/red]")
        return False

    # Keep the value in this process only. Config resolution reads the
    # environment fallback, while refresh tokens remain in the OS keyring.
    import os

    variable = getattr(getattr(config, "spotify", None), "client_id_env", "SPOTIFY_CLIENT_ID")
    os.environ[str(variable or "SPOTIFY_CLIENT_ID")] = client_id
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csv_to_clonehero",
        description="Build a Clone Hero library from Spotify playlist CSVs.",
    )
    parser.add_argument("--config", help="path to config.yaml", default=None)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="parse input CSVs only")
    search = sub.add_parser("search", help="search and score charts")
    search.add_argument(
        "--exact-only", action="store_true", help="skip artist expansion"
    )

    expand = sub.add_parser(
        "expand", aliases=["search-expansion"], help="cached exact pass plus artist expansion"
    )
    expand.add_argument("--dry-run", action="store_true", help="search and plan, but download nothing")

    plan = sub.add_parser("plan", help="determine what would be downloaded")
    plan.add_argument(
        "--exact-only", action="store_true", help="ignore persisted artist expansion candidates"
    )

    build = sub.add_parser("build", help="full process: scan, search, plan, download, report")
    build.add_argument("--dry-run", action="store_true",
                       help="everything except downloading")
    build.add_argument(
        "--exact-only", action="store_true", help="skip artist expansion and plan exact songs only"
    )

    sub.add_parser("report", help="regenerate reports from stored state")
    sub.add_parser("doctor", help="check setup and connectivity")
    sub.add_parser("reset-cache", help="clear search cache (keeps downloads)")

    web = sub.add_parser("web", help="start the web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default="127.0.0.1", help="bind address; non-loopback requires --lan")
    web.add_argument("--lan", action="store_true", help="allow trusted same-Wi-Fi devices with a generated token")
    web.add_argument("--public-url", default="", help="HTTPS URL of a reverse proxy/tunnel for remote access")
    web.add_argument("--access-token", default=None, help="explicit LAN bearer token; otherwise generate one")
    web.add_argument("--no-browser", action="store_true", help="don't open the browser")
    web.add_argument(
        "--reload",
        action="store_true",
        help="reload the local server automatically when Python/template files change",
    )

    keys = sub.add_parser("keys", help="generate production secrets (SECRET_KEY, TOKEN_ENCRYPTION_KEY)")

    serve = sub.add_parser("serve", help="run the production multi-user web app (waitress)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--threads", type=int, default=8)
    serve.add_argument("--wsgi", action="store_true", help="only print the WSGI invocation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load(args.config)
    _setup_logging("DEBUG" if args.verbose else config.logging.level)

    # One-time state DB relocation, before any connection is opened.
    from csv_to_clonehero.config import migrate_state_db

    try:
        note = migrate_state_db(config)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if note:
        console.print(f"[cyan]{note}[/cyan]")

    handlers = {
        "scan": cmd_scan,
        "search": cmd_search,
        "expand": cmd_expand,
        "search-expansion": cmd_expand,
        "plan": cmd_plan,
        "build": cmd_build,
        "report": cmd_report,
        "doctor": cmd_doctor,
        "reset-cache": cmd_reset_cache,
        "web": cmd_web,
        "keys": cmd_keys,
        "serve": cmd_serve,
    }
    return handlers[args.command](config, args)
