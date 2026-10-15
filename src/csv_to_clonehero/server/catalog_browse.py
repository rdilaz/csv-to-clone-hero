"""Immutable full-catalog browsing, prepared by the existing job worker."""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from csv_to_clonehero.catalog import MAX_CATALOG_HEADER_BYTES, load_enchor_catalog
from csv_to_clonehero.catalog_validation import inspected_difficulty_coverage
from csv_to_clonehero.matching import score_expansion_candidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title


def catalog_key(chart: Any) -> str:
    return f"{chart.source}:{int(chart.chart_id)}"


def catalog_year(value: Any) -> int | None:
    """Return an explicit year from chart metadata, without guessing missing dates."""
    match = re.search(r"(?<!\d)((?:18|19|20|21)\d{2})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def catalog_compatibility(config: Any, chart: Any, db: Any) -> tuple[bool, str, dict]:
    if chart.source not in config.sources.enabled or not chart.supports_download:
        return False, "This chart source does not support downloads.", {}
    if chart.source == "enchor" and re.fullmatch(r"[0-9a-fA-F]{32}", str(chart.md5 or "")) is None:
        return False, "A valid chart package address is unavailable.", {}
    if not db.is_chart_artifact_eligible(chart.source, chart.chart_id):
        return False, "This chart artifact is rejected or unavailable.", {}
    # Reuse content/instrument/source hard gates, without treating an explicit
    # catalog choice as an uncertain identity match or imposing a score floor.
    try:
        result = score_expansion_candidate(normalize_artist(chart.artist), chart, config, include_compatible=True)
    except (TypeError, ValueError, AttributeError):
        return False, "Chart compatibility could not be verified.", {}
    gates = dict(result.gate_factors)
    if gates.get("hard_gate"):
        return False, "; ".join(result.reasons) or "This chart is incompatible.", gates
    return True, "", gates


def current_catalog_choice(config, db, workspace, choice, candidate, *, catalog_chart=None, allow_load=True) -> bool:
    projection = db._conn.execute("SELECT metadata_json FROM workspace_candidates WHERE workspace_id=? AND kind='catalog' AND candidate_id=?",
                                  (workspace, int(choice["candidate_id"]))).fetchone()
    if projection is None:
        return False
    if catalog_chart is None and allow_load:
        catalog = load_enchor_catalog(config.catalog_path)
        if not catalog.available:
            return False
        catalog_chart = catalog.get_chart(candidate.source, candidate.chart_id)
    if catalog_chart is None:
        return False
    metadata = json.loads(projection["metadata_json"])
    if str(catalog_chart.md5 or "") != str(candidate.md5 or "") or str(getattr(catalog_chart, "_catalog_row_hash", "")) != metadata.get("catalog_row_hash"):
        return False
    return catalog_compatibility(config, catalog_chart, db)[0]


def _preview_start(chart: Any) -> int | None:
    metadata = chart.source_metadata if isinstance(chart.source_metadata, dict) else {}
    for key in ("preview_start_ms", "preview_start_time", "previewStartTime", "preview_start", "previewStart"):
        value = metadata.get(key)
        if value is None:
            continue
        try:
            number = int(value)
            if 0 <= number <= (chart.song_length_ms or 24 * 3600 * 1000):
                return number
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def public_catalog_item(config: Any, db: Any, chart: Any, selected: dict) -> dict:
    key = catalog_key(chart)
    artifact = (str(chart.source or "").casefold(), str(chart.md5).casefold() if chart.md5 else f"chart:{chart.chart_id}")
    saved = selected.get(artifact)
    playable, reason, _ = catalog_compatibility(config, chart, db)
    md5 = str(chart.md5 or "").lower()
    valid_md5 = chart.source == "enchor" and re.fullmatch(r"[0-9a-f]{32}", md5) is not None
    return {
        "catalog_key": key, "source": chart.source, "chart_id": int(chart.chart_id),
        "candidate_id": int(saved["candidate_id"]) if saved is not None else None,
        "song_key": "|".join((normalize_artist(chart.artist), normalize_title(chart.name))),
        "title": chart.name, "artist": chart.artist, "album": chart.album,
        "charter": chart.charter, "genre": chart.genre, "year": chart.year,
        "parsed_year": catalog_year(chart.year),
        "instruments": list(chart.instruments or []), "song_length_ms": chart.song_length_ms,
        "preview_start_ms": _preview_start(chart), "md5": md5 if valid_md5 else "",
        "has_video_background": bool(chart.has_video_background),
        "selectable": playable, "blocked_reason": reason,
        "selected": saved is not None, "selected_in_scope": str(saved["kind"]) if saved is not None else None,
        "selected_catalog_key": f"{saved['source']}:{int(saved['chart_id'])}" if saved is not None and saved["kind"] == "catalog" else None,
        "difficulty_coverage": inspected_difficulty_coverage(db, chart),
        "preview": {"source": "enchor-package", "url": f"https://files.enchor.us/{md5}.sng" if valid_md5 else None,
                    "has_video": bool(chart.has_video_background), "available": valid_md5,
                    "availability": "unverified-until-open" if valid_md5 else "unavailable",
                    "reason": "" if valid_md5 else "A valid chart package address is unavailable."},
    }


class CatalogBrowser:
    def __init__(self, config: Any):
        self.config = config
        self._lock = threading.RLock()
        self._key = None
        self._requested_key = None
        self._state = "check_required"
        self._catalog = None
        self._entries = ()
        self._search = ()
        self._decades = ()
        self._songs = 0
        self._orders = OrderedDict()

    def identity(self) -> tuple:
        path: Path = self.config.catalog_path
        try:
            stat = path.stat()
            with path.open("rb") as stream:
                header = stream.readline(MAX_CATALOG_HEADER_BYTES + 1)
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, hashlib.sha256(header).hexdigest())
        except OSError:
            return (None,)

    def request(self) -> None:
        identity = self.identity()
        with self._lock:
            if identity == self._key or identity == self._requested_key:
                return
            self._requested_key = identity
            self._state = "queued"

    def pending(self) -> bool:
        with self._lock:
            return self._requested_key is not None and self._state == "queued"

    def prepare(self) -> None:
        with self._lock:
            identity = self._requested_key
            if identity is None:
                return
            self._state = "running"
        # Local authoritative snapshot only; never invoke a live search crawl.
        try:
            catalog = load_enchor_catalog(self.config.catalog_path)
            entries = catalog.charts() if catalog.available else ()
            search = tuple(" ".join(str(value or "") for value in (chart.artist, chart.name, chart.charter, chart.album)).casefold() for chart in entries)
            songs = len({(normalize_artist(chart.artist), normalize_title(chart.name)) for chart in entries})
            decades = tuple((year // 10 * 10) if (year := catalog_year(getattr(chart, "year", None))) is not None else None for chart in entries)
        except Exception:
            # A malformed optional metadata field must not kill the shared
            # worker or expose parser paths through public readiness.
            catalog, entries, search, songs, decades = None, (), (), 0, ()
        with self._lock:
            if identity != self.identity():
                self._state = "check_required"
                self._requested_key = None
                return
            self._key = identity
            self._catalog = catalog if catalog is not None and catalog.available else None
            self._entries, self._search, self._songs = entries, search, songs
            self._decades = decades
            self._orders.clear()
            self._state = "ready" if self._catalog is not None else "unavailable"
            self._requested_key = None

    def readiness(self) -> dict:
        with self._lock:
            current = self._key == self.identity() and self._catalog is not None
            ready = self._state == "ready" and current
            state = "ready" if ready else self._state if self._state in {"queued", "running", "unavailable"} else "check_required"
            return {"status": state, "available": ready, "busy": state in {"queued", "running"},
                    "message": "Chart catalog ready." if ready else "The local chart catalog is unavailable." if state == "unavailable" else "Preparing the chart catalog.",
                    "snapshot_token": "sha256:" + self._catalog.sha256 if ready else None,
                    "total_charts": len(self._entries) if ready else None,
                    "total_songs": self._songs if ready else None}

    def chart(self, source: str, chart_id: int):
        with self._lock:
            if not self.readiness()["available"]:
                return None
            return self._catalog.get_chart(source, chart_id)

    def page(self, *, offset: int, limit: int, q: str, order: str, seed: str, decade: int | None = None) -> tuple[tuple, int]:
        with self._lock:
            terms = tuple(q.casefold().split())
            cache_key = (terms, order, seed, decade)
            indices = self._orders.get(cache_key)
            if indices is None:
                indices = [index for index, text in enumerate(self._search) if all(term in text for term in terms)
                           and (decade is None or self._decades[index] == decade)]
                if order == "shuffle":
                    random.Random(hashlib.sha256((self._catalog.sha256 + seed).encode()).digest()).shuffle(indices)
                indices = tuple(indices)
                self._orders[cache_key] = indices
                self._orders.move_to_end(cache_key)
                while len(self._orders) > 4:
                    self._orders.popitem(last=False)
            return tuple(self._entries[index] for index in indices[offset:offset + limit]), len(indices)
