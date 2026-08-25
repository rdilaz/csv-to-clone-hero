"""Filesystem safety: Windows filename sanitization, library path layout and
safe archive extraction."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from csv_to_clonehero.models import SINGLES_ALBUM_LABEL

if TYPE_CHECKING:
    from csv_to_clonehero.config import AppConfig
    from csv_to_clonehero.models import ChartCandidate, Track

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Keep individual path components well below the 255-byte limit so full paths
# stay comfortably under Windows' 260-char MAX_PATH for typical library roots.
MAX_COMPONENT_LENGTH = 80

# Chart archives always carry the ``.sng`` suffix, which counts against the
# per-component budget.  Filename builders must truncate the base name so the
# extension fits without pushing a component back over MAX_COMPONENT_LENGTH.
SNG_SUFFIX = ".sng"

# Budget for a path relative to the library root. Clone Hero/YARG scan
# recursively, so the constraint is Windows MAX_PATH, not the games': keep
# absolute paths short even for deep library roots.
MAX_RELPATH_CHARS = 180


def sanitize_component(name: str, fallback: str = "Unknown", *, max_length: int = MAX_COMPONENT_LENGTH) -> str:
    """Sanitize a single path component (file or folder name) for Windows."""
    cleaned = INVALID_CHARS_RE.sub("_", name)
    cleaned = cleaned.strip().rstrip(".").strip()
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned


def sanitize_relative_path(*components: str) -> Path:
    """Build a sanitized relative path from components."""
    return Path(*(sanitize_component(c) for c in components if c))


def casefold_key(path: Path) -> str:
    """Case-insensitive identity for a path (prevents capitalization dupes)."""
    return str(path).casefold()


def _chart_filename(artist: str, title: str, charter: str) -> str:
    """``<Artist> - <Title> [<Charter>].sng`` sanitized for Windows."""
    name = f"{artist} - {title}"
    if charter:
        name += f" [{charter}]"
    base_cap = MAX_COMPONENT_LENGTH - len(SNG_SUFFIX)
    return sanitize_component(name, fallback="chart", max_length=base_cap) + SNG_SUFFIX


def _budgeted_filename(folders: list[str], artist: str, title: str, charter: str) -> str:
    """Build the chart filename within MAX_RELPATH_CHARS for the whole
    relative path. The title truncates first; the charter tag is dropped if
    that is not enough. Artist/album folders stay intact and the per-component
    cap (sanitize_component) remains the final backstop."""
    base_cap = MAX_COMPONENT_LENGTH - len(SNG_SUFFIX)
    overhead = sum(len(folder) + 1 for folder in folders) + len(SNG_SUFFIX)
    prefix = f"{artist} - "
    suffix = f" [{charter}]" if charter else ""
    # The filename is one path component, so the per-component cap bounds the
    # title room too (keeps the charter tag from being cut in half).
    component_room = base_cap - len(prefix) - len(suffix)
    room = min(MAX_RELPATH_CHARS - overhead - len(prefix) - len(suffix), component_room)
    if room < 8 and suffix:
        # Dropping the charter tag buys room for a recognizable title.
        suffix = ""
        room = min(
            MAX_RELPATH_CHARS - overhead - len(prefix),
            base_cap - len(prefix),
        )
    keep = min(len(title), max(8, room))
    short_title = title[:keep].rstrip(" .") or title[:keep] or "chart"
    return sanitize_component(f"{prefix}{short_title}{suffix}", fallback="chart", max_length=base_cap) + SNG_SUFFIX


def destination_path(
    config: AppConfig,
    candidate: ChartCandidate,
    kind: str,
    track: Track | None = None,
) -> Path:
    """Library-relative path for a chart under the configured layout.

    kind is "exact" or "expansion". For exact songs the matched track's
    metadata is canonical (its album groups the folder); the chart's own
    album is only a fallback. Expansion songs file under the chart's pack.
    """
    layout = config.library.layout

    if layout == "legacy":
        section = (
            config.hierarchy.exact_section
            if kind == "exact"
            else config.hierarchy.expansion_section
        )
        artist_dir = sanitize_component(candidate.artist)
        filename = _chart_filename(candidate.artist, candidate.name, candidate.charter)
        return sanitize_relative_path(
            config.hierarchy.root_folder, section, artist_dir, filename
        )

    if kind == "exact" and track is not None:
        artist = track.primary_artist or candidate.artist
        title = track.title or candidate.name
        album = track.album.strip() or candidate.album.strip() or SINGLES_ALBUM_LABEL
    else:
        artist = candidate.artist
        title = candidate.name
        album = candidate.album.strip() or SINGLES_ALBUM_LABEL

    use_pack = layout == "pack" or (layout == "hybrid" and kind == "expansion")
    if use_pack:
        folders = [candidate.pack_name.strip() or artist]
    else:
        folders = [artist, album]

    folders = [sanitize_component(folder) for folder in folders]
    # _budgeted_filename already sanitizes; re-sanitizing would let the
    # per-component cap eat the ".sng" extension.
    filename = _budgeted_filename(folders, artist, title, candidate.charter)
    return Path(*folders, filename)


def safe_extract_zip(archive_path: Path, destination: Path) -> list[Path]:
    """Extract a zip archive, rejecting path-traversal entries.

    Returns the list of extracted file paths. Raises ValueError on unsafe
    archives; nothing is written for rejected entries.
    """
    destination = destination.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            # Normalize and check the target stays inside destination.
            member_name = info.filename.replace("\\", "/")
            if member_name.startswith("/") or ":" in member_name.split("/")[0]:
                raise ValueError(f"Unsafe archive member (absolute path): {info.filename!r}")
            target = (destination / member_name).resolve()
            if not str(target).startswith(str(destination) + "\\") and target != destination:
                # Also allow POSIX separators when running tests elsewhere.
                if not str(target).startswith(str(destination) + "/"):
                    raise ValueError(f"Unsafe archive member (path traversal): {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            extracted.append(target)
    return extracted


def is_within(child: Path, parent: Path) -> bool:
    """True if child resolves to a location inside parent."""
    child = child.resolve()
    parent = parent.resolve()
    return child == parent or parent in child.parents
