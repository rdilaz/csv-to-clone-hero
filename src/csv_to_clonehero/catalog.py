"""Read-only Enchor catalog snapshots and the one-shot export helper.

The application never crawls here.  A snapshot is authoritative only when its
header says so and its checksum covers every chart row.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title

FORMAT_VERSION = 1
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "enchor.jsonl"


def _row_key(artist: str, title: str) -> tuple[str, str]:
    return normalize_artist(artist), normalize_title(title)


class CatalogLoadError(ValueError):
    """The local snapshot cannot safely be used for negative filtering."""


class EnchorCatalog:
    """Indexed, immutable-in-process catalog snapshot."""

    def __init__(self, path: Path | str = DEFAULT_CATALOG_PATH) -> None:
        self.path = Path(path)
        self.complete = False
        self.diagnostic = "catalog unavailable"
        self.snapshot_id = ""
        self._artists: set[str] = set()
        self._songs: dict[tuple[str, str], list[ChartCandidate]] = defaultdict(list)
        self._load()

    @property
    def available(self) -> bool:
        return self.complete

    @property
    def chart_count(self) -> int:
        return sum(len(rows) for rows in self._songs.values())

    def has_artist(self, artist: str) -> bool:
        return normalize_artist(artist) in self._artists

    def has_song(self, artist: str, title: str) -> bool:
        return bool(self._songs.get(_row_key(artist, title)))

    def candidates(self, artist: str, title: str) -> tuple[ChartCandidate, ...]:
        return tuple(self._songs.get(_row_key(artist, title), ()))

    def artists(self) -> tuple[str, ...]:
        return tuple(sorted(self._artists))

    def artist_songs(self, artist: str) -> tuple[tuple[str, tuple[ChartCandidate, ...]], ...]:
        key = normalize_artist(artist)
        return tuple(
            (title, tuple(rows))
            for (artist_key, title), rows in self._songs.items()
            if artist_key == key
        )

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read()
            lines = raw.splitlines()
            if not lines:
                raise CatalogLoadError("catalog is empty")
            header = json.loads(lines[0])
            if header.get("type") != "enchor-catalog" or header.get("format_version") != FORMAT_VERSION:
                raise CatalogLoadError("unsupported catalog format")
            if header.get("complete") is not True:
                raise CatalogLoadError("catalog snapshot is incomplete")
            expected = str(header.get("sha256") or "")
            actual = hashlib.sha256(b"\n".join(lines[1:]) + (b"\n" if len(lines) > 1 else b"")).hexdigest()
            if expected != actual:
                raise CatalogLoadError("catalog checksum mismatch")
            for raw_line in lines[1:]:
                row = json.loads(raw_line)
                if not isinstance(row, Mapping) or row.get("type") != "chart":
                    raise CatalogLoadError("malformed catalog row")
                candidate = ChartCandidate(**{k: v for k, v in row["candidate"].items() if k in ChartCandidate.__dataclass_fields__})
                if not candidate.chart_id:
                    raise CatalogLoadError("catalog row has no chart id")
                self._artists.add(normalize_artist(candidate.artist))
                self._songs[_row_key(candidate.artist, candidate.name)].append(candidate)
            self.snapshot_id = str(header.get("snapshot_id") or expected[:16])
            self.complete = True
            self.diagnostic = ""
        except (OSError, json.JSONDecodeError, TypeError, KeyError, CatalogLoadError) as exc:
            self.complete = False
            self.diagnostic = f"catalog unavailable: {exc}"


@lru_cache(maxsize=4)
def load_enchor_catalog(path: Path | str = DEFAULT_CATALOG_PATH) -> EnchorCatalog:
    """Load one immutable snapshot once per process."""
    return EnchorCatalog(Path(path).resolve())


def export_catalog(
    source: Any,
    destination: Path | str,
    *,
    checkpoint: Path | str | None = None,
    page_size: int = 250,
) -> dict[str, Any]:
    """Crawl broad Enchor search once, resuming from an external checkpoint.

    ``source`` is an Enchor adapter; this intentionally uses its throttled
    ``_post`` and ``_parse_chart`` methods but does not use the normal result cap.
    """
    destination = Path(destination)
    checkpoint_path = Path(checkpoint or str(destination) + ".checkpoint.json")
    partial_path = Path(str(destination) + ".partial")
    state = {"page": 1, "total": None, "ids": []}
    if checkpoint_path.exists():
        state.update(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    seen = set(int(value) for value in state.get("ids", []))
    rows: dict[int, dict[str, Any]] = {}
    if destination.exists():
        for line in destination.read_text(encoding="utf-8").splitlines():
            if line:
                row = json.loads(line)
                if row.get("type") == "chart":
                    rows[int(row["candidate"]["chart_id"])] = row
    if partial_path.exists():
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            if line:
                row = json.loads(line)
                if row.get("type") == "chart":
                    rows[int(row["candidate"]["chart_id"])] = row
    page = max(1, int(state.get("page", 1)))
    total = state.get("total")
    while total is None or len(seen) < int(total):
        payload = {"search": "", "per_page": min(250, max(1, page_size)), "page": page,
                   "instrument": None, "difficulty": None, "drumType": None,
                   "drumsReviewed": False, "sort": None, "source": "bridge"}
        response = None
        for attempt in range(8):
            try:
                response = source._post("/search", payload)
                break
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in (500, 503) and attempt < 7:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        if response is None:
            raise CatalogLoadError("Enchor request failed")
        data = response.get("data")
        reported = source._response_total(response)
        if not isinstance(data, list) or reported is None or (total is not None and reported != total):
            raise CatalogLoadError("unstable or malformed catalog page")
        total = reported
        if int(response.get("page", page)) != page or not data:
            raise CatalogLoadError("catalog page sequence is incomplete")
        page_rows: list[dict[str, Any]] = []
        for item in data:
            candidate = source._parse_chart(item)
            if candidate.chart_id:
                seen.add(int(candidate.chart_id))
                row = {"type": "chart", "candidate": asdict(candidate)}
                rows[int(candidate.chart_id)] = row
                page_rows.append(row)
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        with partial_path.open("a", encoding="utf-8", newline="\n") as stream:
            for row in page_rows:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_data = json.dumps({"page": page + 1, "total": total, "ids": sorted(seen)})
        fd, temporary = tempfile.mkstemp(prefix=checkpoint_path.name + ".", dir=checkpoint_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(checkpoint_data)
            _atomic_replace(temporary, checkpoint_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        page += 1
    if len(seen) != int(total):
        raise CatalogLoadError("catalog final count does not match reported total")
    body = "".join(json.dumps(rows[key], sort_keys=True, separators=(",", ":")) + "\n" for key in sorted(rows))
    checksum = hashlib.sha256(body.encode()).hexdigest()
    header = json.dumps({"type": "enchor-catalog", "format_version": FORMAT_VERSION,
                         "complete": True, "snapshot_id": str(int(time.time())),
                         "chart_count": len(rows), "out_of": int(total), "sha256": checksum},
                        sort_keys=True, separators=(",", ":")) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(header)
            stream.write(body)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    _atomic_replace(temporary, destination)
    partial_path.unlink(missing_ok=True)
    checkpoint_path.unlink(missing_ok=True)
    return {"complete": True, "chart_count": len(rows), "out_of": int(total), "path": str(destination)}


def _atomic_replace(source: Path, destination: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))
