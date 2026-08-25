"""Import playlist exports: Spotify CSVs and iTunes/Apple Music exports.

Supported inputs:

* ``.csv`` - Spotify playlist exports (Chosic/Exportify style); column order
  does not matter and common header aliases are recognized.
* ``.txt`` - iTunes/Music tab-delimited playlist export (``Name``, ``Artist``,
  ``Album`` columns).
* ``.xml`` - iTunes/Music XML library/playlist plist (stdlib ``plistlib``).

CSV hardening: UTF-8 BOM aware decoding with legacy-encoding fallbacks, and
comma/tab delimiter sniffing. iTunes files have no playlist column, so the
playlist label is the filename stem.
"""

from __future__ import annotations

import csv
import io
import logging
import plistlib
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

SUPPORTED_EXTENSIONS = {".csv", ".txt", ".xml"}

# iTunes XML libraries can be tens of MB; refuse pathological files rather
# than let plistlib chew on gigabytes of XML in memory.
MAX_XML_BYTES = 50 * 1024 * 1024

# Header aliases (compared lowercase, stripped). Includes iTunes column names.
TITLE_HEADERS = {"song", "track name", "name", "title", "track", "song name"}
ARTIST_HEADERS = {
    "artist", "artists", "artist name", "artist names", "artist(s)",
    "artist name(s)", "artists name", "artist names(s)",
}
ALBUM_HEADERS = {"album", "album name"}
TRACK_ID_HEADERS = {"spotify track id", "track id", "spotify id", "id"}
URI_HEADERS = {"spotify uri", "uri", "track uri", "spotify track uri", "spotify track url"}
PLAYLIST_HEADERS = {"playlist", "playlist name"}

# Decode order: UTF-8 (BOM-aware) first, then the legacy Windows code page
# iTunes/Music may emit, then a lossless last resort.
_FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


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


def _read_text(path: Path) -> str:
    """Decode a text export, trying UTF-8 (BOM-aware) then legacy encodings."""
    raw = path.read_bytes()
    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    """Pick comma vs. tab from the header line (comma wins ties)."""
    header = next((line for line in sample.splitlines() if line.strip()), "")
    return "\t" if header.count("\t") > header.count(",") else ","


def _build_track(
    title: str,
    artist_full: str,
    album: str,
    playlist: str,
    spotify_id: str = "",
) -> Track:
    primary, featured = split_artists(artist_full)
    title_norm = normalize_title(title)
    primary_norm = normalize_artist(primary)
    return Track(
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


def import_csv_file(path: Path) -> list[Track]:
    """Parse one delimited file (.csv or iTunes tab-delimited .txt)."""
    tracks: list[Track] = []
    text = _read_text(path)
    delimiter = _sniff_delimiter(text[:8192])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
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
        spotify_id = extract_spotify_id(
            row.get(track_id_col, "") if track_id_col else "",
            row.get(uri_col, "") if uri_col else "",
        )
        album = (row.get(album_col) or "").strip() if album_col else ""
        playlist = (
            (row.get(playlist_col) or "").strip() if playlist_col else ""
        ) or default_playlist
        tracks.append(_build_track(title, artist_full, album, playlist, spotify_id))
    return tracks


def import_itunes_xml(path: Path) -> list[Track]:
    """Parse an iTunes/Music XML library (plist ``Tracks`` dict)."""
    size = path.stat().st_size
    if size > MAX_XML_BYTES:
        raise ValueError(
            f"{path.name}: iTunes XML too large "
            f"({size / (1024 * 1024):.0f} MB; limit {MAX_XML_BYTES // (1024 * 1024)} MB)"
        )
    try:
        with open(path, "rb") as fh:
            plist = plistlib.load(fh)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise ValueError(f"{path.name}: not a valid iTunes XML plist ({exc})") from exc
    if not isinstance(plist, dict) or not isinstance(plist.get("Tracks"), dict):
        raise ValueError(f"{path.name}: no Tracks dict found (not an iTunes library XML?)")

    default_playlist = path.stem
    tracks: list[Track] = []
    for entry in plist["Tracks"].values():
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("Name") or "").strip()
        artist_full = str(entry.get("Artist") or entry.get("Album Artist") or "").strip()
        album = str(entry.get("Album") or "").strip()
        if not title or not artist_full:
            continue
        tracks.append(_build_track(title, artist_full, album, default_playlist))
    return tracks


def import_playlist_file(path: Path) -> list[Track]:
    """Dispatch on extension: iTunes XML plist, or delimited text (.csv/.txt)."""
    if path.suffix.lower() == ".xml":
        return import_itunes_xml(path)
    return import_csv_file(path)


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


def import_playlist_dir(input_dir: Path, skip: set[str] | None = None) -> ImportResult:
    """Import every supported playlist file in the directory, deduplicated.

    ``skip`` is a set of basenames to ignore entirely (file picker disables).
    """
    skip = skip or set()
    files = sorted(
        (p for p in input_dir.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    ) if input_dir.exists() else []
    if not files:
        log.warning("No playlist files (.csv/.txt/.xml) found in %s", input_dir)

    unique: dict[str, Track] = {}
    file_row_counts: dict[str, int] = {}
    total_rows = 0
    skipped_rows = 0

    for path in files:
        if path.name in skip:
            log.info("Skipping disabled playlist file: %s", path.name)
            continue
        try:
            rows = import_playlist_file(path)
        except ValueError as exc:
            log.error("Skipping %s: %s", path.name, exc)
            skipped_rows += 1
            continue
        file_row_counts[path.name] = len(rows)
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
