"""Safe, reusable export of persisted Clone Hero chart selections.

The legacy build pipeline downloads directly into its configured library.  The
Spotify workflow needs a narrower boundary: discovery and selection are
already persisted, and this module is responsible only for turning that
selection set into verified artifacts.  In particular, a destination path is
never used as proof that a chart was downloaded successfully.

The public entry point is :class:`ExportService`.  It accepts an iterable (or
zero-argument callback) of persisted selection rows, a mapping/callback of
``ChartSource`` instances, and an optional artifact provider/cache.  The
service can then export the same verified artifacts to a Songs folder or a
ZIP archive without reading the destination as a cache.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import stat
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.filesystem import MAX_COMPONENT_LENGTH, destination_path, is_within
from csv_to_clonehero.models import ChartCandidate, Track
from csv_to_clonehero.sources.base import ChartSource

log = logging.getLogger(__name__)


APP_ID = "csv-to-clonehero"
MANIFEST_FORMAT = "csv-to-clonehero-manifest"
MANIFEST_VERSION = 1
MANIFEST_FILENAME = "csv-to-clonehero-manifest.json"

_INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DRIVE_RE = re.compile(r"^[A-Za-z]:($|/)")
_HASH_RE = re.compile(r"^[0-9a-fA-F]+$")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_METADATA_BASENAMES = {
    "song.ini",
    "chart.ini",
    "metadata.ini",
    "song.json",
    "metadata.json",
    "song.yaml",
    "song.yml",
    "song.toml",
    "song.dta",
}
_CHART_BASENAMES = {
    "notes.chart",
    "notes.mid",
    "notes.midi",
    "notes.dta",
    "chart.chart",
    "chart.mid",
    "chart.midi",
    "notes",
}

# Modern .sng packages (served by Enchor) use the SNGPKG binary container
# instead of a ZIP archive; both formats must validate.
SNGPKG_MAGIC = b"SNGPKG"


class ExportError(Exception):
    """Base exception for explicit exporter failures."""


class ManifestError(ExportError):
    """A manifest exists but cannot be trusted as app-owned state."""


@dataclass(frozen=True)
class SourceHash:
    """A source-provided checksum that is explicitly trusted for bytes."""

    algorithm: str
    value: str
    verified: bool = True

    def label(self) -> str:
        return f"{self.algorithm}:{self.value}"


@dataclass(frozen=True)
class ArtifactDownload:
    """Optional result wrapper for an injected artifact provider.

    ``payload`` may be bytes, a readable binary stream, or a path.  A provider
    may attach a checksum when it knows that the checksum covers the returned
    bytes.  A plain ``Path``/``bytes`` return remains supported for small test
    providers and simple adapters.
    """

    payload: Any
    source_hash: str = ""
    hash_algorithm: str = "sha256"
    hash_verified: bool = True


@dataclass(frozen=True)
class SngValidation:
    """Result of inspecting a Clone Hero ``.sng`` archive."""

    valid: bool
    size_bytes: int = 0
    members: tuple[str, ...] = ()
    metadata_members: tuple[str, ...] = ()
    chart_members: tuple[str, ...] = ()
    error: str = ""

    def __bool__(self) -> bool:
        return self.valid


@dataclass(frozen=True)
class ArtifactIdentity:
    """Stable identity for a verified artifact, independent of its target."""

    source: str
    chart_id: Any
    cache_key: str
    source_hash: str = ""


@dataclass(frozen=True)
class VerifiedArtifact:
    """A validated local artifact ready to copy to one or more destinations."""

    path: Path
    identity: ArtifactIdentity
    sha256: str
    size_bytes: int
    source_hash: str = ""
    source_hash_algorithm: str = ""


@dataclass
class ExportSelection:
    """Convenient in-memory form of a persisted selection.

    The service also accepts SQLite rows, dictionaries, and the existing
    ``models.Selection`` dataclass directly.  This model is useful for callers
    that do not want to construct a database-shaped row in tests or another
    application boundary.
    """

    candidate: ChartCandidate
    kind: str = "exact"
    target_relpath: str = ""
    status: str = "selected"
    score: float | None = None
    explanation: str = ""
    planned_bytes: int = 0
    selection_id: Any = None
    track_id: Any = None
    track: Track | None = None
    eligibility: str = ""
    eligible: bool | None = None


@dataclass(frozen=True)
class PreflightIssue:
    """One actionable preflight finding."""

    code: str
    message: str
    fatal: bool = True
    selection_index: int | None = None


@dataclass
class PreflightItem:
    """Normalized selection and all information discovered without downloading."""

    index: int
    selection: Any
    candidate: ChartCandidate | None = None
    source: ChartSource | None = None
    source_name: str = ""
    chart_id: Any = ""
    kind: str = "exact"
    target_relpath: str = ""
    target_path: Path | None = None
    estimated_bytes: int = 0
    source_hash: SourceHash | None = None
    artifact_key: str = ""
    download_info: Any = None
    track: Track | None = None
    selection_id: Any = None
    issues: list[PreflightIssue] = field(default_factory=list)
    duplicate_of: int | None = None

    @property
    def fatal(self) -> bool:
        return any(issue.fatal for issue in self.issues)

    @property
    def eligible(self) -> bool:
        return not self.fatal and self.duplicate_of is None


@dataclass
class PreflightResult:
    """Complete no-write validation result."""

    mode: str
    destination_root: Path
    items: list[PreflightItem] = field(default_factory=list)
    issues: list[PreflightIssue] = field(default_factory=list)
    estimated_bytes: int = 0
    available_bytes: int | None = None
    current_bytes: int = 0
    budget_bytes: int | None = None

    @property
    def errors(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.fatal]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if not issue.fatal]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def eligible_items(self) -> list[PreflightItem]:
        if self.errors:
            return []
        return [item for item in self.items if item.eligible]


@dataclass
class ExportItemResult:
    """Persistable result for one selected chart."""

    selection_index: int
    selection_id: Any = None
    source: str = ""
    chart_id: Any = ""
    target_relpath: str = ""
    target_path: Path | None = None
    status: str = "pending"
    result: str = ""
    bytes_count: int = 0
    sha256: str = ""
    source_hash: str = ""
    source_hash_algorithm: str = ""
    artifact_key: str = ""
    failure: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def actual_bytes(self) -> int:
        return self.bytes_count

    @property
    def local_sha256(self) -> str:
        return self.sha256

    @property
    def failure_reason(self) -> str:
        return self.failure

    @property
    def error(self) -> str:
        return self.failure

    @property
    def target_path_string(self) -> str:
        return str(self.target_path) if self.target_path is not None else ""

    @property
    def completed(self) -> bool:
        return self.status == "published"


@dataclass(frozen=True)
class ManifestRecord:
    """An app-owned file record.  Paths are always destination-relative."""

    source: str
    chart_id: Any
    sha256: str
    bytes_count: int
    target_relpath: str
    source_hash: str = ""
    source_hash_algorithm: str = ""
    artifact_key: str = ""
    selection_id: Any = None

    @property
    def bytes(self) -> int:
        return self.bytes_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "owned_by": APP_ID,
            "selection_id": self.selection_id,
            "source": self.source,
            "chart_id": self.chart_id,
            "sha256": self.sha256,
            "bytes": self.bytes_count,
            "source_hash": self.source_hash,
            "source_hash_algorithm": self.source_hash_algorithm,
            "artifact_key": self.artifact_key,
            "target_relpath": self.target_relpath,
            # Keep the explicit name useful to consumers that call it a path.
            "target_path": self.target_relpath,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManifestRecord":
        target = value.get("target_relpath") or value.get("target_path") or value.get("path") or ""
        target = _normalise_relative_target(target)
        if not target:
            raise ValueError("manifest record has no target path")
        source = str(value.get("source") or "")
        if not source:
            raise ValueError("manifest record has no source")
        if "chart_id" not in value:
            raise ValueError("manifest record has no chart ID")
        sha256 = str(value.get("sha256") or "").lower()
        if not _is_hash_value(sha256, 64):
            raise ValueError("manifest record has no valid SHA-256")
        return cls(
            source=source,
            chart_id=value["chart_id"],
            sha256=sha256,
            bytes_count=int(value.get("bytes", value.get("bytes_count", 0)) or 0),
            target_relpath=target,
            source_hash=str(value.get("source_hash") or ""),
            source_hash_algorithm=str(value.get("source_hash_algorithm") or ""),
            artifact_key=str(value.get("artifact_key") or ""),
            selection_id=value.get("selection_id"),
        )


@dataclass
class ExportManifest:
    """Versioned app-owned manifest used for safe managed synchronization."""

    records: list[ManifestRecord] = field(default_factory=list)
    destination: str = ""
    kind: str = "folder"
    generated_at: str = ""
    version: int = MANIFEST_VERSION
    app: str = APP_ID
    format: str = MANIFEST_FORMAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "app": self.app,
            "generated_at": self.generated_at or _utc_now(),
            "kind": self.kind,
            "destination": self.destination,
            "records": [record.to_dict() for record in self.records],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExportManifest":
        if value.get("app") != APP_ID or value.get("format") != MANIFEST_FORMAT:
            raise ValueError("manifest is not owned by csv-to-clonehero")
        if int(value.get("version", 0)) != MANIFEST_VERSION:
            raise ValueError("unsupported manifest version")
        raw_records = value.get("records", value.get("items", value.get("files", [])))
        if not isinstance(raw_records, list):
            raise ValueError("manifest records are not a list")
        records = [ManifestRecord.from_dict(record) for record in raw_records]
        return cls(
            records=records,
            destination=str(value.get("destination") or ""),
            kind=str(value.get("kind") or "folder"),
            generated_at=str(value.get("generated_at") or ""),
            version=MANIFEST_VERSION,
            app=APP_ID,
            format=MANIFEST_FORMAT,
        )


@dataclass
class SyncResult:
    """Managed-file changes made from one trusted prior manifest."""

    stale_paths: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)
    preserved_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def removed(self) -> list[str]:
        return self.removed_paths


@dataclass
class ExportOptions:
    """Policy knobs for an export run.

    ``None`` means use the corresponding existing application configuration
    when one was supplied.  The exporter intentionally has no dependency on a
    database; ``manifest_path`` and ``artifact_cache_dir`` are the two pieces
    of state callers may explicitly place wherever their application prefers.
    """

    budget_bytes: int | None = None
    minimum_free_bytes: int = 0
    estimated_chart_bytes: int | None = None
    export_floor: float | None = None
    overwrite_owned: bool = True
    managed_sync: bool = False
    manifest_path: Path | None = None
    include_manifest_in_zip: bool = True
    max_archive_members: int = 4096
    max_member_bytes: int = 512 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 10_000.0
    # When a single chart fails to download/verify, keep exporting the rest and
    # report the failures instead of aborting the whole batch.
    continue_on_error: bool = False
    # Retry a chart's download+verification this many extra times before
    # recording it as failed (transient rate-limits/corrupt responses).
    artifact_retries: int = 2
    # Concurrent download workers. 1 is serial; higher hides per-request
    # latency. The source adapter still throttles global request rate.
    download_workers: int = 1


@dataclass
class ExportResult:
    """Outcome of a folder or ZIP export."""

    kind: str
    destination: Path
    success: bool
    preflight: PreflightResult
    items: list[ExportItemResult] = field(default_factory=list)
    manifest: ExportManifest | None = None
    manifest_path: Path | None = None
    sync: SyncResult | None = None
    output_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def published(self) -> list[ExportItemResult]:
        return [item for item in self.items if item.status == "published"]

    @property
    def failed(self) -> list[ExportItemResult]:
        return [item for item in self.items if item.failure or item.status in {"failed", "preflight_failed"}]

    @property
    def bytes_count(self) -> int:
        return sum(item.bytes_count for item in self.items if item.status == "published")

    @property
    def output_path(self) -> Path:
        return self.destination

    @property
    def manifest_file(self) -> Path | None:
        return self.manifest_path

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def published_count(self) -> int:
        return len(self.published)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def duplicate_count(self) -> int:
        return sum(item.status == "duplicate" for item in self.items)


@runtime_checkable
class ArtifactCache(Protocol):
    """Minimal cache protocol accepted by :class:`ExportService`."""

    def get(self, key: str) -> Path | None:
        ...

    def put(self, key: str, source: Path) -> Path:
        ...


@runtime_checkable
class ArtifactProvider(Protocol):
    """Provider protocol used when a source adapter is not sufficient."""

    def __call__(self, candidate: ChartCandidate, source: ChartSource, destination: Path) -> Any:
        ...


class FileArtifactCache:
    """Small atomic filesystem cache keyed only by artifact identity."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F]{16,128}", key):
            key = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{key}.sng"

    def get(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.is_file() else None

    def put(self, key: str, source: Path) -> Path:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)
        try:
            _copy_file(source, part)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        return target

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_hash_value(value: str, length: int | None = None) -> bool:
    if not value or not _HASH_RE.fullmatch(value):
        return False
    return length is None or len(value) == length


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return value[name]
    except (KeyError, IndexError, TypeError, AttributeError):
        return getattr(value, name, default)


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    if hasattr(value, "keys"):
        try:
            return {key: value[key] for key in value.keys()}
        except Exception:
            return None
    return None


def _coerce_candidate(value: Any) -> ChartCandidate | None:
    if isinstance(value, ChartCandidate):
        return value
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    data = _as_dict(value)
    if data is None:
        if all(hasattr(value, key) for key in ("source", "chart_id", "name", "artist")):
            return value
        return None
    nested = data.get("candidate")
    if nested is not None:
        candidate = _coerce_candidate(nested)
        if candidate is not None:
            return candidate
    for key in ("candidate_json", "candidate_data", "chart"):
        if key in data and data[key] is not None:
            candidate = _coerce_candidate(data[key])
            if candidate is not None:
                return candidate
    names = {item.name for item in fields(ChartCandidate)}
    payload = {key: data[key] for key in names if key in data}
    required = {"source", "chart_id", "md5", "name", "artist", "name_norm", "artist_norm"}
    if not required.issubset(payload):
        return None
    try:
        return ChartCandidate(**payload)
    except (TypeError, ValueError):
        return None


def _coerce_track(value: Any) -> Track | None:
    if isinstance(value, Track):
        return value
    data = _as_dict(value)
    if data is None:
        return None
    names = {item.name for item in fields(Track)}
    payload = {key: data[key] for key in names if key in data}
    required = {"track_key", "title", "title_norm", "artist_full", "artist_norm", "primary_artist", "primary_artist_norm"}
    if not required.issubset(payload):
        return None
    try:
        return Track(**payload)
    except (TypeError, ValueError):
        return None


def _normalise_relative_target(value: Any) -> str:
    """Validate and normalize an app-relative chart target."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise ValueError("target path is absolute")
    parts = raw.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("target path contains an unsafe component")
    for part in parts:
        if len(part) > MAX_COMPONENT_LENGTH:
            raise ValueError(f"target component is too long: {part!r}")
        if _INVALID_NAME_RE.search(part) or part.endswith((".", " ")):
            raise ValueError(f"target component has unsafe characters: {part!r}")
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise ValueError(f"target component uses a reserved name: {part!r}")
    if parts[-1].casefold().endswith(".sng") is False:
        raise ValueError("target path must name a .sng file")
    return "/".join(parts)


def _safe_archive_member_name(value: str) -> tuple[str, bool]:
    """Return a normalized ZIP member name or raise for an unsafe name."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("archive member has an empty or NUL name")
    if value.startswith(("/", "\\")) or _DRIVE_RE.match(value.replace("\\", "/")):
        raise ValueError(f"archive member is absolute: {value!r}")
    if "\\" in value:
        raise ValueError(f"archive member uses a backslash: {value!r}")
    directory = value.endswith("/")
    core = value[:-1] if directory else value
    parts = core.split("/")
    if not core or any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"archive member has unsafe traversal: {value!r}")
    for part in parts:
        if len(part) > 255 or _INVALID_NAME_RE.search(part) or part.endswith((".", " ")):
            raise ValueError(f"archive member has unsafe characters: {value!r}")
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise ValueError(f"archive member uses a reserved name: {value!r}")
    return "/".join(parts) + ("/" if directory else ""), directory


def _walk_sngpkg_pairs(data: bytes, start: int, max_pairs: int = 64) -> dict[str, str]:
    """Walk SNGPKG length-prefixed key/value pairs starting at ``start``."""
    metadata: dict[str, str] = {}
    pos = start
    for _ in range(max_pairs):
        if pos + 4 > len(data):
            break
        key_len = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if not 1 <= key_len <= 64 or pos + key_len + 4 > len(data):
            break
        try:
            key = data[pos:pos + key_len].decode("utf-8")
        except UnicodeDecodeError:
            break
        pos += key_len
        value_len = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if value_len > 4096 or pos + value_len > len(data):
            break
        try:
            value = data[pos:pos + value_len].decode("utf-8")
        except UnicodeDecodeError:
            break
        pos += value_len
        metadata[key] = value
    recognized = {"name", "artist", "album", "charter", "genre", "year", "song_length"}
    return metadata if recognized.intersection(metadata) else {}


def parse_sngpkg_metadata(data: bytes) -> dict[str, str]:
    """Best-effort extraction of the SNGPKG metadata key/value section.

    The binary package stores metadata as ``[u32 key length][key][u32 value
    length][value]`` pairs. The section is located by finding a known key whose
    preceding four bytes encode that key's own length, then walking pairs.
    """
    for key in ("name", "artist", "album"):
        marker = key.encode("ascii")
        offset = 0
        while True:
            offset = data.find(marker, offset)
            if offset < 4:
                break
            key_len = int.from_bytes(data[offset - 4:offset], "little")
            if key_len == len(marker):
                metadata = _walk_sngpkg_pairs(data, offset - 4)
                if metadata:
                    return metadata
            offset += 1
    return {}


def _validate_sngpkg(archive: Path, size: int, max_archive_bytes: int) -> SngValidation:
    """Validate an ``SNGPKG`` binary .sng package (the format Enchor serves)."""
    try:
        with open(archive, "rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        return SngValidation(False, size_bytes=size, error=f"cannot read chart package: {exc}")
    if len(head) < 10 or head[:6] != SNGPKG_MAGIC:
        return SngValidation(False, size_bytes=size, error="malformed chart package header")
    version = int.from_bytes(head[6:10], "little")
    if not 1 <= version <= 16:
        return SngValidation(False, size_bytes=size, error=f"unsupported chart package version: {version}")
    if size > max_archive_bytes:
        return SngValidation(False, size_bytes=size, error="chart package is too large")
    if not parse_sngpkg_metadata(head):
        return SngValidation(False, size_bytes=size, error="chart package lacks song metadata")
    return SngValidation(
        True,
        size_bytes=size,
        members=("package",),
        metadata_members=("package:metadata",),
        chart_members=("package:charts",),
    )


def validate_sng(
    path: Path | str,
    *,
    max_members: int = 4096,
    max_member_bytes: int = 512 * 1024 * 1024,
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024,
    max_compression_ratio: float = 10_000.0,
) -> SngValidation:
    """Safely validate a nonempty Clone Hero ``.sng`` ZIP.

    Validation is performed in place and never extracts untrusted members.
    It checks ZIP integrity, safe member names, symlink entries, bounded
    declared sizes, and the presence of both chart data and song/chart
    metadata.  The limits are deliberately configurable for unusual but
    legitimate charts.
    """
    archive = Path(path)
    try:
        size = archive.stat().st_size
    except OSError as exc:
        return SngValidation(False, error=f"cannot stat chart: {exc}")
    if size <= 0:
        return SngValidation(False, size_bytes=size, error="chart file is empty")
    try:
        with open(archive, "rb") as fh:
            magic = fh.read(6)
    except OSError as exc:
        return SngValidation(False, size_bytes=size, error=f"cannot read chart: {exc}")
    if magic == SNGPKG_MAGIC:
        return _validate_sngpkg(archive, size, max_archive_bytes)

    members: list[str] = []
    metadata: list[str] = []
    chart_files: list[str] = []
    total_uncompressed = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            infos = zf.infolist()
            if not infos:
                return SngValidation(False, size_bytes=size, error="chart ZIP has no members")
            if len(infos) > max_members:
                return SngValidation(False, size_bytes=size, error="chart ZIP has too many members")
            for info in infos:
                normalized, directory = _safe_archive_member_name(info.filename)
                key = normalized.casefold()
                if key in seen:
                    return SngValidation(False, size_bytes=size, error=f"duplicate archive member: {info.filename!r}")
                seen.add(key)
                members.append(normalized)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    return SngValidation(False, size_bytes=size, error=f"symbolic-link member is not allowed: {info.filename!r}")
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    return SngValidation(False, size_bytes=size, error=f"special-file member is not allowed: {info.filename!r}")
                if info.flag_bits & 0x1:
                    return SngValidation(False, size_bytes=size, error=f"encrypted member is not allowed: {info.filename!r}")
                if directory:
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    return SngValidation(False, size_bytes=size, error=f"invalid ZIP size for {info.filename!r}")
                if info.file_size > max_member_bytes:
                    return SngValidation(False, size_bytes=size, error=f"archive member is too large: {info.filename!r}")
                total_uncompressed += info.file_size
                if total_uncompressed > max_archive_bytes:
                    return SngValidation(False, size_bytes=size, error="chart ZIP expands beyond safety limit")
                if info.file_size and info.compress_size == 0:
                    return SngValidation(False, size_bytes=size, error=f"invalid compression size for {info.filename!r}")
                if info.compress_size:
                    ratio = info.file_size / info.compress_size
                    if ratio > max_compression_ratio:
                        return SngValidation(False, size_bytes=size, error=f"suspicious compression ratio for {info.filename!r}")
                basename = normalized.rsplit("/", 1)[-1].casefold()
                if basename in _METADATA_BASENAMES or (
                    basename.endswith((".ini", ".json", ".yaml", ".yml", ".toml"))
                    and basename not in {"notes.ini", "chart.ini.tmp"}
                ):
                    metadata.append(normalized)
                if basename in _CHART_BASENAMES or basename.endswith((".chart", ".mid", ".midi")):
                    chart_files.append(normalized)

            if not metadata:
                return SngValidation(False, size_bytes=size, members=tuple(members), error="chart ZIP lacks song/chart metadata")
            if not chart_files:
                return SngValidation(False, size_bytes=size, members=tuple(members), metadata_members=tuple(metadata), error="chart ZIP lacks notes/chart data")
            nonempty_metadata = {info.filename.replace("\\", "/") for info in infos if not info.is_dir() and info.file_size > 0}
            if not any(name in nonempty_metadata for name in metadata):
                return SngValidation(False, size_bytes=size, members=tuple(members), metadata_members=tuple(metadata), chart_members=tuple(chart_files), error="chart metadata files are empty")
            if not any(name in nonempty_metadata for name in chart_files):
                return SngValidation(False, size_bytes=size, members=tuple(members), metadata_members=tuple(metadata), chart_members=tuple(chart_files), error="chart data files are empty")
            bad_member = zf.testzip()
            if bad_member is not None:
                return SngValidation(False, size_bytes=size, members=tuple(members), metadata_members=tuple(metadata), chart_members=tuple(chart_files), error=f"ZIP CRC validation failed for {bad_member!r}")
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return SngValidation(False, size_bytes=size, error=f"malformed chart ZIP: {exc}")
    return SngValidation(
        True,
        size_bytes=size,
        members=tuple(members),
        metadata_members=tuple(metadata),
        chart_members=tuple(chart_files),
    )


validate_sng_archive = validate_sng


def is_valid_sng(path: Path | str, **kwargs: Any) -> bool:
    return bool(validate_sng(path, **kwargs))


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    algorithm = algorithm.lower().replace("-", "")
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise ExportError(f"unsupported hash algorithm: {algorithm}") from exc
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with source.open("rb") as src, target.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return written


def _write_bytes(path: Path, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    return len(payload)


def _source_hash_from_value(value: Any, *, default_algorithm: str = "sha256", verified: bool = True) -> SourceHash | None:
    if value is None:
        return None
    algorithm = default_algorithm
    raw = value
    if isinstance(value, SourceHash):
        return value if value.verified else None
    if isinstance(value, Mapping):
        raw = value.get("value", value.get("hash", value.get("checksum", "")))
        algorithm = str(value.get("algorithm", value.get("hash_algorithm", default_algorithm)) or default_algorithm)
        verified = bool(value.get("verified", value.get("hash_verified", verified)))
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        algorithm = str(value[0])
        raw = value[1]
        if len(value) >= 3:
            verified = bool(value[2])
    raw = str(raw or "").strip().lower()
    algorithm = algorithm.lower().replace("-", "")
    if not verified or not raw or not _HASH_RE.fullmatch(raw):
        return None
    try:
        digest_size = hashlib.new(algorithm).digest_size * 2
    except ValueError:
        return None
    if len(raw) != digest_size:
        return None
    return SourceHash(algorithm=algorithm, value=raw, verified=True)


def _source_hash(candidate: Any, source: Any, info: Any) -> SourceHash | None:
    source_verified = bool(getattr(source, "download_hash_verified", False))
    values: list[tuple[Any, str, bool]] = []
    for owner in (info, candidate):
        if owner is None:
            continue
        owner_verified = source_verified or bool(
            _field(owner, "hash_verified", _field(owner, "download_hash_verified", False))
        )
        for name, algorithm in (
            ("sha256", "sha256"),
            ("sha1", "sha1"),
            ("md5", "md5"),
            ("hash", "sha256"),
            ("checksum", "sha256"),
        ):
            value = _field(owner, name, None)
            if value:
                if name in {"hash", "checksum"}:
                    algorithm = str(_field(owner, "hash_algorithm", algorithm) or algorithm)
                values.append((value, algorithm, owner_verified))
    for name in ("get_download_hash", "get_artifact_hash", "download_hash", "artifact_hash"):
        method = getattr(source, name, None)
        if callable(method):
            try:
                values.append((method(candidate), "sha256", source_verified))
            except Exception:
                log.debug("source hash lookup failed", exc_info=True)
    for value, algorithm, verified in values:
        result = _source_hash_from_value(value, default_algorithm=algorithm, verified=verified)
        if result is not None:
            return result
    return None


def _safe_path(root: Path, relative: str) -> Path:
    target = (root / Path(relative.replace("/", os.sep))).resolve()
    if not is_within(target, root):
        raise ValueError(f"target path escapes destination root: {relative!r}")
    return target


def _path_key(relative: str) -> str:
    return relative.replace("\\", "/").casefold()


def _utc_manifest_path(config: AppConfig | None, root: Path) -> Path:
    """Use state outside the Songs root, with one manifest per destination."""
    resolved = str(root.resolve()).casefold().encode("utf-8", "surrogatepass")
    token = hashlib.sha256(resolved).hexdigest()[:16]
    if config is not None:
        return config.database_path.parent / "export-manifests" / f"{token}-{MANIFEST_FILENAME}"
    return root.parent / f".{root.name or 'songs'}-{MANIFEST_FILENAME}"


def load_manifest(path: Path | str) -> ExportManifest | None:
    """Load only a valid app-owned manifest; return ``None`` otherwise."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            return None
        return ExportManifest.from_dict(raw)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


read_manifest = load_manifest


def write_manifest(manifest: ExportManifest, path: Path | str) -> Path:
    """Atomically write a manifest without overwriting an unknown file."""
    destination = Path(path).expanduser()
    if destination.exists() and load_manifest(destination) is None:
        raise ManifestError(f"refusing to overwrite an unknown manifest: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    try:
        with part.open("w", encoding="utf-8", newline="\n") as target:
            target.write(manifest.to_json())
            target.flush()
            os.fsync(target.fileno())
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)
    return destination


class ExportService:
    """Export persisted selections through a verified artifact boundary.

    Parameters are intentionally injectable so a web job can supply its own
    source resolver, artifact cache, disk-usage probe, and downloader without
    coupling this service to Flask or the database.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        sources: Mapping[str, ChartSource] | Iterable[ChartSource] | Callable[[str], ChartSource | None] | None = None,
        *,
        source_resolver: Callable[[str], ChartSource | None] | None = None,
        artifact_provider: ArtifactProvider | Callable[..., Any] | None = None,
        artifact_cache: ArtifactCache | None = None,
        artifact_cache_dir: Path | str | None = None,
        track_resolver: Mapping[Any, Track] | Callable[[Any], Track | None] | None = None,
        disk_usage: Callable[[Path], Any] | None = None,
        validator: Callable[..., SngValidation] = validate_sng,
    ) -> None:
        self.config = config
        self.artifact_provider = artifact_provider
        self.track_resolver = track_resolver
        self.disk_usage = disk_usage
        self.validator = validator
        self._temporary_cache: tempfile.TemporaryDirectory[str] | None = None

        if source_resolver is not None:
            self._source_resolver = source_resolver
        elif isinstance(sources, Mapping):
            by_name = dict(sources)
            self._source_resolver = lambda name: by_name.get(name)
        elif callable(sources):
            self._source_resolver = sources
        else:
            by_name = {getattr(source, "name", ""): source for source in (sources or ())}
            self._source_resolver = lambda name: by_name.get(name)

        if artifact_cache is not None:
            self.artifact_cache = artifact_cache
        else:
            if artifact_cache_dir is None and config is not None:
                artifact_cache_dir = config.database_path.parent / "export-artifacts"
            if artifact_cache_dir is None:
                self._temporary_cache = tempfile.TemporaryDirectory(prefix="csv-to-clonehero-artifacts-")
                artifact_cache_dir = self._temporary_cache.name
            self.artifact_cache = FileArtifactCache(artifact_cache_dir)

        self._staging_root = Path(
            tempfile.mkdtemp(prefix="csv-to-clonehero-export-")
        )

    def close(self) -> None:
        """Release temporary cache/staging state owned by this service."""
        shutil.rmtree(self._staging_root, ignore_errors=True)
        if self._temporary_cache is not None:
            self._temporary_cache.cleanup()
            self._temporary_cache = None

    def __enter__(self) -> "ExportService":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _resolve_source(self, name: str) -> ChartSource | None:
        try:
            return self._source_resolver(name)
        except Exception:
            log.debug("source resolver failed for %s", name, exc_info=True)
            return None

    def _resolve_track(self, value: Any) -> Track | None:
        track = _coerce_track(value)
        if track is not None:
            return track
        if self.track_resolver is None:
            return None
        try:
            if isinstance(self.track_resolver, Mapping):
                return _coerce_track(self.track_resolver.get(value))
            return _coerce_track(self.track_resolver(value))
        except Exception:
            log.debug("track resolver failed", exc_info=True)
            return None

    @staticmethod
    def _selection_candidate(selection: Any) -> ChartCandidate | None:
        candidate = _coerce_candidate(_field(selection, "candidate", None))
        if candidate is not None:
            return candidate
        return _coerce_candidate(_field(selection, "candidate_json", None)) or _coerce_candidate(selection)

    def _selection_target(
        self,
        selection: Any,
        candidate: ChartCandidate,
        kind: str,
        track: Track | None,
    ) -> str:
        supplied = _field(selection, "target_relpath", None)
        if supplied is None or str(supplied).strip() == "":
            supplied = _field(selection, "target_path", None)
        if supplied is not None and str(supplied).strip() != "":
            return _normalise_relative_target(supplied)
        if self.config is None:
            raise ValueError("selection has no target_relpath and no AppConfig was supplied")
        return _normalise_relative_target(str(destination_path(self.config, candidate, kind, track=track)))

    @staticmethod
    def _eligibility_issues(
        selection: Any,
        candidate: ChartCandidate,
        export_floor: float | None,
        checker: Callable[..., Any] | None,
        index: int,
    ) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        status = str(_field(selection, "status", "selected") or "selected").casefold()
        if status not in {"selected", "accepted", "eligible", "recommended", "eligible_review"}:
            issues.append(PreflightIssue("selection_not_selected", f"selection status is {status!r}", selection_index=index))
        explicit = _field(selection, "eligible", None)
        if explicit is False:
            issues.append(PreflightIssue("selection_ineligible", "persisted selection is marked ineligible", selection_index=index))
        eligibility = _field(selection, "eligibility", None)
        if eligibility is None:
            eligibility = _field(selection, "eligibility_state", None)
        if eligibility is None:
            eligibility = _field(selection, "export_status", None)
        if eligibility is not None and str(eligibility).casefold() in {
            "excluded",
            "incompatible",
            "ineligible",
            "rejected",
            "no_match",
            "not_exportable",
        }:
            issues.append(PreflightIssue("selection_ineligible", f"persisted eligibility is {eligibility!r}", selection_index=index))
        for owner, label in ((selection, "selection"), (candidate, "candidate")):
            exportable = _field(owner, "exportable", None)
            if exportable is False:
                issues.append(PreflightIssue("not_exportable", f"{label} is not exportable", selection_index=index))
            downloadable = _field(owner, "downloadable", None)
            if downloadable is False:
                issues.append(PreflightIssue("not_downloadable", f"{label} is not downloadable", selection_index=index))
        severe = _field(candidate, "issue_severity", None)
        if severe is None:
            severe = _field(candidate, "source_issue_severity", None)
        if str(severe or "").casefold() in {"critical", "severe", "fatal"}:
            issues.append(PreflightIssue("severe_source_issue", "source marked chart issue as severe", selection_index=index))
        decision = str(_field(selection, "decision", "") or "").casefold()
        if decision in {"reject", "rejected", "incompatible"}:
            issues.append(PreflightIssue("selection_ineligible", f"persisted decision is {decision!r}", selection_index=index))
        score = _field(selection, "score", None)
        if score is not None and export_floor is not None:
            try:
                if float(score) < export_floor:
                    issues.append(PreflightIssue("below_export_floor", f"score {float(score):.1f} is below export floor {export_floor:.1f}", selection_index=index))
            except (TypeError, ValueError):
                issues.append(PreflightIssue("invalid_score", "persisted selection score is not numeric", selection_index=index))
        if checker is not None:
            try:
                result = _invoke_checker(checker, selection, candidate)
                if result is False:
                    issues.append(PreflightIssue("eligibility_checker", "injected eligibility check rejected chart", selection_index=index))
                elif isinstance(result, str) and result:
                    issues.append(PreflightIssue("eligibility_checker", result, selection_index=index))
            except Exception as exc:
                issues.append(PreflightIssue("eligibility_checker", f"eligibility check failed: {exc}", selection_index=index))
        return issues

    def _source_hash(self, candidate: ChartCandidate, source: ChartSource, info: Any) -> SourceHash | None:
        return _source_hash(candidate, source, info)

    def _artifact_key(self, source_name: str, chart_id: Any, source_hash: SourceHash | None) -> str:
        suffix = source_hash.label() if source_hash is not None else ""
        identity = f"{source_name}\x00{chart_id}\x00{suffix}".encode("utf-8", "surrogatepass")
        return hashlib.sha256(identity).hexdigest()

    def _estimated_bytes(self, selection: Any, info: Any, options: ExportOptions) -> int:
        for owner in (selection, info):
            value = _field(owner, "planned_bytes", None)
            if value is None:
                value = _field(owner, "size_bytes", None)
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                pass
        if options.estimated_chart_bytes is not None:
            return max(0, int(options.estimated_chart_bytes))
        if self.config is not None:
            return max(0, int(self.config.storage.estimated_chart_bytes))
        return 0

    def _prepare(
        self,
        selections: Iterable[Any],
        destination_root: Path,
        *,
        mode: str,
        options: ExportOptions,
        eligibility_checker: Callable[..., Any] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> PreflightResult:
        mode = mode.casefold()
        if mode not in {"folder", "zip"}:
            raise ValueError("mode must be 'folder' or 'zip'")
        root = Path(destination_root).expanduser().resolve()
        result = PreflightResult(mode=mode, destination_root=root)
        if root.exists() and not root.is_dir():
            result.issues.append(PreflightIssue("destination_not_directory", f"destination is not a directory: {root}"))

        prepared: list[PreflightItem] = []
        values = list(selections)
        preflight_total = len(values)
        for index, selection in enumerate(values):
            _report_progress(progress, index, preflight_total, "Checking download sizes")
            item = PreflightItem(index=index, selection=selection)
            candidate = self._selection_candidate(selection)
            item.candidate = candidate
            item.selection_id = _field(selection, "selection_id", _field(selection, "id", index))
            if candidate is None:
                item.issues.append(PreflightIssue("missing_candidate", "persisted selection has no chart candidate", selection_index=index))
                prepared.append(item)
                continue
            candidate_id = _field(selection, "candidate_id", None)
            if getattr(candidate, "db_id", None) is None and candidate_id is not None:
                try:
                    candidate.db_id = int(candidate_id)
                except (TypeError, ValueError, AttributeError):
                    pass
            item.source_name = str(_field(candidate, "source", "") or _field(selection, "source", "") or "")
            item.chart_id = _field(candidate, "chart_id", _field(selection, "chart_id", ""))
            item.kind = str(_field(selection, "kind", "exact") or "exact").casefold()
            if item.kind not in {"exact", "expansion"}:
                item.issues.append(PreflightIssue("invalid_selection_kind", f"unsupported selection kind: {item.kind!r}", selection_index=index))
            track_value = _field(selection, "track", None)
            if track_value is None:
                track_value = _field(selection, "track_data", None)
            if track_value is None:
                track_id = _field(selection, "track_id", None)
                if track_id is not None:
                    track_value = self._resolve_track(track_id)
            item.track = _coerce_track(track_value)
            try:
                item.target_relpath = self._selection_target(selection, candidate, item.kind, item.track)
                item.target_path = _safe_path(root, item.target_relpath)
            except (TypeError, ValueError, OSError) as exc:
                item.issues.append(PreflightIssue("unsafe_target_path", str(exc), selection_index=index))
            item.issues.extend(self._eligibility_issues(
                selection,
                candidate,
                options.export_floor if options.export_floor is not None else (
                    self.config.matching.review_threshold if self.config is not None else None
                ),
                eligibility_checker,
                index,
            ))
            if not item.source_name:
                item.issues.append(PreflightIssue("missing_source", "candidate has no source name", selection_index=index))
            else:
                item.source = self._resolve_source(item.source_name)
                if item.source is None:
                    item.issues.append(PreflightIssue("source_unavailable", f"source is unavailable: {item.source_name}", selection_index=index))
            if self.config is not None and not self.config.build.direct_download_enabled:
                item.issues.append(PreflightIssue("direct_download_disabled", "direct downloads are disabled by configuration", selection_index=index))
            if not bool(getattr(candidate, "supports_download", True)):
                item.issues.append(PreflightIssue("candidate_not_downloadable", "candidate does not expose a direct download", selection_index=index))
            if item.source is not None:
                source_capability = getattr(item.source, "supports_direct_download", None)
                if source_capability is False:
                    item.issues.append(PreflightIssue("source_not_downloadable", f"source {item.source_name!r} has no direct-download capability", selection_index=index))
                elif source_capability is None and self.artifact_provider is None:
                    item.issues.append(PreflightIssue("source_capability_unknown", f"source {item.source_name!r} does not declare direct-download capability", selection_index=index))
                get_info = getattr(item.source, "get_download_info", None)
                has_download = callable(getattr(item.source, "download_chart", None))
                if callable(get_info):
                    try:
                        item.download_info = get_info(candidate)
                    except Exception as exc:
                        item.issues.append(PreflightIssue("download_info_failed", f"could not inspect download capability: {exc}", selection_index=index))
                    if item.download_info is not None and _field(item.download_info, "supports_direct_download", True) is False:
                        item.issues.append(PreflightIssue("download_info_not_downloadable", "source download metadata marks chart as non-downloadable", selection_index=index))
                elif self.artifact_provider is None and not has_download:
                    item.issues.append(PreflightIssue("source_protocol_incomplete", "source has no download method or download info", selection_index=index))
                item.source_hash = self._source_hash(candidate, item.source, item.download_info)
            if item.source is not None and item.source_hash is not None:
                item.artifact_key = self._artifact_key(item.source_name, item.chart_id, item.source_hash)
            elif item.source_name:
                item.artifact_key = self._artifact_key(item.source_name, item.chart_id, None)
            item.estimated_bytes = self._estimated_bytes(selection, item.download_info, options)
            prepared.append(item)

        # Target collisions are unsafe when two different artifacts would
        # compete for the same file.  Identical selection/content identities
        # collapse to the first item instead, matching the selection tray's
        # duplicate-collapse behavior.
        by_target: dict[str, PreflightItem] = {}
        by_identity: dict[str, PreflightItem] = {}
        for item in prepared:
            if item.fatal or not item.target_relpath:
                continue
            target_key = _path_key(item.target_relpath)
            if item.source_hash is not None:
                # A verified source checksum is content identity even when
                # two persisted selections refer to different chart rows.
                identity = f"hash\x00{item.source_hash.label()}"
            else:
                identity = f"{item.source_name.casefold()}\x00{item.chart_id!r}"
            prior_identity = by_identity.get(identity)
            if prior_identity is not None:
                item.duplicate_of = prior_identity.index
                item.issues.append(PreflightIssue("duplicate_content", f"duplicate chart content/identity; collapsed into selection {prior_identity.index}", fatal=False, selection_index=item.index))
                continue
            prior_target = by_target.get(target_key)
            if prior_target is not None:
                issue = PreflightIssue("duplicate_target_path", f"target path duplicates selection {prior_target.index}: {item.target_relpath}", selection_index=item.index)
                item.issues.append(issue)
                prior_target.issues.append(PreflightIssue("duplicate_target_path", f"target path duplicates selection {item.index}: {prior_target.target_relpath}", selection_index=prior_target.index))
                continue
            by_identity[identity] = item
            by_target[target_key] = item

        unique = [item for item in prepared if item.eligible]
        result.estimated_bytes = sum(item.estimated_bytes for item in unique)
        result.current_bytes = 0 if mode == "zip" else _existing_chart_bytes(root)
        result.budget_bytes = options.budget_bytes
        if result.budget_bytes is None and self.config is not None:
            result.budget_bytes = self.config.storage.max_library_bytes
        existing_targets = sum(
            item.target_path.stat().st_size
            for item in unique
            if item.target_path is not None and item.target_path.is_file()
        )
        projected_bytes = max(0, result.current_bytes + result.estimated_bytes - existing_targets)
        if result.budget_bytes is not None and result.estimated_bytes and projected_bytes > result.budget_bytes:
            result.issues.append(PreflightIssue(
                "configured_budget_exceeded",
                f"estimated export uses {projected_bytes} bytes, above configured budget {result.budget_bytes}",
            ))
        probe_path = root if root.exists() else _nearest_existing_parent(root)
        usage = self.disk_usage or _default_disk_usage
        try:
            raw_usage = usage(probe_path)
            result.available_bytes = int(getattr(raw_usage, "free", raw_usage[2] if isinstance(raw_usage, tuple) else raw_usage))
        except (OSError, TypeError, ValueError, IndexError):
            result.available_bytes = None
        required = result.estimated_bytes + max(0, int(options.minimum_free_bytes))
        if result.available_bytes is not None and result.available_bytes < required:
            result.issues.append(PreflightIssue(
                "insufficient_disk_space",
                f"only {result.available_bytes} bytes are free; at least {required} are required",
            ))
        # Give callers a flat issue list while preserving item-local reasons.
        for item in prepared:
            result.issues.extend(item.issues)
        result.items = prepared
        return result

    def preflight(
        self,
        selections: Iterable[Any] | Callable[[], Iterable[Any]],
        destination_root: Path | str,
        *,
        mode: str = "folder",
        options: ExportOptions | None = None,
        eligibility_checker: Callable[..., Any] | None = None,
    ) -> PreflightResult:
        """Run all no-write checks against a selection iterable/callback."""
        values = _consume_selections(selections)
        return self._prepare(
            values,
            Path(destination_root),
            mode=mode,
            options=options or ExportOptions(),
            eligibility_checker=eligibility_checker,
        )

    preflight_export = preflight

    def _cache_get(self, key: str) -> Path | None:
        cache = self.artifact_cache
        for name in ("get", "get_artifact", "lookup"):
            method = getattr(cache, name, None)
            if callable(method):
                try:
                    value = method(key)
                    return Path(value) if value else None
                except (OSError, KeyError, ValueError):
                    return None
        return None

    def _cache_delete(self, key: str) -> None:
        method = getattr(self.artifact_cache, "delete", None)
        if callable(method):
            try:
                method(key)
            except OSError:
                pass
            return
        cached = self._cache_get(key)
        if cached is not None:
            cached.unlink(missing_ok=True)

    def _cache_put(self, key: str, source: Path) -> Path:
        for name in ("put", "store", "store_artifact"):
            method = getattr(self.artifact_cache, name, None)
            if callable(method):
                value = method(key, source)
                if value is None:
                    break
                return Path(value)
        # A read-only injected cache still gets a stable service-local copy.
        fallback = FileArtifactCache(self._staging_root / "verified")
        return fallback.put(key, source)

    def _provider_result(self, candidate: ChartCandidate, source: ChartSource, part: Path) -> ArtifactDownload | Any:
        provider = self.artifact_provider
        if provider is None:
            downloader = getattr(source, "download_chart", None)
            if not callable(downloader):
                raise ExportError("source has no download_chart method")
            return _invoke_download(downloader, candidate, part)
        target = provider
        for name in ("materialize", "download", "fetch", "get_artifact"):
            method = getattr(provider, name, None)
            if callable(method):
                target = method
                break
        return _invoke_provider(target, candidate, source, part)

    def _verify_artifact(
        self,
        path: Path,
        item: PreflightItem,
        options: ExportOptions,
        provider_hash: SourceHash | None = None,
    ) -> VerifiedArtifact:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ExportError(f"artifact cannot be read: {exc}") from exc
        if size <= 0:
            raise ExportError("downloaded chart is empty")
        validation = _invoke_validator(self.validator, path, options)
        if not validation:
            raise ExportError(validation.error or "downloaded chart failed .sng validation")
        expected = provider_hash or item.source_hash
        source_hash = expected.value if expected is not None else ""
        source_algorithm = expected.algorithm if expected is not None else ""
        expected_hashes: list[SourceHash] = []
        for candidate_hash in (item.source_hash, provider_hash):
            if candidate_hash is not None and all(
                candidate_hash.label() != existing.label() for existing in expected_hashes
            ):
                expected_hashes.append(candidate_hash)
        for expected_hash in expected_hashes:
            actual_source_hash = _hash_file(path, expected_hash.algorithm)
            if not hmac.compare_digest(actual_source_hash.lower(), expected_hash.value.lower()):
                raise ExportError(
                    f"source checksum mismatch (expected {expected_hash.value}, got {actual_source_hash})"
                )
        sha256 = _hash_file(path, "sha256")
        identity = ArtifactIdentity(
            source=item.source_name,
            chart_id=item.chart_id,
            cache_key=item.artifact_key or self._artifact_key(item.source_name, item.chart_id, expected),
            source_hash=expected.label() if expected is not None else "",
        )
        return VerifiedArtifact(
            path=path,
            identity=identity,
            sha256=sha256,
            size_bytes=size,
            source_hash=source_hash,
            source_hash_algorithm=source_algorithm,
        )

    def _materialize(self, item: PreflightItem, options: ExportOptions) -> VerifiedArtifact:
        if item.candidate is None or item.source is None:
            raise ExportError("item was not eligible for materialization")
        cached = self._cache_get(item.artifact_key)
        if cached is not None and cached.is_file():
            try:
                return self._verify_artifact(cached, item, options)
            except ExportError:
                self._cache_delete(item.artifact_key)

        part = self._staging_root / f"{item.artifact_key or uuid.uuid4().hex}.sng.part"
        part.unlink(missing_ok=True)
        provider_hash: SourceHash | None = None
        try:
            outcome = self._provider_result(item.candidate, item.source, part)
            if isinstance(outcome, ArtifactDownload):
                provider_hash = _source_hash_from_value(
                    outcome.source_hash,
                    default_algorithm=outcome.hash_algorithm,
                    verified=outcome.hash_verified,
                )
                outcome = outcome.payload
            elif isinstance(outcome, VerifiedArtifact):
                outcome = outcome.path
            elif isinstance(outcome, Mapping):
                payload = outcome.get("payload", outcome.get("path", outcome.get("artifact")))
                provider_hash = _source_hash_from_value(
                    outcome.get("source_hash", outcome.get("sha256", "")),
                    default_algorithm=str(outcome.get("hash_algorithm", "sha256")),
                    verified=bool(outcome.get("hash_verified", True)),
                )
                outcome = payload
            elif isinstance(outcome, (tuple, list)) and len(outcome) >= 1:
                if len(outcome) >= 2:
                    provider_hash = _source_hash_from_value(outcome[1], default_algorithm="sha256", verified=True)
                outcome = outcome[0]
            if outcome is not None and not part.exists():
                _write_payload(outcome, part)
            if not part.is_file():
                raise ExportError("artifact provider did not produce a file")
            verified = self._verify_artifact(part, item, options, provider_hash=provider_hash)
            stored = self._cache_put(item.artifact_key, part)
            try:
                return self._verify_artifact(stored, item, options, provider_hash=provider_hash)
            except ExportError:
                self._cache_delete(item.artifact_key)
                raise
        finally:
            part.unlink(missing_ok=True)

    def _materialize_with_retry(self, item: PreflightItem, options: ExportOptions) -> VerifiedArtifact:
        """Materialize a chart, retrying transient download/verify failures.

        Raises the last failure when all retries are exhausted; the export loop
        decides whether to abort or continue based on ``continue_on_error``.
        """
        attempts = 1 + max(0, int(options.artifact_retries))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._materialize(item, options)
            except Exception as exc:  # noqa: BLE001 - retried at the export level
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2.0 * (2 ** attempt), 8.0))
        if last_exc is not None:
            raise last_exc
        raise ExportError("download failed")

    def _materialize_all(
        self,
        eligible: list[PreflightItem],
        results: list[ExportItemResult],
        options: ExportOptions,
        progress: Callable[[int, int, str], None] | None,
    ) -> tuple[dict[int, VerifiedArtifact], list[str]]:
        """Materialize every eligible chart, optionally in parallel.

        Returns ``(artifacts, errors)``. Individual failures are recorded on the
        matching ``results`` entry; the caller decides abort-vs-continue via
        ``continue_on_error``.
        """
        total = len(eligible)
        workers = max(1, int(options.download_workers))
        artifacts: dict[int, VerifiedArtifact] = {}
        errors: list[str] = []
        progress_lock = threading.Lock()
        completed = 0

        def materialize_one(item: PreflightItem) -> tuple[int, VerifiedArtifact | None, str | None]:
            nonlocal completed
            label = item.target_relpath or f"chart {item.index}"
            try:
                artifact = self._materialize_with_retry(item, options)
            except Exception as exc:  # noqa: BLE001 - reported per item
                item_result = self._result_for(results, item.index)
                item_result.status = "failed"
                item_result.result = "artifact_failed"
                item_result.failure = str(exc)
                message = f"selection {item.index}: {item_result.failure or 'download failed'}"
                with progress_lock:
                    completed += 1
                    _report_progress(progress, completed, total, label)
                return item.index, None, message
            with progress_lock:
                completed += 1
                _report_progress(progress, completed, total, label)
            return item.index, artifact, None

        if workers <= 1 or total <= 1:
            for item in eligible:
                _index, artifact, message = materialize_one(item)
                if artifact is not None:
                    artifacts[_index] = artifact
                elif message:
                    errors.append(message)
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(materialize_one, item) for item in eligible]
                for future in futures:
                    try:
                        _index, artifact, message = future.result()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"materialization worker failed: {exc}")
                        continue
                    if artifact is not None:
                        artifacts[_index] = artifact
                    elif message:
                        errors.append(message)
        return artifacts, errors

    def _manifest_path(self, root: Path, options: ExportOptions, explicit: Path | str | None) -> Path:
        if explicit is not None:
            return Path(explicit).expanduser()
        if options.manifest_path is not None:
            return Path(options.manifest_path).expanduser()
        return _utc_manifest_path(self.config, root)

    def _prior_manifest(self, path: Path, root: Path) -> tuple[ExportManifest | None, str | None]:
        if not path.exists():
            return None, None
        manifest = load_manifest(path)
        if manifest is None:
            return None, f"manifest exists but is not a trusted app-owned manifest: {path}"
        if manifest.destination and _path_key(manifest.destination) != _path_key(str(root.resolve())):
            return None, f"manifest belongs to another destination: {path}"
        return manifest, None

    def _initial_results(self, preflight: PreflightResult) -> list[ExportItemResult]:
        results: list[ExportItemResult] = []
        for item in preflight.items:
            if item.fatal:
                failure = "; ".join(issue.message for issue in item.issues if issue.fatal)
                status = "preflight_failed"
                result = item.issues[0].code if item.issues else "preflight_failed"
            elif item.duplicate_of is not None:
                failure = ""
                status = "duplicate"
                result = "duplicate_content"
            else:
                failure = ""
                status = "pending"
                result = ""
            source_hash = item.source_hash
            results.append(ExportItemResult(
                selection_index=item.index,
                selection_id=item.selection_id,
                source=item.source_name,
                chart_id=item.chart_id,
                target_relpath=item.target_relpath,
                target_path=item.target_path,
                status=status,
                result=result,
                source_hash=source_hash.value if source_hash is not None else "",
                source_hash_algorithm=source_hash.algorithm if source_hash is not None else "",
                artifact_key=item.artifact_key,
                failure=failure,
            ))
        global_errors = [issue for issue in preflight.errors if issue.selection_index is None]
        if global_errors:
            failure = "; ".join(issue.message for issue in global_errors)
            result_code = global_errors[0].code
            for item_result in results:
                if item_result.status == "pending":
                    item_result.status = "preflight_failed"
                    item_result.result = result_code
                    item_result.failure = failure
        return results

    @staticmethod
    def _result_for(results: list[ExportItemResult], index: int) -> ExportItemResult:
        return results[index]

    @staticmethod
    def _mark_pending_failed(
        results: Iterable[ExportItemResult],
        *,
        result: str,
        failure: str,
    ) -> None:
        for item_result in results:
            if item_result.status == "pending":
                item_result.status = "failed"
                item_result.result = result
                item_result.failure = failure

    def _capacity_check(self, root: Path, required: int, options: ExportOptions, mode: str) -> str | None:
        if required <= 0:
            return None
        usage = self.disk_usage or _default_disk_usage
        probe = root if root.exists() else _nearest_existing_parent(root)
        try:
            raw = usage(probe)
            free = int(getattr(raw, "free", raw[2] if isinstance(raw, tuple) else raw))
        except (OSError, TypeError, ValueError, IndexError):
            return None
        reserve = max(0, int(options.minimum_free_bytes))
        if free < required + reserve:
            return f"disk free space dropped below required {required + reserve} bytes"
        return None

    def _make_manifest(
        self,
        results: Iterable[ExportItemResult],
        *,
        root: Path,
        kind: str,
    ) -> ExportManifest:
        records: list[ManifestRecord] = []
        seen: set[str] = set()
        for result in results:
            if result.status != "published" or not result.target_relpath or not result.sha256:
                continue
            key = _path_key(result.target_relpath)
            if key in seen:
                continue
            seen.add(key)
            records.append(ManifestRecord(
                source=result.source,
                chart_id=result.chart_id,
                sha256=result.sha256,
                bytes_count=result.bytes_count,
                target_relpath=result.target_relpath,
                source_hash=result.source_hash,
                source_hash_algorithm=result.source_hash_algorithm,
                artifact_key=result.artifact_key,
                selection_id=result.selection_id,
            ))
        return ExportManifest(
            records=records,
            destination="" if kind == "zip" else str(root.resolve()),
            kind=kind,
            generated_at=_utc_now(),
        )

    def _publish_folder(
        self,
        preflight: PreflightResult,
        results: list[ExportItemResult],
        artifacts: dict[int, VerifiedArtifact],
        *,
        root: Path,
        options: ExportOptions,
        prior: ExportManifest | None,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, [f"cannot create destination root: {exc}"]
        owned = {_path_key(record.target_relpath) for record in (prior.records if prior else ())}
        actual_total = sum(artifact.size_bytes for artifact in artifacts.values())
        budget = options.budget_bytes
        if budget is None and self.config is not None:
            budget = self.config.storage.max_library_bytes
        replacement_bytes = sum(
            result.target_path.stat().st_size
            for result in results
            if result.selection_index in artifacts
            and result.target_path is not None
            and result.target_path.is_file()
        )
        if budget is not None:
            projected = max(0, _existing_chart_bytes(root) + actual_total - replacement_bytes)
            if projected > budget:
                return False, [f"actual export uses {projected} bytes, above configured budget {budget}"]
        capacity_error = self._capacity_check(root, actual_total, options, "folder")
        if capacity_error:
            errors.append(capacity_error)
            return False, errors

        parts: list[tuple[int, Path, Path]] = []
        for index, artifact in artifacts.items():
            item_result = self._result_for(results, index)
            target = item_result.target_path
            if target is None:
                errors.append(f"selection {index} has no target path")
                continue
            if target.exists():
                if target.is_symlink():
                    item_result.status = "failed"
                    item_result.result = "unsafe_existing_target"
                    item_result.failure = "refusing to replace a symbolic-link target"
                    errors.append(item_result.failure)
                    continue
                try:
                    existing_hash = _hash_file(target, "sha256")
                except OSError as exc:
                    existing_hash = ""
                    item_result.failure = str(exc)
                if existing_hash == artifact.sha256 and _path_key(item_result.target_relpath) in owned:
                    item_result.status = "published"
                    item_result.result = "already_present"
                    item_result.bytes_count = artifact.size_bytes
                    item_result.sha256 = artifact.sha256
                    item_result.source_hash = artifact.source_hash
                    item_result.source_hash_algorithm = artifact.source_hash_algorithm
                    continue
                if existing_hash == artifact.sha256:
                    item_result.status = "failed"
                    item_result.result = "target_exists_unowned"
                    item_result.failure = "identical target exists but is not listed in an app-owned manifest"
                    errors.append(f"{item_result.target_relpath}: {item_result.failure}")
                    continue
                if _path_key(item_result.target_relpath) not in owned or not options.overwrite_owned:
                    item_result.status = "failed"
                    item_result.result = "target_exists"
                    item_result.failure = "target exists and is not an app-owned identical chart"
                    errors.append(f"{item_result.target_relpath}: {item_result.failure}")
                    continue
            part = target.with_name(target.name + ".part")
            part.unlink(missing_ok=True)
            try:
                _copy_file(artifact.path, part)
                parts.append((index, part, target))
            except OSError as exc:
                part.unlink(missing_ok=True)
                item_result.status = "failed"
                item_result.result = "stage_failed"
                item_result.failure = str(exc)
                errors.append(f"{item_result.target_relpath}: {exc}")

        if errors:
            for _index, part, _target in parts:
                part.unlink(missing_ok=True)
            for index, artifact in artifacts.items():
                item_result = self._result_for(results, index)
                if item_result.status == "pending":
                    item_result.status = "failed"
                    item_result.result = "publication_aborted"
                    item_result.failure = "publication aborted because another target failed"
            return False, errors

        for index, part, target in parts:
            item_result = self._result_for(results, index)
            try:
                os.replace(part, target)
            except OSError as exc:
                part.unlink(missing_ok=True)
                errors.append(f"{item_result.target_relpath}: {exc}")
                item_result.status = "failed"
                item_result.result = "publish_failed"
                item_result.failure = str(exc)
                for _other_index, other_part, _other_target in parts:
                    other_part.unlink(missing_ok=True)
                for other_index in artifacts:
                    other_result = self._result_for(results, other_index)
                    if other_result.status == "pending":
                        other_result.status = "failed"
                        other_result.result = "publication_aborted"
                        other_result.failure = "publication aborted because another target failed"
                return False, errors
            item_result.status = "published"
            item_result.result = "published"
            item_result.bytes_count = artifacts[index].size_bytes
            item_result.sha256 = artifacts[index].sha256
            item_result.source_hash = artifacts[index].source_hash
            item_result.source_hash_algorithm = artifacts[index].source_hash_algorithm
        for index, artifact in artifacts.items():
            item_result = self._result_for(results, index)
            if item_result.status == "pending":
                item_result.status = "published"
                item_result.result = "already_present"
                item_result.bytes_count = artifact.size_bytes
                item_result.sha256 = artifact.sha256
                item_result.source_hash = artifact.source_hash
                item_result.source_hash_algorithm = artifact.source_hash_algorithm
        return True, errors

    def _publish_zip(
        self,
        preflight: PreflightResult,
        results: list[ExportItemResult],
        artifacts: dict[int, VerifiedArtifact],
        *,
        archive_path: Path,
        manifest: ExportManifest,
        options: ExportOptions,
    ) -> tuple[bool, list[str], int]:
        errors: list[str] = []
        actual_total = sum(artifact.size_bytes for artifact in artifacts.values())
        budget = options.budget_bytes
        if budget is None and self.config is not None:
            budget = self.config.storage.max_library_bytes
        if budget is not None and actual_total > budget:
            return False, [f"actual ZIP contents use {actual_total} bytes, above configured budget {budget}"], 0
        capacity_error = self._capacity_check(archive_path.parent, actual_total, options, "zip")
        if capacity_error:
            return False, [capacity_error], 0
        archive_path = archive_path.expanduser()
        if archive_path.exists() and archive_path.is_symlink():
            return False, ["refusing to replace a symbolic-link ZIP target"], 0
        try:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            part = archive_path.with_name(archive_path.name + ".part")
            part.unlink(missing_ok=True)
        except OSError as exc:
            return False, [f"cannot prepare ZIP destination: {exc}"], 0
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for index, artifact in artifacts.items():
                    item_result = self._result_for(results, index)
                    arcname = item_result.target_relpath
                    try:
                        arcname = _normalise_relative_target(arcname)
                    except ValueError as exc:
                        item_result.status = "failed"
                        item_result.result = "unsafe_archive_target"
                        item_result.failure = str(exc)
                        errors.append(str(exc))
                        raise ExportError(str(exc)) from exc
                    info = zipfile.ZipInfo(arcname)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 0
                    info.external_attr = 0
                    with archive.open(info, "w") as target, artifact.path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(chunk)
                    item_result.status = "published"
                    item_result.result = "archived"
                    item_result.bytes_count = artifact.size_bytes
                    item_result.sha256 = artifact.sha256
                    item_result.source_hash = artifact.source_hash
                    item_result.source_hash_algorithm = artifact.source_hash_algorithm
                manifest.records = self._make_manifest(
                    results,
                    root=Path(preflight.destination_root),
                    kind="zip",
                ).records
                if options.include_manifest_in_zip:
                    info = zipfile.ZipInfo(MANIFEST_FILENAME)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 0
                    info.external_attr = 0
                    archive.writestr(info, manifest.to_json().encode("utf-8"))
            with part.open("r+b") as target:
                os.fsync(target.fileno())
            os.replace(part, archive_path)
            return True, errors, archive_path.stat().st_size
        except (OSError, ExportError, zipfile.BadZipFile, RuntimeError) as exc:
            part.unlink(missing_ok=True)
            if not errors:
                errors.append(str(exc))
            for index in artifacts:
                item_result = self._result_for(results, index)
                if item_result.status in {"pending", "published"}:
                    item_result.status = "failed"
                    item_result.result = "archive_failed"
                    item_result.failure = str(exc)
            return False, errors, 0
        finally:
            part.unlink(missing_ok=True)

    def _remove_stale(
        self,
        root: Path,
        prior: ExportManifest | None,
        current_paths: Iterable[str],
    ) -> SyncResult:
        sync = SyncResult()
        if prior is None:
            return sync
        current = {_path_key(path) for path in current_paths}
        for record in prior.records:
            if _path_key(record.target_relpath) in current:
                continue
            sync.stale_paths.append(record.target_relpath)
            try:
                target = _safe_path(root, record.target_relpath)
            except ValueError as exc:
                sync.errors.append(f"{record.target_relpath}: {exc}")
                continue
            if not target.exists() and not target.is_symlink():
                sync.removed_paths.append(record.target_relpath)
                continue
            if target.is_dir():
                sync.preserved_paths.append(record.target_relpath)
                sync.errors.append(f"{record.target_relpath}: expected a file, preserving directory")
                continue
            try:
                target.unlink()
                sync.removed_paths.append(record.target_relpath)
            except OSError as exc:
                sync.errors.append(f"{record.target_relpath}: {exc}")
        return sync

    def _result(
        self,
        kind: str,
        destination: Path,
        preflight: PreflightResult,
        results: list[ExportItemResult],
        *,
        success: bool,
        errors: list[str] | None = None,
        manifest: ExportManifest | None = None,
        manifest_path: Path | None = None,
        sync: SyncResult | None = None,
        output_bytes: int = 0,
    ) -> ExportResult:
        return ExportResult(
            kind=kind,
            destination=destination,
            success=success,
            preflight=preflight,
            items=results,
            manifest=manifest,
            manifest_path=manifest_path,
            sync=sync,
            output_bytes=output_bytes,
            errors=list(errors or ()),
        )

    def export_folder(
        self,
        selections: Iterable[Any] | Callable[[], Iterable[Any]],
        destination_root: Path | str,
        *,
        manifest_path: Path | str | None = None,
        managed: bool | None = None,
        managed_sync: bool | None = None,
        options: ExportOptions | None = None,
        eligibility_checker: Callable[..., Any] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ExportResult:
        """Export selected charts to a Songs folder with atomic file moves."""
        opts = options or ExportOptions()
        if managed is None and managed_sync is not None:
            managed = managed_sync
        if managed is not None:
            opts.managed_sync = managed
        values = _consume_selections(selections)
        root = Path(destination_root).expanduser().resolve()
        manifest_file = self._manifest_path(root, opts, manifest_path)
        preflight = self._prepare(values, root, mode="folder", options=opts, eligibility_checker=eligibility_checker, progress=progress)
        results = self._initial_results(preflight)
        if not preflight.ok:
            return self._result("folder", root, preflight, results, success=False, errors=[issue.message for issue in preflight.errors], manifest_path=manifest_file)

        prior, manifest_error = self._prior_manifest(manifest_file, root)
        if manifest_error is not None and "belongs to another destination" in manifest_error and not opts.managed_sync:
            # A caller may intentionally reuse one manifest location for a
            # fresh, non-managed export.  It is not ownership evidence for
            # this root, but replacing the valid manifest after publication
            # is still safe.  Managed sync must never cross destinations.
            prior, manifest_error = None, None
        if manifest_error is not None:
            return self._result("folder", root, preflight, results, success=False, errors=[manifest_error], manifest_path=manifest_file)

        eligible = preflight.eligible_items
        total = len(eligible)
        artifacts, errors = self._materialize_all(eligible, results, opts, progress)
        if errors and not opts.continue_on_error:
            for item in preflight.eligible_items:
                item_result = self._result_for(results, item.index)
                if item_result.status == "pending":
                    item_result.status = "failed"
                    item_result.result = "export_aborted"
                    item_result.failure = "export aborted because another artifact failed"
            return self._result("folder", root, preflight, results, success=False, errors=errors, manifest_path=manifest_file)

        # A provider may reveal a duplicate only after actual bytes are
        # available.  Collapse it before any destination file is touched.
        by_hash: dict[str, int] = {}
        for index in list(artifacts):
            artifact = artifacts[index]
            prior_index = by_hash.get(artifact.sha256)
            if prior_index is not None:
                duplicate_result = self._result_for(results, index)
                duplicate_result.status = "duplicate"
                duplicate_result.result = "duplicate_content"
                duplicate_result.bytes_count = artifact.size_bytes
                duplicate_result.sha256 = artifact.sha256
                duplicate_result.source_hash = artifact.source_hash
                duplicate_result.source_hash_algorithm = artifact.source_hash_algorithm
                del artifacts[index]
            else:
                by_hash[artifact.sha256] = index
        _report_progress(progress, total, total, "Writing files")
        publish_ok, publish_errors = self._publish_folder(
            preflight,
            results,
            artifacts,
            root=root,
            options=opts,
            prior=prior,
        )
        if not publish_ok:
            self._mark_pending_failed(
                results,
                result="publication_failed",
                failure=publish_errors[0] if publish_errors else "folder publication failed",
            )
            return self._result("folder", root, preflight, results, success=False, errors=publish_errors, manifest_path=manifest_file)

        manifest = self._make_manifest(results, root=root, kind="folder")
        try:
            write_manifest(manifest, manifest_file)
        except Exception as exc:
            return self._result("folder", root, preflight, results, success=False, errors=[str(exc)], manifest_path=manifest_file)

        sync = None
        if opts.managed_sync:
            sync = self._remove_stale(root, prior, (record.target_relpath for record in manifest.records))
            if sync.errors:
                return self._result("folder", root, preflight, results, success=False, errors=list(sync.errors), manifest=manifest, manifest_path=manifest_file, sync=sync)
        return self._result(
            "folder",
            root,
            preflight,
            results,
            success=True,
            errors=errors if opts.continue_on_error else None,
            manifest=manifest,
            manifest_path=manifest_file,
            sync=sync,
        )

    export_to_folder = export_folder

    def export_zip(
        self,
        selections: Iterable[Any] | Callable[[], Iterable[Any]],
        archive_path: Path | str,
        *,
        manifest_path: Path | str | None = None,
        managed: bool | None = None,
        managed_sync: bool | None = None,
        options: ExportOptions | None = None,
        archive_root: Path | str | None = None,
        eligibility_checker: Callable[..., Any] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ExportResult:
        """Export selected charts and the manifest into one atomic ZIP."""
        opts = options or ExportOptions()
        if managed is None and managed_sync is not None:
            managed = managed_sync
        if managed is not None:
            opts.managed_sync = managed
        values = _consume_selections(selections)
        archive = Path(archive_path).expanduser().resolve()
        root = Path(archive_root).expanduser().resolve() if archive_root is not None else archive.parent
        external_spec = manifest_path if manifest_path is not None else opts.manifest_path
        external_manifest = Path(external_spec).expanduser() if external_spec is not None else None
        preflight = self._prepare(values, root, mode="zip", options=opts, eligibility_checker=eligibility_checker, progress=progress)
        results = self._initial_results(preflight)
        if not preflight.ok:
            return self._result("zip", archive, preflight, results, success=False, errors=[issue.message for issue in preflight.errors], manifest_path=external_manifest)

        if external_manifest is not None and external_manifest.exists() and load_manifest(external_manifest) is None:
            return self._result("zip", archive, preflight, results, success=False, errors=[f"manifest exists but is not a trusted app-owned manifest: {external_manifest}"], manifest_path=external_manifest)

        eligible = preflight.eligible_items
        total = len(eligible)
        artifacts, errors = self._materialize_all(eligible, results, opts, progress)
        if errors and not opts.continue_on_error:
            for item in preflight.eligible_items:
                item_result = self._result_for(results, item.index)
                if item_result.status == "pending":
                    item_result.status = "failed"
                    item_result.result = "export_aborted"
                    item_result.failure = "export aborted because another artifact failed"
            return self._result("zip", archive, preflight, results, success=False, errors=errors, manifest_path=external_manifest)

        by_hash: dict[str, int] = {}
        for index in list(artifacts):
            artifact = artifacts[index]
            if artifact.sha256 in by_hash:
                duplicate_result = self._result_for(results, index)
                duplicate_result.status = "duplicate"
                duplicate_result.result = "duplicate_content"
                duplicate_result.bytes_count = artifact.size_bytes
                duplicate_result.sha256 = artifact.sha256
                duplicate_result.source_hash = artifact.source_hash
                duplicate_result.source_hash_algorithm = artifact.source_hash_algorithm
                del artifacts[index]
            else:
                by_hash[artifact.sha256] = index
        manifest = self._make_manifest(results, root=root, kind="zip")
        _report_progress(progress, total, total, "Writing ZIP archive")
        publish_ok, publish_errors, output_bytes = self._publish_zip(
            preflight,
            results,
            artifacts,
            archive_path=archive,
            manifest=manifest,
            options=opts,
        )
        if not publish_ok:
            self._mark_pending_failed(
                results,
                result="publication_failed",
                failure=publish_errors[0] if publish_errors else "ZIP publication failed",
            )
            return self._result("zip", archive, preflight, results, success=False, errors=publish_errors, manifest=manifest, manifest_path=external_manifest, output_bytes=output_bytes)
        if external_manifest is not None:
            try:
                write_manifest(manifest, external_manifest)
            except Exception as exc:
                return self._result("zip", archive, preflight, results, success=False, errors=[str(exc)], manifest=manifest, manifest_path=external_manifest, output_bytes=output_bytes)
        return self._result("zip", archive, preflight, results, success=True, errors=errors if opts.continue_on_error else None, manifest=manifest, manifest_path=external_manifest, output_bytes=output_bytes)

    export_to_zip = export_zip

    def export(
        self,
        selections: Iterable[Any] | Callable[[], Iterable[Any]],
        destination: Path | str,
        *,
        mode: str = "folder",
        format: str | None = None,
        **kwargs: Any,
    ) -> ExportResult:
        """Dispatch to folder or ZIP export using one stable API."""
        if format is not None:
            mode = format
        if mode.casefold() == "zip":
            return self.export_zip(selections, destination, **kwargs)
        if mode.casefold() == "folder":
            return self.export_folder(selections, destination, **kwargs)
        raise ValueError("mode must be 'folder' or 'zip'")

    def sync_managed(
        self,
        destination_root: Path | str,
        current_paths: Iterable[str],
        *,
        manifest_path: Path | str | None = None,
    ) -> SyncResult:
        """Remove stale paths owned by a prior manifest, and nothing else."""
        root = Path(destination_root).expanduser().resolve()
        manifest_file = self._manifest_path(root, ExportOptions(), manifest_path)
        prior, error = self._prior_manifest(manifest_file, root)
        if error is not None:
            return SyncResult(errors=[error])
        return self._remove_stale(root, prior, current_paths)


Exporter = ExportService
ExportConfig = ExportOptions
ExportItem = ExportItemResult
ExportSummary = ExportResult
ManifestEntry = ManifestRecord
Artifact = VerifiedArtifact
ArtifactStore = ArtifactCache


def sync_managed_library(
    destination_root: Path | str,
    current_paths: Iterable[str],
    *,
    manifest_path: Path | str,
) -> SyncResult:
    """Functional wrapper for ownership-safe managed synchronization."""
    root = Path(destination_root).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    if manifest is None:
        return SyncResult(errors=[f"manifest is missing or not app-owned: {manifest_path}"])
    if manifest.destination and _path_key(manifest.destination) != _path_key(str(root)):
        return SyncResult(errors=[f"manifest belongs to another destination: {manifest_path}"])
    helper = object.__new__(ExportService)
    return helper._remove_stale(root, manifest, current_paths)


def _report_progress(progress: Callable[..., Any] | None, done: int, total: int, label: str = "") -> None:
    """Best-effort progress callback; a failing observer never breaks an export."""
    if progress is None:
        return
    try:
        progress(int(done), int(total), str(label))
    except Exception:
        pass


def _consume_selections(selections: Iterable[Any] | Callable[[], Iterable[Any]]) -> list[Any]:
    values = selections() if callable(selections) else selections
    if values is None:
        return []
    return list(values)


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _default_disk_usage(path: Path) -> Any:
    return shutil.disk_usage(path)


def _existing_chart_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for directory, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.casefold().endswith(".sng"):
                continue
            try:
                total += (Path(directory) / filename).stat().st_size
            except OSError:
                pass
    return total


def _invoke_checker(checker: Callable[..., Any], selection: Any, candidate: ChartCandidate) -> Any:
    try:
        signature = inspect.signature(checker)
    except (TypeError, ValueError):
        return checker(selection, candidate)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()) or len(positional) >= 2:
        return checker(selection, candidate)
    return checker(candidate)


def _invoke_validator(validator: Callable[..., SngValidation], path: Path, options: ExportOptions) -> SngValidation:
    """Allow compact injected validators as well as the full validator API."""
    kwargs = {
        "max_members": options.max_archive_members,
        "max_member_bytes": options.max_member_bytes,
        "max_archive_bytes": options.max_archive_bytes,
        "max_compression_ratio": options.max_compression_ratio,
    }
    try:
        signature = inspect.signature(validator)
    except (TypeError, ValueError):
        return validator(path, **kwargs)
    accepts_kwargs = any(
        parameter.kind == parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return validator(path, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters
    }
    return validator(path, **accepted)


def _invoke_download(method: Callable[..., Any], candidate: ChartCandidate, destination: Path) -> Any:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(candidate, destination)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()) or len(positional) >= 2:
        return method(candidate, destination)
    return method(candidate)


def _invoke_provider(method: Callable[..., Any], candidate: ChartCandidate, source: ChartSource, destination: Path) -> Any:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(candidate, source, destination)
    parameters = list(signature.parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters) or len(positional) >= 3:
        return method(candidate, source, destination)
    if len(positional) == 2:
        first_name = positional[0].name.casefold()
        if "source" in first_name:
            return method(source, candidate)
        return method(candidate, destination)
    if len(positional) == 1:
        return method(candidate)
    return method()


def _write_payload(payload: Any, destination: Path) -> int:
    if isinstance(payload, Path):
        return _copy_file(payload, destination)
    if isinstance(payload, str):
        return _copy_file(Path(payload), destination)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return _write_bytes(destination, bytes(payload))
    if hasattr(payload, "read"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with destination.open("wb") as target:
            while True:
                chunk = payload.read(1024 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                target.write(chunk)
                written += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        return written
    raise ExportError(f"unsupported artifact provider result: {type(payload).__name__}")


__all__ = [
    "APP_ID",
    "MANIFEST_FILENAME",
    "ArtifactCache",
    "Artifact",
    "ArtifactDownload",
    "ArtifactIdentity",
    "ArtifactProvider",
    "ArtifactStore",
    "ExportError",
    "ExportConfig",
    "ExportItem",
    "ExportItemResult",
    "ExportManifest",
    "ExportOptions",
    "ExportResult",
    "ExportSelection",
    "ExportService",
    "Exporter",
    "ExportSummary",
    "FileArtifactCache",
    "ManifestError",
    "ManifestEntry",
    "ManifestRecord",
    "PreflightIssue",
    "PreflightItem",
    "PreflightResult",
    "SngValidation",
    "SourceHash",
    "SyncResult",
    "VerifiedArtifact",
    "is_valid_sng",
    "load_manifest",
    "read_manifest",
    "sync_managed_library",
    "validate_sng",
    "validate_sng_archive",
    "write_manifest",
]
