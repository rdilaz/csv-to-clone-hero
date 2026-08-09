"""Command-line interface.

Commands: scan, search, plan, build, report, doctor, reset-cache.
"""

from __future__ import annotations

import argparse
import logging
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
        f"{stats.csv_files} CSVs ({stats.total_rows} rows), "
        f"{stats.primary_artists} primary artists"
    )
    return 0


def cmd_search(config, _args) -> int:
    from csv_to_clonehero.pipeline import run_search

    db = _open_db(config)
    sources = _open_sources(config)
    try:
        stats = run_search(config, db, sources, _open_overrides(config))
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


def cmd_plan(config, _args) -> int:
    from csv_to_clonehero.pipeline import run_plan

    db = _open_db(config)
    sources = _open_sources(config)
    try:
        stats = run_plan(config, db, sources)
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
        result = run_build(config, db, sources, _open_overrides(config), dry_run=dry_run)
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
    from csv_to_clonehero.reporting import generate_reports

    db = _open_db(config)
    try:
        paths = generate_reports(config, db)
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


def cmd_doctor(config, _args) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "[green]OK[/green]" if good else "[red]FAIL[/red]"
        console.print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

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
    csv_count = len(list(input_path.glob("*.csv"))) if input_path.exists() else 0
    check(f"input folder {input_path}", input_path.exists(),
          f"{csv_count} CSV files" if input_path.exists() else "create it and add CSVs")

    library_root = config.library_root
    try:
        library_root.mkdir(parents=True, exist_ok=True)
        probe = library_root / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        check(f"library folder {library_root} writable", True)
    except OSError as exc:
        check(f"library folder {library_root} writable", False, str(exc))

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

    app = create_app(config)
    url = f"http://127.0.0.1:{args.port}"
    console.print(f"[green bold]Web interface running at {url}[/green bold]")
    console.print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host="127.0.0.1", port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csv_to_clonehero",
        description="Build a Clone Hero library from Spotify playlist CSVs.",
    )
    parser.add_argument("--config", help="path to config.yaml", default=None)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="parse input CSVs only")
    sub.add_parser("search", help="search and score charts")
    sub.add_parser("plan", help="determine what would be downloaded")

    build = sub.add_parser("build", help="full process: scan, search, plan, download, report")
    build.add_argument("--dry-run", action="store_true",
                       help="everything except downloading")

    sub.add_parser("report", help="regenerate reports from stored state")
    sub.add_parser("doctor", help="check setup and connectivity")
    sub.add_parser("reset-cache", help="clear search cache (keeps downloads)")

    web = sub.add_parser("web", help="start the local web interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="don't open the browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load(args.config)
    _setup_logging("DEBUG" if args.verbose else config.logging.level)

    handlers = {
        "scan": cmd_scan,
        "search": cmd_search,
        "plan": cmd_plan,
        "build": cmd_build,
        "report": cmd_report,
        "doctor": cmd_doctor,
        "reset-cache": cmd_reset_cache,
        "web": cmd_web,
    }
    return handlers[args.command](config, args)
