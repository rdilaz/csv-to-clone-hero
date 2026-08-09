"""Selection and storage planning.

Turns scored candidates into an ordered download plan that respects the
configured storage budget and folder hierarchy:

priority tiers:
  0 = exact Spotify playlist matches
  1 = high-evidence artist expansion   (score >= 90)
  2 = medium-evidence artist expansion (score >= 85)
  3 = low-evidence artist expansion    (score >= expansion.min_score)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.filesystem import sanitize_component
from csv_to_clonehero.models import ChartCandidate

log = logging.getLogger(__name__)


def _target_relpath(config: AppConfig, section: str, artist: str, name: str, charter: str) -> Path:
    """<root>/<section>/<Artist>/<Artist> - <Title> [<Charter>].sng"""
    artist_dir = sanitize_component(artist)
    filename = f"{artist} - {name}"
    if charter:
        filename += f" [{charter}]"
    filename = sanitize_component(filename, fallback="chart") + ".sng"
    return Path(config.hierarchy.root_folder) / section / artist_dir / filename


def _expansion_tier(score: float) -> int:
    if score >= 90:
        return 1
    if score >= 85:
        return 2
    return 3


def build_plan(
    config: AppConfig,
    db: Database,
    size_lookup=None,
) -> list[dict]:
    """Build the selection list from scored state.

    ``size_lookup`` is an optional callable(candidate) -> int | None used to
    fetch real download sizes (HEAD requests). When it returns None the
    conservative estimate from config is used.

    Returns the selection dicts (also persisted to the selections table).
    """
    estimate = config.storage.estimated_chart_bytes

    # ---- gather exact-match winners ---------------------------------------
    exact_rows = [
        row
        for row in db.get_match_scores()
        if row["decision"] == "AUTO_ACCEPT"
        or (config.matching.download_review_matches and row["decision"] == "REVIEW")
    ]
    # Best candidate per track only.
    best_per_track: dict[int, object] = {}
    for row in exact_rows:
        current = best_per_track.get(row["track_id"])
        if current is None or row["score"] > current["score"]:
            best_per_track[row["track_id"]] = row

    plan: list[dict] = []
    seen_md5: set[str] = set()
    seen_song: set[str] = set()
    seen_path: set[str] = set()

    def _try_add(kind, candidate, score, explanation, tier, track_id=None, artist_norm=""):
        if candidate.md5 and candidate.md5 in seen_md5:
            return False
        song_key = f"{candidate.artist_norm}|{candidate.name_norm}"
        if song_key in seen_song:
            return False
        section = (
            config.hierarchy.exact_section if kind == "exact" else config.hierarchy.expansion_section
        )
        relpath = _target_relpath(config, section, candidate.artist, candidate.name, candidate.charter)
        path_key = str(relpath).casefold()
        if path_key in seen_path:
            # Same destination from a differently-cased duplicate chart.
            return False
        seen_path.add(path_key)
        seen_song.add(song_key)
        if candidate.md5:
            seen_md5.add(candidate.md5)
        plan.append(
            {
                "kind": kind,
                "track_id": track_id,
                "artist_norm": artist_norm,
                "candidate_id": candidate.db_id,
                "priority_tier": tier,
                "status": "selected",
                "score": score,
                "explanation": explanation,
                "planned_bytes": 0,
                "target_relpath": str(relpath),
                "_candidate": candidate,
            }
        )
        return True

    for row in best_per_track.values():
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is None:
            continue
        _try_add(
            "exact",
            candidate,
            row["score"],
            json.loads(row["breakdown_json"]).get("explanation", ""),
            0,
            track_id=row["track_id"],
        )

    # ---- gather expansion candidates --------------------------------------
    # Group by artist so caps apply per artist; best score per song only.
    expansion_rows = db.get_expansion_candidates()
    by_artist: dict[str, list] = {}
    artist_caps: dict[str, int] = {
        row["artist_norm"]: row["cap"] for row in db.get_expansion_artists()
    }
    best_per_song: dict[tuple[str, str], object] = {}
    for row in expansion_rows:
        if row["score"] < config.expansion.min_score:
            continue
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is None:
            continue
        song_key = (row["artist_norm"], f"{candidate.artist_norm}|{candidate.name_norm}")
        current = best_per_song.get(song_key)
        if current is None or row["score"] > current["score"]:
            best_per_song[song_key] = row

    for (artist_norm, _), row in best_per_song.items():
        by_artist.setdefault(artist_norm, []).append(row)

    expansion_pool: list[dict] = []
    for artist_norm, rows in by_artist.items():
        cap = artist_caps.get(artist_norm, 0)
        rows.sort(key=lambda r: r["score"], reverse=True)
        for row in rows[:cap]:
            expansion_pool.append(row)

    # Sort expansion by evidence tier, then score descending.
    expansion_pool.sort(key=lambda r: (_expansion_tier(r["score"]), -r["score"]))
    for row in expansion_pool:
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is None:
            continue
        _try_add(
            "expansion",
            candidate,
            row["score"],
            json.loads(row["breakdown_json"]).get("explanation", ""),
            _expansion_tier(row["score"]),
            artist_norm=row["artist_norm"],
        )

    # ---- resolve sizes and apply the storage budget ------------------------
    budget = config.storage.max_library_bytes
    used = 0
    for entry in plan:
        candidate = entry["_candidate"]
        size = None
        if size_lookup is not None:
            try:
                size = size_lookup(candidate)
            except Exception as exc:  # network failures are non-fatal here
                log.debug("size lookup failed for %s: %s", candidate.name, exc)
        planned = size if size else estimate
        entry["planned_bytes"] = planned
        if used + planned <= budget:
            used += planned
        else:
            entry["status"] = "skipped_for_space"

    # ---- persist -----------------------------------------------------------
    db.replace_selections([{k: v for k, v in e.items() if not k.startswith("_")} for e in plan])
    return plan
