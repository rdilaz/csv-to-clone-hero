"""Spotify-first discovery orchestration.

This module is deliberately separate from :mod:`csv_to_clonehero.pipeline`.
The legacy pipeline owns automatic planning and downloads; this workflow only
imports a Spotify snapshot, discovers chart candidates, scores them, and
persists the result in a discovery workspace.  Selection and export can then
be performed by a browser integration without any implicit download step.

Network calls are made synchronously.  That keeps the injected ``Database``
connection on its caller thread and is sufficient for a resumable, blocking
Flask job.  Source adapters remain responsible for their own pagination; the
resolver also understands optional page/cap arguments and records the
pagination envelope returned by an adapter.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from rapidfuzz import fuzz

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.matching import (
    ELIGIBLE_REVIEW,
    EXCLUDED,
    INCOMPATIBLE,
    RECOMMENDED,
    eligibility_for,
    score_candidate,
    score_expansion_candidate,
)
from csv_to_clonehero.models import ChartCandidate, ScoreBreakdown, Track
from csv_to_clonehero.normalization import normalize_artist, normalize_title
from csv_to_clonehero.reporting import generate_spotify_reports
from csv_to_clonehero.sources.base import (
    ChartSource,
    get_source_capabilities,
    source_result_cap,
)
from csv_to_clonehero.spotify import LibraryImportResult, SpotifyClient


log = logging.getLogger(__name__)

NORMALIZATION_VERSION = "normalization-v1"
DEFAULT_WORKSPACE_KEY = "spotify"
DEFAULT_WORKSPACE_NAME = "Spotify library"
DEFAULT_SCAN_KEY = "exact"
DEFAULT_EXPANSION_SCAN_KEY = "expansion"
DEFAULT_RESULT_CAP = 250
CALIBRATION_SONG_COUNTS = (1, 5, 10, 25)

ProgressCallback = Callable[..., None]
LogCallback = Callable[..., None]


class DiscoveryCancelled(Exception):
    """Cooperative local cancellation requested between network operations."""


@dataclass
class DiscoveryStats:
    """Counters emitted by an exact or expansion discovery run."""

    playlists: int = 0
    library_items: int = 0
    library_tracks: int = 0
    total_tracks: int = 0
    total_queries: int = 0
    completed_queries: int = 0
    failed_queries: int = 0
    cached_queries: int = 0
    network_queries: int = 0
    catalog_queries: int = 0
    song_queries: int = 0
    fallback_queries: int = 0
    fallback_tracks: int = 0
    completed_tracks: int = 0
    scoring_tasks_total: int = 0
    scoring_tasks_completed: int = 0
    exact_candidates: int = 0
    exact_candidates_scored: int = 0
    exact_eligible: int = 0
    exact_recommended: int = 0
    expansion_artists_requested: int = 0
    expansion_artists_scanned: int = 0
    expansion_candidates: int = 0
    expansion_candidates_scored: int = 0
    expansion_eligible: int = 0
    source_errors: int = 0
    query_errors: int = 0
    partial: bool = False
    already_owned_tracks: int = 0
    suppressed_tracks: int = 0
    estimate: dict[str, Any] = field(default_factory=dict)
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)

    @property
    def tracks(self) -> int:
        return self.library_tracks

    @property
    def queries(self) -> int:
        return self.total_queries

    @property
    def tasks_total(self) -> int:
        return self.total_queries

    @property
    def tasks_completed(self) -> int:
        return self.completed_queries

    @property
    def exact_queries(self) -> int:
        return self.total_queries

    @property
    def exact_queries_completed(self) -> int:
        return self.completed_queries

    @property
    def exact_tracks(self) -> int:
        return self.total_tracks

    @property
    def eligible_candidates(self) -> int:
        return self.exact_eligible + self.expansion_eligible

    @property
    def messages(self) -> list[str]:
        return list(self.error_messages)


def estimate_spotify_duration(
    config: Any,
    *,
    query_count: int,
    track_count: int,
    cached_query_count: int = 0,
) -> dict[str, Any]:
    """Return a conservative initial estimate for the blocking discovery pass.

    Enchor is intentionally resolved sequentially.  The estimate therefore uses
    a measured-style range rather than pretending that the configured request
    delay is the complete request duration.  Once a run has enough completed
    work, the web layer replaces this bootstrap value with its observed rate.
    The four reference sizes are included so callers can display the same
    calibration basis used by the test fixtures and manual timing checks.
    """
    network = _value(config, "network", default=config)
    delay = max(0.0, float(_value(network, "request_delay_seconds", default=0.5) or 0.0))
    total_queries = max(0, int(query_count))
    cached = min(total_queries, max(0, int(cached_query_count)))
    fresh = total_queries - cached
    tracks = max(0, int(track_count))

    # These bounds cover a normal response, retries, and local candidate
    # scoring. They are deliberately exposed as a range because API latency
    # and source throttling vary substantially between runs.
    lower = fresh * max(1.5, delay + 1.0) + cached * 0.02 + tracks * 0.01
    typical = fresh * max(4.0, delay + 3.5) + cached * 0.03 + tracks * 0.025
    upper = fresh * max(9.0, delay + 8.0) + cached * 0.08 + tracks * 0.06
    query_seconds = fresh * max(4.0, delay + 3.5) + cached * 0.03
    scoring_seconds = tracks * 0.025
    if total_queries or tracks:
        lower += 3.0
        typical += 8.0
        upper += 20.0

    return {
        "available": bool(total_queries or tracks),
        "seconds": round(typical, 1),
        "lower_seconds": round(lower, 1),
        "upper_seconds": round(upper, 1),
        "query_count": total_queries,
        "fresh_queries": fresh,
        "cached_queries": cached,
        "track_count": tracks,
        "estimated_query_seconds": round(query_seconds, 1),
        "estimated_scoring_seconds": round(scoring_seconds, 1),
        "calibration_song_counts": list(CALIBRATION_SONG_COUNTS),
        "message": "Initial estimate; remaining time switches to the live completion rate after the run starts.",
    }


@dataclass
class DiscoveryResult:
    """Terminal value returned by :func:`run_spotify_discovery`.

    ``status == "complete"`` means every discovery task reached a terminal
    state.  ``partial`` is set when one or more source/query tasks failed or a
    source reported a cap/truncation.  A partial result is still useful and is
    intentionally persisted for browser review.
    """

    stats: DiscoveryStats = field(default_factory=DiscoveryStats)
    workspace_id: int | None = None
    workspace_key: str = ""
    scan_key: str = ""
    status: str = "created"
    phase: str = "idle"
    partial: bool = False
    error: str | None = None
    library_result: LibraryImportResult | None = None
    workspace: Any = None

    @property
    def library(self) -> LibraryImportResult | None:
        return self.library_result

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def success(self) -> bool:
        return self.complete and self.error is None

    @property
    def terminal(self) -> bool:
        return self.status not in {"created", "running", "pending"}

    @property
    def total_tasks(self) -> int:
        return self.stats.total_queries

    @property
    def completed_tasks(self) -> int:
        return self.stats.completed_queries

    @property
    def total_tracks(self) -> int:
        return self.stats.total_tracks


def run_spotify_inventory(
    config: AppConfig,
    db: Database,
    spotify_client: Any | None = None,
    *,
    client: Any | None = None,
    workspace_id: int | str | None = None,
    workspace_key: str = DEFAULT_WORKSPACE_KEY,
    include_liked: bool = False,
    on_progress: ProgressCallback | None = None,
) -> DiscoveryResult:
    """Sync Spotify containers without contacting chart sources."""
    spotify_client = client or spotify_client or _default_spotify_client(config)
    actual_id, actual_key = _ensure_workspace(
        db, workspace_id=workspace_id, workspace_key=workspace_key,
        workspace_name=DEFAULT_WORKSPACE_NAME, phase="spotify_inventory",
    )
    stats = DiscoveryStats()
    try:
        list_method = _value(spotify_client, "list_playlists", "get_playlists", default=None)
        playlists = list(list_method() if callable(list_method) else [])
        db.upsert_spotify_playlists(playlists)
        stats.playlists = len(playlists)
        if include_liked:
            liked_method = _value(spotify_client, "get_liked_tracks", "liked_tracks", default=None)
            if callable(liked_method):
                stats.library_items = len(list(liked_method()))
        db.update_workspace(actual_id, status="ready_for_scope", phase="ready_for_scope", metadata={
            "workflow": "spotify_inventory", "playlist_count": stats.playlists,
            "liked_item_count": stats.library_items,
        })
        payload = {"status": "ready_for_scope", "phase": "ready_for_scope", "message": "Spotify inventory ready for scope"}
        if on_progress is not None:
            _call_progress(on_progress, payload, 0, 0)
        return DiscoveryResult(stats=stats, workspace_id=actual_id, workspace_key=actual_key,
                               scan_key="inventory", status="ready_for_scope", phase="ready_for_scope")
    except Exception as exc:
        error = _error_text(exc)
        db.update_workspace(actual_id, status="failed", phase="spotify_inventory", metadata={"error": error})
        return DiscoveryResult(stats=stats, workspace_id=actual_id, workspace_key=actual_key,
                               scan_key="inventory", status="failed", phase="spotify_inventory", error=error)


# Friendly aliases for integrations that use the longer names.
SpotifyDiscoveryStats = DiscoveryStats
SpotifyDiscoveryResult = DiscoveryResult
SpotifyPipelineStats = DiscoveryStats
SpotifyPipelineResult = DiscoveryResult
ExpansionStats = DiscoveryStats
ExpansionResult = DiscoveryResult


@dataclass(frozen=True)
class _QuerySpec:
    key: str
    kind: str
    source_name: str
    source_version: str
    artist: str
    title: str
    cap: int
    query_text: str
    source: ChartSource = field(compare=False, repr=False)
    matcher_version: str = field(compare=False, repr=False)
    filters: Mapping[str, Any] = field(compare=False, repr=False)


@dataclass
class _QueryOutcome:
    candidates: list[ChartCandidate] = field(default_factory=list)
    error: str | None = None
    task_id: int | None = None
    cached: bool = False
    source_total: int | None = None
    returned_count: int = 0
    cap_reached: bool = False


class _RunContext:
    """Own progress persistence and callback compatibility for one scan."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        workspace_id: int,
        scan_key: str,
        stats: DiscoveryStats,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: LogCallback | None = None,
        on_track_scored: ProgressCallback | None = None,
        cancel_event: Any | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.workspace_id = workspace_id
        self.scan_key = scan_key
        self.stats = stats
        self.on_progress = on_progress
        self.on_log = on_log
        self.on_track_scored = on_track_scored
        self.cancel_event = cancel_event
        self.phase = "idle"
        self.message = ""
        self.task_total = 0
        self.task_done = 0
        self.task_failed = 0
        self.track_total = 0
        self.track_done = 0
        self.scoring_total = 0
        self.scoring_done = 0
        self._exact_keys: set[tuple[int, str, int]] = set()
        self._expansion_keys: set[tuple[str, str, int]] = set()
        self._exact_eligible: set[tuple[int, str, int]] = set()
        self._exact_recommended: set[tuple[int, str, int]] = set()
        self._expansion_eligible: set[tuple[str, str, int]] = set()

    @property
    def persist_progress(self) -> bool:
        scan = getattr(self.config, "scan", None)
        return bool(_value(scan, "persist_progress", default=True))

    def set_phase(self, phase: str, message: str = "") -> None:
        self.phase = phase
        if message:
            self.message = message
        self.emit()

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DiscoveryCancelled("Spotify discovery cancelled locally")

    def set_totals(self, *, tasks: int | None = None, tracks: int | None = None) -> None:
        if tasks is not None:
            self.task_total = max(0, int(tasks))
            self.stats.total_queries = self.task_total
        if tracks is not None:
            self.track_total = max(0, int(tracks))
            self.stats.total_tracks = self.track_total
            if self.scoring_done == 0:
                self.scoring_total = self.track_total
            self.stats.scoring_tasks_total = self.scoring_total

    def add_scoring_tasks(self, count: int) -> None:
        """Extend the work denominator for a second scoring pass."""
        self.scoring_total += max(0, int(count))
        self.stats.scoring_tasks_total = self.scoring_total
        self.emit()

    def log(self, message: str, *, level: str = "info") -> None:
        text = str(message)[:1000]
        if level == "error":
            log.error(text)
        elif level == "warning":
            log.warning(text)
        else:
            log.info(text)
        if self.on_log is None:
            return
        _call_log(self.on_log, text, level)

    def task_finished(
        self,
        *,
        error: str | None = None,
        cached: bool = False,
        source_error: bool = False,
    ) -> None:
        self.task_done += 1
        self.stats.completed_queries = self.task_done
        if cached:
            self.stats.cached_queries += 1
        else:
            self.stats.network_queries += 1
        if error:
            self.task_failed += 1
            self.stats.failed_queries = self.task_failed
            self.stats.query_errors += 1
            self.stats.errors += 1
            if source_error:
                self.stats.source_errors += 1
            if error not in self.stats.error_messages:
                self.stats.error_messages.append(error)
            self.stats.partial = True
        self.emit()

    def track_finished(self, track: Track) -> None:
        self.scoring_done += 1
        self.track_done = min(self.track_total, self.scoring_done)
        self.stats.scoring_tasks_completed = self.scoring_done
        self.stats.completed_tracks = self.track_done
        self.emit(message=f"Scored {track.title}")
        if self.on_track_scored is not None:
            payload = self.payload(message=f"Scored {track.title}")
            _call_track_callback(
                self.on_track_scored,
                payload,
                self.scoring_done,
                self.scoring_total,
                self.stats.exact_eligible,
                self.stats.exact_recommended,
            )

    def note_exact_score(
        self,
        track: Track,
        candidate: ChartCandidate,
        eligibility: str,
    ) -> None:
        key = (int(track.db_id or 0), str(candidate.source), int(candidate.chart_id))
        if key not in self._exact_keys:
            self._exact_keys.add(key)
            self.stats.exact_candidates += 1
            self.stats.exact_candidates_scored += 1
        if eligibility in {RECOMMENDED, ELIGIBLE_REVIEW}:
            self._exact_eligible.add(key)
        if eligibility == RECOMMENDED:
            self._exact_recommended.add(key)
        self.stats.exact_eligible = len(self._exact_eligible)
        self.stats.exact_recommended = len(self._exact_recommended)

    def note_expansion_score(
        self,
        artist_norm: str,
        candidate: ChartCandidate,
        eligibility: str,
    ) -> None:
        key = (artist_norm, str(candidate.source), int(candidate.chart_id))
        if key not in self._expansion_keys:
            self._expansion_keys.add(key)
            self.stats.expansion_candidates += 1
            self.stats.expansion_candidates_scored += 1
        if eligibility in {RECOMMENDED, ELIGIBLE_REVIEW}:
            self._expansion_eligible.add(key)
        self.stats.expansion_eligible = len(self._expansion_eligible)

    def payload(self, *, status: str = "running", message: str | None = None) -> dict[str, Any]:
        work_total = self.task_total + self.scoring_total
        work_done = self.task_done + self.scoring_done
        duration = estimate_spotify_duration(
            self.config,
            query_count=self.task_total,
            track_count=self.scoring_total,
            cached_query_count=self.stats.cached_queries,
        )
        query_fraction = self.task_done / self.task_total if self.task_total else 0.0
        scoring_fraction = self.scoring_done / self.scoring_total if self.scoring_total else 0.0
        estimated_completed_seconds = (
            duration["estimated_query_seconds"] * min(1.0, query_fraction)
            + duration["estimated_scoring_seconds"] * min(1.0, scoring_fraction)
        )
        return {
            "phase": self.phase,
            "status": status,
            "message": self.message if message is None else message,
            "total_tasks": self.task_total,
            "completed_tasks": self.task_done,
            "failed_tasks": self.task_failed,
            "cached_queries": self.stats.cached_queries,
            "total_tracks": self.track_total,
            "completed_tracks": self.track_done,
            "scoring_tasks_total": self.scoring_total,
            "scoring_tasks_completed": self.scoring_done,
            "total_work": work_total,
            "completed_work": work_done,
            "remaining_work": max(0, work_total - work_done),
            "estimated_total_seconds": duration["seconds"],
            "estimated_lower_seconds": duration["lower_seconds"],
            "estimated_upper_seconds": duration["upper_seconds"],
            "estimated_query_seconds": duration["estimated_query_seconds"],
            "estimated_scoring_seconds": duration["estimated_scoring_seconds"],
            "estimated_completed_seconds": round(estimated_completed_seconds, 1),
            "done": self.task_done,
            "total": self.task_total,
            "partial": bool(self.stats.partial),
        }

    def emit(self, *, status: str = "running", message: str | None = None) -> None:
        payload = self.payload(status=status, message=message)
        if self.persist_progress:
            self.db.update_workspace_progress(self.workspace_id, payload)
        if self.on_progress is not None:
            _call_progress(self.on_progress, payload, self.task_done, self.task_total)


def _value(owner: Any, *names: str, default: Any = None) -> Any:
    if owner is None:
        return default
    for name in names:
        try:
            value = getattr(owner, name)
        except (AttributeError, TypeError):
            continue
        if value is not None:
            return value
    return default


def _callable_value(owner: Any, *names: str, default: Any = None) -> Any:
    value = _value(owner, *names, default=default)
    if callable(value):
        try:
            return value()
        except TypeError:
            return default
    return value


def _positive_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _error_text(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:1000]


def _call_progress(callback: ProgressCallback, payload: Mapping[str, Any], done: int, total: int) -> None:
    """Support both Flask-style payload callbacks and legacy ``done,total`` callbacks."""
    try:
        signature = inspect.signature(callback)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        positional = []
        has_varargs = False
    try:
        if has_varargs or len(positional) >= 2:
            callback(done, total)
        else:
            callback(dict(payload))
    except Exception:
        log.debug("progress callback failed", exc_info=True)


def _call_log(callback: LogCallback, message: str, level: str) -> None:
    try:
        signature = inspect.signature(callback)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if len(positional) >= 2:
            callback(level, message)
        else:
            callback(message)
    except Exception:
        log.debug("log callback failed", exc_info=True)


def _call_track_callback(
    callback: ProgressCallback,
    payload: Mapping[str, Any],
    done: int,
    total: int,
    eligible: int,
    recommended: int,
) -> None:
    try:
        signature = inspect.signature(callback)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if len(positional) >= 4:
            callback(done, total, eligible, recommended)
        elif len(positional) >= 2:
            callback(done, total)
        else:
            callback(dict(payload))
    except Exception:
        log.debug("track callback failed", exc_info=True)


def _source_name(source: Any) -> str:
    value = _callable_value(source, "name", default="")
    text = str(value or "").strip()
    return text or source.__class__.__name__.casefold()


def _source_version(source: Any) -> str:
    value = _callable_value(
        source,
        "source_version",
        "cache_version",
        "api_version",
        "version",
        "SOURCE_VERSION",
        "__version__",
        default="",
    )
    text = str(value or "").strip()
    if text:
        return text
    cls = source.__class__
    return f"class:{cls.__module__}.{cls.__qualname__}:1"


def _matcher_version(config: Any) -> str:
    value = _callable_value(config, "matcher_version", default="")
    if not value:
        value = _callable_value(_value(config, "matching", default=None), "matcher_version", default="")
    return str(value or "spotify-v1")


def _configured_cap(config: Any, kind: str) -> int:
    search = _value(config, "search", default=config)
    specific = _value(search, "artist_result_cap", "artist_search_result_cap", default=None) if kind == "artist" else None
    value = specific if specific is not None else _value(
        search,
        "result_cap",
        "search_result_cap",
        "max_results",
        default=DEFAULT_RESULT_CAP,
    )
    return max(1, _positive_int(value, DEFAULT_RESULT_CAP) or DEFAULT_RESULT_CAP)


def _effective_cap(config: Any, source: Any, kind: str) -> int:
    configured = _configured_cap(config, kind)
    try:
        declared = source_result_cap(source, kind)
    except Exception:
        declared = None
    if declared is None:
        return configured
    return max(1, min(configured, int(declared)))


def _stable_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(child, depth + 1)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_stable_value(child, depth + 1) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _source_filters(config: Any, source: Any, kind: str) -> dict[str, Any]:
    search = _value(config, "search", default=config)
    filters: dict[str, Any] = {
        "normalization_version": NORMALIZATION_VERSION,
        "max_search_pages": _value(search, "max_search_pages", default=None),
    }
    for name in ("max_artist_pages", "artist_page_size"):
        value = _value(source, name, default=None)
        if value is not None:
            filters[name] = value
    declared = _callable_value(source, "cache_filters", "search_filters", "filters", default=None)
    if isinstance(declared, Mapping):
        filters["source_filters"] = _stable_value(declared)
    else:
        filters["source_filters"] = {}
    if kind == "artist":
        filters["query_endpoint"] = "artist"
    else:
        filters["query_endpoint"] = "song"
    return filters


def build_source_query_key(
    db: Database,
    config: AppConfig,
    source: ChartSource,
    kind: str,
    artist: str,
    title: str = "",
) -> str:
    """Build the cache key used by the Spotify resolver.

    The helper is public so an estimate endpoint or a Flask integration can
    calculate the same key without reaching into this module's internals.
    """
    name = _source_name(source)
    version = _source_version(source)
    cap = _effective_cap(config, source, kind)
    filters = _source_filters(config, source, kind)
    matcher = _matcher_version(config)
    return db.query_key(
        kind,
        name,
        artist.strip().casefold(),
        title.strip().casefold(),
        normalize_artist(artist),
        normalize_title(title),
        source_name=name,
        source_version=version,
        result_cap=cap,
        matcher_version=matcher,
        filters=filters,
    )


def _make_query_spec(
    db: Database,
    config: AppConfig,
    source: ChartSource,
    kind: str,
    artist: str,
    title: str = "",
) -> _QuerySpec:
    name = _source_name(source)
    version = _source_version(source)
    cap = _effective_cap(config, source, kind)
    filters = _source_filters(config, source, kind)
    matcher = _matcher_version(config)
    key = db.query_key(
        kind,
        name,
        artist.strip().casefold(),
        title.strip().casefold(),
        normalize_artist(artist),
        normalize_title(title),
        source_name=name,
        source_version=version,
        result_cap=cap,
        matcher_version=matcher,
        filters=filters,
    )
    query_text = artist if kind == "artist" else f"{artist} - {title}".strip(" -")
    return _QuerySpec(
        key=key,
        kind=kind,
        source_name=name,
        source_version=version,
        artist=artist,
        title=title,
        cap=cap,
        query_text=query_text,
        source=source,
        matcher_version=matcher,
        filters=filters,
    )


def _dedupe_sources(sources: Iterable[ChartSource] | ChartSource | None) -> list[ChartSource]:
    if sources is None:
        return []
    if hasattr(sources, "search_song") and not isinstance(sources, (list, tuple, set)):
        values = [sources]  # type: ignore[list-item]
    else:
        values = list(sources)  # type: ignore[arg-type]
    result: list[ChartSource] = []
    seen: set[str] = set()
    for source in values:
        name = _source_name(source)
        if name in seen:
            continue
        seen.add(name)
        result.append(source)
    return result


def _looks_like_spotify_client(value: Any) -> bool:
    return callable(_value(value, "fetch_library", "import_library", default=None))


def _resolve_sources(
    config: AppConfig,
    sources: Iterable[ChartSource] | ChartSource | None,
) -> list[ChartSource]:
    if sources is None:
        from csv_to_clonehero.sources import build_sources

        sources = build_sources(config)
    return _dedupe_sources(sources)


def _normalise_library_result(value: Any) -> LibraryImportResult:
    if isinstance(value, LibraryImportResult):
        return value
    if isinstance(value, Mapping):
        records = value.get("records", value.get("tracks", []))
        playlists = value.get("playlists", [])
        outcomes = value.get("outcomes", [])
        return LibraryImportResult(list(records or []), list(playlists or []), list(outcomes or []))
    if hasattr(value, "records"):
        return LibraryImportResult(
            list(getattr(value, "records", []) or []),
            list(getattr(value, "playlists", []) or []),
            list(getattr(value, "outcomes", []) or []),
            getattr(value, "counts", None) or LibraryImportResult([]).counts,
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return LibraryImportResult(list(value))
    raise TypeError("Spotify client did not return a LibraryImportResult")


def _fetch_library(
    client: Any,
    include_liked: bool,
    playlist_ids: Iterable[str] | None = None,
) -> LibraryImportResult:
    method = _value(client, "fetch_library", "import_library", default=None)
    if not callable(method):
        raise TypeError("Spotify client must provide fetch_library() or import_library()")
    selected = None
    if playlist_ids is not None:
        ids = {str(value) for value in playlist_ids if str(value).strip()}
        list_method = _value(client, "list_playlists", "get_playlists", default=None)
        if callable(list_method):
            selected = [playlist for playlist in list_method() if str(getattr(playlist, "id", "")) in ids]
    try:
        signature = inspect.signature(method)
        accepts_keyword = "include_liked" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        accepts_playlists = "playlists" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_keyword = True
        accepts_playlists = True
    kwargs: dict[str, Any] = {}
    if accepts_keyword:
        kwargs["include_liked"] = include_liked
    if selected is not None and accepts_playlists:
        kwargs["playlists"] = selected
    result = method(**kwargs) if kwargs else method()
    normalized = _normalise_library_result(result)
    if playlist_ids is not None and selected is None:
        ids = {str(value) for value in playlist_ids if str(value).strip()}
        normalized.records = [record for record in normalized.records if not record.playlist_ids or ids.intersection(record.playlist_ids)]
        normalized.playlists = [playlist for playlist in normalized.playlists if str(getattr(playlist, "id", "")) in ids]
    return normalized


def _default_spotify_client(config: AppConfig) -> SpotifyClient:
    network = _value(config, "network", default=None)
    spotify = _value(config, "spotify", default=None)
    client_id = _callable_value(config, "spotify_client_id", default="")
    if not client_id:
        client_id = _callable_value(spotify, "effective_client_id", "client_id", default="")
    scopes = _callable_value(config, "spotify_scopes", default=None)
    if not scopes:
        scopes = _callable_value(spotify, "normalized_scopes", "scopes", default=())
    return SpotifyClient(
        client_id=str(client_id or ""),
        scopes=scopes or (),
        timeout=float(_value(network, "timeout_seconds", default=30.0) or 30.0),
        max_retries=int(_value(network, "retries", default=3) or 3),
    )


def _candidate_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    values = getattr(value, "__dict__", None)
    return dict(values) if isinstance(values, dict) else {}


def _candidate_identity(value: Any, source_name: str, index: int) -> tuple[str, str]:
    data = _candidate_mapping(value)
    chart_id = data.get("chart_id", data.get("chartId", getattr(value, "chart_id", None)))
    if chart_id not in (None, "", 0, "0"):
        return source_name, str(chart_id)
    md5 = data.get("md5", data.get("hash", getattr(value, "md5", "")))
    if md5:
        return source_name, f"md5:{md5}"
    return source_name, f"row:{index}"


def _chart_id(value: Any) -> int:
    raw = getattr(value, "chart_id", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _candidate_metadata(value: Any) -> dict[str, Any]:
    data = _candidate_mapping(value)
    metadata = data.get("source_metadata", getattr(value, "source_metadata", {}))
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _metadata_number(metadata: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = metadata.get(name)
        if value is not None and not isinstance(value, bool):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = -1
            if parsed >= 0:
                return parsed
    pagination = metadata.get("pagination")
    if isinstance(pagination, Mapping):
        for name in names:
            value = pagination.get(name)
            if value is not None and not isinstance(value, bool):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    parsed = -1
                if parsed >= 0:
                    return parsed
    return None


def _unpack_search_result(value: Any) -> tuple[list[Any], dict[str, Any]]:
    """Accept list, mapping, tuple, and small adapter result envelopes."""
    if value is None:
        return [], {}
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], Mapping):
        return _result_items(value[0]), dict(value[1])
    if isinstance(value, Mapping):
        for key in ("candidates", "results", "items", "data"):
            if key in value:
                metadata = dict(value.get("metadata", value.get("pagination", {})) or {})
                metadata.update(
                    {
                        str(k): v
                        for k, v in value.items()
                        if k not in {"candidates", "results", "items", "data", "metadata", "pagination"}
                    }
                )
                return _result_items(value.get(key)), metadata
        return [value], {}
    for item_name in ("candidates", "results", "items"):
        items = getattr(value, item_name, None)
        if items is not None:
            metadata = getattr(value, "metadata", getattr(value, "pagination", {}))
            return _result_items(items), dict(metadata) if isinstance(metadata, Mapping) else {}
    return _result_items(value), {}


def _result_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _method_parameters(method: Any) -> dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return {}


def _call_search_method(
    method: Any,
    args: tuple[Any, ...],
    parameters: Mapping[str, inspect.Parameter],
    *,
    cap: int,
    page: int | None = None,
    page_size: int | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    cap_names = ("result_cap", "cap", "max_results", "limit")
    for name in cap_names:
        if name in parameters:
            kwargs[name] = cap if name != "limit" or page is None else page_size or cap
            break
    has_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if has_kwargs and not kwargs:
        kwargs["result_cap"] = cap
    if page is not None:
        if "page" in parameters:
            kwargs["page"] = page
        if "offset" in parameters:
            kwargs["offset"] = (page - 1) * (page_size or cap)
        if "per_page" in parameters:
            kwargs["per_page"] = page_size or cap
        elif "page_size" in parameters:
            kwargs["page_size"] = page_size or cap
    return method(*args, **kwargs)


def _invoke_source_search(
    spec: _QuerySpec,
    config: AppConfig,
) -> tuple[list[Any], dict[str, Any]]:
    method_name = "search_artist" if spec.kind == "artist" else "search_song"
    method = getattr(spec.source, method_name, None)
    if not callable(method):
        raise TypeError(f"source {spec.source_name!r} has no {method_name}() method")
    parameters = _method_parameters(method)
    args = (spec.artist,) if spec.kind == "artist" else (spec.artist, spec.title)
    has_page_argument = "page" in parameters or "offset" in parameters
    search = _value(config, "search", default=config)
    configured_pages = _positive_int(_value(search, "max_search_pages", default=None), 20) or 20
    source_pages = _positive_int(_value(spec.source, "max_artist_pages", default=None), configured_pages)
    max_pages = source_pages if spec.kind == "artist" else configured_pages
    page_size = min(spec.cap, _positive_int(_value(spec.source, "artist_page_size", default=None), spec.cap) or spec.cap)

    if not has_page_argument:
        return _unpack_search_result(
            _call_search_method(method, args, parameters, cap=spec.cap)
        )

    values: list[Any] = []
    metadata: dict[str, Any] = {}
    for page in range(1, max_pages + 1):
        page_values, page_metadata = _unpack_search_result(
            _call_search_method(
                method,
                args,
                parameters,
                cap=spec.cap,
                page=page,
                page_size=page_size,
            )
        )
        values.extend(page_values)
        metadata.update(page_metadata)
        total = _metadata_number(page_metadata, "source_total", "total", "found", "out_of")
        if not page_values:
            break
        if total is not None and len(values) >= total:
            break
        next_value = page_metadata.get("next") or page_metadata.get("has_more")
        if next_value is False:
            break
        if len(values) >= spec.cap:
            break
    metadata["pages"] = min(page, max_pages)
    return values, metadata


def _prepare_result_candidates(
    db: Database,
    source: ChartSource,
    raw_values: Iterable[Any],
    adapter_metadata: Mapping[str, Any],
    cap: int,
) -> tuple[list[ChartCandidate], int | None, int, bool]:
    source_name = _source_name(source)
    values = list(raw_values)
    unique_values: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(values):
        identity = _candidate_identity(value, source_name, index)
        if identity in seen:
            continue
        seen.add(identity)
        unique_values.append(value)

    source_total = _metadata_number(adapter_metadata, "source_total", "total", "found", "out_of")
    explicit_cap = bool(adapter_metadata.get("cap_reached", False))
    explicit_returned = _metadata_number(adapter_metadata, "returned_count")
    for value in unique_values:
        data = _candidate_mapping(value)
        candidate_metadata = _candidate_metadata(value)
        candidate_total = _metadata_number(
            candidate_metadata,
            "source_total",
            "total",
            "found",
            "out_of",
        )
        source_total = max(
            [
                number
                for number in (
                    source_total,
                    _metadata_number(data, "source_total"),
                    candidate_total,
                )
                if number is not None
            ],
            default=None,
        )
        explicit_cap = explicit_cap or bool(data.get("cap_reached", False)) or bool(
            candidate_metadata.get("cap_reached", False)
        )
    capped = unique_values[:cap]
    returned_count = len(capped)
    supports_pagination = False
    try:
        capabilities = get_source_capabilities(source)
        supports_pagination = capabilities.supports_pagination
    except Exception:
        pass
    cap_reached = explicit_cap or len(unique_values) > cap
    if source_total is not None and returned_count >= cap and source_total > cap:
        cap_reached = True
    # An adapter that advertises pagination and returns exactly the requested
    # cap has no safe way to prove that the catalog ended there.
    if not cap_reached and returned_count >= cap and supports_pagination and source_total is None:
        cap_reached = True

    pagination = {
        "source_total": source_total,
        "returned_count": returned_count,
        "result_cap": cap,
        "cap_reached": cap_reached,
        "complete": not cap_reached,
    }
    pagination.update({key: _stable_value(value) for key, value in adapter_metadata.items()})
    pagination["source_total"] = source_total
    pagination["returned_count"] = returned_count
    pagination["result_cap"] = cap
    pagination["cap_reached"] = cap_reached
    pagination["complete"] = not cap_reached

    prepared: list[ChartCandidate] = []
    for value in capped:
        if isinstance(value, ChartCandidate):
            candidate: Any = copy.copy(value)
        else:
            candidate = _candidate_mapping(value)
        if isinstance(candidate, Mapping):
            candidate = dict(candidate)
            candidate["source"] = source_name
            candidate["source_total"] = source_total
            candidate["returned_count"] = returned_count
            candidate["cap_reached"] = cap_reached
            existing_metadata = candidate.get("source_metadata", {})
            existing_metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
            existing_metadata["pagination"] = dict(pagination)
            candidate["source_metadata"] = existing_metadata
        else:
            candidate.source = source_name
            candidate.source_total = source_total
            candidate.returned_count = returned_count
            candidate.cap_reached = cap_reached
            existing_metadata = getattr(candidate, "source_metadata", {})
            candidate.source_metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
            candidate.source_metadata["pagination"] = dict(pagination)
        try:
            stored = db.upsert_candidate(candidate)
        except Exception as exc:
            log.warning("Could not persist %s candidate: %s", source_name, _error_text(exc))
            continue
        prepared.append(stored)
    # ``explicit_returned`` is retained only as a compatibility signal for
    # adapters whose envelope reports the post-cap count.
    if explicit_returned is not None and not returned_count:
        returned_count = min(cap, explicit_returned)
    return prepared, source_total, returned_count, cap_reached


def _prepare_cached_candidates(
    source: ChartSource,
    candidates: Iterable[ChartCandidate],
    *,
    cap: int,
    source_total: int | None,
    returned_count: int | None,
    cap_reached: bool,
) -> list[ChartCandidate]:
    result: list[ChartCandidate] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(candidates):
        candidate = copy.copy(value)
        candidate.source = _source_name(source)
        identity = _candidate_identity(candidate, candidate.source, index)
        if identity in seen:
            continue
        seen.add(identity)
        candidate.source_total = source_total
        candidate.returned_count = min(cap, returned_count or 0) if returned_count is not None else len(result) + 1
        candidate.cap_reached = cap_reached
        metadata = dict(candidate.source_metadata) if isinstance(candidate.source_metadata, Mapping) else {}
        metadata.setdefault(
            "pagination",
            {
                "source_total": source_total,
                "returned_count": returned_count or len(result) + 1,
                "result_cap": cap,
                "cap_reached": cap_reached,
                "complete": not cap_reached,
            },
        )
        candidate.source_metadata = metadata
        result.append(candidate)
        if len(result) >= cap:
            break
    return result


def _task_metadata(spec: _QuerySpec, **updates: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "query_kind": spec.kind,
        "source": spec.source_name,
        "source_version": spec.source_version,
        "matcher_version": spec.matcher_version,
        "normalization_version": NORMALIZATION_VERSION,
        "result_cap": spec.cap,
        "filters": _stable_value(spec.filters),
    }
    metadata.update(updates)
    return metadata


def _cache_settings(config: Any) -> tuple[bool, int, bool, bool]:
    cache = _value(config, "cache", default=None)
    scan = _value(config, "scan", default=None)
    enabled = bool(_value(cache, "enabled", default=True))
    ttl = _value(cache, "ttl_days", "cache_ttl_days", default=None)
    if ttl is None:
        ttl = _callable_value(config, "cache_ttl_days", default=None)
    ttl_days = int(ttl if ttl is not None else _value(_value(config, "network", default=None), "cache_ttl_days", default=7) or 7)
    resume = bool(_value(scan, "resume", default=True)) and bool(_value(cache, "resume", default=True))
    retry_failed = bool(_value(scan, "retry_failed_queries", default=True))
    return enabled, ttl_days, resume, retry_failed


def _resolve_queries(
    ctx: _RunContext,
    config: AppConfig,
    db: Database,
    specs: Iterable[_QuerySpec],
    outcomes: dict[str, _QueryOutcome],
    task_ids: dict[str, int],
    scan_id: int,
    *,
    only_missing: bool = True,
    persisted_errors: Mapping[str, str] | None = None,
) -> None:
    enabled, ttl_days, resume, retry_failed = _cache_settings(config)
    specs_list = list(specs)
    for spec in specs_list:
        ctx.check_cancelled()
        if only_missing and spec.key in outcomes:
            continue
        persisted_error = (persisted_errors or {}).get(spec.key)
        if persisted_error:
            task_id = db.upsert_discovery_task(
                ctx.workspace_id,
                source=spec.source_name,
                kind=spec.kind,
                query_key=spec.key,
                query_text=spec.query_text,
                scan_id=scan_id,
                status="error",
                total=1,
                completed=1,
                result_cap=spec.cap,
                error=persisted_error,
                metadata=_task_metadata(spec, resumed_error=True, error=persisted_error),
            )
            task_ids[spec.key] = task_id
            outcomes[spec.key] = _QueryOutcome(
                error=persisted_error,
                task_id=task_id,
                cached=True,
            )
            ctx.task_finished(error=persisted_error, cached=True, source_error=True)
            ctx.log(
                f"Reusing previous failed {spec.source_name} {spec.kind} query: {spec.query_text}",
                level="warning",
            )
            continue
        task_id = db.upsert_discovery_task(
            ctx.workspace_id,
            source=spec.source_name,
            kind=spec.kind,
            query_key=spec.key,
            query_text=spec.query_text,
            scan_id=scan_id,
            status="pending",
            total=1,
            completed=0,
            result_cap=spec.cap,
            metadata=_task_metadata(spec),
        )
        task_ids[spec.key] = task_id
        db.update_discovery_task(task_id, status="running", completed=0, total=1)
        ctx.log(f"Resolving {spec.source_name} {spec.kind} query: {spec.query_text}")

        cached_value: tuple[list[ChartCandidate], str | None] | None = None
        cache_metadata: Any = None
        if enabled and resume:
            cached_value = db.get_cached_query(
                spec.key,
                ttl_days,
                result_cap=spec.cap,
                source_name=spec.source_name,
                source_version=spec.source_version,
            )
            cache_metadata = db.get_cached_query_metadata(spec.key)
            if cached_value is not None and cached_value[1] and retry_failed:
                cached_value = None

        if cached_value is not None:
            cached_candidates, cached_error = cached_value
            if cached_error:
                error = str(cached_error)[:1000]
                db.update_discovery_task(
                    task_id,
                    status="error",
                    completed=1,
                    total=1,
                    error=error,
                    metadata_json=_task_metadata(spec, cached=True, error=error),
                )
                outcomes[spec.key] = _QueryOutcome(error=error, task_id=task_id, cached=True)
                ctx.task_finished(error=error, cached=True, source_error=True)
                continue
            source_total = cache_metadata["source_total"] if cache_metadata is not None else None
            returned_count = cache_metadata["returned_count"] if cache_metadata is not None else None
            cap_reached = bool(cache_metadata["cap_reached"]) if cache_metadata is not None else False
            candidates = _prepare_cached_candidates(
                spec.source,
                cached_candidates,
                cap=spec.cap,
                source_total=source_total,
                returned_count=returned_count,
                cap_reached=cap_reached,
            )
            if cap_reached:
                ctx.stats.partial = True
            metadata = _task_metadata(
                spec,
                cached=True,
                source_total=source_total,
                returned_count=len(candidates),
                cap_reached=cap_reached,
            )
            db.update_discovery_task(
                task_id,
                status="completed",
                completed=1,
                total=1,
                source_total=source_total,
                returned_count=len(candidates),
                cap_reached=cap_reached,
                metadata_json=metadata,
            )
            outcomes[spec.key] = _QueryOutcome(
                candidates=candidates,
                task_id=task_id,
                cached=True,
                source_total=source_total,
                returned_count=len(candidates),
                cap_reached=cap_reached,
            )
            ctx.task_finished(cached=True)
            continue

        try:
            raw_values, adapter_metadata = _invoke_source_search(spec, config)
            candidates, source_total, returned_count, cap_reached = _prepare_result_candidates(
                db,
                spec.source,
                raw_values,
                adapter_metadata,
                spec.cap,
            )
        except Exception as exc:
            error = _error_text(exc)
            # Search errors intentionally do not become search-cache hits.  A
            # later run can retry them while this run still reaches a terminal
            # task state and keeps results from other sources.
            db.record_error("spotify_discovery", error, f"{spec.source_name}:{spec.query_text}")
            db.update_discovery_task(
                task_id,
                status="error",
                completed=1,
                total=1,
                error=error,
                metadata_json=_task_metadata(spec, error=error, cached=False),
            )
            outcomes[spec.key] = _QueryOutcome(error=error, task_id=task_id)
            ctx.task_finished(error=error, source_error=True)
            ctx.log(f"{spec.source_name} query failed: {error}", level="warning")
            continue

        query_metadata = _task_metadata(
            spec,
            cached=False,
            source_total=source_total,
            returned_count=returned_count,
            cap_reached=cap_reached,
        )
        # ``complete=False`` in search_queries means the source was truncated,
        # not that this discovery task is unfinished.  Mark the task terminal
        # immediately after recording the cache row.  A disabled cache still
        # persists candidates/tasks, but does not retain a reusable query row.
        if enabled:
            db.record_query(
                spec.key,
                spec.kind,
                spec.query_text,
                candidates,
                source_name=spec.source_name,
                source_version=spec.source_version,
                result_cap=spec.cap,
                source_total=source_total,
                cap_reached=cap_reached,
                returned_count=returned_count,
                complete=not cap_reached,
                metadata=query_metadata,
                workspace_id=ctx.workspace_id,
                task_id=task_id,
            )
        db.update_discovery_task(
            task_id,
            status="completed",
            completed=1,
            total=1,
            source_total=source_total,
            returned_count=returned_count,
            cap_reached=cap_reached,
            error=None,
            metadata_json=query_metadata,
        )
        outcomes[spec.key] = _QueryOutcome(
            candidates=candidates,
            task_id=task_id,
            source_total=source_total,
            returned_count=returned_count,
            cap_reached=cap_reached,
        )
        ctx.task_finished()
        if cap_reached:
            ctx.stats.partial = True
            ctx.log(
                f"{spec.source_name} returned a capped {spec.kind} catalog for {spec.query_text}",
                level="warning",
            )


def _primary_artist_specs(
    db: Database,
    config: AppConfig,
    sources: list[ChartSource],
    tracks: list[Track],
) -> tuple[dict[str, tuple[str, str, str]], dict[str, list[_QuerySpec]]]:
    counts: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for track in tracks:
        norm = track.primary_artist_norm or normalize_artist(track.primary_artist)
        counts[norm] = counts.get(norm, 0) + 1
        display_names.setdefault(norm, track.primary_artist)
    search = _value(config, "search", default=config)
    catalog_first = bool(_value(search, "artist_catalog_first", default=True))
    minimum = _positive_int(_value(search, "artist_catalog_min_songs", default=3), 3) or 3
    track_specs: dict[str, tuple[str, str, str]] = {}
    query_specs: dict[str, list[_QuerySpec]] = {}
    for track in tracks:
        norm = track.primary_artist_norm or normalize_artist(track.primary_artist)
        if catalog_first and counts.get(norm, 0) >= minimum:
            logical = ("artist", display_names.get(norm, track.primary_artist), "")
        else:
            logical = ("song", track.primary_artist, track.title)
        track_specs[track.track_key] = logical
        query_specs[track.track_key] = [
            _make_query_spec(db, config, source, *logical) for source in sources
        ]
    return track_specs, query_specs


def _score_breakdown_json(breakdown: Any, eligibility: str, reasons: Sequence[str]) -> str:
    if hasattr(breakdown, "as_dict") and callable(breakdown.as_dict):
        try:
            data = breakdown.as_dict()
        except Exception:
            data = {}
    else:
        try:
            data = asdict(breakdown)
        except (TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    # These top-level fields are compatibility aliases of the grouped
    # ``factors`` envelope and can multiply source issue evidence several
    # times per candidate.
    for duplicate in ("machine_factors", "match_factors", "quality_factors", "gate_factors"):
        data.pop(duplicate, None)
    factors = data.get("factors")
    if isinstance(factors, dict):
        issue_groups = [factors.get("source_issues")]
        grouped_match = factors.get("match")
        if isinstance(grouped_match, dict):
            issue_groups.append(grouped_match.get("source_issues"))
        for issue_group in issue_groups:
            if isinstance(issue_group, dict) and isinstance(issue_group.get("details"), list):
                issue_group["details"] = [str(value)[:500] for value in issue_group["details"][:20]]
    explanation = ""
    explain = getattr(breakdown, "explain", None)
    if callable(explain):
        try:
            explanation = str(explain())
        except Exception:
            explanation = ""
    data.update(
        {
            "explanation": explanation,
            "quality": float(getattr(breakdown, "quality", 0.0) or 0.0),
            "score": float(getattr(breakdown, "final_score", 0.0) or 0.0),
            "decision": str(getattr(breakdown, "decision", "REJECT") or "REJECT"),
            "eligibility": eligibility,
            "eligibility_state": eligibility,
            "eligibility_reasons": list(reasons),
        }
    )
    return json.dumps(data, ensure_ascii=True, default=str)


def _score_eligibility(breakdown: Any, config: AppConfig) -> tuple[str, list[str]]:
    state = str(getattr(breakdown, "eligibility", "") or "")
    reasons = [str(reason) for reason in (getattr(breakdown, "eligibility_reasons", []) or [])]
    if state in {RECOMMENDED, ELIGIBLE_REVIEW, EXCLUDED, INCOMPATIBLE}:
        return state, reasons
    try:
        result = eligibility_for(breakdown, config)
        return str(result.state), list(result.reasons)
    except Exception:
        decision = str(getattr(breakdown, "decision", "REJECT") or "REJECT")
        score = float(getattr(breakdown, "final_score", 0.0) or 0.0)
        matching = _value(config, "matching", default=config)
        auto = float(_value(matching, "recommendation_threshold", "auto_accept_threshold", default=90.0) or 90.0)
        review = float(_value(matching, "export_floor", "review_threshold", default=70.0) or 70.0)
        if decision == "INCOMPATIBLE":
            return INCOMPATIBLE, reasons
        if score >= auto:
            return RECOMMENDED, reasons or ["score meets recommendation threshold"]
        if score >= review:
            return ELIGIBLE_REVIEW, reasons or ["score meets export review threshold"]
        return EXCLUDED, reasons or ["score below export eligibility threshold"]


def _scoring_error(error: BaseException) -> ScoreBreakdown:
    breakdown = ScoreBreakdown(decision="REJECT")
    breakdown.reasons.append(f"scoring error: {_error_text(error)}")
    return breakdown


def _shortlist_exact_candidates(
    config: AppConfig,
    track: Track,
    candidates: Iterable[
        tuple[ChartCandidate, list[tuple[int | None, _QuerySpec]]]
    ],
) -> list[tuple[ChartCandidate, list[tuple[int | None, _QuerySpec]]]]:
    """Keep likely identity matches before expensive scoring and persistence.

    Source responses remain complete in the query cache. This only bounds the
    per-track fan-out, which previously produced hundreds of clearly unrelated
    rows for broad Enchor responses.
    """
    values = list(candidates)
    search = _value(config, "search", default=config)
    limit = max(5, int(_value(search, "candidate_shortlist_size", default=12) or 12))
    if len(values) <= limit:
        return values
    target_title = track.title_norm or normalize_title(track.title)
    target_artist = track.primary_artist_norm or normalize_artist(track.primary_artist)

    ranked: list[
        tuple[bool, float, float, int, tuple[ChartCandidate, list[tuple[int | None, _QuerySpec]]]]
    ] = []
    for index, value in enumerate(values):
        candidate = value[0]
        candidate_title = candidate.name_norm or normalize_title(candidate.name)
        candidate_artist = candidate.artist_norm or normalize_artist(candidate.artist)
        title_score = float(fuzz.ratio(target_title, candidate_title))
        artist_score = float(fuzz.ratio(target_artist, candidate_artist))
        exact_title = bool(target_title and target_title == candidate_title)
        relevance = title_score * 0.72 + artist_score * 0.28
        ranked.append((exact_title, relevance, artist_score, -index, value))

    ranked.sort(key=lambda item: item[:4], reverse=True)
    exact = [item for item in ranked if item[0]][:limit]
    selected = exact + [item for item in ranked if not item[0]][: max(0, limit - len(exact))]
    return [item[4] for item in selected]


def _score_exact_track(
    ctx: _RunContext,
    config: AppConfig,
    db: Database,
    track: Track,
    specs: list[_QuerySpec],
    outcomes: Mapping[str, _QueryOutcome],
    source_by_name: Mapping[str, ChartSource],
    *,
    catalog: bool,
) -> tuple[bool, bool]:
    """Score all candidates for one track; return ``(had_error, has_eligible)``."""
    by_identity: dict[tuple[str, int], tuple[ChartCandidate, list[tuple[int | None, _QuerySpec]]]] = {}
    had_error = False
    for spec in specs:
        outcome = outcomes.get(spec.key)
        if outcome is None:
            had_error = True
            continue
        if outcome.error:
            had_error = True
            error = f"{spec.source_name} {spec.kind} query for {track.title}: {outcome.error}"
            db.record_error("spotify_discovery", error, track.track_key)
            continue
        for candidate in outcome.candidates:
            identity = (str(candidate.source), _chart_id(candidate))
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = (candidate, [(outcome.task_id, spec)])
            else:
                existing[1].append((outcome.task_id, spec))

    has_eligible = False
    shortlisted = _shortlist_exact_candidates(config, track, by_identity.values())
    for candidate, origins in shortlisted:
        source = source_by_name.get(str(candidate.source))
        if source is None and specs:
            source = specs[0].source
        try:
            breakdown = score_candidate(track, candidate, config, source)
        except Exception as exc:
            breakdown = _scoring_error(exc)
            db.record_error("spotify_discovery_score", _error_text(exc), track.track_key)
        eligibility, reasons = _score_eligibility(breakdown, config)
        ctx.note_exact_score(track, candidate, eligibility)
        if eligibility in {RECOMMENDED, ELIGIBLE_REVIEW}:
            has_eligible = True
        # Keep incompatible rows in the reusable raw query cache, not in the
        # user workspace. They cannot be selected and dominated write volume
        # in large runs.
        if eligibility == INCOMPATIBLE:
            continue
        metadata = {
            "query_kind": "artist" if catalog else "song",
            "catalog": catalog,
            "query_keys": [spec.key for _task_id, spec in origins],
            "source": str(candidate.source),
            "source_version": _source_version(source) if source is not None else "",
        }
        breakdown_json = _score_breakdown_json(breakdown, eligibility, reasons)
        task_id = origins[0][0] if origins else None
        db.upsert_workspace_candidate(
            ctx.workspace_id,
            track.db_id,
            candidate,
            score=float(getattr(breakdown, "final_score", 0.0) or 0.0),
            quality=float(getattr(breakdown, "quality", 0.0) or 0.0),
            decision=str(getattr(breakdown, "decision", "REJECT") or "REJECT"),
            eligibility=eligibility,
            kind="exact",
            task_id=task_id,
            breakdown_json=breakdown_json,
            metadata=metadata,
        )
        # One candidate can be returned by a catalog query and a fallback
        # query.  Preserve both task relationships without creating another
        # exact candidate row.
        for extra_task_id, extra_spec in origins[1:]:
            if extra_task_id is not None:
                db.upsert_workspace_candidate(
                    ctx.workspace_id,
                    track.db_id,
                    candidate,
                    score=float(getattr(breakdown, "final_score", 0.0) or 0.0),
                    quality=float(getattr(breakdown, "quality", 0.0) or 0.0),
                    decision=str(getattr(breakdown, "decision", "REJECT") or "REJECT"),
                    eligibility=eligibility,
                    kind="exact",
                    task_id=extra_task_id,
                    breakdown_json=breakdown_json,
                    metadata={**metadata, "query_key": extra_spec.key},
                )
    return had_error, has_eligible


def _ensure_workspace(
    db: Database,
    *,
    workspace_id: int | str | None,
    workspace_key: str,
    workspace_name: str,
    phase: str,
) -> tuple[int, str]:
    if workspace_id is not None:
        row = db.get_workspace(workspace_id)
        if row is None:
            raise ValueError(f"unknown discovery workspace: {workspace_id}")
        actual_id = int(row["id"])
        actual_key = str(row["workspace_key"])
        db.update_workspace(actual_id, status="running", phase=phase)
        return actual_id, actual_key
    actual_key = workspace_key.strip() or DEFAULT_WORKSPACE_KEY
    actual_id = db.create_workspace(
        workspace_key=actual_key,
        name=workspace_name or DEFAULT_WORKSPACE_NAME,
        kind="exact",
        status="running",
        phase=phase,
        metadata={"workflow": "spotify_discovery"},
    )
    return actual_id, actual_key


def _begin_scan(db: Database, workspace_id: int, scan_key: str, kind: str) -> int:
    return db.create_discovery_scan(
        workspace_id,
        scan_key=scan_key,
        kind=kind,
        status="running",
        metadata={"workflow": "spotify_discovery", "kind": kind},
    )


def _finish(
    ctx: _RunContext,
    db: Database,
    *,
    workspace_key: str,
    scan_key: str,
    scan_id: int,
    status: str,
    phase: str,
    library_result: LibraryImportResult | None = None,
    error: str | None = None,
) -> DiscoveryResult:
    ctx.phase = phase
    if error:
        ctx.stats.errors += 1
        if error not in ctx.stats.error_messages:
            ctx.stats.error_messages.append(error)
    ctx.stats.partial = ctx.stats.partial or bool(ctx.task_failed) or bool(error)
    ctx.stats.total_queries = ctx.task_total
    ctx.stats.completed_queries = ctx.task_done
    ctx.stats.failed_queries = ctx.task_failed
    ctx.stats.scoring_tasks_total = ctx.scoring_total
    ctx.stats.scoring_tasks_completed = ctx.scoring_done
    final_message = error or (
        "Discovery completed with partial results" if ctx.stats.partial else "Discovery complete"
    )
    payload = ctx.payload(status=status, message=final_message)
    payload["error"] = error
    # Terminal state is always persisted even when periodic progress updates
    # are disabled by configuration.
    ctx.db.update_workspace_progress(ctx.workspace_id, payload)
    if ctx.on_progress is not None:
        _call_progress(ctx.on_progress, payload, ctx.task_done, ctx.task_total)
    metadata = {
        "workflow": "spotify_discovery",
        "scan_key": scan_key,
        "scan_id": scan_id,
        "partial": ctx.stats.partial,
        "error": error,
        "stats": asdict(ctx.stats),
    }
    db.update_workspace(
        ctx.workspace_id,
        status=status,
        phase=phase,
        total_tasks=ctx.task_total,
        completed_tasks=ctx.task_done,
        failed_tasks=ctx.task_failed,
        total_tracks=ctx.track_total,
        completed_tracks=ctx.track_done,
        source_count=len({row["source"] for row in db.get_discovery_tasks(ctx.workspace_id)}),
        error_count=ctx.stats.query_errors + (1 if error else 0),
        metadata=metadata,
    )
    # ``create_discovery_scan`` is also the compatibility update API for the
    # additive schema.  The task and workspace rows carry the detailed state.
    db.create_discovery_scan(
        ctx.workspace_id,
        scan_key=scan_key,
        kind="expansion" if scan_key == DEFAULT_EXPANSION_SCAN_KEY else "exact",
        status=status,
        metadata=metadata,
    )
    try:
        generate_spotify_reports(ctx.config, db, ctx.workspace_id)
    except Exception as report_error:
        # Reports are supporting output; never turn a completed discovery into
        # a failed scan because a report directory is temporarily unavailable.
        ctx.log(f"Could not write Spotify discovery reports: {_error_text(report_error)}", level="warning")
    row = db.get_workspace(ctx.workspace_id)
    ctx.log(final_message, level="error" if error else "warning" if ctx.stats.partial else "info")
    return DiscoveryResult(
        stats=ctx.stats,
        workspace_id=ctx.workspace_id,
        workspace_key=workspace_key,
        scan_key=scan_key,
        status=status,
        phase=phase,
        partial=ctx.stats.partial,
        error=error,
        library_result=library_result,
        workspace=row,
    )


def build_spotify_estimate(
    config: AppConfig,
    db: Database,
    tracks: Iterable[Track] | None = None,
    sources: Iterable[ChartSource] | ChartSource | None = None,
    *,
    workspace_id: int | str | None = None,
) -> dict[str, Any]:
    """Build the same cache-aware query estimate used by Spotify discovery."""
    source_list = _resolve_sources(config, sources)
    if tracks is None:
        tracks = db.get_spotify_tracks()
    track_list = list(tracks)
    owned_ids = db.get_workspace_owned_track_ids(workspace_id) if workspace_id is not None else set()
    searchable = [track for track in track_list if int(track.db_id or 0) not in owned_ids]
    _track_specs, specs_by_track = _primary_artist_specs(db, config, source_list, searchable)
    specs = {spec.key: spec for values in specs_by_track.values() for spec in values}
    enabled, ttl_days, resume, _retry_failed = _cache_settings(config)
    cached = 0
    partial_cached = 0
    catalog = 0
    songs = 0
    for spec in specs.values():
        if spec.kind == "artist":
            catalog += 1
        else:
            songs += 1
        if not (enabled and resume):
            continue
        # Freshness-only check: materializing thousands of cached candidates
        # per spec made large-workspace estimates take minutes.
        row = db.fresh_cached_query(
            spec.key, ttl_days, result_cap=spec.cap,
            source_name=spec.source_name, source_version=spec.source_version,
        )
        if row is not None:
            cached += 1
            if bool(row["cap_reached"]):
                partial_cached += 1
    fallback_upper_bound = sum(
        1 for track in searchable
        if _track_specs.get(track.track_key, ("song", "", ""))[0] == "artist"
    )
    return {
        "selected_tracks": len(track_list),
        "searchable_tracks": len(searchable),
        "already_owned": len(track_list) - len(searchable),
        "suppressed_tracks": len(track_list) - len(searchable),
        "catalog_queries": catalog,
        "song_queries": songs,
        "logical_query_count": len(specs),
        "query_count": len(specs),
        "fresh_queries": max(0, len(specs) - cached),
        "reusable_cached_queries": cached,
        "cached_queries": cached,
        "partial_cached_queries": partial_cached,
        "possible_fallback_queries": fallback_upper_bound * max(1, len(source_list)),
        "source_caps": {
            _source_name(source): {
                "artist": source_result_cap(source, "artist"),
                "song": source_result_cap(source, "song"),
            } for source in source_list
        },
        "duration": estimate_spotify_duration(
            config,
            query_count=len(specs),
            track_count=len(searchable),
            cached_query_count=cached,
        ),
    }


def _resolve_injection_aliases(
    sources: Iterable[ChartSource] | ChartSource | None,
    spotify_client: SpotifyClient | Any | None,
    *,
    chart_sources: Iterable[ChartSource] | ChartSource | None,
    client: Any | None,
) -> tuple[Iterable[ChartSource] | ChartSource | None, Any | None]:
    if chart_sources is not None:
        sources = chart_sources
    if client is not None:
        spotify_client = client
    # Also accept the natural positional order (..., spotify_client, sources)
    # without making the Flask-facing keyword form ambiguous.
    if _looks_like_spotify_client(sources) and spotify_client is not None and not _looks_like_spotify_client(spotify_client):
        sources, spotify_client = spotify_client, sources
    elif _looks_like_spotify_client(sources) and spotify_client is None:
        spotify_client, sources = sources, None
    return sources, spotify_client


def run_spotify_discovery(
    config: AppConfig,
    db: Database,
    sources: Iterable[ChartSource] | ChartSource | None = None,
    spotify_client: SpotifyClient | Any | None = None,
    *,
    chart_sources: Iterable[ChartSource] | ChartSource | None = None,
    client: Any | None = None,
    spotify: Any | None = None,
    library_result: LibraryImportResult | Any | None = None,
    prefetched_library: LibraryImportResult | Any | None = None,
    pre_fetched_library: LibraryImportResult | Any | None = None,
    include_liked: bool = True,
    playlist_ids: Iterable[str] | None = None,
    workspace_id: int | str | None = None,
    workspace_key: str = DEFAULT_WORKSPACE_KEY,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    scan_key: str = DEFAULT_SCAN_KEY,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    on_track_scored: ProgressCallback | None = None,
    cancellation_event: Any | None = None,
    cancel_event: Any | None = None,
) -> DiscoveryResult:
    """Run the complete blocking Spotify exact-song discovery pass.

    ``sources`` and ``spotify_client`` are injectable.  ``library_result`` or
    ``prefetched_library`` skips Spotify network access, which is useful for
    tests and for a caller that already performed the sync.  The function does
    not call a planner, create legacy selections, or download a chart.
    """
    sources, spotify_client = _resolve_injection_aliases(
        sources,
        spotify_client if spotify_client is not None else spotify,
        chart_sources=chart_sources,
        client=client,
    )
    prefetched = library_result
    if prefetched is None:
        prefetched = prefetched_library if prefetched_library is not None else pre_fetched_library
    previous_workspace = db.get_workspace(workspace_id) if workspace_id is not None else None
    actual_workspace_id, actual_workspace_key = _ensure_workspace(
        db,
        workspace_id=workspace_id,
        workspace_key=workspace_key,
        workspace_name=workspace_name,
        phase="spotify_sync",
    )
    resume_interrupted = bool(
        previous_workspace is not None
        and str(previous_workspace["status"] or "") in {"interrupted", "cancelled"}
    )
    persisted_errors: dict[str, str] = {}
    if resume_interrupted:
        for task in db.get_discovery_tasks(actual_workspace_id):
            if task["status"] in {"error", "interrupted"} and task["query_key"]:
                persisted_errors[str(task["query_key"])] = str(
                    task["error"] or "Previous query failed during the interrupted scan"
                )
    scope = db.get_workspace_scope_data(actual_workspace_id)
    selected_playlist_ids = list(playlist_ids) if playlist_ids is not None else (
        scope.get("playlist_ids", []) if scope is not None else None
    )
    if scope is not None and playlist_ids is None:
        include_liked = bool(scope.get("include_liked"))
    cancellation_event = cancellation_event or cancel_event
    actual_scan_key = scan_key.strip() or DEFAULT_SCAN_KEY
    scan_id = _begin_scan(db, actual_workspace_id, actual_scan_key, "exact")
    stats = DiscoveryStats()
    ctx = _RunContext(
        config,
        db,
        actual_workspace_id,
        actual_scan_key,
        stats,
        on_progress=on_progress,
        on_log=on_log,
        on_track_scored=on_track_scored,
        cancel_event=cancellation_event,
    )
    ctx.set_phase("spotify_sync", "Fetching Spotify library")

    try:
        library = _normalise_library_result(prefetched) if prefetched is not None else _fetch_library(
            spotify_client if spotify_client is not None else _default_spotify_client(config),
            include_liked,
            selected_playlist_ids,
        )
        stored = db.replace_spotify_library(library, workspace_id=actual_workspace_id)
        stats.playlists = len(library.playlists)
        stats.library_items = len(library.outcomes) or int(
            getattr(getattr(library, "counts", None), "total_item_count", 0) or 0
        )
        stats.library_tracks = len(stored)
        stats.total_tracks = len(stored)
        ctx.set_totals(tracks=len(stored))
        ctx.log(f"Imported {len(stored)} Spotify tracks from {len(library.playlists)} playlists")
    except Exception as exc:
        error = _error_text(exc)
        db.record_error("spotify_sync", error)
        return _finish(
            ctx,
            db,
            workspace_key=actual_workspace_key,
            scan_key=actual_scan_key,
            scan_id=scan_id,
            status="failed",
            phase="spotify_sync",
            error=error,
        )

    try:
        source_list = _resolve_sources(config, sources)
        # ``replace_spotify_library`` intentionally leaves legacy/older
        # Spotify rows available for migration safety.  The returned records
        # are the current snapshot and avoid searching stale rows on a later
        # sync.
        tracks = list(stored)
        owned_ids = db.get_workspace_owned_track_ids(actual_workspace_id)
        searchable_tracks = [track for track in tracks if int(track.db_id or 0) not in owned_ids]
        stats.already_owned_tracks = len(tracks) - len(searchable_tracks)
        stats.suppressed_tracks = stats.already_owned_tracks
        stats.library_tracks = len(tracks)
        stats.total_tracks = len(searchable_tracks)
        ctx.set_phase("source_search", "Preparing chart-source queries")
        ctx.set_totals(tracks=len(searchable_tracks))
        source_by_name = {_source_name(source): source for source in source_list}
        track_specs, specs_by_track = _primary_artist_specs(db, config, source_list, searchable_tracks)
        first_specs: dict[str, _QuerySpec] = {}
        for specs in specs_by_track.values():
            for spec in specs:
                first_specs.setdefault(spec.key, spec)
        stats.catalog_queries = sum(1 for spec in first_specs.values() if spec.kind == "artist")
        stats.song_queries = sum(1 for spec in first_specs.values() if spec.kind == "song")
        ctx.set_totals(tasks=len(first_specs), tracks=len(searchable_tracks))
        outcomes: dict[str, _QueryOutcome] = {}
        task_ids: dict[str, int] = {}
        _resolve_queries(
            ctx,
            config,
            db,
            first_specs.values(),
            outcomes,
            task_ids,
            scan_id,
            persisted_errors=persisted_errors,
        )

        ctx.set_phase("scoring", "Scoring exact-song candidates")
        fallback_tracks: list[Track] = []
        for track in searchable_tracks:
            ctx.check_cancelled()
            specs = specs_by_track.get(track.track_key, [])
            catalog = bool(track_specs.get(track.track_key, ("song", "", ""))[0] == "artist")
            had_error, has_eligible = _score_exact_track(
                ctx,
                config,
                db,
                track,
                specs,
                outcomes,
                source_by_name,
                catalog=catalog,
            )
            ctx.track_finished(track)
            all_queries_failed = bool(specs) and all(
                outcomes.get(spec.key) is not None and outcomes[spec.key].error
                for spec in specs
            )
            if catalog and (had_error or not has_eligible) and not (
                resume_interrupted and all_queries_failed
            ):
                fallback_tracks.append(track)

        if fallback_tracks:
            stats.fallback_tracks = len(fallback_tracks)
            fallback_specs: dict[str, list[_QuerySpec]] = {}
            for track in fallback_tracks:
                fallback_specs[track.track_key] = [
                    _make_query_spec(db, config, source, "song", track.primary_artist, track.title)
                    for source in source_list
                ]
            new_specs = {
                spec.key: spec
                for specs in fallback_specs.values()
                for spec in specs
                if spec.key not in outcomes
            }
            stats.fallback_queries = len(new_specs)
            stats.song_queries += sum(1 for spec in new_specs.values() if spec.kind == "song")
            all_specs = set(first_specs) | set(new_specs)
            ctx.set_totals(tasks=len(all_specs), tracks=len(searchable_tracks))
            ctx.add_scoring_tasks(len(fallback_tracks))
            _resolve_queries(
                ctx,
                config,
                db,
                new_specs.values(),
                outcomes,
                task_ids,
                scan_id,
                persisted_errors=persisted_errors,
            )
            ctx.set_phase("scoring", "Scoring per-song fallback candidates")
            for track in fallback_tracks:
                _score_exact_track(
                    ctx,
                    config,
                    db,
                    track,
                    fallback_specs[track.track_key],
                    outcomes,
                    source_by_name,
                    catalog=False,
                )
                ctx.track_finished(track)

        ctx.set_phase("complete", "Finalizing exact-song discovery")
        stats.total_queries = ctx.task_total
        stats.completed_queries = ctx.task_done
        stats.failed_queries = ctx.task_failed
        return _finish(
            ctx,
            db,
            workspace_key=actual_workspace_key,
            scan_key=actual_scan_key,
            scan_id=scan_id,
            status="complete",
            phase="complete",
            library_result=library,
        )
    except DiscoveryCancelled as exc:
        message = _error_text(exc)
        try:
            db.update_workspace(actual_workspace_id, status="cancelled", phase="cancelled", metadata={"cancelled": True, "message": message})
            for task in db.get_discovery_tasks(actual_workspace_id):
                if task["status"] in {"pending", "running"}:
                    db.update_discovery_task(int(task["id"]), status="cancelled", error=message)
        except Exception:
            pass
        return _finish(
            ctx, db, workspace_key=actual_workspace_key, scan_key=actual_scan_key,
            scan_id=scan_id, status="cancelled", phase="cancelled", library_result=library,
            error=message,
        )
    except Exception as exc:
        error = _error_text(exc)
        db.record_error("spotify_discovery", error)
        return _finish(
            ctx,
            db,
            workspace_key=actual_workspace_key,
            scan_key=actual_scan_key,
            scan_id=scan_id,
            status="failed",
            phase=ctx.phase,
            library_result=library,
            error=error,
        )


def _expansion_cap(config: Any, track_count: int) -> int:
    expansion = _value(config, "expansion", default=config)
    if track_count >= int(_value(expansion, "min_songs_many", default=5) or 5):
        return max(0, int(_value(expansion, "cap_many_songs", default=50) or 0))
    if track_count >= 2:
        return max(0, int(_value(expansion, "cap_some_songs", default=25) or 0))
    return max(0, int(_value(expansion, "cap_one_song", default=10) or 0))


def _current_spotify_tracks(db: Database) -> list[Track]:
    """Prefer tracks in the current Spotify membership snapshot on resync."""
    tracks = db.get_spotify_tracks()
    try:
        membership_ids = {
            str(row["spotify_track_id"])
            for row in db.get_spotify_playlist_memberships()
            if row["spotify_track_id"]
        }
        membership_ids.update(
            str(row["spotify_track_id"])
            for row in db.get_spotify_liked_tracks()
            if row["spotify_track_id"]
        )
    except (AttributeError, KeyError, TypeError):
        return tracks
    if not membership_ids:
        return tracks
    return [track for track in tracks if str(track.spotify_id) in membership_ids]


def run_expansion_discovery(
    config: AppConfig,
    db: Database,
    workspace_id: int | str,
    sources: Iterable[ChartSource] | ChartSource | None = None,
    *,
    chart_sources: Iterable[ChartSource] | ChartSource | None = None,
    selected_artists: Iterable[Any] | Any | None = None,
    selected_artist_norms: Iterable[str] | None = None,
    selected_primary_artists: Iterable[Any] | Any | None = None,
    artists: Iterable[Any] | Any | None = None,
    scan_key: str = DEFAULT_EXPANSION_SCAN_KEY,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
) -> DiscoveryResult:
    """Discover expansion charts only for explicitly selected primary artists.

    Existing exact rows are never updated: expansion rows use ``kind`` equal
    to ``"expansion"``, a null track association, and the selected artist's
    normalized name.  If ``selected_artists`` is omitted, only rows already
    marked selected in ``workspace_expansion_artists`` are used.
    """
    if not isinstance(workspace_id, (int, str)) and isinstance(sources, (int, str)):
        workspace_id, sources = sources, workspace_id  # type: ignore[assignment]
    if chart_sources is not None:
        sources = chart_sources
    if selected_primary_artists is not None:
        selected_artists = selected_primary_artists
    if artists is not None:
        selected_artists = artists
    actual_workspace_id, actual_workspace_key = _ensure_workspace(
        db,
        workspace_id=workspace_id,
        workspace_key=DEFAULT_WORKSPACE_KEY,
        workspace_name=DEFAULT_WORKSPACE_NAME,
        phase="expansion",
    )
    actual_scan_key = scan_key.strip() or DEFAULT_EXPANSION_SCAN_KEY
    scan_id = _begin_scan(db, actual_workspace_id, actual_scan_key, "expansion")
    stats = DiscoveryStats()
    ctx = _RunContext(
        config,
        db,
        actual_workspace_id,
        actual_scan_key,
        stats,
        on_progress=on_progress,
        on_log=on_log,
    )
    try:
        source_list = _resolve_sources(config, sources)
        tracks = _current_spotify_tracks(db)
        stats.library_tracks = len(tracks)
        stats.total_tracks = len(tracks)
        primary: dict[str, dict[str, Any]] = {}
        for track in tracks:
            norm = track.primary_artist_norm or normalize_artist(track.primary_artist)
            entry = primary.setdefault(
                norm,
                {"artist_name": track.primary_artist, "artist_norm": norm, "track_count": 0},
            )
            entry["track_count"] += 1

        if selected_artist_norms is not None:
            selected_values: list[Any] = (
                [selected_artist_norms]
                if isinstance(selected_artist_norms, (str, bytes))
                else list(selected_artist_norms)
            )
            if selected_artists is not None:
                selected_values = [*(list(selected_artists) if not isinstance(selected_artists, (str, bytes, Mapping)) else [selected_artists]), *selected_values]
            selected_artists = selected_values
        if selected_artists is None:
            selected_rows = db.get_workspace_expansion_artists(actual_workspace_id, selected_only=True)
            values = [dict(row) for row in selected_rows]
        elif isinstance(selected_artists, (str, bytes, Mapping)):
            values = [selected_artists]
        else:
            values = list(selected_artists)

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, str):
                data = {"artist_name": value}
            elif isinstance(value, Mapping):
                data = dict(value)
            else:
                data = _candidate_mapping(value)
            if data and data.get("selected") in (False, 0, "0", "false", "False", "no"):
                continue
            name = str(data.get("artist_name", data.get("name", data.get("artist", ""))) or "").strip()
            norm = str(data.get("artist_norm", data.get("normalized", "")) or "").strip() or normalize_artist(name)
            # This membership check is the primary-artist boundary.  Featured
            # artists never enter expansion just because they appear in Track.
            if norm not in primary or norm in seen:
                continue
            entry = dict(primary[norm])
            configured_cap = data.get("cap")
            try:
                entry["cap"] = (
                    max(0, int(configured_cap))
                    if configured_cap is not None
                    else _expansion_cap(config, entry["track_count"])
                )
            except (TypeError, ValueError):
                entry["cap"] = _expansion_cap(config, entry["track_count"])
            entry["selected"] = True
            selected.append(entry)
            seen.add(norm)

        db.replace_workspace_expansion_artists(actual_workspace_id, selected)
        for entry in selected:
            db.set_expansion_artist_selected(actual_workspace_id, entry["artist_norm"], True)
        stats.expansion_artists_requested = len(selected)
        eligible_artists = [entry for entry in selected if entry["cap"] > 0]
        ctx.set_phase("expansion_search", "Preparing selected artist expansion")
        ctx.set_totals(tasks=len(eligible_artists) * len(source_list), tracks=0)
        # Expansion does not rescore exact tracks, but the workspace still
        # represents the already imported library snapshot.
        ctx.track_done = len(tracks)
        ctx.track_total = len(tracks)
        stats.completed_tracks = len(tracks)
        ctx.scoring_total = 0
        stats.scoring_tasks_total = 0

        specs: list[tuple[dict[str, Any], _QuerySpec]] = []
        for entry in eligible_artists:
            for source in source_list:
                specs.append(
                    (
                        entry,
                        _make_query_spec(
                            db,
                            config,
                            source,
                            "artist",
                            entry["artist_name"],
                            "",
                        ),
                    )
                )
        outcomes: dict[str, _QueryOutcome] = {}
        task_ids: dict[str, int] = {}
        _resolve_queries(
            ctx,
            config,
            db,
            [spec for _entry, spec in specs],
            outcomes,
            task_ids,
            scan_id,
        )
        source_by_name = {_source_name(source): source for source in source_list}
        by_artist: dict[str, list[tuple[_QuerySpec, _QueryOutcome, dict[str, Any]]]] = {}
        for entry, spec in specs:
            outcome = outcomes.get(spec.key, _QueryOutcome(error="query did not reach a terminal state"))
            by_artist.setdefault(entry["artist_norm"], []).append((spec, outcome, entry))

        for artist_norm, artist_queries in by_artist.items():
            entry = artist_queries[0][2]
            ctx.set_phase("expansion_scoring", f"Scoring expansion for {entry['artist_name']}")
            candidates: dict[tuple[str, int], tuple[ChartCandidate, _QuerySpec, int | None]] = {}
            for spec, outcome, _entry in artist_queries:
                if outcome.error:
                    message = f"{spec.source_name} expansion query for {entry['artist_name']}: {outcome.error}"
                    db.record_error("spotify_expansion", message, artist_norm)
                    continue
                for candidate in outcome.candidates:
                    candidates.setdefault(
                        (str(candidate.source), _chart_id(candidate)),
                        (candidate, spec, outcome.task_id),
                    )

            scored: list[tuple[ChartCandidate, Any, str, list[str], _QuerySpec, int | None]] = []
            for candidate, spec, task_id in candidates.values():
                source = source_by_name.get(str(candidate.source), spec.source)
                try:
                    breakdown = score_expansion_candidate(artist_norm, candidate, config, source)
                except Exception as exc:
                    breakdown = _scoring_error(exc)
                    db.record_error("spotify_expansion_score", _error_text(exc), artist_norm)
                eligibility, reasons = _score_eligibility(breakdown, config)
                scored.append((candidate, breakdown, eligibility, reasons, spec, task_id))

            # Preserve every returned result for review, while storing a stable
            # rank and the configured artist cap in metadata.  Consumers can
            # enforce the cap at selection/export time without hiding lower
            # ranked source-owned cards.
            scored.sort(
                key=lambda item: (
                    -float(getattr(item[1], "final_score", 0.0) or 0.0),
                    -float(getattr(item[1], "quality", 0.0) or 0.0),
                    str(item[0].source),
                    int(item[0].chart_id),
                )
            )
            for rank, (candidate, breakdown, eligibility, reasons, spec, task_id) in enumerate(scored, 1):
                metadata = {
                    "query_kind": "artist",
                    "artist_norm": artist_norm,
                    "artist_cap": entry["cap"],
                    "cap_rank": rank,
                    "within_artist_cap": rank <= entry["cap"],
                    "query_key": spec.key,
                    "source_version": spec.source_version,
                }
                db.upsert_workspace_candidate(
                    actual_workspace_id,
                    None,
                    candidate,
                    score=float(getattr(breakdown, "final_score", 0.0) or 0.0),
                    quality=float(getattr(breakdown, "quality", 0.0) or 0.0),
                    decision=str(getattr(breakdown, "decision", "REJECT") or "REJECT"),
                    eligibility=eligibility,
                    kind="expansion",
                    artist_norm=artist_norm,
                    task_id=task_id,
                    breakdown_json=_score_breakdown_json(breakdown, eligibility, reasons),
                    metadata=metadata,
                )
                ctx.note_expansion_score(artist_norm, candidate, eligibility)
            stats.expansion_artists_scanned += 1

        return _finish(
            ctx,
            db,
            workspace_key=actual_workspace_key,
            scan_key=actual_scan_key,
            scan_id=scan_id,
            status="complete",
            phase="complete",
        )
    except Exception as exc:
        error = _error_text(exc)
        db.record_error("spotify_expansion", error)
        return _finish(
            ctx,
            db,
            workspace_key=actual_workspace_key,
            scan_key=actual_scan_key,
            scan_id=scan_id,
            status="failed",
            phase=ctx.phase,
            error=error,
        )


# Public aliases used by Flask integrations and earlier plan drafts.
run_discovery = run_spotify_discovery
run_spotify_expansion_discovery = run_expansion_discovery
discover_spotify = run_spotify_discovery
run_spotify_scan = run_spotify_discovery
source_query_key = build_source_query_key
make_source_query_key = build_source_query_key
DiscoveryRunStats = DiscoveryStats
DiscoveryRunResult = DiscoveryResult


__all__ = [
    "DEFAULT_EXPANSION_SCAN_KEY",
    "DEFAULT_RESULT_CAP",
    "DEFAULT_SCAN_KEY",
    "DEFAULT_WORKSPACE_KEY",
    "DiscoveryResult",
    "DiscoveryRunResult",
    "DiscoveryRunStats",
    "DiscoveryStats",
    "ExpansionResult",
    "ExpansionStats",
    "LibraryImportResult",
    "NORMALIZATION_VERSION",
    "SpotifyDiscoveryResult",
    "SpotifyDiscoveryStats",
    "SpotifyPipelineResult",
    "SpotifyPipelineStats",
    "SpotifyClient",
    "build_source_query_key",
    "estimate_spotify_duration",
    "discover_spotify",
    "run_discovery",
    "run_expansion_discovery",
    "run_spotify_discovery",
    "run_spotify_expansion_discovery",
    "run_spotify_scan",
    "source_query_key",
    "make_source_query_key",
]
