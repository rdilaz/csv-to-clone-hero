"""Build pipeline: scan -> search -> score -> plan -> relocate -> download -> report."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.csv_import import artist_track_counts, import_playlist_dir
from csv_to_clonehero.database import Database
from csv_to_clonehero.downloader import DownloadManager
from csv_to_clonehero.matching import score_candidate, score_expansion_candidate
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
    exact_queries: int = 0
    exact_queries_completed: int = 0
    exact_tracks: int = 0
    review_candidates: int = 0


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
    disabled_files = db.get_disabled_files()
    result = import_playlist_dir(config.input_path, skip=disabled_files)

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
    db.set_meta(
        "exact_progress",
        json.dumps(
            {
                "started": False,
                "total_tracks": len(tracks),
                "scored_tracks": 0,
                "total_queries": 0,
                "completed_queries": 0,
                "review_tracks": 0,
                "review_candidates": 0,
                "complete": False,
            }
        ),
    )
    return stats


# -------------------------------------------------------------------- search
def _fetch(source: ChartSource, kind: str, artist: str, title: str = "") -> tuple[list, str | None]:
    """Network-only fetch (runs on worker threads). Never touches the DB."""
    try:
        if kind == "song":
            return source.search_song(artist, title), None
        return source.search_artist(artist), None
    except SourceError as exc:
        return [], str(exc)


def _resolve_queries(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    queries: list[tuple[str, str, str]],
    progress_label: str,
    on_progress=None,
    on_result=None,
) -> dict[str, tuple[list, str | None]]:
    """Resolve (kind, source, artist, title) queries to candidates.

    Fresh cache entries are used as-is; misses are fetched concurrently
    (network only) and recorded on the main thread. Errors are NOT cached,
    so transient failures are retried on the next run.

    ``queries`` items are (kind, artist, title); returns a map of
    query_key -> (candidates, error).
    """
    ttl = config.network.cache_ttl_days
    outcomes: dict[str, tuple[list, str | None]] = {}
    specs: dict[str, tuple[str, ChartSource, str, str]] = {}
    cached: dict[str, tuple[list, str | None]] = {}

    for kind, artist, title in queries:
        for source in sources:
            key = db.query_key(kind, source.name, artist.casefold(), title.casefold())
            if key in specs:
                continue
            spec = (kind, source, artist, title)
            specs[key] = spec
            cached_result = db.get_cached_query(key, ttl)
            if cached_result is not None:
                cached[key] = cached_result

    total = len(specs)
    completed = 0

    def complete(key: str, result: tuple[list, str | None]) -> None:
        nonlocal completed
        outcomes[key] = result
        if on_result is not None:
            on_result(key, result[0], result[1])
        completed += 1
        if on_progress is not None:
            on_progress(completed, total)

    # Cached queries are resolved synchronously, but still go through the same
    # callback as network results. This lets tracks whose cache is complete be
    # scored while unrelated network queries are still running.
    for key, result in cached.items():
        complete(key, result)

    misses = {key: spec for key, spec in specs.items() if key not in cached}
    if misses:
        workers = max(1, config.network.concurrency)
        with _progress() as progress:
            task = progress.add_task(progress_label, total=len(misses))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_fetch, source, kind, artist, title): key
                    for key, (kind, source, artist, title) in misses.items()
                }
                for future in as_completed(futures):
                    key = futures[future]
                    kind, source, artist, title = misses[key]
                    candidates, error = future.result()

                    # All SQLite writes stay on this writer thread. Errors are
                    # intentionally not cached so transient failures retry.
                    for candidate in candidates:
                        db.upsert_candidate(candidate)
                    if error is None:
                        db.record_query(
                            key, kind, f"{artist} - {title}".strip(" -"), candidates
                        )
                    complete(key, (candidates, error))
                    progress.advance(task)

    return outcomes


def _query_outcomes(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    outcomes: dict[str, tuple[list, str | None]],
    kind: str,
    artist: str,
    title: str = "",
) -> tuple[list, list[str]]:
    """Collect (candidates, errors) for one logical query across sources."""
    candidates: list = []
    errors: list[str] = []
    for source in sources:
        key = db.query_key(kind, source.name, artist.casefold(), title.casefold())
        found, error = outcomes.get(key, ([], None))
        candidates.extend(found)
        if error:
            errors.append(error)
    return candidates, errors


def _decide_track(
    config: AppConfig,
    db: Database,
    track: Track,
    candidates: list[ChartCandidate],
    decisions: dict | None = None,
    forced_accept_ids: set[int] | None = None,
) -> str:
    """Score all candidates for a track; return the track-level outcome.

    Manual accepts are definitive winners, not score boosts. Manual rejects are
    excluded from the winner pool. ``decisions`` normally uses
    ``(source, chart_id)`` keys; integer chart-id keys are accepted for older
    callers.
    """
    decisions = decisions or {}
    forced_accept_ids = forced_accept_ids or set()

    def manual_for(chart: ChartCandidate) -> str:
        return decisions.get(
            (chart.source, chart.chart_id), decisions.get(chart.chart_id, "")
        )

    scored: list[tuple[ChartCandidate, object, str, bool]] = []
    for chart in candidates:
        breakdown = score_candidate(track, chart, config)
        manual = manual_for(chart)
        forced = chart.chart_id in forced_accept_ids
        if manual == "reject":
            breakdown.decision = "REJECT"
            breakdown.final_score = 0.0
            breakdown.reasons.append("manually rejected")
        elif manual == "accept":
            if breakdown.decision == "INCOMPATIBLE":
                breakdown.reasons.append("manual accept ignored: instrument incompatible")
            else:
                breakdown.reasons.append("manually accepted")
        elif forced and breakdown.decision != "INCOMPATIBLE":
            breakdown.reasons.append("manual force-accept override")
        scored.append((chart, breakdown, manual, forced))

    # A manual accept (or force-accept override) wins regardless of automatic
    # score. If several legacy overrides conflict, prefer the explicit web
    # decision and then the highest-scoring candidate deterministically.
    accepted = [
        item
        for item in scored
        if item[1].decision != "INCOMPATIBLE"
        and (item[2] == "accept" or item[3])
    ]
    accepted.sort(
        key=lambda item: (
            0 if item[2] == "accept" else 1,
            -item[1].final_score,
            item[0].source,
            item[0].chart_id,
        )
    )

    if accepted:
        winner = accepted[0]
        winner_breakdown = winner[1]
        winner_breakdown.decision = "AUTO_ACCEPT"
        winner_breakdown.final_score = max(
            winner_breakdown.final_score, config.matching.auto_accept_threshold
        )
        for item in scored:
            if item is winner or item[1].decision == "INCOMPATIBLE":
                continue
            if item[2] == "reject":
                continue
            item[1].decision = "REJECT"
            item[1].reasons.append(
                "not selected because another candidate was manually accepted"
            )
    else:
        # Automatic scoring still chooses the best compatible, non-rejected
        # candidate. A rejected candidate can be the best remaining row, which
        # correctly produces a no-match result. The quality tie-break keeps
        # higher-quality charts winning when final scores saturate at the cap.
        eligible = [
            item
            for item in scored
            if item[1].decision != "INCOMPATIBLE" and item[2] != "reject"
        ]
        winner = (
            max(eligible, key=lambda item: (item[1].final_score, item[1].quality))
            if eligible
            else None
        )

    if not scored:
        return "NO_MATCH"

    for chart, breakdown, _manual, _forced in scored:
        db.record_match_score(
            track.db_id,
            chart.db_id,
            breakdown.final_score,
            breakdown.decision,
            json.dumps(
                {"explanation": breakdown.explain(), "quality": breakdown.quality}
            ),
        )

    if winner is None:
        return "NO_MATCH"

    return {
        "AUTO_ACCEPT": "AUTO_ACCEPTED",
        "REVIEW": "REVIEW",
        "REJECT": "REJECTED",
    }.get(winner[1].decision, "NO_MATCH")


def _update_search_stats(stats: SearchStats, outcome: str, had_error: bool) -> None:
    if had_error and outcome in ("NO_MATCH", "REJECTED"):
        stats.errors += 1
    elif outcome == "AUTO_ACCEPTED":
        stats.auto += 1
    elif outcome == "REVIEW":
        stats.review += 1
    else:
        stats.no_match += 1


def _track_query_specs(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    tracks: list[Track],
    overrides: Overrides,
    query_titles: dict[str, str],
) -> tuple[dict[str, tuple[str, str, str]], dict[str, list[str]]]:
    """Return (track_key -> query spec, track_key -> source query keys).

    Catalog-first: artists with enough unique primary-artist tracks use one
    artist-catalog query shared by all their tracks; everyone else uses a
    per-song query. The artist search name matches the expansion pass exactly.
    """
    primary_counts = Counter(track.primary_artist_norm for track in tracks)
    display_names: dict[str, str] = {}
    for track in tracks:
        display_names.setdefault(track.primary_artist_norm, track.primary_artist)

    catalog_first = config.search.artist_catalog_first
    min_songs = config.search.artist_catalog_min_songs

    specs: dict[str, tuple[str, str, str]] = {}
    for track in tracks:
        if catalog_first and primary_counts[track.primary_artist_norm] >= min_songs:
            artist_name = overrides.artist_alias(track.primary_artist_norm) or display_names[
                track.primary_artist_norm
            ]
            specs[track.track_key] = ("artist", artist_name, "")
        else:
            specs[track.track_key] = (
                "song",
                track.primary_artist,
                query_titles[track.track_key],
            )

    keys = {
        track_key: [
            db.query_key(kind, source.name, artist.casefold(), title.casefold())
            for source in sources
        ]
        for track_key, (kind, artist, title) in specs.items()
    }
    return specs, keys


def run_exact_search(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    on_progress=None,
    on_track_scored=None,
    on_search_start=None,
) -> SearchStats:
    """Search and score exact playlist songs incrementally.

    Network workers only fetch data. The main thread persists each completed
    query and scores every track waiting on that query as soon as all of its
    enabled sources have resolved.
    """
    stats = SearchStats()
    tracks = db.get_tracks()
    force_accept = {(r.track_key, r.chart_id) for r in overrides.force_accept}
    reject_charts = set(overrides.reject_charts)
    manual_decisions = db.get_manual_decisions()

    # A direct search call must rebuild scores too; run_scan also clears these
    # rows, but callers such as the review tests intentionally skip scan.
    db.clear_match_scores()

    query_titles = {
        track.track_key: overrides.title_alias(track.title) or track.title
        for track in tracks
    }
    track_specs, query_keys = _track_query_specs(
        config, db, sources, tracks, overrides, query_titles
    )
    queries = list({spec for spec in track_specs.values()})

    waiting_tracks: dict[str, set[str]] = {}
    pending: dict[str, set[str]] = {}
    track_by_key = {track.track_key: track for track in tracks}
    for track in tracks:
        keys = set(query_keys[track.track_key])
        pending[track.track_key] = keys
        for key in keys:
            waiting_tracks.setdefault(key, set()).add(track.track_key)

    stats.exact_queries = len(queries)
    stats.exact_tracks = len(tracks)
    progress_state = {
        "started": True,
        "total_tracks": len(tracks),
        "scored_tracks": 0,
        "total_queries": stats.exact_queries,
        "completed_queries": 0,
        "review_tracks": 0,
        "review_candidates": 0,
        "complete": False,
        "started_at": time.time(),
    }
    db.set_meta("exact_progress", json.dumps(progress_state))
    if on_search_start is not None:
        on_search_start(stats.exact_queries, len(tracks))

    outcomes: dict[str, tuple[list, str | None]] = {}
    scored_track_keys: set[str] = set()
    track_outcomes: dict[str, tuple[str, bool, bool]] = {}
    first_wave_total = 0

    def persist_progress() -> None:
        db.set_meta("exact_progress", json.dumps(progress_state))

    def score_one(track: Track) -> None:
        if track.track_key in scored_track_keys:
            return
        scored_track_keys.add(track.track_key)

        kind, artist, title = track_specs[track.track_key]
        found: list[ChartCandidate] = []
        errors: list[str] = []
        for source in sources:
            key = db.query_key(kind, source.name, artist.casefold(), title.casefold())
            candidates, error = outcomes.get(key, ([], None))
            found.extend(candidates)
            if error:
                errors.append(error)

        for error in errors:
            db.record_error("search", error, track.track_key)

        # A source can return the same chart through more than one logical
        # query. Score and persist it once per track.
        unique_candidates: dict[tuple[str, int], ChartCandidate] = {}
        for candidate in found:
            if candidate.chart_id in reject_charts:
                continue
            unique_candidates[(candidate.source, candidate.chart_id)] = candidate
        track_decisions = {
            (source, chart_id): decision
            for (track_key, source, chart_id), decision in manual_decisions.items()
            if track_key == track.track_key
        }
        had_manual = bool(track_decisions)
        forced_ids = {
            chart_id for track_key, chart_id in force_accept if track_key == track.track_key
        }
        outcome = _decide_track(
            config,
            db,
            track,
            list(unique_candidates.values()),
            track_decisions,
            forced_ids,
        )
        rows = db.get_match_scores_for_track(track.db_id)
        review_candidates = sum(1 for row in rows if row["decision"] == "REVIEW")
        had_error = bool(errors)
        _update_search_stats(stats, outcome, had_error)
        track_outcomes[track.track_key] = (outcome, had_error, had_manual)
        stats.review_candidates += review_candidates
        progress_state["scored_tracks"] = len(scored_track_keys)
        progress_state["review_tracks"] = stats.review
        progress_state["review_candidates"] = stats.review_candidates
        persist_progress()
        if on_track_scored is not None:
            on_track_scored(
                len(scored_track_keys), len(tracks), stats.review, stats.review_candidates
            )

    def on_result(key: str, candidates: list, error: str | None) -> None:
        # _resolve_queries has already persisted the query and candidates when
        # this callback runs. Fan the result out to every track sharing it.
        outcomes[key] = (candidates, error)
        for track_key in waiting_tracks.get(key, ()):
            pending[track_key].discard(key)
            if not pending[track_key]:
                score_one(track_by_key[track_key])

    def on_query_progress(done: int, total: int) -> None:
        nonlocal first_wave_total
        first_wave_total = total
        progress_state["completed_queries"] = done
        progress_state["total_queries"] = total
        stats.exact_queries_completed = done
        persist_progress()
        if on_progress is not None:
            on_progress(done, total)

    _resolve_queries(
        config,
        db,
        sources,
        queries,
        "Searching exact playlist songs",
        on_progress=on_query_progress,
        on_result=on_result,
    )

    # With no enabled sources there are no callbacks to drain the pending
    # sets, but every track still needs an explicit no-match outcome.
    for track in tracks:
        if track.track_key not in scored_track_keys:
            score_one(track)

    # Fallback wave: catalog-scored tracks that missed or errored get a
    # per-song query. Manual decisions block fallback.
    fallback_specs: dict[str, tuple[str, str, str]] = {}
    for track in tracks:
        outcome_info = track_outcomes.get(track.track_key)
        if outcome_info is None:
            continue
        outcome, had_error, had_manual = outcome_info
        spec = track_specs[track.track_key]
        if spec[0] == "artist" and not had_manual and (
            outcome in ("REJECTED", "NO_MATCH") or had_error
        ):
            db.delete_match_scores_for_track(track.db_id)
            fallback_specs[track.track_key] = (
                "song",
                track.primary_artist,
                query_titles[track.track_key],
            )

    if fallback_specs:
        fallback_queries = list({spec for spec in fallback_specs.values()})
        for track_key, spec in fallback_specs.items():
            track_specs[track_key] = spec
        new_keys = {
            track_key: [
                db.query_key(kind, source.name, artist.casefold(), title.casefold())
                for source in sources
            ]
            for track_key, (kind, artist, title) in fallback_specs.items()
        }
        query_keys.update(new_keys)
        for track_key in fallback_specs:
            track = track_by_key[track_key]
            keys = set(query_keys[track_key])
            pending[track_key] = keys
            for key in keys:
                waiting_tracks.setdefault(key, set()).add(track_key)
            scored_track_keys.discard(track_key)

        stats.exact_queries += len(fallback_queries)

        def on_fallback_progress(done: int, total: int) -> None:
            progress_state["completed_queries"] = first_wave_total + done
            progress_state["total_queries"] = first_wave_total + total
            stats.exact_queries_completed = first_wave_total + done
            persist_progress()
            if on_progress is not None:
                on_progress(first_wave_total + done, first_wave_total + total)

        _resolve_queries(
            config,
            db,
            sources,
            fallback_queries,
            "Falling back to per-song search",
            on_progress=on_fallback_progress,
            on_result=on_result,
        )

        for track in tracks:
            if track.track_key in fallback_specs and track.track_key not in scored_track_keys:
                score_one(track)

    progress_state["complete"] = True
    persist_progress()
    return stats


def run_expansion_search(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    on_progress=None,
    on_track_scored=None,
    on_search_start=None,
) -> SearchStats:
    """Rebuild exact scores from cache, then run the artist expansion pass."""
    stats = run_exact_search(
        config,
        db,
        sources,
        overrides,
        on_progress=on_progress,
        on_track_scored=on_track_scored,
        on_search_start=on_search_start,
    )
    _run_expansion(config, db, sources, overrides, stats, on_progress)
    return stats


def run_search(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    on_progress=None,
    include_expansion: bool | None = None,
    on_track_scored=None,
    on_search_start=None,
) -> SearchStats:
    if include_expansion is None:
        # Preserve the terminal pipeline's historical default. Web callers
        # pass False for the exact-only first pass.
        include_expansion = config.expansion.enabled

    search_function = run_expansion_search if include_expansion else run_exact_search
    stats = search_function(
        config,
        db,
        sources,
        overrides,
        on_progress=on_progress,
        on_track_scored=on_track_scored,
        on_search_start=on_search_start,
    )

    db.set_meta("search_stats", json.dumps(vars(stats)))
    return stats


def _run_expansion(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    stats: SearchStats,
    on_progress=None,
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

    queries = [
        ("artist", overrides.artist_alias(a["artist_norm"]) or a["artist_name"], "")
        for a in eligible
    ]
    outcomes = _resolve_queries(
        config, db, sources, queries, "Searching artist expansion", on_progress
    )

    for artist in eligible:
        search_name = overrides.artist_alias(artist["artist_norm"]) or artist["artist_name"]
        all_candidates, errors = _query_outcomes(
            config, db, sources, outcomes, "artist", search_name
        )
        for error in errors:
            db.record_error("expansion_search", error, artist["artist_norm"])
        accepted = 0
        for candidate in all_candidates:
            breakdown = score_expansion_candidate(artist["artist_norm"], candidate, config)
            db.record_expansion_candidate(
                artist_db_ids[artist["artist_norm"]],
                candidate.db_id,
                breakdown.final_score,
                json.dumps(
                    {"explanation": breakdown.explain(), "quality": breakdown.quality}
                ),
            )
            if breakdown.decision == "AUTO_ACCEPT":
                accepted += 1
        stats.expansion_candidates += accepted


# ---------------------------------------------------------------------- plan
def run_plan(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    on_progress=None,
    include_expansion: bool | None = None,
) -> PlanStats:
    def size_lookup(candidate: ChartCandidate) -> int | None:
        for source in sources:
            if source.name == candidate.source and hasattr(source, "head_size"):
                return source.head_size(candidate)
        return None

    # Real download sizes make the storage estimate accurate; failures fall
    # back to the conservative estimate from config.
    plan = build_plan(
        config, db, size_lookup=size_lookup,
        workers=max(1, config.network.concurrency), on_progress=on_progress,
        include_expansion=include_expansion,
    )

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


# ---------------------------------------------------------------- relocation
@dataclass
class RelocateStats:
    checked: int = 0
    moved: int = 0
    errors: int = 0


def run_relocation(config: AppConfig, db: Database, on_progress=None) -> RelocateStats:
    """Move completed downloads into the active layout.

    Runs after planning (fresh target paths) and before downloads. Per-file
    atomic rename with the DB path updated immediately after each move, so an
    interrupted run resumes cleanly and a second run is a no-op.
    """
    stats = RelocateStats()
    root = config.library_root.resolve()
    selections = db.get_selections(status="selected")
    done = 0
    for row in selections:
        done += 1
        if on_progress is not None:
            on_progress(done, len(selections))
        download = db.get_download_for_candidate(row["candidate_id"])
        if (
            download is None
            or download["status"] != "completed"
            or not download["path"]
        ):
            continue
        stats.checked += 1
        current = Path(download["path"])
        target = (root / row["target_relpath"]).resolve()
        if str(current).casefold() == str(target).casefold():
            continue
        try:
            if target.exists():
                # A previous run moved the file but stopped before the DB
                # update; just repoint the row.
                db.update_download_path(row["candidate_id"], str(target))
            elif current.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(current, target)  # atomic, same volume
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.move(str(current), str(target))  # cross-volume copy
                db.update_download_path(row["candidate_id"], str(target))
                stats.moved += 1
            # else: file deleted externally; leave the row so the download
            # step re-fetches into the new location.
        except OSError as exc:
            stats.errors += 1
            db.record_error("relocate", str(exc), str(current))
    return stats


def cleanup_empty_folders(config: AppConfig) -> int:
    """Remove empty folders left under the library root after relocation.

    Only empty directories are removed (rmdir never touches content), and the
    library root itself is always kept. Covers the legacy section folders and
    any artist/album/pack dirs emptied by a layout change.
    """
    root = config.library_root.resolve()
    if not root.is_dir():
        return 0
    removed = 0
    # Bottom-up so emptied parent folders are collected too.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            path.rmdir()  # succeeds only when the folder is empty
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------- build
def run_downloads(
    config: AppConfig, db: Database, sources: list[ChartSource], on_progress=None
) -> tuple[int, int, int]:
    """Download all selected charts. Returns (downloaded, errors, bytes)."""
    manager = DownloadManager(config, db)
    source_by_name = {s.name: s for s in sources}
    selections = db.get_selections(status="selected")

    downloaded = errors = bytes_total = done = 0
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
            done += 1
            if on_progress is not None:
                on_progress(done, len(selections))
    return downloaded, errors, bytes_total


def run_build(
    config: AppConfig,
    db: Database,
    sources: list[ChartSource],
    overrides: Overrides,
    dry_run: bool,
    include_expansion: bool | None = None,
    on_progress=None,
    on_track_scored=None,
    on_search_start=None,
) -> BuildResult:
    """Full pipeline with friendly console output."""
    result = BuildResult()
    if include_expansion is None:
        include_expansion = config.expansion.enabled

    console.print("[bold][1/5] Reading Spotify playlists...[/bold]")
    result.scan = run_scan(config, db, overrides)
    console.print(
        f"  {result.scan.csv_files} playlist CSVs, {result.scan.total_rows} rows, "
        f"[green]{result.scan.unique_tracks} unique tracks[/green], "
        f"{result.scan.primary_artists} primary artists"
    )

    console.print("[bold][2/5] Searching chart sources...[/bold]")
    result.search = run_search(
        config,
        db,
        sources,
        overrides,
        on_progress=on_progress,
        include_expansion=include_expansion,
        on_track_scored=on_track_scored,
        on_search_start=on_search_start,
    )
    console.print(
        f"  Exact songs: [green]{result.search.auto} auto matches[/green], "
        f"[yellow]{result.search.review} review[/yellow], "
        f"[red]{result.search.no_match} no match[/red]"
        + (f", [red]{result.search.errors} errors[/red]" if result.search.errors else "")
    )
    if include_expansion:
        console.print(
            f"  Artist expansion: {result.search.expansion_candidates} quality candidates "
            f"across {result.search.expansion_artists} artists"
    )

    console.print("[bold][3/5] Planning library...[/bold]")
    result.plan = run_plan(config, db, sources, include_expansion=include_expansion)
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
        relocated = run_relocation(config, db)
        if relocated.moved or relocated.errors:
            console.print(
                f"  Relocated {relocated.moved} existing charts to the current layout"
                + (f", [red]{relocated.errors} errors[/red]" if relocated.errors else "")
            )
        result.downloaded, result.download_errors, result.download_bytes = run_downloads(
            config, db, sources
        )
        removed = cleanup_empty_folders(config)
        console.print(
            f"  [green]{result.downloaded} downloaded[/green]"
            + (
                f", [red]{result.download_errors} errors[/red]"
                if result.download_errors
                else ""
            )
            + f" ({result.download_bytes / (1024**3):.2f} GiB)"
            + (f", {removed} empty folders removed" if removed else "")
        )

    console.print("[bold][5/5] Writing reports...[/bold]")
    from csv_to_clonehero.reporting import generate_reports

    paths = generate_reports(config, db)
    console.print(f"  Reports written to [cyan]{config.reports_path}[/cyan] ({len(paths)} files)")
    console.print(f"  Library folder: [cyan]{config.library_root}[/cyan]")
    return result
