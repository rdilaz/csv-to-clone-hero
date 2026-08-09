"""Filesystem safety: Windows filename sanitization and safe archive extraction."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Keep individual path components well below the 255-byte limit so full paths
# stay comfortably under Windows' 260-char MAX_PATH for typical library roots.
MAX_COMPONENT_LENGTH = 80


def sanitize_component(name: str, fallback: str = "Unknown") -> str:
    """Sanitize a single path component (file or folder name) for Windows."""
    cleaned = INVALID_CHARS_RE.sub("_", name)
    cleaned = cleaned.strip().rstrip(".").strip()
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > MAX_COMPONENT_LENGTH:
        cleaned = cleaned[:MAX_COMPONENT_LENGTH].rstrip(" .")
    return cleaned


def sanitize_relative_path(*components: str) -> Path:
    """Build a sanitized relative path from components."""
    return Path(*(sanitize_component(c) for c in components if c))


def casefold_key(path: Path) -> str:
    """Case-insensitive identity for a path (prevents capitalization dupes)."""
    return str(path).casefold()


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
