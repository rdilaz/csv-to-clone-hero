"""Read-only inventory of local Clone Hero ``.sng`` archives.

The inventory is deliberately independent of the database and web layers.  It
only observes the filesystem and validated archive metadata, so callers can
use the result for a UI, a database import, or a one-off diagnostic.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from csv_to_clonehero.exporter import SNGPKG_MAGIC, SngValidation, parse_sngpkg_metadata, validate_sng
from csv_to_clonehero.normalization import normalize_artist, normalize_title


_REPARSE_POINT = 0x0400
_FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
_TEXT_LIMIT = 4096
_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_DURATION_RE = re.compile(r"^(\d{1,4}):([0-5]\d)(?::([0-5]\d))?$")


@dataclass(frozen=True)
class InventoryLimits:
    """Safety and work limits for :func:`scan_library`.

    ``max_files`` limits discovered ``.sng`` files, not all directory entries.
    ``max_depth`` is measured below ``root``.  Archive limits are passed to
    :func:`csv_to_clonehero.exporter.validate_sng` unchanged.
    """

    max_files: int = 100_000
    max_depth: int = 64
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_bytes: int = 0
    max_metadata_bytes: int = 1 * 1024 * 1024
    max_members: int = 4096
    max_member_bytes: int = 512 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 10_000.0

    def __post_init__(self) -> None:
        if self.max_files < 0 or self.max_depth < 0:
            raise ValueError("max_files and max_depth must be non-negative")
        if self.max_file_bytes <= 0 or self.max_metadata_bytes <= 0:
            raise ValueError("file and metadata limits must be positive")
        if self.max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative")
        if self.max_members <= 0 or self.max_member_bytes <= 0 or self.max_archive_bytes <= 0:
            raise ValueError("archive limits must be positive")
        if not math.isfinite(self.max_compression_ratio) or self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be positive and finite")


@dataclass(frozen=True)
class InventoryProgress:
    """A point-in-time scan update delivered to the optional callback."""

    discovered: int = 0
    processed: int = 0
    valid: int = 0
    invalid: int = 0
    skipped: int = 0
    current: Path | None = None
    done: bool = False

    @property
    def total(self) -> int:
        return self.discovered


@dataclass
class InventoryEntry:
    """One discovered ``.sng`` file and the metadata safely read from it."""

    root: Path
    path: Path
    size: int = 0
    mtime: float = 0.0
    sha256: str | None = None
    validation: SngValidation | None = None
    artist: str = ""
    artist_norm: str = ""
    title: str = ""
    title_norm: str = ""
    album: str = ""
    album_norm: str = ""
    duration_ms: int | None = None
    parser_source: str = ""
    diagnostics: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.validation and self.validation.valid)

    @property
    def accepted(self) -> bool:
        """Alias useful to callers that treat invalid files as rejected."""
        return self.valid

    @property
    def relative_path(self) -> Path:
        try:
            return self.path.relative_to(self.root)
        except ValueError:
            return self.path

    @property
    def size_bytes(self) -> int:
        return self.size

    @property
    def mtime_ns(self) -> int:
        return int(self.mtime * 1_000_000_000)


@dataclass
class InventoryResult:
    """Complete result of a bounded library scan."""

    root: Path
    entries: list[InventoryEntry] = field(default_factory=list)
    diagnostics: tuple[str, ...] = ()
    progress: InventoryProgress = field(default_factory=lambda: InventoryProgress(done=True))

    @property
    def valid_entries(self) -> list[InventoryEntry]:
        return [entry for entry in self.entries if entry.valid]

    @property
    def invalid_entries(self) -> list[InventoryEntry]:
        return [entry for entry in self.entries if not entry.valid]

    @property
    def scanned(self) -> int:
        return len(self.entries)


def _is_reparse_or_link(entry: os.DirEntry[str]) -> bool:
    """Return true without following a directory link or Windows junction."""
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _iter_sng_files(root: Path, limits: InventoryLimits) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    diagnostics: list[str] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda item: item.name.casefold())
        except OSError as exc:
            diagnostics.append(f"cannot read directory {directory}: {exc}")
            continue

        subdirectories: list[Path] = []
        for child in children:
            if _is_reparse_or_link(child):
                continue
            child_path = Path(child.path)
            try:
                is_directory = child.is_dir(follow_symlinks=False)
                is_file = child.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_directory:
                if depth < limits.max_depth:
                    subdirectories.append(child_path)
                continue
            if not is_file or child.name.casefold().endswith(".sng") is False:
                continue
            if len(files) >= limits.max_files:
                diagnostics.append(f"file limit reached ({limits.max_files})")
                return files, diagnostics
            files.append(child_path)
        stack.extend((child, depth + 1) for child in reversed(subdirectories))
    return files, diagnostics


def _decode(raw: bytes) -> tuple[str | None, str | None]:
    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _TEXT_LIMIT or "\x00" in value:
        return None
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        return None
    return value


def _metadata_value(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    folded = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None


def _parse_ini(text: str) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str.casefold
    parser.read_string(text)
    sections = sorted(parser.sections(), key=lambda section: (section.casefold() != "song", section.casefold()))
    values: dict[str, Any] = {}
    for section in sections:
        for key, value in parser.items(section):
            values.setdefault(key.casefold(), value)
    return values


def _json_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    mappings = [value]
    for key in ("song", "metadata", "track", "chart"):
        nested = _metadata_value(value, (key,))
        if isinstance(nested, Mapping):
            mappings.append(nested)
    return mappings


def _duration_ms(value: Any, key: str) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        match = _DURATION_RE.fullmatch(value)
        if match:
            groups = [int(item) if item is not None else 0 for item in match.groups()]
            milliseconds = ((groups[0] * 60 + groups[1]) * 60 + groups[2]) * 1000 if groups[2] else (groups[0] * 60 + groups[1]) * 1000
        else:
            try:
                number = float(value)
            except ValueError:
                return None
            if not math.isfinite(number):
                return None
            value = number
        if isinstance(value, str):
            return milliseconds if 0 <= milliseconds <= _MAX_DURATION_MS else None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    number = float(value)
    if number < 0:
        return None
    is_seconds = "second" in key.casefold() or key.casefold() in {"duration", "length"}
    milliseconds = round(number * 1000) if is_seconds else round(number)
    return milliseconds if milliseconds <= _MAX_DURATION_MS else None


def _extract_metadata(payloads: list[tuple[str, bytes]], diagnostics: list[str]) -> tuple[dict[str, Any], str]:
    values: dict[str, Any] = {}
    sources: list[str] = []
    for name, raw in payloads:
        text, encoding = _decode(raw)
        if text is None:
            diagnostics.append(f"could not decode metadata member {name}")
            continue
        basename = name.rsplit("/", 1)[-1].casefold()
        try:
            if basename in {"song.ini", "chart.ini"}:
                parsed = _parse_ini(text)
                source = basename
            elif basename in {"song.json", "metadata.json"}:
                parsed_json = json.loads(text)
                mappings = _json_mappings(parsed_json)
                parsed = {}
                for mapping in reversed(mappings):
                    parsed.update({str(key).casefold(): value for key, value in mapping.items()})
                source = basename
            else:
                continue
        except (configparser.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(f"could not parse metadata member {name}: {exc}")
            continue
        if encoding == "latin-1":
            diagnostics.append(f"metadata member {name} decoded with latin-1")
        for field_name, names in {
            "artist": ("artist", "artist_name", "artistname", "song_artist"),
            "title": ("name", "title", "song_name", "songname"),
            "album": ("album", "album_name", "albumname"),
        }.items():
            if field_name not in values:
                candidate = _safe_text(_metadata_value(parsed, names))
                if candidate is not None:
                    values[field_name] = candidate
        if "duration_ms" not in values:
            duration_key = next(
                (key for key in ("duration_ms", "durationms", "song_length", "songlength", "length_ms", "duration_seconds", "duration", "length") if key in parsed),
                None,
            )
            if duration_key is not None:
                duration = _duration_ms(parsed[duration_key], duration_key)
                if duration is not None:
                    values["duration_ms"] = duration
        sources.append(source)
    return values, ",".join(dict.fromkeys(sources))


def _previous_entries(previous: InventoryResult | Mapping[Any, Any] | None) -> dict[str, Any]:
    if previous is None:
        return {}
    if isinstance(previous, InventoryResult):
        values: Iterable[tuple[Any, Any]] = ((entry.path, entry) for entry in previous.entries)
    elif isinstance(previous, Mapping):
        values = previous.items()
    else:
        return {}
    result: dict[str, Any] = {}
    for key, value in values:
        if isinstance(value, InventoryEntry):
            result[str(value.path)] = value
            result[str(value.relative_path)] = value
        elif isinstance(value, Mapping):
            result[str(key)] = value
    return result


def _reuse_hash(previous: Any, path: Path, size: int, mtime_ns: int) -> str | None:
    if previous is None:
        return None
    if isinstance(previous, Mapping):
        old_size = previous.get("size", previous.get("size_bytes"))
        old_mtime = previous.get("mtime_ns")
        if old_mtime is None:
            old_mtime = previous.get("mtime")
        digest = previous.get("sha256")
    else:
        old_size = getattr(previous, "size", getattr(previous, "size_bytes", None))
        old_mtime = getattr(previous, "mtime_ns", None)
        if old_mtime is None:
            old_mtime = getattr(previous, "mtime", None)
        digest = getattr(previous, "sha256", None)
    try:
        same_time = int(old_mtime) == mtime_ns if old_mtime is not None else False
    except (TypeError, ValueError):
        same_time = False
    if not same_time and old_mtime is not None:
        try:
            same_time = float(old_mtime) == os.stat(path).st_mtime
        except (OSError, TypeError, ValueError):
            same_time = False
    return str(digest) if same_time and old_size == size and digest else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _progress(callback: Callable[[InventoryProgress], None] | None, state: InventoryProgress) -> None:
    if callback is not None:
        callback(state)


def scan_library(
    root: Path | str,
    *,
    limits: InventoryLimits | None = None,
    previous: InventoryResult | Mapping[Any, Any] | None = None,
    hash_files: bool = False,
    progress: Callable[[InventoryProgress], None] | None = None,
) -> InventoryResult:
    """Recursively inventory ``.sng`` files under ``root`` without writes.

    Symlinks and Windows reparse points are ignored.  Invalid archives remain
    in ``entries`` with their validation error, which lets a caller report
    corruption instead of silently losing a file.  The optional ``previous``
    result can avoid re-hashing unchanged files.
    """
    active_limits = limits or InventoryLimits()
    library_root = Path(root).expanduser().absolute()
    diagnostics: list[str] = []
    try:
        root_stat = library_root.stat()
        root_lstat = library_root.lstat()
    except OSError as exc:
        return InventoryResult(library_root, diagnostics=(f"cannot stat library root: {exc}",))
    if stat.S_ISLNK(root_lstat.st_mode) or bool(getattr(root_lstat, "st_file_attributes", 0) & _REPARSE_POINT):
        return InventoryResult(library_root, diagnostics=("library root is a symbolic link or reparse point",))
    if not stat.S_ISDIR(root_stat.st_mode):
        return InventoryResult(library_root, diagnostics=("library root is not a directory",))

    paths, traversal_diagnostics = _iter_sng_files(library_root, active_limits)
    diagnostics.extend(traversal_diagnostics)
    previous_map = _previous_entries(previous)
    entries: list[InventoryEntry] = []
    valid = invalid = skipped = total_bytes = processed = 0
    _progress(progress, InventoryProgress(discovered=len(paths)))

    for path in paths:
        processed += 1
        file_diagnostics: list[str] = []
        if _path_is_reparse_or_link(path):
            skipped += 1
            _progress(progress, InventoryProgress(len(paths), processed, valid, invalid, skipped, path))
            continue
        try:
            info = path.stat()
        except OSError as exc:
            entries.append(InventoryEntry(library_root, path, diagnostics=(f"cannot stat file: {exc}",)))
            invalid += 1
            _progress(progress, InventoryProgress(len(paths), processed, valid, invalid, skipped, path))
            continue
        size = int(info.st_size)
        mtime = float(info.st_mtime)
        total_bytes += size
        if size > active_limits.max_file_bytes:
            file_diagnostics.append(f"file exceeds max_file_bytes ({active_limits.max_file_bytes})")
            validation = None
            skipped += 1
        elif active_limits.max_total_bytes and total_bytes > active_limits.max_total_bytes:
            file_diagnostics.append(f"scan exceeds max_total_bytes ({active_limits.max_total_bytes})")
            validation = None
            skipped += 1
        else:
            validation = validate_sng(
                path,
                max_members=active_limits.max_members,
                max_member_bytes=active_limits.max_member_bytes,
                max_archive_bytes=active_limits.max_archive_bytes,
                max_compression_ratio=active_limits.max_compression_ratio,
            )
            if validation.valid:
                valid += 1
            else:
                invalid += 1
                if validation.error:
                    file_diagnostics.append(validation.error)

        digest = None
        if hash_files and validation is not None:
            prior = previous_map.get(str(path)) or previous_map.get(str(path.relative_to(library_root)))
            digest = _reuse_hash(prior, path, size, info.st_mtime_ns)
            if digest is None:
                try:
                    digest = _sha256(path)
                except OSError as exc:
                    file_diagnostics.append(f"could not hash file: {exc}")

        parser_source = ""
        metadata: dict[str, Any] = {}
        if validation is not None and validation.valid:
            payloads: list[tuple[str, bytes]] = []
            package_metadata: dict[str, str] | None = None
            try:
                with open(path, "rb") as fh:
                    head = fh.read(6)
                if head == SNGPKG_MAGIC:
                    with open(path, "rb") as fh:
                        package_metadata = parse_sngpkg_metadata(fh.read(8192))
            except OSError as exc:
                file_diagnostics.append(f"could not read metadata: {exc}")
            if package_metadata is not None:
                # SNGPKG packages carry their metadata inline; map it onto the
                # same fields the ZIP song.ini parser produces.
                metadata = {
                    "artist": package_metadata.get("artist", ""),
                    "title": package_metadata.get("name", ""),
                    "album": package_metadata.get("album", ""),
                }
                song_length = str(package_metadata.get("song_length", "") or "")
                if song_length.isdigit():
                    metadata["duration_ms"] = int(song_length)
                parser_source = "sngpkg"
            else:
                try:
                    with zipfile.ZipFile(path, "r") as archive:
                        for member in validation.metadata_members:
                            basename = member.rsplit("/", 1)[-1].casefold()
                            if basename not in {"song.ini", "chart.ini", "song.json", "metadata.json"}:
                                continue
                            info_by_name = {item.filename.casefold(): item for item in archive.infolist()}
                            member_info = info_by_name.get(member.casefold())
                            if member_info is None or member_info.is_dir():
                                file_diagnostics.append(f"metadata member disappeared: {member}")
                                continue
                            if member_info.file_size > active_limits.max_metadata_bytes:
                                file_diagnostics.append(f"metadata member exceeds limit: {member}")
                                continue
                            payloads.append((member, archive.read(member_info)))
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, KeyError) as exc:
                    file_diagnostics.append(f"could not read metadata: {exc}")
                metadata, parser_source = _extract_metadata(payloads, file_diagnostics)

        artist = str(metadata.get("artist", ""))
        title = str(metadata.get("title", ""))
        album = str(metadata.get("album", ""))
        entry = InventoryEntry(
            root=library_root,
            path=path,
            size=size,
            mtime=mtime,
            sha256=digest,
            validation=validation,
            artist=artist,
            artist_norm=normalize_artist(artist) if artist else "",
            title=title,
            title_norm=normalize_title(title) if title else "",
            album=album,
            album_norm=normalize_title(album) if album else "",
            duration_ms=metadata.get("duration_ms"),
            parser_source=parser_source,
            diagnostics=tuple(file_diagnostics),
        )
        entries.append(entry)
        _progress(progress, InventoryProgress(len(paths), processed, valid, invalid, skipped, path))

    final_progress = InventoryProgress(len(paths), processed, valid, invalid, skipped, done=True)
    _progress(progress, final_progress)
    return InventoryResult(library_root, entries, tuple(diagnostics), final_progress)


__all__ = [
    "InventoryEntry",
    "InventoryLimits",
    "InventoryProgress",
    "InventoryResult",
    "scan_library",
]
