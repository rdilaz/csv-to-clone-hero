"""Report generation: human- and machine-readable summaries in reports/."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database

log = logging.getLogger(__name__)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _candidate_fields(candidate) -> dict:
    return {
        "candidate_artist": candidate.artist,
        "candidate_title": candidate.name,
        "source": candidate.source,
        "chart_id": candidate.chart_id,
        "charter": candidate.charter,
        "album": candidate.album,
        "genre": candidate.genre,
        "year": candidate.year,
        "instruments": ", ".join(candidate.instruments),
        "diff_guitar": candidate.diff_guitar,
        "modchart": candidate.modchart,
        "has_issues": candidate.has_issues,
        "icon": candidate.icon,
        "pack_name": candidate.pack_name,
        "reference": candidate.reference,
    }


def generate_reports(config: AppConfig, db: Database) -> list[Path]:
    reports_dir = config.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _add(path: Path) -> Path:
        written.append(path)
        return path

    tracks = {t.db_id: t for t in db.get_tracks(include_ignored=True)}
    scores = db.get_match_scores()

    # Best score per track decides the track outcome.
    best_per_track: dict[int, object] = {}
    for row in scores:
        current = best_per_track.get(row["track_id"])
        if current is None or row["score"] > current["score"]:
            best_per_track[row["track_id"]] = row

    exact_rows: list[dict] = []
    review_rows: list[dict] = []
    rejected_rows: list[dict] = []

    for row in scores:
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is None:
            continue
        track = tracks.get(row["track_id"])
        breakdown = json.loads(row["breakdown_json"])
        base = {
            "spotify_artist": track.artist_full if track else "",
            "spotify_title": track.title if track else "",
            "track_key": row["track_key"],
            "playlists": ", ".join(track.playlists) if track else "",
            "score": round(row["score"], 1),
            "decision": row["decision"],
            "explanation": breakdown.get("explanation", ""),
            **_candidate_fields(candidate),
        }
        if row["decision"] == "AUTO_ACCEPT":
            exact_rows.append(base)
        elif row["decision"] == "REVIEW":
            review_rows.append(base)
        else:
            rejected_rows.append(base)

    # Tracks with no candidates at all, whose query errored, or where every
    # candidate was rejected below the review threshold.
    no_match_rows: list[dict] = []
    error_rows: list[dict] = []
    for track in tracks.values():
        best = best_per_track.get(track.db_id)
        if best is not None and best["decision"] in ("AUTO_ACCEPT", "REVIEW"):
            continue
        note = (
            "no chart candidates found (or all incompatible)"
            if best is None
            else "all candidates rejected below the review threshold - see rejected_candidates.csv"
        )
        no_match_rows.append(
            {
                "spotify_artist": track.artist_full,
                "spotify_title": track.title,
                "track_key": track.track_key,
                "playlists": ", ".join(track.playlists),
                "note": note,
            }
        )

    for err in db.get_errors():
        error_rows.append(
            {"stage": err["stage"], "message": err["message"], "detail": err["detail"]}
        )

    # Expansion selections.
    expansion_rows: list[dict] = []
    downloaded_rows: list[dict] = []
    skipped_rows: list[dict] = []
    selections = db.get_selections()
    downloads = {d["candidate_id"]: d for d in db.get_downloads()}
    for sel in selections:
        candidate = db.get_candidate(sel["candidate_id"])
        if candidate is None:
            continue
        row = {
            "kind": sel["kind"],
            "score": round(sel["score"], 1),
            "planned_bytes": sel["planned_bytes"],
            "target_relpath": sel["target_relpath"],
            "status": sel["status"],
            "explanation": sel["explanation"],
            **_candidate_fields(candidate),
        }
        download = downloads.get(sel["candidate_id"])
        if download:
            row["download_status"] = download["status"]
            row["local_path"] = download["path"]
            row["bytes"] = download["bytes"]
            row["download_error"] = download["error"]
        if sel["status"] == "skipped_for_space":
            skipped_rows.append(row)
        elif sel["kind"] == "expansion":
            expansion_rows.append(row)
        if download and download["status"] == "completed":
            downloaded_rows.append(row)

    exact_rows.sort(key=lambda r: (r["spotify_artist"], r["spotify_title"]))
    review_rows.sort(key=lambda r: -r["score"])
    expansion_rows.sort(key=lambda r: (r["candidate_artist"], r["candidate_title"]))

    _write_csv(
        _add(reports_dir / "exact_matches.csv"),
        ["spotify_artist", "spotify_title", "candidate_artist", "candidate_title", "source",
         "chart_id", "score", "decision", "explanation", "charter", "album", "genre", "year",
         "instruments", "diff_guitar", "reference", "playlists", "track_key"],
        exact_rows,
    )
    _write_csv(
        _add(reports_dir / "artist_expansion.csv"),
        ["candidate_artist", "candidate_title", "source", "chart_id", "score", "kind", "status",
         "download_status", "explanation", "charter", "album", "genre", "year", "instruments",
         "diff_guitar", "planned_bytes", "target_relpath", "reference"],
        expansion_rows,
    )
    _write_csv(
        _add(reports_dir / "review_needed.csv"),
        ["spotify_artist", "spotify_title", "candidate_artist", "candidate_title", "source",
         "chart_id", "score", "decision", "explanation", "reference", "instruments",
         "diff_guitar", "charter", "album", "genre", "year", "modchart", "has_issues",
         "playlists", "track_key"],
        review_rows,
    )
    _write_csv(
        _add(reports_dir / "no_matches.csv"),
        ["spotify_artist", "spotify_title", "track_key", "playlists", "note"],
        no_match_rows,
    )
    _write_csv(
        _add(reports_dir / "rejected_candidates.csv"),
        ["spotify_artist", "spotify_title", "candidate_artist", "candidate_title", "source",
         "chart_id", "score", "decision", "explanation", "reference", "instruments",
         "charter", "album", "track_key"],
        rejected_rows,
    )
    _write_csv(
        _add(reports_dir / "downloaded.csv"),
        ["candidate_artist", "candidate_title", "kind", "score", "local_path", "bytes",
         "source", "chart_id", "charter", "reference"],
        downloaded_rows,
    )
    _write_csv(
        _add(reports_dir / "download_errors.csv"),
        ["stage", "message", "detail"],
        error_rows,
    )
    _write_csv(
        _add(reports_dir / "skipped_for_space.csv"),
        ["candidate_artist", "candidate_title", "kind", "score", "planned_bytes",
         "source", "chart_id", "reference", "explanation"],
        skipped_rows,
    )

    # ---------------------------------------------------------------- summary
    scan_stats = json.loads(db.get_meta("scan_stats", "{}"))
    search_stats = json.loads(db.get_meta("search_stats", "{}"))
    plan_stats = json.loads(db.get_meta("plan_stats", "{}"))

    total_downloaded_bytes = sum(d["bytes"] for d in db.get_downloads(status="completed"))
    planned_bytes = plan_stats.get("planned_bytes", 0)
    budget = config.storage.max_library_bytes

    summary = {
        "csv_files": scan_stats.get("csv_files", 0),
        "total_rows": scan_stats.get("total_rows", 0),
        "unique_tracks": scan_stats.get("unique_tracks", 0),
        "primary_artists": scan_stats.get("primary_artists", 0),
        "exact": {
            "auto_matches": search_stats.get("auto", 0),
            "review": search_stats.get("review", 0),
            "no_match": search_stats.get("no_match", 0),
            "errors": search_stats.get("errors", 0),
        },
        "expansion": {
            "artists_expanded": search_stats.get("expansion_artists", 0),
            "quality_candidates": search_stats.get("expansion_candidates", 0),
            "selected": plan_stats.get("expansion_selected", 0),
        },
        "plan": {
            "exact_selected": plan_stats.get("exact_selected", 0),
            "expansion_selected": plan_stats.get("expansion_selected", 0),
            "skipped_for_space": plan_stats.get("skipped_for_space", 0),
            "estimated_bytes": planned_bytes,
            "estimated_gib": round(planned_bytes / (1024**3), 2),
        },
        "downloads": {
            "completed": len(db.get_downloads(status="completed")),
            "errors": len(db.get_downloads(status="error")),
            "bytes_downloaded": total_downloaded_bytes,
            "gib_downloaded": round(total_downloaded_bytes / (1024**3), 2),
        },
        "storage": {
            "budget_gib": config.storage.max_library_gib,
            "estimated_remaining_bytes": max(0, budget - planned_bytes),
        },
        "library_root": str(config.library_root),
    }

    (_add(reports_dir / "summary.json")).write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    lines = [
        "CSV to Clone Hero - build summary",
        "=" * 40,
        f"{summary['csv_files']} playlist CSVs",
        f"{summary['total_rows']} rows",
        f"{summary['unique_tracks']} unique tracks",
        f"{summary['primary_artists']} primary artists",
        "",
        "Exact songs:",
        f"  {summary['exact']['auto_matches']} auto matches",
        f"  {summary['exact']['review']} review",
        f"  {summary['exact']['no_match']} no match",
    ]
    if summary["exact"]["errors"]:
        lines.append(f"  {summary['exact']['errors']} errors")
    lines += [
        "",
        "Artist expansion:",
        f"  {summary['expansion']['selected']} selected candidates",
        "",
        f"Estimated library size: {summary['plan']['estimated_gib']:.1f} GiB",
        f"Downloaded: {summary['downloads']['completed']} charts "
        f"({summary['downloads']['gib_downloaded']:.2f} GiB)",
    ]
    if summary["plan"]["skipped_for_space"]:
        lines.append(f"Skipped for space: {summary['plan']['skipped_for_space']}")
    lines.append(f"Library folder: {summary['library_root']}")
    (_add(reports_dir / "summary.txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return written
