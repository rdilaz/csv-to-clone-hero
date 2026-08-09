"""Build pipeline: scan -> search -> score -> plan -> download -> report."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.csv_import import artist_track_counts, import_playlist_dir
from csv_to_clonehero.database import Database
from csv_to_clonehero.downloader import DownloadManager
from csv_to_clonehero.matching import pick_best, score_expansion_candidate
from csv_to_clonehero.models import ChartCandidate, Track
from csv_to_clonehero.normalization import normalize_artist
from csv_to_clonehero.overrides import Overrides
from csv_to_clonehero.planner import build_plan
from csv_to_clonehero.sources.base import ChartSource, SourceError

log = logging.getLogger(__name__)
console = Console()


@dataclass
class ScanStats:
    csv_files: int = 0
    total_rows: int = 0
    unique_tracks: int = 0
    primary_artists: int = 0


@dataclass
class SearchStats:
    auto: int = 0
    review: int = 0
    no_match: int = 0
    errors: int = 0
    expansion_candidates: int = 0
    expansion_artists: int = 0


@dataclass
class PlanStats:
    exact_selected: int = 0
    expansion_selected: int = 0
    skipped_for_space: int = 0
    planned_bytes: int = 0


@dataclass
class BuildResult:
    scan: ScanStats = field(default_factory=ScanStats)
    search: SearchStats = field(default_factory=SearchStats)
    plan: PlanStats = field(default_factory=PlanStats)
    downloaded: int = 0
    download_errors: int = 0
    download_bytes: int = 0


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    )


# ---------------------------------------------------------------------- scan
def run_scan(config: AppConfig, db: Database, overrides: Overrides) -> ScanStats:
    result = import_playlist_dir(config.input_path)

    # Apply ignore-track overrides before persisting.
    tracks = [t for t in result.tracks if t.track_key not in set(overrides.ignore_tracks)]

    db.replace_tracks(tracks, result.file_row_counts)

    primary_counts, _ = artist_track_counts(tracks)
    stats = ScanStats(
        csv_files=len(result.file_row_counts),
        total_rows=result.total_rows,
        unique_tracks=len(tracks),
        primary_artists=len(primary_counts),
    )
    db.set_meta("scan_stats", json.dumps(vars(stats)))
    return stats


# -------------------------------------------------------------------- search
def _search_song_cached(
    config: AppConfig, db: Database, source: ChartSource, artist: str, title: str
) -> tuple[list[ChartCandidate], str | None, bool]:
    """Returns (candidates, error, from_cache)."""
    key = db.query_key("song", source.name, artist.casefold(), title.casefold())
    cached = db.get_cached_query(key, config.network.cache_ttl_days)
    if cached is not None:
        return cached[0], cached[1], True
    try:
        candidates = source.search_song(artist, title)
        error = None
    except SourceError as exc:
        candidates, error = [], str(exc)
    for candidate in candidates:
        db.upsert_candidate(candidate)
    db.record_query(key, "song", f"{artist} - {title}", candidates, error)
    return candidates, error, False


def _search_artist_cached(
    config: AppConfig, db: Database, source: ChartSource, artist: str
) -> tuple[list[ChartCandidate], str | None]:
    key = db.query_key("artist", source.name, artist.casefold())
    cached = db.get_cached_query(key, config.network.cache_ttl_days)
    if cached is not None:
        return cached[0], cached[1]
    try:
        candidates = source.search_artist(artist)
        error = None
    except SourceError as exc:
        candidates, error = [], str(exc)
    for candidate in candidates:
        db.upsert_candidate(candidate)
    db.record_query(key, "artist", artist, candidates, error)
    return candidates, error


def _decide_track(
    config: AppConfig, db: Database, track: Track, candidates: list[ChartCandidate]
) -> str:
    """Score all candidates for a track; return the track-level outcome."""
    best_chart, best_score, scored = pick_best(track, candidates, config)
    for chart, breakdown in scored:
        db.record_match_score(
            track.db_id, chart.db_id, breakdown.final_score, breakdown.decision,
            json.dumps({"explanation": breakdown.explain()}),
        )
    if not scored:
        return "NO_MATCH"
    if best_score.decision == "INCOMPATIBLE":
        return "NO_MATCH"
    return {"AUTO_ACCEPT": "AUTO_ACCEPTED", "REVIEW": "REVIEW", "REJECT": "REJECTED"}.get(
        best_score.decision, "NO_MATCH"
    )


def run_search(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
) -> SearchStats:
    stats = SearchStats()
    tracks = db.get_tracks()
    force_accept = {(r.track_key, r.chart_id) for r in overrides.force_accept}
    reject_charts = set(overrides.reject_charts)

    # ---------------- exact playlist pass ----------------
    with _progress() as progress:
        task = progress.add_task("Searching exact playlist songs", total=len(tracks))
        for track in tracks:
            query_title = overrides.title_alias(track.title) or track.title
            all_candidates: list[ChartCandidate] = []
            had_error = False
            for source in sources:
                candidates, error, _ = _search_song_cached(
                    config, db, source, track.primary_artist, query_title
                )
                if error:
                    had_error = True
                    db.record_error("search", error, track.track_key)
                all_candidates.extend(c for c in candidates if c.chart_id not in reject_charts)
            outcome = _decide_track(config, db, track, all_candidates)

            # Manual force-accept wins over the automatic decision.
            for chart in all_candidates:
                if (track.track_key, chart.chart_id) in force_accept:
                    db.record_match_score(
                        track.db_id, chart.db_id, 100.0, "AUTO_ACCEPT",
                        json.dumps({"explanation": "manual force-accept override"}),
                    )
                    outcome = "AUTO_ACCEPTED"

            if had_error and outcome in ("NO_MATCH", "REJECTED"):
                stats.errors += 1
            elif outcome == "AUTO_ACCEPTED":
                stats.auto += 1
            elif outcome == "REVIEW":
                stats.review += 1
            else:
                # REJECTED = candidates existed but none cleared the review
                # threshold; they are listed in rejected_candidates.csv.
                stats.no_match += 1
            progress.advance(task)

    # ---------------- artist expansion pass ----------------
    if config.expansion.enabled:
        _run_expansion(config, db, sources, overrides, stats)

    db.set_meta("search_stats", json.dumps(vars(stats)))
    return stats


def _run_expansion(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    stats: SearchStats,
) -> None:
    tracks = db.get_tracks()
    primary_counts, featured_only_counts = artist_track_counts(tracks)

    display_names: dict[str, str] = {}
    for track in tracks:
        display_names.setdefault(track.primary_artist_norm, track.primary_artist)
        for feat in track.featured_artists:
            display_names.setdefault(normalize_artist(feat), feat)

    exp = config.expansion
    artist_rows: list[dict] = []

    def _cap_for(count: int, featured_only: bool, artist_norm: str) -> int:
        override = overrides.artist_override(artist_norm)
        if override and override.expansion_disabled:
            return 0
        if override and override.expansion_cap is not None:
            return override.expansion_cap
        if featured_only:
            return exp.cap_featured_only
        if count >= exp.min_songs_many:
            return exp.cap_many_songs
        if count >= 2:
            return exp.cap_some_songs
        return exp.cap_one_song

    for artist_norm, count in primary_counts.items():
        artist_rows.append(
            {
                "artist_name": display_names.get(artist_norm, artist_norm),
                "artist_norm": artist_norm,
                "track_count": count,
                "is_featured_only": False,
                "cap": _cap_for(count, False, artist_norm),
                "disabled": False,
            }
        )
    for artist_norm, count in featured_only_counts.items():
        artist_rows.append(
            {
                "artist_name": display_names.get(artist_norm, artist_norm),
                "artist_norm": artist_norm,
                "track_count": count,
                "is_featured_only": True,
                "cap": _cap_for(count, True, artist_norm),
                "disabled": False,
            }
        )

    db.replace_expansion_artists(artist_rows)
    eligible = [a for a in artist_rows if a["cap"] > 0]
    stats.expansion_artists = len(eligible)

    artist_db_ids = {row["artist_norm"]: row["id"] for row in db.get_expansion_artists()}

    with _progress() as progress:
        task = progress.add_task("Searching artist expansion", total=len(eligible))
        for artist in eligible:
            search_name = overrides.artist_alias(artist["artist_norm"]) or artist["artist_name"]
            all_candidates: list[ChartCandidate] = []
            for source in sources:
                candidates, error = _search_artist_cached(config, db, source, search_name)
                if error:
                    db.record_error("expansion_search", error, artist["artist_norm"])
                all_candidates.extend(candidates)
            accepted = 0
            for candidate in all_candidates:
                breakdown = score_expansion_candidate(artist["artist_norm"], candidate, config)
                db.record_expansion_candidate(
                    artist_db_ids[artist["artist_norm"]],
                    candidate.db_id,
                    breakdown.final_score,
                    json.dumps({"explanation": breakdown.explain()}),
                )
                if breakdown.decision == "AUTO_ACCEPT":
                    accepted += 1
            stats.expansion_candidates += accepted
            progress.advance(task)


# ---------------------------------------------------------------------- plan
def run_plan(
    config: AppConfig, db: Database, sources: list[ChartSource]
) -> PlanStats:
    def size_lookup(candidate: ChartCandidate) -> int | None:
        for source in sources:
            if source.name == candidate.source and hasattr(source, "head_size"):
                return source.head_size(candidate)
        return None

    # Only do live HEAD size lookups when downloading is actually possible or
    # when not in dry-run estimation mode... estimation is wanted in dry-run too,
    # so always attempt, but failures fall back to the conservative estimate.
    plan = build_plan(config, db, size_lookup=size_lookup)

    stats = PlanStats()
    for entry in plan:
        if entry["status"] == "skipped_for_space":
            stats.skipped_for_space += 1
        elif entry["kind"] == "exact":
            stats.exact_selected += 1
            stats.planned_bytes += entry["planned_bytes"]
        else:
            stats.expansion_selected += 1
            stats.planned_bytes += entry["planned_bytes"]
    db.set_meta("plan_stats", json.dumps(vars(stats)))
    return stats


# --------------------------------------------------------------------- build
def run_downloads(
    config: AppConfig, db: Database, sources: list[ChartSource]
) -> tuple[int, int, int]:
    """Download all selected charts. Returns (downloaded, errors, bytes)."""
    manager = DownloadManager(config, db)
    source_by_name = {s.name: s for s in sources}
    selections = db.get_selections(status="selected")

    downloaded = errors = bytes_total = 0
    with _progress() as progress:
        task = progress.add_task("Downloading charts", total=len(selections))
        for row in selections:
            candidate = db.get_candidate(row["candidate_id"])
            source = source_by_name.get(candidate.source) if candidate else None
            if candidate is None or source is None:
                errors += 1
                progress.advance(task)
                continue
            success, message, size = manager.download_selection(
                candidate, source, row["target_relpath"]
            )
            if success:
                downloaded += 1
                bytes_total += size
            else:
                errors += 1
                db.record_error("download", message, candidate.name)
            progress.advance(task)
    return downloaded, errors, bytes_total


def run_build(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    dry_run: bool,
) -> BuildResult:
    """Full pipeline with friendly console output."""
    result = BuildResult()

    console.print("[bold][1/5] Reading Spotify playlists...[/bold]")
    result.scan = run_scan(config, db, overrides)
    console.print(
        f"  {result.scan.csv_files} playlist CSVs, {result.scan.total_rows} rows, "
        f"[green]{result.scan.unique_tracks} unique tracks[/green], "
        f"{result.scan.primary_artists} primary artists"
    )

    console.print("[bold][2/5] Searching chart sources...[/bold]")
    result.search = run_search(config, db, sources, overrides)
    console.print(
        f"  Exact songs: [green]{result.search.auto} auto matches[/green], "
        f"[yellow]{result.search.review} review[/yellow], "
        f"[red]{result.search.no_match} no match[/red]"
        + (f", [red]{result.search.errors} errors[/red]" if result.search.errors else "")
    )
    if config.expansion.enabled:
        console.print(
            f"  Artist expansion: {result.search.expansion_candidates} quality candidates "
            f"across {result.search.expansion_artists} artists"
        )

    console.print("[bold][3/5] Planning library...[/bold]")
    result.plan = run_plan(config, db, sources)
    gib = result.plan.planned_bytes / (1024**3)
    console.print(
        f"  Selected [green]{result.plan.exact_selected} exact[/green] + "
        f"[green]{result.plan.expansion_selected} expansion[/green] tracks "
        f"([cyan]{gib:.1f} GiB[/cyan] estimated)"
    )
    if result.plan.skipped_for_space:
        console.print(
            f"  [yellow]{result.plan.skipped_for_space} selections skipped for space[/yellow]"
        )

    if dry_run or not config.build.direct_download_enabled:
        console.print("[bold][4/5] Downloading...[/bold] [cyan]skipped (dry run)[/cyan]")
    else:
        console.print("[bold][4/5] Downloading...[/bold]")
        result.downloaded, result.download_errors, result.download_bytes = run_downloads(
            config, db, sources
        )
        console.print(
            f"  [green]{result.downloaded} downloaded[/green]"
            + (
                f", [red]{result.download_errors} errors[/red]"
                if result.download_errors
                else ""
            )
            + f" ({result.download_bytes / (1024**3):.2f} GiB)"
        )

    console.print("[bold][5/5] Writing reports...[/bold]")
    from csv_to_clonehero.reporting import generate_reports

    paths = generate_reports(config, db)
    console.print(f"  Reports written to [cyan]{config.reports_path}[/cyan] ({len(paths)} files)")
    console.print(f"  Library folder: [cyan]{config.library_root}[/cyan]")
    return result
