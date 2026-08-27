"""Report generation: human- and machine-readable summaries in reports/."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database, _candidate_from_json

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
    """Write the legacy build reports from the current database snapshot.

    Reports are derived output: regenerating them never changes matching or
    download state, and the destination remains separate from the Songs folder.
    """
    reports_dir = config.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _add(path: Path) -> Path:
        written.append(path)
        return path

    tracks = {t.db_id: t for t in db.get_tracks(include_ignored=True)}
    scores = db.get_match_scores()

    # Prefer the current winning decision over raw score. A definitive manual
    # accept may intentionally have a lower automatic score than a rejected
    # alternative.
    def _row_rank(row) -> tuple[int, float]:
        rank = {"AUTO_ACCEPT": 0, "REVIEW": 1, "REJECT": 2, "INCOMPATIBLE": 3}
        return rank.get(row["decision"], 4), -row["score"]

    best_per_track: dict[int, object] = {}
    for row in scores:
        current = best_per_track.get(row["track_id"])
        if current is None or _row_rank(row) < _row_rank(current):
            best_per_track[row["track_id"]] = row

    # ---- master list (artist -> album -> song with match state) ------------
    try:
        exact_progress = json.loads(db.get_meta("exact_progress", "{}"))
    except (TypeError, ValueError):
        exact_progress = {}
    search_started = bool(exact_progress.get("started")) or bool(scores)

    master_rows: list[dict] = []
    master_tree = db.get_master_list()
    for artist, albums in master_tree.items():
        for album, songs in albums.items():
            for track in songs:
                best = best_per_track.get(track.db_id)
                match_status = ""
                selected_source = ""
                selected_chart_id = ""
                score = ""
                if search_started:
                    if best is not None and best["decision"] == "AUTO_ACCEPT":
                        match_status = "auto"
                        winner = db.get_candidate(best["candidate_id"])
                        if winner is not None:
                            selected_source = winner.source
                            selected_chart_id = winner.chart_id
                            score = round(best["score"], 1)
                    elif best is not None and best["decision"] == "REVIEW":
                        match_status = "review"
                    else:
                        match_status = "no match"
                master_rows.append(
                    {
                        "artist": artist,
                        "album": album,
                        "title": track.title,
                        "playlists": ", ".join(track.playlists),
                        "match_status": match_status,
                        "selected_source": selected_source,
                        "selected_chart_id": selected_chart_id,
                        "score": score,
                    }
                )
    _write_csv(
        _add(reports_dir / "master_list.csv"),
        ["artist", "album", "title", "playlists", "match_status",
         "selected_source", "selected_chart_id", "score"],
        master_rows,
    )

    exact_rows: list[dict] = []
    review_candidate_rows: list[dict] = []
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
            review_candidate_rows.append(base)
        else:
            rejected_rows.append(base)

    review_rows: list[dict] = []
    review_groups: dict[str, list[dict]] = {}
    auto_track_keys = {
        row["track_key"] for row in scores if row["decision"] == "AUTO_ACCEPT"
    }
    for row in review_candidate_rows:
        if row["track_key"] not in auto_track_keys:
            review_groups.setdefault(row["track_key"], []).append(row)
    for rows in review_groups.values():
        rows.sort(key=lambda item: -item["score"])
        best = dict(rows[0])
        best["alternatives"] = " | ".join(
            f"{item['candidate_artist']} - {item['candidate_title']} "
            f"({item['score']:.1f}, {item['source']}:{item['chart_id']})"
            for item in rows[1:]
        )
        review_rows.append(best)

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
          "playlists", "track_key", "alternatives"],
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
    manual_decisions = db.get_manual_decisions()
    manual_accepts = sum(1 for decision in manual_decisions.values() if decision == "accept")
    manual_rejects = sum(1 for decision in manual_decisions.values() if decision == "reject")

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
            "review_tracks": len(review_rows),
            "review_candidates": len(review_candidate_rows),
            "no_match": search_stats.get("no_match", 0),
            "errors": search_stats.get("errors", 0),
        },
        "expansion": {
            "artists_expanded": search_stats.get("expansion_artists", 0),
            "quality_candidates": search_stats.get("expansion_candidates", 0),
            "selected": plan_stats.get("expansion_selected", 0),
        },
        "review": {
            "tracks": len(review_rows),
            "candidates": len(review_candidate_rows),
            "manual_accepts": manual_accepts,
            "manual_rejects": manual_rejects,
        },
        "manual_decisions": {
            "accept": manual_accepts,
            "reject": manual_rejects,
            "total": len(manual_decisions),
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
        f"  {summary['review']['tracks']} review tracks / "
        f"{summary['review']['candidates']} review candidates",
        f"  {summary['exact']['no_match']} no match",
    ]
    if summary["exact"]["errors"]:
        lines.append(f"  {summary['exact']['errors']} errors")
    lines += [
        "",
        "Manual decisions:",
        f"  {summary['manual_decisions']['accept']} accepts",
        f"  {summary['manual_decisions']['reject']} rejects",
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
    master_artist_count = len(master_tree)
    master_album_count = sum(len(albums) for albums in master_tree.values())
    lines += [
        "",
        f"Master list: {master_artist_count} artists, {master_album_count} albums, "
        f"{len(master_rows)} songs (see master_list.csv)",
    ]
    lines.append(f"Library folder: {summary['library_root']}")
    (_add(reports_dir / "summary.txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return written


def generate_spotify_reports(
    config: AppConfig,
    db: Database,
    workspace_id: int | str | None = None,
) -> list[Path]:
    """Write a compact, export-safe report for a Spotify discovery workspace.

    Spotify artwork is omitted from the CSV files. Explicit Spotify/source
    reference URLs may be included for navigation, but tokens, audio URLs, and
    credentials are not. The report contains library identity, source-owned
    candidate metadata, terminal import statuses, and export ownership records.
    """
    reports_dir = config.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    tracks = db.get_spotify_tracks(True)
    track_by_id = {track.db_id: track for track in tracks}
    scope = db.get_workspace_scope_data(workspace_id) if workspace_id is not None else None
    owned_ids = db.get_workspace_owned_track_ids(workspace_id) if workspace_id is not None else set()
    library_rows = [
        {
            "spotify_id": track.spotify_id,
            "track_key": track.track_key,
            "artist": track.artist_full,
            "title": track.title,
            "album": track.album,
            "playlists": ", ".join(track.playlists),
            "duration_ms": track.duration_ms or "",
            "isrc": track.isrc,
            "spotify_url": track.spotify_url,
            "already_owned": int(track.db_id in owned_ids),
        }
        for track in tracks
    ]
    library_path = reports_dir / "spotify_library.csv"
    _write_csv(
        library_path,
        [
            "spotify_id", "track_key", "artist", "title", "album", "playlists",
            "duration_ms", "isrc", "spotify_url", "already_owned",
        ],
        library_rows,
    )
    written.append(library_path)

    status_rows = [dict(row) for row in db.get_spotify_item_statuses(workspace_id)]
    status_path = reports_dir / "spotify_item_statuses.csv"
    _write_csv(
        status_path,
        ["source", "container_id", "item_key", "status", "reason", "title", "occurred_at"],
        status_rows,
    )
    written.append(status_path)

    candidate_rows: list[dict] = []
    if workspace_id is not None:
        for row in db.get_workspace_candidates(workspace_id, include_rejected=True):
            candidate_json = row["candidate_json"] if "candidate_json" in row.keys() else None
            if candidate_json:
                try:
                    candidate = _candidate_from_json(row["candidate_id"], candidate_json)
                except Exception:
                    candidate = db.get_candidate(row["candidate_id"])
            else:
                candidate = db.get_candidate(row["candidate_id"])
            track = track_by_id.get(row["track_id"])
            if candidate is None:
                continue
            breakdown = _load_json_text(row["breakdown_json"])
            candidate_rows.append(
                {
                    "spotify_artist": track.artist_full if track else "",
                    "spotify_title": track.title if track else "",
                    "spotify_id": track.spotify_id if track else "",
                    "candidate_artist": candidate.artist,
                    "candidate_title": candidate.name,
                    "source": candidate.source,
                    "chart_id": candidate.chart_id,
                    "score": row["score"],
                    "quality": row["quality"],
                    "decision": row["decision"],
                    "eligibility": row["eligibility"],
                    "reasons": "; ".join(breakdown.get("eligibility_reasons", [])),
                    "source_artwork_url": candidate.source_artwork_url,
                    "reference": candidate.reference,
                }
            )
    candidate_path = reports_dir / "spotify_candidates.csv"
    _write_csv(
        candidate_path,
        [
            "spotify_artist", "spotify_title", "spotify_id", "candidate_artist",
            "candidate_title", "source", "chart_id", "score", "quality",
            "decision", "eligibility", "reasons", "source_artwork_url", "reference",
        ],
        candidate_rows,
    )
    written.append(candidate_path)

    export_rows: list[dict] = []
    for export in db.list_export_records(workspace_id):
        export_data = dict(export)
        export_data["metadata"] = _load_json_text(export_data.get("metadata_json"))
        export_data["manifest"] = _load_json_text(export_data.get("manifest_json"))
        export_data.pop("metadata_json", None)
        export_data.pop("manifest_json", None)
        export_rows.append(export_data)
    export_path = reports_dir / "spotify_exports.json"
    export_path.write_text(json.dumps(export_rows, indent=2, default=str), encoding="utf-8")
    written.append(export_path)

    scans = [dict(row) for row in db.get_discovery_scans(workspace_id)] if workspace_id is not None else []
    scan_path = reports_dir / "spotify_scan.json"
    scan_path.write_text(
        json.dumps({
            "workspace_id": workspace_id,
            "scope": scope,
            "suppressed_tracks": len(owned_ids),
            "scans": scans,
        }, indent=2, default=str),
        encoding="utf-8",
    )
    written.append(scan_path)
    return written


def _load_json_text(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


spotify_reports = generate_spotify_reports
