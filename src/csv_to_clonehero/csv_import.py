"""Import Spotify playlist CSV exports (Chosic/Exportify style).

Column order does not matter; common header aliases are recognized.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from csv_to_clonehero.models import Track
from csv_to_clonehero.normalization import (
    extract_spotify_id,
    make_track_key,
    normalize_artist,
    normalize_title,
    split_artists,
)

log = logging.getLogger(__name__)

# Header aliases (compared lowercase, stripped).
TITLE_HEADERS = {"song", "track name", "name", "title", "track", "song name"}
ARTIST_HEADERS = {
    "artist", "artists", "artist name", "artist names", "artist(s)",
    "artist name(s)", "artists name", "artist names(s)",
}
ALBUM_HEADERS = {"album", "album name"}
TRACK_ID_HEADERS = {"spotify track id", "track id", "spotify id", "id"}
URI_HEADERS = {"spotify uri", "uri", "track uri", "spotify track uri", "spotify track url"}
PLAYLIST_HEADERS = {"playlist", "playlist name"}


@dataclass
class ImportResult:
    tracks: list[Track]
    file_row_counts: dict[str, int] = field(default_factory=dict)
    total_rows: int = 0
    skipped_rows: int = 0


def _match_header(header: str, aliases: set[str]) -> bool:
    return header.strip().lower() in aliases


def _find_column(fieldnames: list[str], aliases: set[str]) -> str | None:
    for name in fieldnames:
        if _match_header(name, aliases):
            return name
    return None


def import_csv_file(path: Path) -> list[Track]:
    """Parse one CSV file into (unmerged) Track objects."""
    tracks: list[Track] = []
    # utf-8-sig handles the BOM some exporters emit.
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        title_col = _find_column(fieldnames, TITLE_HEADERS)
        artist_col = _find_column(fieldnames, ARTIST_HEADERS)
        album_col = _find_column(fieldnames, ALBUM_HEADERS)
        track_id_col = _find_column(fieldnames, TRACK_ID_HEADERS)
        uri_col = _find_column(fieldnames, URI_HEADERS)
        playlist_col = _find_column(fieldnames, PLAYLIST_HEADERS)

        if title_col is None or artist_col is None:
            raise ValueError(
                f"{path.name}: could not find title/artist columns "
                f"(headers: {', '.join(fieldnames)})"
            )

        default_playlist = path.stem
        for row in reader:
            title = (row.get(title_col) or "").strip()
            artist_full = (row.get(artist_col) or "").strip()
            if not title or not artist_full:
                continue
            primary, featured = split_artists(artist_full)
            spotify_id = extract_spotify_id(
                row.get(track_id_col, "") if track_id_col else "",
                row.get(uri_col, "") if uri_col else "",
            )
            title_norm = normalize_title(title)
            primary_norm = normalize_artist(primary)
            album = (row.get(album_col) or "").strip() if album_col else ""
            playlist = (
                (row.get(playlist_col) or "").strip() if playlist_col else ""
            ) or default_playlist

            tracks.append(
                Track(
                    track_key=make_track_key(spotify_id, primary_norm, title_norm),
                    title=title,
                    title_norm=title_norm,
                    artist_full=artist_full,
                    artist_norm=normalize_artist(artist_full),
                    primary_artist=primary,
                    primary_artist_norm=primary_norm,
                    featured_artists=featured,
                    album=album,
                    album_norm=normalize_title(album) if album else "",
                    spotify_id=spotify_id,
                    playlists=[playlist],
                )
            )
    return tracks


def merge_track(existing: Track, incoming: Track) -> None:
    """Merge a duplicate row's metadata into the existing unique track."""
    for playlist in incoming.playlists:
        if playlist not in existing.playlists:
            existing.playlists.append(playlist)
    # Fill in anything the first occurrence lacked.
    if not existing.spotify_id and incoming.spotify_id:
        existing.spotify_id = incoming.spotify_id
    if not existing.album and incoming.album:
        existing.album, existing.album_norm = incoming.album, incoming.album_norm
    for artist in incoming.featured_artists:
        if artist not in existing.featured_artists:
            existing.featured_artists.append(artist)


def import_playlist_dir(input_dir: Path) -> ImportResult:
    """Import every .csv in the directory and deduplicate the tracks."""
    csv_files = sorted(input_dir.glob("*.csv"))
    if not csv_files:
        log.warning("No CSV files found in %s", input_dir)

    unique: dict[str, Track] = {}
    file_row_counts: dict[str, int] = {}
    total_rows = 0
    skipped_rows = 0

    for csv_path in csv_files:
        try:
            rows = import_csv_file(csv_path)
        except ValueError as exc:
            log.error("Skipping %s: %s", csv_path.name, exc)
            skipped_rows += 1
            continue
        file_row_counts[csv_path.name] = len(rows)
        total_rows += len(rows)
        for track in rows:
            existing = unique.get(track.track_key)
            if existing is None:
                unique[track.track_key] = track
            else:
                merge_track(existing, track)

    return ImportResult(
        tracks=list(unique.values()),
        file_row_counts=file_row_counts,
        total_rows=total_rows,
        skipped_rows=skipped_rows,
    )


def artist_track_counts(tracks: list[Track]) -> tuple[dict[str, int], dict[str, int]]:
    """Count unique tracks per artist.

    Returns (primary_counts, featured_only_counts), keyed by normalized artist.
    Artists appear in featured_only only when never a primary artist.
    """
    primary: dict[str, set[str]] = {}
    featured: dict[str, set[str]] = {}
    for track in tracks:
        primary.setdefault(track.primary_artist_norm, set()).add(track.track_key)
        for feat in track.featured_artists:
            featured.setdefault(normalize_artist(feat), set()).add(track.track_key)
    featured_only = {k: v for k, v in featured.items() if k not in primary}
    return (
        {k: len(v) for k, v in primary.items()},
        {k: len(v) for k, v in featured_only.items()},
    )
