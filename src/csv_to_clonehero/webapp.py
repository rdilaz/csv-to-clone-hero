"""Browser interface with local, LAN, and authenticated public-URL modes.

Lets a nontechnical user:

1. get CSVs (links to Chosic / Exportify),
2. upload them into input_playlists,
3. run a dry run or a full build with on-screen progress,
4. read the summary and download reports.

No terminal required.
"""

from __future__ import annotations

import json
import hmac
import inspect
import logging
import os
import re
import secrets
import shutil
import sqlite3
import string
import threading
import time
from collections import deque
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx
from flask import Flask, abort, jsonify, redirect, request, send_file

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.csv_import import (
    SUPPORTED_EXTENSIONS,
    artist_track_counts,
    import_playlist_dir,
)
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import ExportOptions, ExportService
from csv_to_clonehero.filesystem import destination_path, is_within, sanitize_component
from csv_to_clonehero.library_inventory import InventoryLimits, scan_library
from csv_to_clonehero.matching import (
    ELIGIBLE_REVIEW,
    EXCLUDED,
    INCOMPATIBLE,
    RECOMMENDED,
    eligibility_for,
    score_candidate,
    score_expansion_candidate,
)
from csv_to_clonehero.models import ChartCandidate, Track
from csv_to_clonehero.spotify import (
    SpotifyClient,
    SpotifyError,
    SpotifyOAuth,
)
from csv_to_clonehero.spotify_pipeline import (
    build_spotify_estimate,
    estimate_spotify_duration,
    run_expansion_discovery,
    run_spotify_discovery,
)
from csv_to_clonehero.pipeline import (
    cleanup_empty_folders,
    run_downloads,
    run_plan,
    run_relocation,
    run_scan,
    run_search,
)
from csv_to_clonehero.reporting import generate_reports, generate_spotify_reports
from csv_to_clonehero.sources import build_sources
from csv_to_clonehero.sources.base import ChartSource
from csv_to_clonehero.normalization import normalize_artist

log = logging.getLogger(__name__)


class _OAuthAccessLogFilter(logging.Filter):
    """Keep Werkzeug callback request lines from exposing OAuth material."""

    _query = re.compile(r"([?&](?:code|state|access_token|refresh_token|token)=)[^&\s\"]*", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if "GET /api/spotify/status?" in message:
                return False
            redacted = self._query.sub(r"\1[REDACTED]", message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True

MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Live-log ring buffer size exposed via /api/status.
LOG_EVENT_LIMIT = 200


class JobState:
    """Shared state for the single background build job."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.action = ""
        self.stage = ""
        self.stages: list[dict] = []
        self.error = ""
        self.finished_at: float | None = None
        self.started_at: float | None = None
        self.last_activity_at: float | None = None
        self.activity = ""
        self.search_active = False
        self.events: deque = deque(maxlen=LOG_EVENT_LIMIT)
        self.result: dict | None = None  # simple-mode completion counts
        self.output_dir = ""  # folder the current/last job wrote to
        self.search_progress = {
            "queries_done": 0,
            "queries_total": 0,
            "scored_tracks": 0,
            "total_tracks": 0,
            "review_tracks": 0,
            "review_candidates": 0,
        }

    def _log_locked(self, message: str) -> None:
        self.events.append({"ts": time.time(), "message": message})

    def log(self, message: str) -> None:
        with self.lock:
            self._log_locked(message)

    def begin_stage(self, label: str) -> None:
        with self.lock:
            self.stage = label
            self.activity = label
            self.last_activity_at = time.time()
            self.stages.append({"label": label, "status": "running", "detail": ""})
            self._log_locked(label)

    def end_stage(self, detail: str = "") -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["status"] = "done"
                self.stages[-1]["detail"] = detail
            self.activity = f"{self.stage}: {detail}" if detail else self.stage
            self.last_activity_at = time.time()
            self._log_locked(
                f"{self.stage} - done" + (f" ({detail})" if detail else "")
            )

    def progress(self, done: int, total: int) -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["detail"] = f"{done} / {total}"
            self.activity = f"{self.stage}: {done} / {total}"
            self.last_activity_at = time.time()
            if done == total or done % 25 == 0:
                self._log_locked(f"{self.stage}: {done} / {total}")

    def search_started(self, queries_total: int, total_tracks: int) -> None:
        with self.lock:
            self.search_active = True
            self.search_progress.update(
                queries_total=queries_total,
                total_tracks=total_tracks,
                queries_done=0,
                scored_tracks=0,
                review_tracks=0,
                review_candidates=0,
            )
            if self.stages:
                self.stages[-1]["detail"] = (
                    f"0 / {queries_total} queries; 0 / {total_tracks} tracks scored"
                )
            self.activity = (
                f"Searching playlist songs: 0 / {queries_total} queries; "
                f"0 / {total_tracks} tracks scored"
            )
            self.last_activity_at = time.time()

    def query_progress(self, done: int, total: int) -> None:
        with self.lock:
            self.search_progress["queries_done"] = done
            self.search_progress["queries_total"] = total
            if self.stages:
                scored = self.search_progress["scored_tracks"]
                tracks = self.search_progress["total_tracks"]
                self.stages[-1]["detail"] = (
                    f"{done} / {total} queries; {scored} / {tracks} tracks scored"
                )
            self.activity = (
                f"Searching chart sources: {done} / {total} queries complete; "
                f"{scored} / {tracks} tracks scored"
            )
            self.last_activity_at = time.time()
            if done == total or done % 25 == 0:
                self._log_locked(f"Search queries: {done} / {total}")

    def track_scored(
        self,
        done: int,
        total: int,
        review_tracks: int,
        review_candidates: int,
    ) -> None:
        with self.lock:
            self.search_progress.update(
                scored_tracks=done,
                total_tracks=total,
                review_tracks=review_tracks,
                review_candidates=review_candidates,
            )
            if self.stages:
                queries_done = self.search_progress["queries_done"]
                queries_total = self.search_progress["queries_total"]
                self.stages[-1]["detail"] = (
                    f"{queries_done} / {queries_total} queries; "
                    f"{done} / {total} tracks scored; "
                    f"{review_candidates} review candidates"
                )
            self.activity = (
                f"Scored {done} / {total} tracks; "
                f"{review_candidates} review candidates available"
            )
            self.last_activity_at = time.time()
            if done == total or done % 50 == 0:
                self._log_locked(f"Tracks scored: {done} / {total}")

    def search_finished(self) -> None:
        with self.lock:
            self.search_active = False

    def fail(self, message: str) -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["status"] = "failed"
            self.error = message
            self.running = False
            self.search_active = False
            self.activity = f"Job failed: {message}"
            self.last_activity_at = time.time()
            self.finished_at = time.time()
            self._log_locked(f"ERROR: {message}")

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "action": self.action,
                "stage": self.stage,
                "stages": list(self.stages),
                "error": self.error,
                "finished_at": self.finished_at,
                "started_at": self.started_at,
                "last_activity_at": self.last_activity_at,
                "activity": self.activity,
                "elapsed_seconds": (
                    max(0.0, time.time() - self.started_at)
                    if self.started_at is not None and self.running
                    else (
                        max(0.0, (self.finished_at or time.time()) - self.started_at)
                        if self.started_at is not None
                        else 0.0
                    )
                ),
                "search_active": self.search_active,
                "search_progress": dict(self.search_progress),
                "events": list(self.events),
                "result": self.result,
                "output_dir": self.output_dir,
            }


class _SpotifyJobState:
    """Thread-safe in-memory companion to the persisted discovery progress."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.running = False
            self.status = "created"
            self.phase = "idle"
            self.message = ""
            self.error = ""
            self.partial = False
            self.workspace_id: int | None = None
            self.workspace_key = "spotify"
            self.scan_key = "exact"
            self.started_at: float | None = None
            self.finished_at: float | None = None
            self.progress: dict[str, Any] = {
                "total_tasks": 0,
                "completed_tasks": 0,
                "failed_tasks": 0,
                "total_tracks": 0,
                "completed_tracks": 0,
                "scoring_tasks_total": 0,
                "scoring_tasks_completed": 0,
                "done": 0,
                "total": 0,
                "total_work": 0,
                "completed_work": 0,
                "remaining_work": 0,
                "estimated_total_seconds": 0.0,
                "estimated_lower_seconds": 0.0,
                "estimated_upper_seconds": 0.0,
                "estimated_query_seconds": 0.0,
                "estimated_scoring_seconds": 0.0,
                "estimated_completed_seconds": 0.0,
            }
            self.events: deque = deque(maxlen=LOG_EVENT_LIMIT)
            self.result: dict[str, Any] | None = None
            self._last_counter_at: float | None = None
            self._last_task_done = 0
            self._last_cached_done = 0
            self._last_scoring_done = 0
            self._query_seconds_samples: deque[float] = deque(maxlen=25)
            self._scoring_seconds_samples: deque[float] = deque(maxlen=50)

    def _log_locked(self, message: str) -> None:
        self.events.append({"ts": time.time(), "message": _safe_message(message)})

    def log(self, message: str) -> None:
        with self.lock:
            self._log_locked(message)

    def begin(self, workspace_id: int, workspace_key: str = "spotify") -> None:
        with self.lock:
            self.reset()
            self.running = True
            self.status = "running"
            self.phase = "starting"
            self.message = "Starting Spotify discovery..."
            self.workspace_id = workspace_id
            self.workspace_key = workspace_key
            self.started_at = time.time()
            self._last_counter_at = self.started_at
            self._log_locked(self.message)

    def progress_update(self, payload: Mapping[str, Any] | None = None) -> None:
        with self.lock:
            data = dict(payload or {})
            now = time.time()
            self.phase = str(data.get("phase") or self.phase or "")
            self.status = str(data.get("status") or self.status or "running")
            self.message = _safe_message(data.get("message") or self.message)
            self.partial = bool(data.get("partial", self.partial))
            for key in self.progress:
                if key in data:
                    try:
                        if key.endswith("seconds"):
                            self.progress[key] = float(data[key])
                        else:
                            self.progress[key] = int(data[key])
                    except (TypeError, ValueError):
                        self.progress[key] = data[key]
            task_done = int(self.progress.get("completed_tasks") or 0)
            cached_done = int(data.get("cached_queries", self._last_cached_done) or 0)
            scoring_done = int(self.progress.get("scoring_tasks_completed") or 0)
            task_delta = max(0, task_done - self._last_task_done)
            cached_delta = max(0, cached_done - self._last_cached_done)
            scoring_delta = max(0, scoring_done - self._last_scoring_done)
            counter_delta = task_delta + scoring_delta
            if counter_delta and self._last_counter_at is not None:
                duration = max(0.0, now - self._last_counter_at)
                fresh_delta = max(0, task_delta - cached_delta)
                if fresh_delta:
                    self._query_seconds_samples.append(duration / fresh_delta)
                elif scoring_delta:
                    self._scoring_seconds_samples.append(duration / scoring_delta)
                self._last_counter_at = now
            self._last_task_done = task_done
            self._last_cached_done = cached_done
            self._last_scoring_done = scoring_done
            if data.get("error"):
                self.error = _safe_message(data["error"])
            if self.message:
                self._log_locked(self.message)

    def finish(self, result: Any = None) -> None:
        with self.lock:
            self.running = False
            self.finished_at = time.time()
            if result is not None:
                self.status = str(getattr(result, "status", self.status) or self.status)
                self.phase = str(getattr(result, "phase", self.phase) or self.phase)
                self.error = _safe_message(getattr(result, "error", "") or self.error)
                self.partial = bool(getattr(result, "partial", self.partial))
                stats = getattr(result, "stats", None)
                if stats is not None:
                    for source, target in (
                        ("total_queries", "total_tasks"),
                        ("completed_queries", "completed_tasks"),
                        ("failed_queries", "failed_tasks"),
                        ("total_tracks", "total_tracks"),
                        ("completed_tracks", "completed_tracks"),
                        ("scoring_tasks_total", "scoring_tasks_total"),
                        ("scoring_tasks_completed", "scoring_tasks_completed"),
                        ("total_work", "total_work"),
                        ("completed_work", "completed_work"),
                        ("remaining_work", "remaining_work"),
                        ("estimated_total_seconds", "estimated_total_seconds"),
                        ("estimated_lower_seconds", "estimated_lower_seconds"),
                        ("estimated_upper_seconds", "estimated_upper_seconds"),
                        ("estimated_query_seconds", "estimated_query_seconds"),
                        ("estimated_scoring_seconds", "estimated_scoring_seconds"),
                        ("estimated_completed_seconds", "estimated_completed_seconds"),
                    ):
                        value = getattr(stats, source, None)
                        if value is not None:
                            self.progress[target] = int(value)
                    self.progress["done"] = self.progress.get("completed_tasks", 0)
                    self.progress["total"] = self.progress.get("total_tasks", 0)
                self.result = _discovery_result_payload(result)
            if self.error:
                self.status = "failed" if self.status not in {"partial", "complete"} else self.status
                self._log_locked(f"Discovery failed: {self.error}")
            else:
                self._log_locked("Spotify discovery complete")

    def fail(self, error: Any) -> None:
        with self.lock:
            self.running = False
            self.status = "failed"
            self.error = _safe_message(error)
            self.message = f"Discovery failed: {self.error}"
            self.finished_at = time.time()
            self._log_locked(self.message)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            started = self.started_at
            finished = self.finished_at
            end = now if self.running else (finished or now)
            progress = dict(self.progress)
            total_work = int(progress.get("total_work") or 0)
            completed_work = min(total_work, int(progress.get("completed_work") or 0))
            elapsed = max(0.0, end - started) if started is not None else 0.0
            remaining_work = max(0, total_work - completed_work)
            estimated_total = float(progress.get("estimated_total_seconds") or 0.0)
            remaining_queries = max(
                0,
                int(progress.get("total_tasks") or 0)
                - int(progress.get("completed_tasks") or 0),
            )
            remaining_scoring = max(
                0,
                int(progress.get("scoring_tasks_total") or 0)
                - int(progress.get("scoring_tasks_completed") or 0),
            )
            query_seconds = (
                sorted(self._query_seconds_samples)[len(self._query_seconds_samples) // 2]
                if self._query_seconds_samples
                else 4.0
            )
            scoring_seconds = (
                sorted(self._scoring_seconds_samples)[len(self._scoring_seconds_samples) // 2]
                if self._scoring_seconds_samples
                else 0.025
            )
            if not self.running:
                eta = 0.0
                estimate_source = "complete" if self.finished_at is not None else "unknown"
            elif total_work == 0:
                eta = 0.0
                estimate_source = "unknown"
            else:
                eta = remaining_queries * query_seconds + remaining_scoring * scoring_seconds
                observed = len(self._query_seconds_samples) + len(self._scoring_seconds_samples)
                if not remaining_queries and not remaining_scoring and total_work > completed_work:
                    eta = max(0.0, estimated_total - elapsed)
                estimate_source = "live_phase_rate" if observed >= 2 else "calibrated"
            sample_count = len(self._query_seconds_samples) + len(self._scoring_seconds_samples)
            uncertainty = 0.2 if sample_count >= 10 else 0.4
            return {
                "running": self.running,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "activity": self.message,
                "error": self.error,
                "partial": self.partial,
                "workspace_id": self.workspace_id,
                "workspace_key": self.workspace_key,
                "scan_key": self.scan_key,
                "started_at": started,
                "finished_at": finished,
                "elapsed_seconds": elapsed,
                "progress": progress,
                "eta_seconds": round(eta, 1),
                "eta_lower_seconds": round(max(0.0, eta * (1.0 - uncertainty)), 1),
                "eta_upper_seconds": round(eta * (1.0 + uncertainty), 1),
                "estimated_total_seconds": round(
                    elapsed + eta if self.running else elapsed, 1
                ),
                "remaining_work": remaining_work,
                "query_seconds_per_item": round(query_seconds, 3),
                "scoring_seconds_per_item": round(scoring_seconds, 4),
                "eta_sample_count": sample_count,
                "estimate_source": estimate_source,
                "events": list(self.events),
                "recent_events": list(self.events)[-6:],
                "result": self.result,
            }


class _ExportJobState:
    """Thread-safe progress state for background chart exports."""

    _PHASE_LABELS = {
        "Checking download sizes",
        "Writing files",
        "Writing ZIP archive",
    }

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.running = False
            self.status = "idle"
            self.kind = ""
            self.done = 0
            self.total = 0
            self.current = ""
            self.error = ""
            self.browser_download = False
            self.destination = ""
            self.started_at: float | None = None
            self.result: dict[str, Any] | None = None
            self.events: deque = deque(maxlen=200)
            self._last_logged = 0
            self._last_label = ""

    def log(self, message: str) -> None:
        with self.lock:
            self.events.append({"ts": time.time(), "message": _safe_message(message)})

    def update(self, done: int, total: int, label: str) -> None:
        with self.lock:
            self.status = "running"
            done = int(done)
            total = int(total)
            self.done = done
            self.total = total
            self.current = str(label or "")
            phase_change = self.current in self._PHASE_LABELS or self._last_label in self._PHASE_LABELS
            step = max(1, total // 25) if total else 1
            if phase_change or (total and done == total) or done - self._last_logged >= step:
                self._last_logged = done
                message = f"{done}/{total} · {self.current}" if total else self.current
                self.events.append({"ts": time.time(), "message": message})
            self._last_label = self.current


_SECRET_QUERY_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|client_secret|code_verifier|authorization|code)\s*[=:]\s*)([^&\s,;]+)"
)
_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[^\s,]+")


def _safe_message(value: Any, secrets_to_hide: Iterable[str] = ()) -> str:
    """Bound user-visible text and remove OAuth-shaped values."""
    text = str(value or "")
    for secret in sorted({str(item) for item in secrets_to_hide if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_SECRET_RE.sub("Bearer [REDACTED]", text)
    return text[:1000]


def _json_object(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return value


def _row_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): child for key, child in value.items()}
    try:
        return {str(key): value[key] for key in value.keys()}
    except (AttributeError, KeyError, TypeError):
        return {}


def _candidate_from_payload(value: Any) -> ChartCandidate | None:
    if isinstance(value, ChartCandidate):
        return value
    data = _row_dict(value)
    if not data:
        return None
    nested = data.get("candidate") or data.get("chart")
    if nested is not None:
        candidate = _candidate_from_payload(nested)
        if candidate is not None:
            return candidate
    allowed = {item.name for item in fields(ChartCandidate)}
    required = {"source", "chart_id", "md5", "name", "artist", "name_norm", "artist_norm"}
    payload = {key: data[key] for key in allowed if key in data}
    if not required.issubset(payload):
        return None
    try:
        return ChartCandidate(**payload)
    except (TypeError, ValueError):
        return None


def _track_payload(track: Track | Mapping[str, Any] | None) -> dict[str, Any]:
    if track is None:
        return {}
    if isinstance(track, Mapping):
        data = dict(track)
    elif is_dataclass(track):
        data = {item.name: getattr(track, item.name) for item in fields(track)}
    else:
        data = {}
        for name in (
            "db_id", "track_key", "spotify_id", "title", "artist_full", "primary_artist",
            "album", "playlists", "spotify_url", "duration_ms", "isrc",
        ):
            if hasattr(track, name):
                data[name] = getattr(track, name)
    return {
        "id": data.get("spotify_id") or data.get("track_key") or data.get("db_id"),
        "db_id": data.get("db_id"),
        "track_key": data.get("track_key", ""),
        "spotify_id": data.get("spotify_id", ""),
        "title": data.get("title", ""),
        "artist": data.get("artist_full") or data.get("primary_artist", ""),
        "artist_full": data.get("artist_full", ""),
        "primary_artist": data.get("primary_artist", ""),
        "album": data.get("album", ""),
        "playlists": list(data.get("playlists") or []),
        "spotify_url": data.get("spotify_url", ""),
        "duration_ms": data.get("duration_ms"),
        "isrc": data.get("isrc", ""),
    }


def _discovery_result_payload(result: Any) -> dict[str, Any]:
    stats = getattr(result, "stats", None)
    stats_payload: dict[str, Any] = {}
    if stats is not None:
        for item in fields(stats) if is_dataclass(stats) else ():
            value = getattr(stats, item.name, None)
            if item.name == "error_messages":
                stats_payload[item.name] = [_safe_message(message) for message in value or ()]
            else:
                stats_payload[item.name] = value
    return {
        "status": str(getattr(result, "status", "") or ""),
        "phase": str(getattr(result, "phase", "") or ""),
        "workspace_id": getattr(result, "workspace_id", None),
        "workspace_key": str(getattr(result, "workspace_key", "") or ""),
        "scan_key": str(getattr(result, "scan_key", "") or ""),
        "partial": bool(getattr(result, "partial", False)),
        "error": _safe_message(getattr(result, "error", "") or ""),
        "stats": stats_payload,
    }


def _call_injected_factory(
    factory: Any,
    *,
    config: AppConfig,
    oauth: Any = None,
    keyring: Any = None,
    purpose: str = "factory",
) -> Any:
    """Call test/application factories with several small compatible shapes."""
    if factory is None:
        return None
    if not callable(factory):
        return factory

    values = {
        "config": config,
        "cfg": config,
        "app_config": config,
        "settings": config,
        "oauth": oauth,
        "auth": oauth,
        "client": oauth,
        "spotify_oauth": oauth,
        "keyring": keyring,
        "credential_store": keyring,
        "store": keyring,
    }
    try:
        signature = inspect.signature(factory)
        parameters = signature.parameters
        kwargs: dict[str, Any] = {}
        has_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        for name, parameter in parameters.items():
            if name in values and values[name] is not None:
                kwargs[name] = values[name]
        required_missing = [
            item
            for item in parameters.values()
            if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and item.default is inspect.Parameter.empty
            and item.name not in kwargs
        ]
        if not required_missing and (kwargs or has_kwargs):
            if has_kwargs:
                for name, value in values.items():
                    if value is not None:
                        kwargs.setdefault(name, value)
            return factory(**kwargs)
    except (TypeError, ValueError):
        pass

    attempts: list[tuple[Any, ...]]
    if purpose == "oauth":
        attempts = [(config, keyring), (config,), (keyring,), ()]
    else:
        attempts = [(config, oauth), (oauth,), (config,), ()]
    last_error: Exception | None = None
    for args in attempts:
        args = tuple(item for item in args if item is not None)
        try:
            return factory(*args)
        except TypeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise TypeError(f"invalid {purpose}")


def _default_keyring() -> Any:
    try:
        import keyring as keyring_module  # type: ignore[import-not-found]

        if callable(getattr(keyring_module, "get_password", None)) and callable(
            getattr(keyring_module, "set_password", None)
        ):
            return keyring_module
    except Exception:
        pass
    return None


def _spotify_account_payload(row: Any) -> dict[str, Any] | None:
    data = _row_dict(row)
    if not data:
        return None
    scopes = _json_object(data.get("scopes_json"), data.get("scopes", []))
    if isinstance(scopes, str):
        scopes = [item for item in scopes.split() if item]
    return {
        "id": data.get("spotify_user_id") or data.get("user_id", ""),
        "spotify_user_id": data.get("spotify_user_id", ""),
        "display_name": data.get("display_name", ""),
        "country": data.get("country", ""),
        "product": data.get("product", ""),
        "href": data.get("href", ""),
        "scopes": list(scopes or []),
    }


def _run_job(
    config: AppConfig,
    state: JobState,
    action: str,
    source_factory: Callable[[AppConfig], list[ChartSource]],
) -> None:
    """Background worker: runs the pipeline, updating state as it goes."""
    dry_run = action in ("dry-run", "exact-dry-run", "expansion-dry-run")
    include_expansion = action.startswith("expansion-")
    db = Database(config.database_path)
    sources: list[ChartSource] = []
    try:
        from csv_to_clonehero.overrides import load_overrides

        overrides = load_overrides(config.base_dir)
        sources = source_factory(config)

        state.begin_stage("Reading Spotify playlists")
        scan = run_scan(config, db, overrides)
        state.end_stage(
            f"{scan.csv_files} CSVs, {scan.total_rows} rows, "
            f"{scan.unique_tracks} unique tracks, {scan.primary_artists} primary artists"
        )

        state.begin_stage("Searching chart sources")
        search = run_search(
            config,
            db,
            sources,
            overrides,
            on_progress=state.query_progress,
            include_expansion=include_expansion,
            on_track_scored=state.track_scored,
            on_search_start=state.search_started,
        )
        state.search_finished()
        state.end_stage(
            f"{search.auto} auto matches, {search.review} review, "
            f"{search.no_match} no match"
            + (f", {search.errors} errors" if search.errors else "")
        )

        state.begin_stage("Planning library")
        plan = run_plan(
            config,
            db,
            sources,
            on_progress=state.progress,
            include_expansion=include_expansion,
        )
        state.end_stage(
            f"{plan.exact_selected} exact + {plan.expansion_selected} expansion, "
            f"{plan.planned_bytes / (1024**3):.1f} GiB estimated"
        )

        if dry_run:
            state.begin_stage("Downloading charts")
            state.end_stage("skipped (dry run)")
        else:
            state.begin_stage("Downloading charts (this can take a while)")
            downloaded, errors, bytes_total = run_downloads(
                config, db, sources, on_progress=state.progress
            )
            state.end_stage(
                f"{downloaded} downloaded ({bytes_total / (1024**3):.2f} GiB)"
                + (f", {errors} errors" if errors else "")
            )

        state.begin_stage("Writing reports")
        generate_reports(config, db)
        state.end_stage(str(config.reports_path))

        with state.lock:
            state.running = False
            state.finished_at = time.time()
            state.activity = "Job complete"
            state.last_activity_at = state.finished_at
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.exception("build job failed")
        state.fail(str(exc))
    finally:
        for source in sources:
            source.close()
        db.close()


def _validate_output_dir(config: AppConfig, raw: str) -> tuple[bool, str, str]:
    """Validate a user-chosen output folder. Returns (ok, error, resolved).

    Browsers cannot open native folder pickers, so this is a validated text
    path: resolvable, creatable/writable, not a drive root or system folder,
    not inside the input dir, and never a folder holding app state.
    """
    raw = (raw or "").strip()
    if not raw:
        return False, "Output folder is required.", ""
    target = config.resolve(raw)
    try:
        target = target.resolve()
    except OSError as exc:
        return False, f"Invalid path: {exc}", ""
    if target.parent == target:
        return False, "Choose a subfolder, not a drive root.", ""
    if target.exists() and not target.is_dir():
        return False, "That path is a file, not a folder.", ""
    if is_within(target, config.input_path):
        return False, "The output folder must not be inside the input folder.", ""
    # Protect the pinned state: the DB must never sit inside a songs folder.
    if is_within(config.database_path, target):
        return False, "That folder contains the app's state database; choose another.", ""
    state_dir = target / "state"
    if state_dir.is_dir() and any(state_dir.glob("*.db")):
        return False, "That folder contains app state (state/*.db); choose another.", ""
    system_root = os.environ.get("SystemRoot")  # e.g. C:\Windows
    if system_root:
        try:
            if is_within(target, Path(system_root)):
                return False, "That is a system folder; choose another.", ""
        except OSError:
            pass
    # Creatable/writable: probe the target itself when it exists, else the
    # nearest existing parent.
    probe_base = target if target.is_dir() else None
    if probe_base is None:
        probe_base = next((p for p in target.parents if p.is_dir()), None)
    if probe_base is None:
        return False, "No existing parent folder to create this under.", ""
    try:
        probe = probe_base / ".c2ch_write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return False, f"Folder is not writable: {exc}", ""
    return True, "", str(target)


def _run_simple_job(
    config: AppConfig,
    state: JobState,
    output_dir: str,
    expansion: bool,
    source_factory: Callable[[AppConfig], list[ChartSource]],
) -> None:
    """Simple-mode build: scan -> search -> auto-accept best review candidates
    -> plan -> relocate -> download -> reports, in one job with no review step.

    Only the output folder is overridden per run; the state DB and reports
    stay pinned to the base dir (see AppConfig.with_output_dir).
    """
    job_config = config.with_output_dir(output_dir)
    db = Database(job_config.database_path)
    sources: list[ChartSource] = []
    try:
        from csv_to_clonehero.overrides import load_overrides

        overrides = load_overrides(config.base_dir)
        sources = source_factory(config)

        state.begin_stage("Reading your music files")
        scan = run_scan(job_config, db, overrides)
        state.end_stage(
            f"{scan.unique_tracks} unique songs from {scan.csv_files} files"
        )

        state.begin_stage("Matching songs")
        first = run_search(
            job_config,
            db,
            sources,
            overrides,
            on_progress=state.query_progress,
            include_expansion=False,
            on_track_scored=state.track_scored,
            on_search_start=state.search_started,
        )
        state.search_finished()
        accepted = db.accept_best_review_candidates()["accepted"]
        if accepted:
            state.log(f"Auto-accepted the best review match for {accepted} songs")
        state.end_stage(
            f"{first.auto} certain matches, {accepted} accepted on review, "
            f"{first.no_match} without a chart"
        )

        if expansion:
            state.begin_stage("Finding more songs by your artists")
            exact = run_search(
                job_config,
                db,
                sources,
                overrides,
                on_progress=state.query_progress,
                include_expansion=True,
                on_track_scored=state.track_scored,
                on_search_start=state.search_started,
            )
            state.search_finished()
            state.end_stage(
                f"{exact.expansion_candidates} extra quality candidates across "
                f"{exact.expansion_artists} artists"
            )
        else:
            # Cache-only re-score so the auto-accepted review matches apply.
            state.begin_stage("Applying accepted matches")
            exact = run_search(
                job_config, db, sources, overrides, include_expansion=False
            )
            state.end_stage(f"{exact.auto} songs matched")

        state.begin_stage("Planning the library")
        plan = run_plan(
            job_config,
            db,
            sources,
            on_progress=state.progress,
            include_expansion=expansion,
        )
        state.end_stage(
            f"{plan.exact_selected} playlist songs"
            + (f" + {plan.expansion_selected} expansion" if expansion else "")
        )

        state.begin_stage("Organizing the library folder")
        relocated = run_relocation(job_config, db)
        if relocated.moved:
            state.log(
                f"Moved {relocated.moved} existing charts into the current layout"
            )
        state.end_stage(
            f"{relocated.moved} charts moved" if relocated.moved else "already up to date"
        )

        state.begin_stage("Downloading charts (this can take a while)")
        downloaded, errors, bytes_total = run_downloads(
            job_config, db, sources, on_progress=state.progress
        )
        removed = cleanup_empty_folders(job_config)
        if removed:
            state.log(f"Removed {removed} empty folders")
        state.end_stage(
            f"{downloaded} downloaded ({bytes_total / (1024**3):.2f} GiB)"
            + (f", {errors} errors" if errors else "")
        )

        state.begin_stage("Writing reports")
        generate_reports(config, db)
        state.end_stage(str(config.reports_path))

        with state.lock:
            state.result = {
                "matched": exact.auto,
                "no_match": exact.no_match,
                "expansion_selected": plan.expansion_selected,
                "downloaded": downloaded,
                "download_errors": errors,
                "bytes": bytes_total,
                "output_dir": str(job_config.library_root),
            }
            state.output_dir = str(job_config.library_root)
            state.running = False
            state.finished_at = time.time()
            state.activity = "Job complete"
            state.last_activity_at = state.finished_at
            state._log_locked("All done.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.exception("simple build job failed")
        state.fail(str(exc))
    finally:
        for source in sources:
            source.close()
        db.close()


def _json_meta(db: Database, key: str) -> dict:
    try:
        value = json.loads(db.get_meta(key, "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _review_payload(
    db: Database,
    state_snapshot: dict,
    include_rejected: bool = False,
) -> dict:
    """Build one review card per Spotify track from persisted score rows."""
    tracks = {track.db_id: track for track in db.get_tracks(include_ignored=True)}
    decisions = db.get_manual_decisions()
    score_rows = db.get_match_scores()
    wanted = {"REVIEW", "REJECT"} if include_rejected else {"REVIEW"}
    grouped: dict[int, list[dict]] = {}
    all_rows: dict[int, list] = {}

    for row in score_rows:
        all_rows.setdefault(row["track_id"], []).append(row)

    review_track_ids = {
        row["track_id"]
        for row in score_rows
        if row["decision"] == "REVIEW"
    }
    # Manual decisions are keyed by source/chart id, so resolve those after
    # loading candidates. The explicit loop keeps rejected candidates out of
    # the default payload without hiding a user's saved decision.
    for row in score_rows:
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is not None and decisions.get(
            (row["track_key"], candidate.source, candidate.chart_id)
        ):
            review_track_ids.add(row["track_id"])
        if include_rejected and row["decision"] == "REJECT":
            review_track_ids.add(row["track_id"])

    for row in score_rows:
        candidate = db.get_candidate(row["candidate_id"])
        if candidate is None:
            continue
        manual = decisions.get((row["track_key"], candidate.source, candidate.chart_id), "")
        include_context = row["decision"] == "AUTO_ACCEPT"
        if row["track_id"] not in review_track_ids or (
            row["decision"] not in wanted and not manual and not include_context
        ):
            continue
        explanation = ""
        try:
            explanation = json.loads(row["breakdown_json"]).get("explanation", "")
        except (TypeError, ValueError):
            pass
        grouped.setdefault(row["track_id"], []).append(
            {
                "candidate_artist": candidate.artist,
                "candidate_title": candidate.name,
                "source": candidate.source,
                "chart_id": candidate.chart_id,
                "score": round(row["score"], 1),
                "decision": row["decision"],
                "automatic_decision": row["decision"],
                "explanation": explanation,
                "charter": candidate.charter,
                "instruments": candidate.instruments,
                "diff_guitar": candidate.diff_guitar,
                "guitar_difficulty": candidate.diff_guitar,
                "url": candidate.url,
                "reference": candidate.reference,
                "chart_reference": candidate.reference,
                "manual": manual,
                "manual_decision": manual,
            }
        )

    cards: list[dict] = []
    for track_id, candidates in grouped.items():
        track = tracks.get(track_id)
        if track is None:
            continue
        candidates.sort(
            key=lambda item: (
                -item["score"],
                item["candidate_artist"].casefold(),
                item["candidate_title"].casefold(),
                item["source"],
                item["chart_id"],
            )
        )
        rows = all_rows.get(track_id, [])
        manual_accept = any(
            decisions.get((track.track_key, candidate["source"], candidate["chart_id"]))
            == "accept"
            and candidate["decision"] != "INCOMPATIBLE"
            for candidate in candidates
        )
        if manual_accept:
            status = "ACCEPTED"
        elif any(row["decision"] == "AUTO_ACCEPT" for row in rows):
            status = "AUTO_ACCEPT"
        elif any(row["decision"] == "REVIEW" for row in rows):
            status = "REVIEW"
        else:
            status = "REJECT"
        cards.append(
            {
                "track_key": track.track_key,
                "spotify_id": track.spotify_id,
                "artist": track.artist_full,
                "title": track.title,
                "album": track.album,
                "playlists": list(track.playlists),
                # Keep the old field names as inexpensive client compatibility
                # aliases while the grouped shape is the primary response.
                "spotify_artist": track.artist_full,
                "spotify_title": track.title,
                "status": status,
                "review_status": status,
                "candidates": candidates,
            }
        )
    cards.sort(key=lambda item: (item["artist"].casefold(), item["title"].casefold()))

    progress = _json_meta(db, "exact_progress")
    fallback_scored = len({row["track_id"] for row in score_rows})
    state_progress = state_snapshot.get("search_progress", {})
    started = bool(progress.get("started", bool(score_rows)))
    searching = bool(state_snapshot.get("search_active", False))
    total_tracks = int(progress.get("total_tracks", len(tracks)))
    scored_tracks = int(progress.get("scored_tracks", fallback_scored))
    if not progress and score_rows:
        scored_tracks = fallback_scored
    exact_complete = bool(progress.get("complete", bool(score_rows))) and not searching
    if searching:
        total_tracks = max(total_tracks, int(state_progress.get("total_tracks", 0)))
        scored_tracks = max(scored_tracks, int(state_progress.get("scored_tracks", 0)))

    metadata = {
        "total_tracks": total_tracks,
        "scored_tracks": scored_tracks,
        "review_track_count": len(cards),
        "review_tracks": len(cards),
        "review_candidate_count": sum(len(card["candidates"]) for card in cards),
        "review_candidates": sum(len(card["candidates"]) for card in cards),
        "candidate_count": sum(len(card["candidates"]) for card in cards),
        "queries_done": int(progress.get("completed_queries", 0)),
        "queries_total": int(progress.get("total_queries", 0)),
        "searching": searching,
        "search_running": searching,
        "search_still_in_progress": searching,
        "exact_scoring_complete": exact_complete,
        "exact_complete": exact_complete,
        "has_search_run": started,
    }
    return {"tracks": cards, "progress": metadata, **metadata}


def _estimate_work(
    config: AppConfig,
    db: Database,
    source_names: list[str],
    overrides,
    disabled_files: set[str] | None = None,
) -> dict:
    """Estimate exact and expansion cache work without making network calls."""
    disabled_files = disabled_files or set()
    if config.input_path.exists() and any(
        p.suffix.lower() in SUPPORTED_EXTENSIONS for p in config.input_path.iterdir()
    ):
        imported = import_playlist_dir(config.input_path, skip=disabled_files)
        tracks = imported.tracks or db.get_tracks()
    else:
        tracks = db.get_tracks()
    ignored = set(overrides.ignore_tracks)
    tracks = [track for track in tracks if track.track_key not in ignored]
    ttl = config.network.cache_ttl_days

    primary_counts, _featured_only_counts = artist_track_counts(tracks)
    display_names: dict[str, str] = {}
    for track in tracks:
        display_names.setdefault(track.primary_artist_norm, track.primary_artist)

    catalog_first = config.search.artist_catalog_first
    min_songs = config.search.artist_catalog_min_songs
    qualifying_artists = {
        artist_norm
        for artist_norm, count in primary_counts.items()
        if catalog_first and count >= min_songs
    }

    exact_keys: set[str] = set()
    for track in tracks:
        title = overrides.title_alias(track.title) or track.title
        if track.primary_artist_norm in qualifying_artists:
            artist_name = overrides.artist_alias(track.primary_artist_norm) or display_names[
                track.primary_artist_norm
            ]
            kind = "artist"
            query_artist = artist_name
            query_title = ""
        else:
            kind = "song"
            query_artist = track.primary_artist
            query_title = title
        for source in source_names:
            exact_keys.add(
                db.query_key(kind, source, query_artist.casefold(), query_title.casefold())
            )
    exact_uncached = sum(1 for key in exact_keys if db.get_cached_query(key, ttl) is None)

    _, featured_only_counts = artist_track_counts(tracks)
    exp = config.expansion

    def cap_for(count: int, featured_only: bool, artist_norm: str) -> int:
        override = overrides.artist_override(artist_norm)
        if override and override.expansion_disabled:
            return 0
        if override and override.expansion_cap is not None:
            return override.expansion_cap
        if featured_only:
            return exp.cap_featured_only
        if count >= exp.min_songs_many:
            return exp.cap_many_songs
        if count >= 2:
            return exp.cap_some_songs
        return exp.cap_one_song

    display_names: dict[str, str] = {}
    for track in tracks:
        display_names.setdefault(track.primary_artist_norm, track.primary_artist)
        for featured in track.featured_artists:
            display_names.setdefault(normalize_artist(featured), featured)
    artists: dict[str, tuple[str, bool, int]] = {}
    for artist_norm, count in primary_counts.items():
        artists[artist_norm] = (display_names.get(artist_norm, artist_norm), False, count)
    for artist_norm, count in featured_only_counts.items():
        artists.setdefault(
            artist_norm, (display_names.get(artist_norm, artist_norm), True, count)
        )
    eligible = [
        (name, artist_norm)
        for artist_norm, (name, featured_only, count) in artists.items()
        if cap_for(count, featured_only, artist_norm) > 0
    ]
    expansion_keys = {
        db.query_key(
            "artist",
            source,
            (overrides.artist_alias(artist_norm) or name).casefold(),
            "",
        )
        for name, artist_norm in eligible
        for source in source_names
    }
    expansion_uncached = sum(
        1 for key in expansion_keys if db.get_cached_query(key, ttl) is None
    )
    return {
        "exact": {
            "song_count": len(tracks),
            "query_count": len(exact_keys),
            "uncached_query_count": exact_uncached,
        },
        "expansion": {
            "eligible_artist_count": len(eligible),
            "query_count": len(expansion_keys),
            "uncached_query_count": expansion_uncached,
        },
    }


def create_app(
    config: AppConfig,
    source_factory: Callable[[AppConfig], list[ChartSource]] | None = None,
    spotify_factory: Callable[..., Any] | Any | None = None,
    oauth_factory: Callable[..., Any] | Any | None = None,
    keyring: Any | None = None,
    lan_access_token: str | None = None,
) -> Flask:
    """Create the local Flask app.

    ``source_factory`` remains the legacy chart-source injection point.  The
    Spotify factories and credential store are optional so offline tests can
    provide tiny fakes without importing or contacting Spotify.
    """
    if source_factory is None:
        source_factory = build_sources

    app = Flask(__name__)
    app.config["LAN_ACCESS_TOKEN"] = str(lan_access_token or "")
    app.config["LAN_MODE"] = bool(lan_access_token)
    app.config["PUBLIC_MODE"] = bool(getattr(getattr(config, "spotify", None), "public_base_url", "").strip())
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    logging.getLogger("werkzeug").addFilter(_OAuthAccessLogFilter())
    recovery_db = Database(config.database_path)
    try:
        recovery_db.recover_interrupted_discovery()
    finally:
        recovery_db.close()
    state = JobState()
    spotify_job = _SpotifyJobState()
    export_job = _ExportJobState()
    auth_lock = threading.RLock()
    auth_client: Any = None
    spotify_client: Any = None
    pending_authorization: dict[str, Any] | None = None
    auth_authenticated = False
    spotify_cancel_event: threading.Event | None = None
    secure_store = keyring if keyring is not None else _default_keyring()
    browser_exports: dict[int, Path] = {}

    # Expose only injectable handles and non-secret state for application
    # integration tests.  Tokens are deliberately absent from this mapping.
    app.extensions["spotify_job"] = spotify_job
    app.extensions["spotify_oauth_factory"] = oauth_factory
    app.extensions["spotify_client_factory"] = spotify_factory

    def _spotify_settings() -> dict[str, Any]:
        settings = getattr(config, "spotify_oauth_kwargs", {})
        if callable(settings):
            settings = settings()
        return dict(settings or {})

    def _get_oauth() -> Any:
        nonlocal auth_client
        with auth_lock:
            if auth_client is not None:
                return auth_client
            if oauth_factory is not None:
                auth_client = _call_injected_factory(
                    oauth_factory,
                    config=config,
                    keyring=secure_store,
                    purpose="oauth",
                )
            else:
                settings = _spotify_settings()
                if secure_store is not None:
                    settings["keyring"] = secure_store
                auth_client = SpotifyOAuth(**settings)
            if auth_client is None:
                raise RuntimeError("Spotify OAuth is not configured")
            return auth_client

    def _get_spotify_client() -> Any:
        nonlocal spotify_client
        with auth_lock:
            if spotify_client is not None:
                return spotify_client
            if spotify_factory is not None:
                oauth = auth_client
                try:
                    signature = inspect.signature(spotify_factory) if callable(spotify_factory) else None
                    names = set(signature.parameters) if signature is not None else set()
                except (TypeError, ValueError):
                    names = set()
                needs_oauth = bool(names & {"oauth", "auth", "client", "spotify_oauth"})
                if needs_oauth and oauth is None:
                    oauth = _get_oauth()
                spotify_client = _call_injected_factory(
                    spotify_factory,
                    config=config,
                    oauth=oauth,
                    keyring=secure_store,
                    purpose="spotify",
                )
            else:
                oauth = _get_oauth()
                spotify_client = SpotifyClient(oauth=oauth)
            if spotify_client is None:
                raise RuntimeError("Spotify client is not configured")
            return spotify_client

    def _oauth_secret_values(oauth: Any = None) -> list[str]:
        values: list[str] = []
        owner = oauth or auth_client
        token = getattr(owner, "token", None)
        if token is not None:
            for name in ("access_token", "refresh_token"):
                value = getattr(token, name, None)
                if value:
                    values.append(str(value))
        for name in ("access_token", "refresh_token", "client_secret"):
            value = getattr(owner, name, None)
            if value and isinstance(value, (str, bytes)):
                values.append(value.decode() if isinstance(value, bytes) else value)
        return values

    def _account_row() -> Any:
        db = Database(config.database_path)
        try:
            return db.get_spotify_account()
        finally:
            db.close()

    def _secure_refresh_token_available(oauth: Any = None) -> bool:
        owner = oauth or auth_client
        loader = getattr(owner, "load_refresh_token", None)
        if callable(loader):
            try:
                return bool(loader())
            except Exception:
                return False
        if secure_store is None:
            return False
        service = str(getattr(config, "keyring_service", "") or "")
        username = str(getattr(config, "keyring_user", "") or "")
        getter = getattr(secure_store, "get_password", None) or getattr(secure_store, "get", None)
        if callable(getter) and service and username:
            try:
                return bool(getter(service, username))
            except Exception:
                return False
        return False

    def _is_authenticated() -> bool:
        if auth_authenticated:
            return True
        row = _account_row()
        if row is None:
            return False
        owner = auth_client
        token = getattr(owner, "token", None) if owner is not None else None
        if token is not None:
            return True
        return _secure_refresh_token_available(owner)

    def _loopback_request() -> bool:
        address = request.remote_addr
        return address in (None, "127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _lan_authorized() -> bool:
        expected = str(app.config.get("LAN_ACCESS_TOKEN") or "")
        supplied = request.headers.get("X-CSV-To-CloneHero-Access-Token", "")
        if not supplied:
            supplied = request.args.get("access_token", "")
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def _safe_local_request() -> bool:
        return _loopback_request() or (bool(app.config.get("LAN_MODE")) and _lan_authorized())

    def _running_jobs() -> list[str]:
        """Names of background jobs currently in flight across every surface."""
        running: list[str] = []
        with state.lock:
            if state.running:
                running.append(str(state.action or "build"))
        with spotify_job.lock:
            if spotify_job.running:
                running.append("Spotify discovery")
        with export_job.lock:
            if export_job.running:
                running.append("chart export")
        return running

    def _busy_response() -> tuple[dict[str, Any], int] | None:
        """Reject a mutating request while any background job is running."""
        jobs = _running_jobs()
        if jobs:
            return {
                "error": f"{jobs[0]} is already running; wait for it to finish first",
                "busy": True,
                "running_jobs": jobs,
            }, 409
        return None

    @app.before_request
    def require_lan_access_token():
        # Spotify's redirect cannot carry our private access token. Its opaque
        # state is validated before an authorization code is exchanged.
        if app.config.get("PUBLIC_MODE") and request.path == "/api/auth/spotify/callback":
            return None
        if _loopback_request() or not app.config.get("LAN_MODE") or _lan_authorized():
            return None
        if request.path.startswith("/api/"):
            return {"error": "LAN access requires the access token from the host computer."}, 401
        return (
            "LAN access requires the access-token URL printed by the host computer.",
            401,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def _workspace_id(value: Any = None, *, required: bool = True) -> int | str | None:
        requested = value
        if requested in (None, ""):
            requested = "spotify"
        db = Database(config.database_path)
        try:
            row = db.get_workspace(requested)
            if row is None:
                if required:
                    raise ValueError("Spotify discovery has not been run yet")
                return None
            return int(row["id"])
        finally:
            db.close()

    def _workspace_row(value: Any = None) -> Any:
        workspace = _workspace_id(value)
        db = Database(config.database_path)
        try:
            return db.get_workspace(workspace)
        finally:
            db.close()

    def _save_workspace_snapshot(workspace: int | str) -> dict[str, Any]:
        """Commit current state and checkpoint SQLite's durable WAL."""
        db = Database(config.database_path)
        try:
            row = db.get_workspace(workspace)
            if row is None:
                raise ValueError("Spotify discovery workspace was not found")
            saved_at = time.time()
            metadata = _json_object(row["metadata_json"], {})
            metadata.update({"saved_at": saved_at, "saved_explicitly": True})
            db.update_workspace(workspace, metadata=metadata)
            db._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            counts = db._conn.execute(
                """SELECT COUNT(*) AS candidates,
                          COUNT(DISTINCT track_id) AS tracks
                   FROM workspace_candidates WHERE workspace_id = ?""",
                (int(row["id"]),),
            ).fetchone()
            return {
                "saved": True,
                "saved_at": saved_at,
                "database": str(config.database_path),
                "candidates": int(counts["candidates"] or 0),
                "tracks": int(counts["tracks"] or 0),
                "selections": len(db.list_workspace_selections(workspace)),
            }
        finally:
            db.close()

    def _workspace_status(value: Any = None) -> dict[str, Any]:
        memory = spotify_job.snapshot()
        workspace_id = value if value not in (None, "") else memory.get("workspace_id") or "spotify"
        db: Database | None = None
        try:
            db = Database(config.database_path)
            row = db.get_workspace(workspace_id)
            progress_row = db.get_workspace_progress(workspace_id) if row is not None else None
        except (ValueError, OSError, sqlite3.OperationalError):
            row = None
            progress_row = None
        finally:
            if db is not None:
                db.close()
        if row is not None:
            workspace_id = int(row["id"])
        persisted = _row_dict(progress_row)
        progress = {
            "total_tasks": int(persisted.get("total_tasks", row["total_tasks"] if row else 0) or 0),
            "completed_tasks": int(persisted.get("completed_tasks", row["completed_tasks"] if row else 0) or 0),
            "failed_tasks": int(persisted.get("failed_tasks", row["failed_tasks"] if row else 0) or 0),
            "total_tracks": int(persisted.get("total_tracks", row["total_tracks"] if row else 0) or 0),
            "completed_tracks": int(persisted.get("completed_tracks", row["completed_tracks"] if row else 0) or 0),
        }
        progress["done"] = progress["completed_tasks"]
        progress["total"] = progress["total_tasks"]
        progress.update({
            key: value
            for key, value in memory.get("progress", {}).items()
            if memory.get("running") and value is not None
        })
        if not memory.get("running") and row is not None:
            metadata = _json_object(row["metadata_json"] if "metadata_json" in row.keys() else "", {})
            stats = metadata.get("stats", {}) if isinstance(metadata, Mapping) else {}
            if isinstance(stats, Mapping):
                scoring_total = int(stats.get("scoring_tasks_total", 0) or 0)
                scoring_done = int(stats.get("scoring_tasks_completed", 0) or 0)
                progress.update({
                    "scoring_tasks_total": scoring_total,
                    "scoring_tasks_completed": scoring_done,
                    "total_work": progress["total_tasks"] + scoring_total,
                    "completed_work": progress["completed_tasks"] + scoring_done,
                    "remaining_work": max(
                        0,
                        progress["total_tasks"] + scoring_total
                        - progress["completed_tasks"] - scoring_done,
                    ),
                })
        result = dict(memory)
        active = bool(memory.get("running"))
        result.update({
            "workspace_id": workspace_id if row is not None or memory.get("workspace_id") else None,
            "workspace_key": str(row["workspace_key"] if row else memory.get("workspace_key", "spotify")),
            "status": str(row["status"] if row else memory.get("status", "created")),
            "phase": str(persisted.get("phase") or (row["phase"] if row else memory.get("phase", "idle"))),
            "progress": progress,
            "message": _safe_message(
                memory.get("message", "") if active else persisted.get("message") or memory.get("message", "")
            ),
            "error": _safe_message(
                memory.get("error", "") if active else persisted.get("error") or memory.get("error", "")
            ),
            "partial": bool(memory.get("partial") or _json_object(row["metadata_json"] if row else "", {}).get("partial", False)),
        })
        # ``progress`` is the stable new shape; aliases make polling clients
        # independent of whether they read the in-memory or SQLite phase.
        result["total"] = progress["total_tasks"]
        result["done"] = progress["completed_tasks"]
        result["total_tasks"] = progress["total_tasks"]
        result["completed_tasks"] = progress["completed_tasks"]
        running_jobs = _running_jobs()
        result["busy"] = bool(running_jobs)
        result["running_jobs"] = running_jobs
        return result

    def _validate_spotify_folder(raw: str) -> tuple[bool, str, str]:
        """Apply the legacy path checks plus Spotify workspace state guards."""
        raw = str(raw or "").strip()
        if not raw:
            return False, "Export folder is required.", ""
        try:
            candidate = config.resolve(raw).expanduser().resolve()
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            if is_within(candidate, config.input_path):
                return False, "The export folder must not be inside the input folder.", ""
            if is_within(candidate, config.state_path):
                return False, "The export folder must not contain app state.", ""
            if system_root and is_within(candidate, Path(system_root)):
                return False, "The export folder must not be a system folder.", ""
        except OSError as exc:
            return False, f"Invalid path: {exc}", ""
        ok, error, resolved = _validate_output_dir(config, raw)
        if not ok:
            return ok, error, resolved
        target = Path(resolved)
        try:
            if is_within(target, config.input_path):
                return False, "The export folder must not be inside the input folder.", ""
            if is_within(target, config.state_path):
                return False, "The export folder must not contain app state.", ""
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            if system_root and is_within(target, Path(system_root)):
                return False, "The export folder must not be a system folder.", ""
        except OSError as exc:
            return False, f"Invalid path: {exc}", ""
        return True, "", str(target)

    def _validate_zip_path(raw: str) -> tuple[bool, str, str]:
        value = str(raw or "").strip()
        if not value:
            return False, "ZIP destination is required.", ""
        target = config.resolve(value)
        try:
            target = target.expanduser().resolve()
        except OSError as exc:
            return False, f"Invalid path: {exc}", ""
        if target.suffix.casefold() != ".zip":
            return False, "ZIP destination must end in .zip.", ""
        if target.exists() and target.is_dir():
            return False, "That path is a folder, not a ZIP file.", ""
        parent_ok, parent_error, parent = _validate_spotify_folder(str(target.parent))
        if not parent_ok:
            return False, parent_error, ""
        try:
            if target.exists() and target.is_symlink():
                return False, "Refusing to replace a symbolic-link ZIP target.", ""
        except OSError as exc:
            return False, f"Invalid path: {exc}", ""
        return True, "", str(Path(parent) / target.name)

    def _candidate_payload(row: Any) -> dict[str, Any]:
        data = _row_dict(row)
        candidate = _candidate_from_payload(_json_object(data.get("candidate_json"), {}))
        breakdown = _json_object(data.get("breakdown_json"), {})
        if not isinstance(breakdown, Mapping):
            breakdown = {}
        if candidate is None:
            return {
                "candidate_id": data.get("candidate_id"),
                "source": data.get("source", ""),
                "chart_id": data.get("chart_id"),
                "score": float(data.get("score") or 0),
                "quality": float(data.get("quality") or 0),
                "eligibility": data.get("eligibility", ""),
                "decision": data.get("decision", ""),
                "selected": bool(data.get("selected")),
            }
        source_artwork = str(
            getattr(candidate, "source_artwork_url", "")
            or getattr(candidate, "artwork_url", "")
            or ""
        )
        artwork_id = str(
            getattr(candidate, "source_artwork_id", "")
            or getattr(candidate, "artwork_id", "")
            or ""
        )
        if artwork_id and re.fullmatch(r"[A-Fa-f0-9]{16,64}", artwork_id):
            source_artwork = f"/api/artwork/source/{candidate.source}/{artwork_id}"
        return {
            "id": data.get("candidate_id"),
            "candidate_id": data.get("candidate_id"),
            "source": str(getattr(candidate, "source", data.get("source", "")) or ""),
            "chart_id": getattr(candidate, "chart_id", data.get("chart_id")),
            "artist": str(getattr(candidate, "artist", "") or ""),
            "title": str(getattr(candidate, "name", "") or ""),
            "name": str(getattr(candidate, "name", "") or ""),
            "charter": str(getattr(candidate, "charter", "") or ""),
            "pack_name": str(getattr(candidate, "pack_name", "") or ""),
            "album": str(getattr(candidate, "album", "") or ""),
            "url": str(getattr(candidate, "url", "") or ""),
            "reference": str(getattr(candidate, "reference", "") or ""),
            "md5": str(getattr(candidate, "md5", "") or ""),
            "instruments": list(getattr(candidate, "instruments", ()) or ()),
            "diff_guitar": getattr(candidate, "diff_guitar", None),
            "supports_download": bool(getattr(candidate, "supports_download", True)),
            "source_artwork_url": source_artwork,
            "source_artwork_id": artwork_id,
            "score": round(float(data.get("score") or breakdown.get("score") or 0), 1),
            "quality": round(float(data.get("quality") or breakdown.get("quality") or 0), 1),
            "eligibility": str(
                data.get("eligibility")
                or breakdown.get("eligibility")
                or breakdown.get("eligibility_state")
                or ""
            ),
            "eligibility_reasons": list(
                breakdown.get("eligibility_reasons")
                or breakdown.get("reasons")
                or []
            ),
            "decision": str(data.get("decision") or breakdown.get("decision") or ""),
            "explanation": str(breakdown.get("explanation") or ""),
            "selected": bool(data.get("selected")),
            "kind": str(data.get("kind") or "exact"),
            "artist_norm": str(data.get("artist_norm") or ""),
        }

    def _folder_payload(folder: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(folder)
        return {
            "id": data.get("id") or data.get("spotify_id") or data.get("folder_id"),
            "folder_id": data.get("folder_id") or data.get("id") or data.get("spotify_id"),
            "spotify_id": data.get("spotify_id") or data.get("id", ""),
            "name": data.get("name", ""),
            "url": data.get("url", ""),
            "owner_id": data.get("owner_id", ""),
            "owner_name": data.get("owner_name", ""),
            "created_by": data.get("owner_name") or data.get("owner_id") or ("Spotify" if data.get("is_liked") else "Unknown Spotify user"),
            # Artwork is navigation-only.  Candidate serializers never copy
            # this field, so Spotify artwork cannot appear on chart cards.
            "artwork_url": data.get("artwork_url", ""),
            "access_status": data.get("access_status", "accessible"),
            "is_liked": bool(data.get("is_liked")),
            "track_count": int(data.get("track_count") or 0),
            "candidate_count": int(data.get("candidate_count") or 0),
            "eligible_count": int(data.get("eligible_count") or 0),
            "no_match_count": int(data.get("no_match_count") or 0),
        }

    def _track_with_candidates(
        db: Database,
        workspace: int | str,
        row: Any,
        candidate_rows: list[Any] | None = None,
    ) -> dict[str, Any]:
        data = _row_dict(row)
        candidates = candidate_rows if candidate_rows is not None else db.get_workspace_candidates(
            workspace,
            track_id=data.get("id") or data.get("spotify_id"),
            kind="exact",
            include_rejected=False,
        )
        visible_candidates = candidates[:5]
        owned_matches = db.get_inventory_identity_matches(
            str(data.get("primary_artist_norm", "") or ""),
            str(data.get("title_norm", "") or ""),
        )
        payload = _track_payload(data)
        payload["db_id"] = data.get("id")
        payload.update(
            {
                "candidate_count": int(data.get("candidate_count") or len(candidates)),
                "eligible_candidate_count": len(candidates),
                "selected_count": int(data.get("selected_count") or 0),
                "already_owned": bool(data.get("already_owned")),
                "owned_count": int(data.get("owned_count") or 0),
                "owned_paths": [f"{row['root_path']}\\{row['relative_path']}" for row in owned_matches],
                "candidates": [_candidate_payload(candidate) for candidate in visible_candidates],
                "status": (
                    "already_owned" if bool(data.get("already_owned")) else
                    "selected" if int(data.get("selected_count") or 0) else
                    "matched" if candidates else "no_match"
                ),
            }
        )
        return payload

    def _track_page_payloads(
        db: Database,
        workspace: int | str,
        rows: list[Any],
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        grouped = db.get_workspace_candidates_for_tracks(
            workspace,
            [int(row["id"]) for row in rows],
            kind="exact",
            include_rejected=include_rejected,
        )
        return [
            _track_with_candidates(db, workspace, row, grouped.get(int(row["id"]), []))
            for row in rows
        ]

    def _paged_track_rows(
        db: Database,
        workspace: int,
        rows: list[Any],
        *,
        view: str = "eligible",
        search_text: str = "",
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[list[Any], int]:
        actual_view = str(view or "eligible").casefold()
        eligible_ids: set[int] = set()
        excluded_ids: set[int] = set()
        if actual_view in {"eligible", "recommended", "excluded"}:
            states = ("RECOMMENDED",) if actual_view == "recommended" else ("RECOMMENDED", "ELIGIBLE_REVIEW")
            placeholders = ",".join("?" for _ in states)
            eligible_ids = {
                int(row["track_id"])
                for row in db._conn.execute(
                    f"""SELECT DISTINCT track_id FROM workspace_candidates
                        WHERE workspace_id = ? AND kind = 'exact'
                          AND eligibility IN ({placeholders}) AND track_id IS NOT NULL""",
                    (workspace, *states),
                )
            }
        if actual_view == "excluded":
            # Tracks that have chart candidates but none eligible: the rescuable
            # "low-quality only" pool a user may still choose to accept.
            excluded_ids = {
                int(row["track_id"])
                for row in db._conn.execute(
                    """SELECT DISTINCT track_id FROM workspace_candidates
                       WHERE workspace_id = ? AND kind = 'exact'
                         AND eligibility = 'EXCLUDED' AND track_id IS NOT NULL""",
                    (workspace,),
                )
            }
        needle = str(search_text or "").strip().casefold()
        filtered = []
        for row in rows:
            if actual_view == "recommended" and int(row["id"]) not in eligible_ids:
                continue
            if actual_view == "eligible" and not (
                int(row["id"]) in eligible_ids
                or int(row["selected_count"] or 0)
                or bool(row["already_owned"])
            ):
                continue
            if actual_view == "excluded" and not (
                int(row["id"]) in excluded_ids
                and int(row["id"]) not in eligible_ids
            ):
                continue
            if needle and needle not in " ".join(
                str(row[key] or "")
                for key in ("primary_artist", "artist_full", "title", "album")
            ).casefold():
                continue
            filtered.append(row)
        total = len(filtered)
        start = max(0, int(offset))
        return (filtered[start:] if limit is None else filtered[start:start + limit]), total

    def _workspace_payload(
        value: Any = None,
        folder_id: Any = None,
        liked: bool = False,
        *,
        view: str = "eligible",
        search_text: str = "",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        workspace = _workspace_id(value)
        db = Database(config.database_path)
        try:
            folders = [_folder_payload(folder) for folder in db.get_library_folders(workspace_id=workspace)]
            rows = db.get_library_track_rows(
                folder_id=folder_id,
                liked=liked,
                workspace_id=workspace,
            )
            page_rows, track_total = _paged_track_rows(
                db,
                workspace,
                rows,
                view=view,
                search_text=search_text,
                offset=offset,
                limit=limit,
            )
            include_rejected = str(view or "").casefold() in {"excluded", "all"}
            tracks = _track_page_payloads(db, workspace, page_rows, include_rejected=include_rejected)
            selections = db.list_workspace_selections(workspace)
            expansion = db.get_workspace_expansion_artists(workspace)
            workspace_row = db.get_workspace(workspace)
        finally:
            db.close()
        return {
            "workspace": _row_dict(workspace_row),
            "folders": folders,
            "tracks": tracks,
            "track_total": track_total,
            "track_offset": max(0, int(offset)),
            "track_limit": limit,
            "has_more": limit is not None and max(0, int(offset)) + len(tracks) < track_total,
            "selected": len(selections),
            "selection_count": len(selections),
            "expansion_artists": [_row_dict(item) for item in expansion],
        }

    def _candidate_row_for_request(
        db: Database,
        workspace: int | str,
        payload: Mapping[str, Any],
    ) -> tuple[Any | None, str, Any, str]:
        kind = str(payload.get("kind") or "exact").casefold()
        if kind not in {"exact", "expansion"}:
            return None, kind, None, "unsupported selection kind"
        track_value = (
            payload.get("track_id")
            or payload.get("spotify_track_id")
            or payload.get("spotify_id")
            or payload.get("track_key")
        )
        artist_norm = str(
            payload.get("artist_norm")
            or payload.get("artist_id")
            or ""
        ).strip()
        if kind == "exact" and not track_value:
            return None, kind, None, "track_id is required"
        if kind == "expansion" and not artist_norm:
            return None, kind, None, "artist_norm is required"
        rows = db.get_workspace_candidates(
            workspace,
            track_id=track_value if kind == "exact" else None,
            kind=kind,
            include_rejected=True,
        )
        requested_candidate = payload.get("candidate_id")
        source = str(payload.get("source") or "")
        chart_id = payload.get("chart_id")
        for row in rows:
            data = _row_dict(row)
            if kind == "expansion" and str(data.get("artist_norm") or "") != artist_norm:
                continue
            if requested_candidate not in (None, ""):
                try:
                    if int(requested_candidate) != int(data.get("candidate_id")):
                        continue
                except (TypeError, ValueError):
                    if str(requested_candidate) != str(data.get("candidate_id")):
                        continue
            if source and source != str(data.get("source") or ""):
                continue
            if chart_id not in (None, "") and str(chart_id) != str(data.get("chart_id")):
                continue
            return row, kind, track_value, artist_norm
        return None, kind, track_value, "no matching workspace candidate"

    def _validate_candidate_eligibility(
        db: Database,
        workspace: int | str,
        row: Any,
        kind: str,
        force: bool = False,
    ) -> tuple[bool, str]:
        data = _row_dict(row)
        persisted = str(data.get("eligibility") or "").upper()
        breakdown = _json_object(data.get("breakdown_json"), {})
        if not isinstance(breakdown, Mapping):
            breakdown = {}
        if persisted not in {RECOMMENDED, ELIGIBLE_REVIEW}:
            persisted = str(
                breakdown.get("eligibility")
                or breakdown.get("eligibility_state")
                or persisted
            ).upper()
        if persisted not in {RECOMMENDED, ELIGIBLE_REVIEW}:
            if not force or persisted in {"INCOMPATIBLE", "REJECTED", "NO_MATCH", "NOT_EXPORTABLE"}:
                return False, "candidate is not eligible for selection"
        # Re-check the persisted scoring state at the web boundary.  This is
        # intentionally independent of the row's boolean selected marker.
        if breakdown and not force:
            try:
                eligibility = eligibility_for(breakdown, config)
                if str(eligibility.state).upper() not in {RECOMMENDED, ELIGIBLE_REVIEW}:
                    return False, "candidate is not eligible for selection"
            except (TypeError, ValueError):
                # Older workspace rows may contain only the eligibility column;
                # the database method still performs the same blocked-state check.
                pass
        return True, ""

    def _selection_payload(db: Database, row: Any) -> dict[str, Any]:
        data = _row_dict(row)
        candidate = _candidate_payload(row)
        return {
            "id": data.get("id"),
            "selection_id": data.get("id"),
            "workspace_id": data.get("workspace_id"),
            "kind": data.get("kind", "exact"),
            "track_id": data.get("spotify_id") or data.get("track_id"),
            "spotify_track_id": data.get("spotify_id", ""),
            "track_key": data.get("track_key", ""),
            "track": {
                "title": data.get("track_title", ""),
                "artist": data.get("track_artist", ""),
                "spotify_id": data.get("spotify_id", ""),
            },
            "artist_norm": data.get("artist_norm", ""),
            "source": data.get("source", ""),
            "chart_id": data.get("chart_id"),
            "candidate_id": data.get("candidate_id"),
            "score": data.get("score"),
            "eligibility": data.get("eligibility", ""),
            "status": data.get("status", "selected"),
            "target_relpath": data.get("target_relpath", ""),
            "candidate": candidate,
        }

    def _profile_from_client(client: Any, token: Any = None) -> dict[str, Any]:
        profile: Any = None
        for name in ("get_current_user", "current_user", "get_profile", "profile", "me"):
            method = getattr(client, name, None)
            if callable(method):
                try:
                    profile = method()
                    break
                except Exception:
                    continue
        if profile is None:
            request_json = getattr(client, "_request_json", None)
            if callable(request_json):
                try:
                    profile = request_json("GET", "/me")
                except Exception:
                    profile = None
        data = _row_dict(profile)
        if not data and isinstance(profile, Mapping):
            data = dict(profile)
        token_scopes = getattr(token, "scope", ()) if token is not None else ()
        if isinstance(token_scopes, str):
            token_scopes = token_scopes.split()
        return {
            "spotify_user_id": str(data.get("id") or data.get("user_id") or ""),
            "user_id": str(data.get("id") or data.get("user_id") or ""),
            "display_name": str(data.get("display_name") or data.get("name") or "Spotify user"),
            "country": str(data.get("country") or ""),
            "product": str(data.get("product") or ""),
            "href": str(data.get("external_urls", {}).get("spotify") if isinstance(data.get("external_urls"), Mapping) else data.get("href") or ""),
            "scopes": list(token_scopes or getattr(config, "spotify_scopes", ()) or ()),
            "metadata": {"source": "spotify", "signed_in_at": time.time()},
        }

    def _persist_refresh_token(token: Any, oauth: Any) -> None:
        refresh = None
        if isinstance(token, Mapping):
            refresh = token.get("refresh_token")
        else:
            refresh = getattr(token, "refresh_token", None)
        if not refresh or secure_store is None:
            return
        service = str(getattr(config, "keyring_service", "") or "")
        username = str(getattr(config, "keyring_user", "") or "")
        setter = getattr(secure_store, "set_password", None) or getattr(secure_store, "set", None)
        if callable(setter) and service and username:
            setter(service, username, str(refresh))

    def _begin_oauth(oauth: Any) -> tuple[str, str, Any, str]:
        requested_state = secrets.token_urlsafe(32)
        port = getattr(getattr(config, "spotify", None), "redirect_port", 8765)
        request_object: Any = None
        method = getattr(oauth, "begin_authorization", None)
        if not callable(method):
            method = getattr(oauth, "authorization_url", None)
        if not callable(method):
            raise RuntimeError("Spotify OAuth client has no authorization method")
        attempts = (
            {"state": requested_state, "port": port},
            {"state": requested_state},
            {"port": port},
            {},
        )
        last: Exception | None = None
        for kwargs in attempts:
            try:
                request_object = method(**kwargs)
                break
            except TypeError as exc:
                last = exc
                continue
        if request_object is None and last is not None:
            raise last
        if isinstance(request_object, (tuple, list)) and request_object:
            url = str(request_object[0] or "")
            if len(request_object) > 1 and request_object[1]:
                requested_state = str(request_object[1])
        elif isinstance(request_object, Mapping):
            url = str(
                request_object.get("url")
                or request_object.get("authorization_url")
                or request_object.get("uri")
                or ""
            )
        elif isinstance(request_object, str):
            url = request_object
        else:
            url = str(
                getattr(request_object, "url", None)
                or getattr(request_object, "authorization_url", None)
                or ""
            )
        if not url:
            raise RuntimeError("Spotify OAuth did not return an authorization URL")
        query_state = parse_qs(urlsplit(url).query).get("state", [""])[0]
        object_state = request_object.get("state", "") if isinstance(request_object, Mapping) else getattr(request_object, "state", "")
        actual_state = str(object_state or query_state or requested_state)
        object_redirect = request_object.get("redirect_uri", "") if isinstance(request_object, Mapping) else getattr(request_object, "redirect_uri", "")
        redirect_uri = str(
            object_redirect
            or getattr(getattr(oauth, "config", None), "redirect_uri", "")
            or getattr(config, "spotify_redirect_uri", "")
            or ""
        )
        parsed = urlsplit(redirect_uri)
        is_loopback = parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port is not None
        is_public = parsed.scheme == "https" and bool(parsed.hostname)
        if not is_loopback and not is_public:
            raise RuntimeError("Spotify OAuth must use a loopback or configured HTTPS redirect")
        return url, actual_state, request_object, redirect_uri

    def _exchange_oauth(oauth: Any, code: str, returned_state: str, request_object: Any) -> Any:
        method = getattr(oauth, "exchange_code", None)
        if not callable(method):
            raise RuntimeError("Spotify OAuth client has no code exchange method")
        attempts = []
        if not isinstance(request_object, str):
            attempts.append(((code,), {"authorization_request": request_object, "returned_state": returned_state}))
        attempts.extend(
            [
                ((code,), {"state": returned_state}),
                ((code, returned_state), {}),
                ((code, request_object), {"returned_state": returned_state}),
                ((code,), {}),
            ]
        )
        last: Exception | None = None
        for args, kwargs in attempts:
            try:
                return method(*args, **kwargs)
            except TypeError as exc:
                last = exc
                continue
        if last is not None:
            raise last
        raise RuntimeError("Spotify OAuth code exchange failed")

    def _start_spotify_job(kind: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        nonlocal spotify_cancel_event
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        busy = _busy_response()
        if busy is not None:
            return busy
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a Spotify discovery job is already running"}, 409
        try:
            workspace = _workspace_id(payload.get("workspace_id"), required=False)
            if workspace is None:
                db = Database(config.database_path)
                try:
                    workspace = db.create_workspace(
                        workspace_key="spotify",
                        name="Spotify library",
                        kind="exact",
                        status="created",
                        phase="idle",
                    )
                finally:
                    db.close()
            workspace = int(workspace)
            if kind == "exact":
                db = Database(config.database_path)
                try:
                    scope = db.get_workspace_scope_data(workspace)
                finally:
                    db.close()
                if scope is None or (not scope.get("playlist_ids") and not scope.get("include_liked")):
                    return {"error": "choose at least one Spotify playlist or enable Liked Songs first"}, 400
            selected_norms = payload.get("artist_norms") or payload.get("artists") or None
            if kind == "expansion":
                db = Database(config.database_path)
                try:
                    selected_rows = db.get_workspace_expansion_artists(workspace, selected_only=True)
                finally:
                    db.close()
                if selected_norms is None and not selected_rows:
                    return {"error": "select at least one expansion artist first"}, 400
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 400

        with spotify_job.lock:
            spotify_job.scan_key = "expansion" if kind == "expansion" else "exact"
            spotify_job.begin(workspace)
            spotify_cancel_event = threading.Event()
            active_cancel_event = spotify_cancel_event
        status_db = Database(config.database_path)
        try:
            status_db.update_workspace_progress(
                workspace,
                status="running",
                phase="starting",
                message="Starting Spotify discovery...",
                error="",
            )
        finally:
            status_db.close()

        def worker() -> None:
            nonlocal spotify_cancel_event
            db = Database(config.database_path)
            sources: list[ChartSource] = []
            result: Any = None
            try:
                sources = list(source_factory(config))
                client = _get_spotify_client()

                def on_progress(value: Any) -> None:
                    if isinstance(value, Mapping):
                        spotify_job.progress_update(value)

                def on_log(value: Any, *_args: Any) -> None:
                    spotify_job.log(_safe_message(value, _oauth_secret_values()))

                if kind == "expansion":
                    norms = selected_norms
                    if isinstance(norms, str):
                        norms = [norms]
                    result = run_expansion_discovery(
                        config,
                        db,
                        workspace,
                        sources,
                        selected_artist_norms=list(norms) if norms is not None else None,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                else:
                    result = run_spotify_discovery(
                        config,
                        db,
                        sources,
                        client,
                        workspace_id=workspace,
                        include_liked=bool(payload.get("include_liked", True)),
                        cancellation_event=active_cancel_event,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                spotify_job.finish(result)
                try:
                    _save_workspace_snapshot(workspace)
                except Exception:
                    log.warning("Could not write automatic Spotify workspace snapshot", exc_info=True)
            except Exception as exc:  # noqa: BLE001 - surfaced through status
                message = _safe_message(exc, _oauth_secret_values())
                log.error("Spotify discovery job failed")
                try:
                    db.update_workspace(workspace, status="failed", phase="failed", metadata={"error": message})
                except Exception:
                    pass
                spotify_job.fail(message)
            finally:
                with spotify_job.lock:
                    if spotify_cancel_event is active_cancel_event:
                        spotify_cancel_event = None
                for source in sources:
                    try:
                        source.close()
                    except Exception:
                        pass
                db.close()

        threading.Thread(target=worker, daemon=True).start()
        return {
            "started": True,
            "job": "spotify-discovery",
            "kind": kind,
            "workspace_id": workspace,
            "workspace_key": "spotify",
        }, 202

    # ---------------------------------------------------------- Spotify auth
    @app.get("/api/auth/status")
    def spotify_auth_status():
        account = _spotify_account_payload(_account_row())
        configured = bool(getattr(config, "spotify_configuration_ready", False))
        authenticated = _is_authenticated()
        return jsonify(
            {
                "authenticated": authenticated,
                "signed_in": authenticated,
                "configured": configured,
                "configuration_error": str(getattr(config, "spotify_configuration_error", "") or "")
                if not configured
                else "",
                "redirect_uri": str(getattr(config, "spotify_redirect_uri", "") or ""),
                "account": account if authenticated else None,
                "spotify_account": account if authenticated else None,
                "private_beta": True,
                "allowlist": "Spotify access is currently a private beta; approved accounts only.",
            }
        )

    @app.get("/api/auth/spotify/start")
    def spotify_auth_start():
        nonlocal pending_authorization
        if not _loopback_request() and not (app.config.get("PUBLIC_MODE") and _safe_local_request()):
            return {"error": "Spotify sign-in requires the private access link"}, 403
        if oauth_factory is None and not bool(getattr(config, "spotify_configuration_ready", False)):
            return {
                "error": str(
                    getattr(config, "spotify_configuration_error", "Spotify OAuth is not configured")
                    or "Spotify OAuth is not configured"
                )
            }, 503
        try:
            oauth = _get_oauth()
            url, oauth_state, request_object, redirect_uri = _begin_oauth(oauth)
            with auth_lock:
                pending_authorization = {
                    "state": oauth_state,
                    "request": request_object,
                    "oauth": oauth,
                    "redirect_uri": redirect_uri,
                    "created_at": time.time(),
                }
            return jsonify(
                {
                    "authorization_url": url,
                    "url": url,
                    "state": oauth_state,
                    "redirect_uri": redirect_uri,
                }
            )
        except Exception as exc:  # noqa: BLE001 - safe OAuth error boundary
            message = _safe_message(exc, _oauth_secret_values())
            log.warning("Spotify sign-in could not start")
            return {"error": message or "Spotify sign-in could not start"}, 503

    @app.get("/api/auth/spotify/callback")
    def spotify_auth_callback():
        nonlocal pending_authorization, auth_authenticated, spotify_client
        if not _loopback_request() and not app.config.get("PUBLIC_MODE"):
            return {"error": "Spotify sign-in callback must return to the host computer"}, 403
        returned_state = str(request.args.get("state", "") or "")
        code = str(request.args.get("code", "") or "")
        provider_error = str(request.args.get("error", "") or "")
        with auth_lock:
            pending = dict(pending_authorization or {}) if pending_authorization else None
        if provider_error:
            with auth_lock:
                pending_authorization = None
            return {"error": "Spotify sign-in was not completed."}, 400
        if pending is None or not returned_state:
            return {"error": "OAuth state is missing; start sign-in again."}, 400
        if time.time() - float(pending.get("created_at", 0)) > 600:
            with auth_lock:
                pending_authorization = None
            return {"error": "OAuth sign-in expired; start again."}, 400
        expected_state = str(pending.get("state", "") or "")
        if not expected_state or not hmac.compare_digest(expected_state, returned_state):
            # Never exchange a code until this process-local CSRF value matches.
            return {"error": "OAuth state validation failed; start sign-in again."}, 400
        if not code:
            return {"error": "Spotify authorization code is missing."}, 400
        oauth = pending.get("oauth")
        try:
            token = _exchange_oauth(oauth, code, returned_state, pending.get("request"))
            _persist_refresh_token(token, oauth)
            # Profile lookup is best effort.  It is made with the authenticated
            # client and only allowlisted account fields enter SQLite.
            try:
                client = _get_spotify_client()
                profile = _profile_from_client(client, token)
            except Exception:
                profile = {
                    "display_name": "Spotify user",
                    "scopes": list(getattr(token, "scope", ()) or ()),
                    "metadata": {"source": "spotify", "signed_in_at": time.time()},
                }
            db = Database(config.database_path)
            try:
                account = db.upsert_spotify_account(profile)
            finally:
                db.close()
            with auth_lock:
                pending_authorization = None
                auth_authenticated = True
            # The cached client is now backed by the exchanged OAuth object.  A
            # factory that returns a per-login client can opt into a fresh one.
            if spotify_factory is not None and spotify_client is None:
                spotify_client = None
            if request.args.get("format") == "json":
                return jsonify({"ok": True, "authenticated": True, "account": _spotify_account_payload(account)})
            landing = "/"
            if app.config.get("PUBLIC_MODE"):
                landing = f"/?access_token={app.config.get('LAN_ACCESS_TOKEN', '')}"
            return redirect(landing, code=303)
        except Exception as exc:  # noqa: BLE001 - do not echo OAuth material
            message = _safe_message(exc, _oauth_secret_values(oauth))
            log.warning("Spotify sign-in failed")
            with auth_lock:
                pending_authorization = None
            return {"error": message or "Spotify sign-in failed"}, 400

    @app.post("/api/auth/spotify/logout")
    @app.get("/api/auth/spotify/logout")
    def spotify_auth_logout():
        nonlocal auth_authenticated, auth_client, spotify_client, pending_authorization
        if not _loopback_request():
            return {"error": "Spotify sign-out must be performed on the host computer"}, 403
        oauth = auth_client
        try:
            if oauth is not None:
                method = getattr(oauth, "clear_token", None) or getattr(oauth, "logout", None)
                if callable(method):
                    method()
            elif secure_store is not None:
                service = str(getattr(config, "keyring_service", "") or "")
                username = str(getattr(config, "keyring_user", "") or "")
                method = getattr(secure_store, "delete_password", None) or getattr(secure_store, "delete", None)
                if callable(method) and service and username:
                    method(service, username)
        except Exception as exc:  # noqa: BLE001 - logout remains local and safe
            log.warning("Spotify sign-out credential cleanup failed")
        db = Database(config.database_path)
        try:
            db.clear_spotify_account()
        finally:
            db.close()
        with auth_lock:
            auth_authenticated = False
            pending_authorization = None
            spotify_client = None
        return jsonify({"ok": True, "authenticated": False, "signed_in": False})

    # -------------------------------------------------------- Spotify discovery
    @app.post("/api/spotify/scan")
    @app.post("/api/discovery/start")
    def spotify_scan_start():
        return _start_spotify_job("exact", request.get_json(silent=True) or {})

    @app.get("/api/spotify/status")
    @app.get("/api/spotify/scan/status")
    @app.get("/api/discovery/status")
    @app.get("/api/discovery/progress")
    def spotify_scan_status():
        payload = request.args.get("workspace_id")
        return jsonify(_workspace_status(payload))

    @app.post("/api/spotify/workspace/save")
    def spotify_workspace_save():
        if not _safe_local_request():
            return {"error": "Spotify workspace is local-only"}, 403
        payload = request.get_json(silent=True) or {}
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
            return jsonify(_save_workspace_snapshot(workspace))
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.get("/api/spotify/workspaces")
    def spotify_workspaces():
        db = Database(config.database_path)
        try:
            items = []
            for row in db.list_workspaces():
                metadata = _json_object(row["metadata_json"], {})
                workspace_id = int(row["id"])
                counts = db._conn.execute(
                    """SELECT COUNT(*) AS candidates, COUNT(DISTINCT track_id) AS matched_tracks
                       FROM workspace_candidates WHERE workspace_id = ?""",
                    (workspace_id,),
                ).fetchone()
                items.append({
                    "id": workspace_id,
                    "key": row["workspace_key"],
                    "name": row["name"] or f"Workspace {workspace_id}",
                    "status": row["status"],
                    "phase": row["phase"],
                    "updated_at": row["updated_at"],
                    "completed_at": row["completed_at"],
                    "saved_at": metadata.get("saved_at"),
                    "saved_explicitly": bool(metadata.get("saved_explicitly")),
                    "total_tracks": int(row["total_tracks"] or 0),
                    "completed_tracks": int(row["completed_tracks"] or 0),
                    "candidates": int(counts["candidates"] or 0),
                    "matched_tracks": int(counts["matched_tracks"] or 0),
                    "selections": len(db.list_workspace_selections(workspace_id)),
                })
            return jsonify({"workspaces": items})
        finally:
            db.close()

    def _get_or_create_spotify_workspace() -> int:
        workspace = _workspace_id(required=False)
        if workspace is not None:
            return int(workspace)
        db = Database(config.database_path)
        try:
            return int(db.create_workspace(
                workspace_key="spotify", name="Spotify library", kind="exact",
                status="created", phase="idle", metadata={"workflow": "spotify_staged"},
            ))
        finally:
            db.close()

    @app.post("/api/spotify/inventory")
    def spotify_inventory_sync():
        if not _safe_local_request():
            return {"error": "Spotify workspace is local-only"}, 403
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        busy = _busy_response()
        if busy is not None:
            return busy
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a Spotify job is already running"}, 409
            workspace = _get_or_create_spotify_workspace()
            spotify_job.begin(workspace)
        status_db = Database(config.database_path)
        try:
            status_db.update_workspace_progress(
                workspace,
                status="running",
                phase="starting",
                message="Starting Spotify inventory sync...",
                error="",
            )
        finally:
            status_db.close()

        def inventory_worker() -> None:
            db = Database(config.database_path)
            try:
                spotify_job.progress_update({"phase": "spotify_inventory", "message": "Syncing Spotify playlists"})
                client = _get_spotify_client()
                method = getattr(client, "list_playlists", None) or getattr(client, "get_playlists", None)
                playlists = list(method() if callable(method) else [])
                with db._conn:
                    db.upsert_spotify_playlists(playlists)
                    db.update_workspace(workspace, status="ready_for_scope", phase="ready_for_scope", metadata={
                        "workflow": "spotify_staged", "playlist_count": len(playlists),
                    })
                spotify_job.finish(type("InventoryResult", (), {
                    "status": "ready_for_scope", "phase": "ready_for_scope", "error": "", "partial": False,
                })())
            except Exception as exc:
                message = _safe_message(exc, _oauth_secret_values())
                try:
                    db.update_workspace(workspace, status="failed", phase="spotify_inventory", metadata={"error": message})
                except Exception:
                    pass
                spotify_job.fail(message)
            finally:
                db.close()

        threading.Thread(target=inventory_worker, daemon=True).start()
        return jsonify({"started": True, "workspace_id": workspace, "phase": "spotify_inventory"}), 202

    @app.route("/api/spotify/scope", methods=["GET", "POST"])
    def spotify_scope():
        if request.method == "POST" and not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        if request.method == "POST":
            busy = _busy_response()
            if busy is not None:
                return busy
        try:
            payload = request.get_json(silent=True) or {} if request.method == "POST" else {}
            requested_workspace = payload.get("workspace_id") or request.args.get("workspace_id")
            workspace = (
                _workspace_id(requested_workspace)
                if requested_workspace not in (None, "")
                else _get_or_create_spotify_workspace()
            )
            db = Database(config.database_path)
            try:
                if request.method == "POST":
                    raw_ids = payload.get("playlist_ids", payload.get("playlists", []))
                    if isinstance(raw_ids, str):
                        raw_ids = [raw_ids]
                    playlist_ids = sorted({str(value).strip() for value in (raw_ids or []) if str(value).strip()})
                    available = {str(row["spotify_id"]) for row in db.get_spotify_playlists()
                                 if str(row["access_status"] or "accessible") == "accessible"}
                    missing = [value for value in playlist_ids if value not in available]
                    if missing:
                        return {"error": "unknown or inaccessible playlist", "playlist_ids": missing}, 400
                    scope_row = db.set_workspace_scope(workspace, playlist_ids, bool(payload.get("include_liked", False)))
                scope = db.get_workspace_scope_data(workspace) or {"playlist_ids": [], "include_liked": False, "fingerprint": ""}
                playlists = []
                account = db.get_spotify_account()
                account_id = str((account["spotify_user_id"] if account else "") or (account["user_id"] if account else "") or "").casefold()
                for row in db.get_spotify_playlists():
                    item = _row_dict(row)
                    owner_id = str(item.get("owner_id") or "")
                    owner_name = str(item.get("owner_name") or "")
                    owner_folded = owner_id.casefold()
                    owner_name_folded = owner_name.casefold()
                    user_owned = bool(item.get("is_owned")) or bool(account_id and owner_folded == account_id)
                    spotify_owned = owner_folded == "spotify" or owner_name_folded == "spotify"
                    item["created_by"] = owner_name or owner_id or "Unknown Spotify user"
                    item["creator_id"] = owner_id
                    item["is_user_owned"] = user_owned
                    item["owner_priority"] = 0 if user_owned else 2 if spotify_owned else 1
                    item["selected"] = str(item.get("spotify_id") or "") in set(scope.get("playlist_ids", []))
                    playlists.append(item)
                playlists.sort(key=lambda item: (int(item.get("owner_priority", 1)), str(item.get("name") or "").casefold()))
                liked_count = len(db.get_spotify_liked_tracks())
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "scope": scope, "playlists": playlists,
                            "liked_count": liked_count, "accessible_count": sum(p.get("access_status") == "accessible" for p in playlists),
                            "inaccessible_count": sum(p.get("access_status") != "accessible" for p in playlists)})
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            return {"error": _safe_message(exc)}, 400

    def _linked_library_root() -> Path | None:
        """Return the persisted linked Clone Hero/YARG Songs folder, if any."""
        db = Database(config.database_path)
        try:
            raw = str(db.get_meta("spotify_inventory_root", "") or "")
        finally:
            db.close()
        if not raw.strip():
            return None
        try:
            return config.resolve(raw).expanduser().resolve()
        except OSError:
            return None

    def _library_access_report(root: Path | None) -> dict[str, Any]:
        """Linked-library health: presence, writability, free space, last scan."""
        payload: dict[str, Any] = {"linked": root is not None, "path": str(root) if root is not None else ""}
        if root is None:
            return payload
        try:
            exists = root.is_dir()
        except OSError:
            exists = False
        payload["exists"] = exists
        payload["writable"] = bool(exists and os.access(root, os.W_OK))
        payload["free_bytes"] = None
        try:
            payload["free_bytes"] = int(shutil.disk_usage(root if exists else config.base_dir).free)
        except OSError:
            pass
        db = Database(config.database_path)
        try:
            row = db.get_inventory_root(root)
            payload["status"] = _row_dict(row) if row is not None else None
        finally:
            db.close()
        return payload

    def _scan_and_store_library(root: Path) -> tuple[Any, Any]:
        limits_data = getattr(config, "inventory", None)
        limits = InventoryLimits(**{
            name: getattr(limits_data, name) for name in InventoryLimits.__dataclass_fields__
            if limits_data is not None and hasattr(limits_data, name)
        }) if limits_data is not None else InventoryLimits()
        db = Database(config.database_path)
        try:
            previous = {row["relative_path"]: row for row in db.get_inventory_entries(root)}
            result = scan_library(root, limits=limits, previous=previous, hash_files=bool(getattr(limits_data, "hash_files", False)))
            root_row = db.save_inventory_result(result)
            db.set_meta("spotify_inventory_root", str(root))
        finally:
            db.close()
        return result, root_row

    @app.post("/api/spotify/library-import")
    @app.post("/api/spotify/library/link")
    def spotify_library_import():
        if not _safe_local_request():
            return {"error": "library linking requires the private access link"}, 403
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.get_json(silent=True) or {}
        raw_root = str(payload.get("path") or payload.get("root") or "").strip()
        if not raw_root:
            return {"error": "Songs folder path is required"}, 400
        try:
            root = config.resolve(raw_root).expanduser().resolve()
            if is_within(root, config.state_path) or is_within(root, config.input_path):
                return {"error": "Songs folder cannot contain app state or input files"}, 400
            created = False
            if not root.is_dir():
                # Clone Hero creates its Songs folder on first launch; mirror
                # that so linking works on a fresh machine.
                try:
                    root.mkdir(parents=True)
                    created = True
                except OSError as exc:
                    return {"error": f"Songs folder does not exist and could not be created: {_safe_message(exc)}"}, 400
        except OSError as exc:
            return {"error": _safe_message(exc)}, 400
        result, root_row = _scan_and_store_library(root)
        report = _library_access_report(root)
        return jsonify({"ok": True, "root": str(root), "created": created,
                        "inventory_root": _row_dict(root_row),
                        "scanned": result.scanned, "valid": len(result.valid_entries),
                        "invalid": len(result.invalid_entries), "diagnostics": list(result.diagnostics),
                        "scanned_at": root_row["scanned_at"],
                        "writable": report["writable"], "free_bytes": report["free_bytes"]}), 200

    @app.get("/api/spotify/inventory/status")
    @app.get("/api/spotify/library/link")
    def spotify_library_import_status():
        if not _safe_local_request():
            return {"error": "library linking requires the private access link"}, 403
        raw_root = request.args.get("path") or request.args.get("root") or ""
        db = Database(config.database_path)
        try:
            if not raw_root:
                raw_root = str(db.get_meta("spotify_inventory_root", "") or "")
        finally:
            db.close()
        resolved = None
        if raw_root:
            try:
                resolved = config.resolve(raw_root).expanduser().resolve()
            except OSError:
                resolved = None
        report = _library_access_report(resolved)
        return jsonify({"path": raw_root, "status": report.get("status"), **report})

    @app.post("/api/spotify/library/verify")
    def spotify_library_verify():
        """Re-check the linked library's accessibility and rescan its contents."""
        if not _safe_local_request():
            return {"error": "library linking requires the private access link"}, 403
        busy = _busy_response()
        if busy is not None:
            return busy
        root = _linked_library_root()
        if root is None:
            return {"error": "no linked library yet. Link a Songs folder first."}, 400
        if not root.is_dir():
            report = _library_access_report(root)
            return jsonify({"ok": False, "error": "linked folder is missing or unreachable", **report}), 400
        result, root_row = _scan_and_store_library(root)
        report = _library_access_report(root)
        return jsonify({"ok": True, "root": str(root), "scanned": result.scanned,
                        "valid": len(result.valid_entries), "invalid": len(result.invalid_entries),
                        "scanned_at": root_row["scanned_at"], **report})

    def _inventory_issue_items(rows: list[Any]) -> dict[str, list[dict[str, Any]]]:
        broken: list[dict[str, Any]] = []
        suspicious: list[dict[str, Any]] = []
        groups: dict[tuple[str, str], list[Any]] = {}
        for row in rows:
            item = {
                "relative_path": row["relative_path"],
                "artist": row["artist"],
                "title": row["title"],
                "size_bytes": int(row["size_bytes"] or 0),
            }
            if row["validation_state"] != "valid":
                reasons = _json_object(row["diagnostics_json"], [])
                item["reasons"] = [str(reason) for reason in reasons] or ["archive failed validation"]
                broken.append(item)
                continue
            reasons = []
            if not row["artist_norm"] or not row["title_norm"]:
                reasons.append("missing artist/title metadata")
            if int(row["size_bytes"] or 0) < 100_000:
                reasons.append("unusually small file")
            if reasons:
                item["reasons"] = reasons
                suspicious.append(item)
            else:
                groups.setdefault((row["artist_norm"], row["title_norm"]), []).append(item)
        duplicates = [
            {"artist": items[0]["artist"], "title": items[0]["title"],
             "entries": [item["relative_path"] for item in items]}
            for items in groups.values() if len(items) > 1
        ]
        return {"broken": broken, "suspicious": suspicious, "duplicates": duplicates}

    @app.get("/api/spotify/library/issues")
    def spotify_library_issues():
        if not _safe_local_request():
            return {"error": "library linking requires the private access link"}, 403
        root = _linked_library_root()
        if root is None:
            return jsonify({"root": "", "broken": [], "suspicious": [], "duplicates": []})
        db = Database(config.database_path)
        try:
            rows = db.get_inventory_entries(root)
        finally:
            db.close()
        return jsonify({"root": str(root), **_inventory_issue_items(rows)})

    @app.post("/api/spotify/library/clean")
    def spotify_library_clean():
        """Quarantine broken/suspicious/duplicate charts out of the linked library.

        Files are moved to a quarantine folder beside the library, never deleted.
        """
        if not _safe_local_request():
            return {"error": "library linking requires the private access link"}, 403
        busy = _busy_response()
        if busy is not None:
            return busy
        root = _linked_library_root()
        if root is None:
            return {"error": "no linked library yet. Link a Songs folder first."}, 400
        if not root.is_dir():
            return {"error": "linked folder is missing or unreachable"}, 400
        payload = request.get_json(silent=True) or {}
        scope = str(payload.get("scope") or "broken").casefold()
        db = Database(config.database_path)
        try:
            rows = db.get_inventory_entries(root)
            issues = _inventory_issue_items(rows)
            targets: set[str] = set()
            if scope in {"broken", "all"}:
                targets.update(item["relative_path"] for item in issues["broken"])
            if scope in {"suspicious", "all"}:
                targets.update(item["relative_path"] for item in issues["suspicious"])
            if scope in {"duplicates", "all"}:
                for group in issues["duplicates"]:
                    # Keep the alphabetically-first copy; quarantine the rest.
                    targets.update(sorted(group["entries"])[1:])
            explicit = payload.get("paths")
            if isinstance(explicit, list) and explicit:
                targets = {str(item) for item in explicit if str(item)}
            if not targets:
                return jsonify({"ok": True, "moved": 0, "quarantine": "", "items": []})
            quarantine_root = root.parent / f"{root.name} - quarantine"
            moved: list[dict[str, Any]] = []
            moved_rels: list[str] = []
            for relative in sorted(targets):
                source = (root / relative.replace("\\", "/")).resolve()
                try:
                    if not is_within(source, root) or not source.is_file():
                        continue
                    target = quarantine_root / relative.replace("\\", "/")
                    if target.exists():
                        target = target.with_name(f"{target.stem} ({secrets.token_hex(3)}){target.suffix}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                except OSError as exc:
                    log.warning("Could not quarantine %s: %s", source, exc)
                    continue
                moved.append({"relative_path": relative, "quarantined_to": str(target)})
                moved_rels.append(relative)
            if moved_rels:
                db.remove_inventory_entries(root, moved_rels)
        finally:
            db.close()
        return jsonify({"ok": True, "moved": len(moved), "quarantine": str(quarantine_root), "items": moved})

    @app.get("/api/spotify/path/browse")
    def spotify_path_browse():
        """Server-side folder browser so remote users can pick a host path.

        With no path it lists drives (Windows) or ``/``; otherwise it lists the
        folder's subfolders. Symlinks and hidden entries are skipped.
        """
        if not _safe_local_request():
            return {"error": "browsing requires the private access link"}, 403
        raw = str(request.args.get("path") or "").strip()
        if not raw:
            folders: list[dict[str, str]] = []
            if os.name == "nt":
                for letter in string.ascii_uppercase:
                    drive = Path(f"{letter}:\\")
                    try:
                        if drive.exists():
                            folders.append({"name": f"{letter}:\\", "path": str(drive)})
                    except OSError:
                        continue
            else:
                folders.append({"name": "/", "path": "/"})
            return jsonify({"path": "", "parent": None, "folders": folders})
        try:
            directory = config.resolve(raw).expanduser().resolve()
        except OSError as exc:
            return {"error": _safe_message(exc)}, 400
        if not directory.is_dir():
            return {"error": "that path is not a folder"}, 400
        entries: list[dict[str, str]] = []
        try:
            with os.scandir(directory) as scan:
                for child in sorted(scan, key=lambda item: item.name.casefold()):
                    try:
                        if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                            continue
                        info = child.stat(follow_symlinks=False)
                        if os.name == "nt" and getattr(info, "st_file_attributes", 0) & 0x2:
                            continue
                    except OSError:
                        continue
                    entries.append({"name": child.name, "path": str(Path(child.path))})
                    if len(entries) >= 500:
                        break
        except OSError as exc:
            return {"error": f"cannot read folder: {_safe_message(exc)}"}, 400
        parent = str(directory.parent) if directory.parent != directory else None
        return jsonify({"path": str(directory), "parent": parent, "folders": entries})

    @app.get("/api/spotify/estimate")
    def spotify_estimate():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            db = Database(config.database_path)
            try:
                tracks = db.get_library_tracks(workspace_id=workspace)
                estimate_sources = list(source_factory(config))
                try:
                    estimate = build_spotify_estimate(config, db, tracks, estimate_sources, workspace_id=workspace)
                finally:
                    for source in estimate_sources:
                        try:
                            source.close()
                        except Exception:
                            pass
                scope = db.get_workspace_scope_data(workspace)
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "scope": scope, "estimate": estimate, **estimate})
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.post("/api/spotify/scan/cancel")
    def spotify_scan_cancel():
        with spotify_job.lock:
            event = spotify_cancel_event
            running = spotify_job.running
        if not running or event is None:
            return {"error": "no Spotify discovery job is running"}, 409
        event.set()
        return jsonify({"ok": True, "status": "cancelling"})

    @app.post("/api/spotify/expansion/start")
    @app.post("/api/discovery/expansion/start")
    @app.post("/api/spotify/expansion/scan")
    @app.post("/api/discovery/expansion")
    def spotify_expansion_start():
        return _start_spotify_job("expansion", request.get_json(silent=True) or {})

    # ------------------------------------------------------------ Spotify data
    @app.get("/api/spotify/library")
    @app.get("/api/library")
    @app.get("/api/spotify/workspace")
    @app.get("/api/discovery/workspace")
    def spotify_library():
        payload = request.args
        try:
            limit = min(100, max(1, int(payload.get("limit", 50)))) if "limit" in payload else None
            result = _workspace_payload(
                payload.get("workspace_id"),
                payload.get("folder_id") or payload.get("playlist_id"),
                payload.get("liked") in {"1", "true", "yes"},
                view=payload.get("view", "eligible"),
                search_text=payload.get("search", ""),
                offset=max(0, int(payload.get("offset", 0))),
                limit=limit,
            )
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 404
        result["library"] = {"folders": result["folders"], "tracks": result["tracks"]}
        return jsonify(result)

    @app.get("/api/spotify/library/folders")
    @app.get("/api/spotify/folders")
    @app.get("/api/library/folders")
    def spotify_library_folders():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            db = Database(config.database_path)
            try:
                folders = [_folder_payload(item) for item in db.get_library_folders(workspace_id=workspace)]
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "folders": folders, "items": folders})
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.get("/api/spotify/library/tracks")
    @app.get("/api/spotify/tracks")
    @app.get("/api/library/tracks")
    def spotify_library_tracks():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            folder_id = request.args.get("folder_id") or request.args.get("playlist_id")
            liked = request.args.get("liked") in {"1", "true", "yes"}
            limit = min(100, max(1, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
            db = Database(config.database_path)
            try:
                rows = db.get_library_track_rows(folder_id, liked=liked, workspace_id=workspace)
                page_rows, track_total = _paged_track_rows(
                    db,
                    workspace,
                    rows,
                    view=request.args.get("view", "eligible"),
                    search_text=request.args.get("search", ""),
                    offset=offset,
                    limit=limit,
                )
                tracks = _track_page_payloads(db, workspace, page_rows)
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "tracks": tracks, "items": tracks,
                            "track_total": track_total, "track_offset": offset,
                            "track_limit": limit, "has_more": offset + len(tracks) < track_total})
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.get("/api/spotify/folders/<folder_id>/tracks")
    def spotify_folder_tracks(folder_id: str):
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            liked = folder_id.casefold() in {"liked", "liked_songs", "liked songs"}
            db = Database(config.database_path)
            try:
                rows = db.get_library_track_rows(folder_id, liked=liked, workspace_id=workspace)
                tracks = _track_page_payloads(db, workspace, rows)
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "folder_id": folder_id, "tracks": tracks})
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    def _candidate_groups_payload(
        workspace: int | str,
        kind: str,
        track_value: Any = None,
        folder_value: Any = None,
        liked: bool = False,
    ) -> dict[str, Any]:
        db = Database(config.database_path)
        try:
            if folder_value not in (None, "") or liked:
                track_rows = db.get_library_track_rows(
                    folder_value,
                    liked=liked,
                    workspace_id=workspace,
                )
                rows = [
                    candidate
                    for track_row in track_rows
                    for candidate in db.get_workspace_candidates(
                        workspace,
                        track_id=track_row["id"],
                        kind=kind,
                        include_rejected=True,
                    )
                ]
            else:
                rows = db.get_workspace_candidates(
                    workspace,
                    track_id=track_value if track_value not in (None, "") else None,
                    kind=kind,
                    include_rejected=True,
                )
            groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                data = _row_dict(row)
                key = (
                    str(data.get("artist_norm") or "")
                    if kind == "expansion"
                    else str(data.get("spotify_id") or data.get("track_key") or data.get("track_id"))
                )
                group = groups.setdefault(
                    key,
                    {
                        "id": key,
                        "kind": kind,
                        "artist_norm": data.get("artist_norm", "") if kind == "expansion" else "",
                        "track": _track_payload(db.get_spotify_track(str(data.get("spotify_id") or "")))
                        if kind == "exact"
                        else None,
                        "candidates": [],
                    },
                )
                group["candidates"].append(_candidate_payload(row))
            group_list = list(groups.values())
            return {
                "workspace_id": workspace,
                "kind": kind,
                "folder_id": folder_value,
                "groups": group_list,
                "candidate_groups": group_list,
                "tracks": group_list if kind == "exact" else [],
                "artists": group_list if kind == "expansion" else [],
            }
        finally:
            db.close()

    @app.get("/api/spotify/candidates")
    @app.get("/api/discovery/candidates")
    def spotify_candidates():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            kind = str(request.args.get("kind", "exact") or "exact").casefold()
            if kind not in {"exact", "expansion"}:
                return {"error": "kind must be exact or expansion"}, 400
            return jsonify(
                _candidate_groups_payload(
                    workspace,
                    kind,
                    request.args.get("track_id"),
                    request.args.get("folder_id") or request.args.get("playlist_id"),
                    request.args.get("liked") in {"1", "true", "yes"},
                )
            )
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.get("/api/spotify/candidates/track/<track_id>")
    @app.get("/api/spotify/tracks/<track_id>/candidates")
    def spotify_track_candidates(track_id: str):
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            return jsonify(_candidate_groups_payload(workspace, "exact", track_id))
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.get("/api/spotify/candidates/folder/<folder_id>")
    @app.get("/api/spotify/folders/<folder_id>/candidates")
    def spotify_folder_candidates(folder_id: str):
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            liked = folder_id.casefold() in {"liked", "liked songs", "liked_songs"}
            return jsonify(
                _candidate_groups_payload(workspace, "exact", folder_value=folder_id, liked=liked)
            )
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    # ---------------------------------------------------------- expansion UI
    def _expansion_artist_payload(workspace: int | str) -> list[dict[str, Any]]:
        db = Database(config.database_path)
        try:
            tracks = db.get_spotify_tracks(True)
            existing = {
                str(row["artist_norm"]): _row_dict(row)
                for row in db.get_workspace_expansion_artists(workspace)
            }
            counts: dict[str, dict[str, Any]] = {}
            for track in tracks:
                norm = str(track.primary_artist_norm or normalize_artist(track.primary_artist))
                entry = counts.setdefault(
                    norm,
                    {"artist_name": track.primary_artist, "artist_norm": norm, "track_count": 0},
                )
                entry["track_count"] += 1
            entries: list[dict[str, Any]] = []
            expansion = config.expansion
            try:
                overrides = __import__(
                    "csv_to_clonehero.overrides", fromlist=["load_overrides"]
                ).load_overrides(config.base_dir)
            except Exception:
                overrides = None
            for norm, entry in sorted(counts.items(), key=lambda item: item[1]["artist_name"].casefold()):
                override = overrides.artist_override(norm) if overrides is not None else None
                if override is not None and override.expansion_disabled:
                    cap = 0
                elif override is not None and override.expansion_cap is not None:
                    cap = int(override.expansion_cap)
                elif entry["track_count"] >= expansion.min_songs_many:
                    cap = expansion.cap_many_songs
                elif entry["track_count"] >= 2:
                    cap = expansion.cap_some_songs
                else:
                    cap = expansion.cap_one_song
                old = existing.get(norm, {})
                entries.append(
                    {
                        **entry,
                        "cap": max(0, int(cap)),
                        "selected": bool(old.get("selected")),
                        "eligible": max(0, int(cap)) > 0,
                    }
                )
            # Keep the database's explicit artist list in sync with the current
            # Spotify snapshot; only primary artists are included here.
            db.replace_workspace_expansion_artists(workspace, entries)
            return entries
        finally:
            db.close()

    @app.get("/api/spotify/expansion/artists")
    @app.get("/api/discovery/expansion/artists")
    def spotify_expansion_artists():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            artists = _expansion_artist_payload(workspace)
            return jsonify({"workspace_id": workspace, "artists": artists})
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.post("/api/spotify/expansion/artists/select")
    @app.post("/api/discovery/expansion/artists/select")
    def spotify_expansion_artist_select():
        try:
            payload = request.get_json(silent=True) or {}
            workspace = _workspace_id(payload.get("workspace_id"))
            available = {item["artist_norm"]: item for item in _expansion_artist_payload(workspace)}
            raw_values = payload.get("artists")
            if raw_values is None:
                raw_values = [payload]
            if isinstance(raw_values, (str, bytes, Mapping)):
                raw_values = [raw_values]
            updates: list[tuple[str, bool]] = []
            for value in raw_values:
                if isinstance(value, str):
                    norm = value.strip()
                    selected = True
                else:
                    data = dict(value or {})
                    name = str(data.get("artist_norm") or data.get("artist") or data.get("name") or "").strip()
                    norm = name
                    selected = bool(data.get("selected", data.get("enabled", True)))
                if norm not in available:
                    return {"error": "artist is not a primary artist in the Spotify library"}, 400
                if available[norm]["cap"] <= 0 and selected:
                    return {"error": "artist is not eligible for expansion"}, 400
                updates.append((norm, selected))
            db = Database(config.database_path)
            try:
                for norm, selected in updates:
                    db.set_expansion_artist_selected(workspace, norm, selected)
                artists = [_row_dict(item) for item in db.get_workspace_expansion_artists(workspace)]
            finally:
                db.close()
            return jsonify({"ok": True, "workspace_id": workspace, "artists": artists})
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 400

    # ------------------------------------------------------------- selections
    def _tray_payload(db: Database, workspace: int | str) -> dict[str, Any]:
        rows = db.list_workspace_selections(workspace)
        items = [_selection_payload(db, row) for row in rows]
        return {
            "workspace_id": workspace,
            "count": len(items),
            "selection_count": len(items),
            "selected_count": len(items),
            "items": items,
            "selections": items,
            "total_estimated_bytes": sum(
                int(_row_dict(row).get("planned_bytes") or 0) for row in rows
            ),
        }

    def _select_recommended_safe(
        db: Database,
        workspace: int | str,
        *,
        kind: str = "exact",
        track_ids: Iterable[Any] | None = None,
        minimum_score: float | None = None,
    ) -> list[int]:
        """Select only rows that pass the same eligibility boundary as export."""
        requested = None
        if track_ids is not None:
            requested = {str(value) for value in track_ids}
        rows = db.get_workspace_candidates(workspace, kind=kind, include_rejected=True)
        grouped: dict[Any, Any] = {}
        blocked = {EXCLUDED, INCOMPATIBLE, "REJECT", "REJECTED", "NOT_EXPORTABLE"}
        for row in rows:
            data = _row_dict(row)
            if requested is not None and not {
                str(data.get("track_id")),
                str(data.get("spotify_id")),
                str(data.get("track_key")),
            } & requested:
                continue
            try:
                score = float(data.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if minimum_score is not None and score < minimum_score:
                continue
            if str(data.get("eligibility") or "").upper() in blocked:
                continue
            valid, _error = _validate_candidate_eligibility(db, workspace, row, kind)
            if not valid:
                continue
            key = (
                data.get("track_id")
                if kind == "exact"
                else (data.get("artist_norm"), data.get("candidate_id"))
            )
            prior = grouped.get(key)
            if prior is None or (
                score,
                float(data.get("quality") or 0),
                -int(data.get("candidate_id") or 0),
            ) > (
                float(prior["score"] or 0),
                float(prior["quality"] or 0),
                -int(prior["candidate_id"] or 0),
            ):
                grouped[key] = row
        selected: list[int] = []
        for row in grouped.values():
            data = _row_dict(row)
            selected.append(
                db.select_workspace_candidate(
                    workspace,
                    data.get("track_id") if kind == "exact" else None,
                    data["candidate_id"],
                    kind=kind,
                    artist_norm=str(data.get("artist_norm") or "") if kind == "expansion" else "",
                    score=data.get("score"),
                    explanation=data.get("breakdown_json") or "",
                    eligibility=data.get("eligibility") or "",
                )
            )
        return selected

    @app.get("/api/spotify/selections")
    @app.get("/api/spotify/selection/tray")
    @app.get("/api/spotify/selection-summary")
    @app.get("/api/discovery/selections")
    def spotify_selection_tray():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            db = Database(config.database_path)
            try:
                return jsonify(_tray_payload(db, workspace))
            finally:
                db.close()
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    @app.post("/api/spotify/select")
    @app.post("/api/spotify/selection")
    @app.post("/api/spotify/selection/select")
    @app.post("/api/spotify/selection/candidate")
    @app.post("/api/spotify/candidates/select")
    @app.delete("/api/spotify/selection/candidate")
    @app.post("/api/discovery/select")
    def spotify_selection_change():
        payload = request.get_json(silent=True) or {}
        action = "clear" if request.method == "DELETE" else str(payload.get("action") or "select").casefold()
        if payload.get("selected") is False or payload.get("clear") is True:
            action = "clear"
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
            db = Database(config.database_path)
            try:
                if action in {"clear", "deselect", "remove"}:
                    kind = str(payload.get("kind") or "exact").casefold()
                    track_value = (
                        payload.get("track_id")
                        or payload.get("spotify_track_id")
                        or payload.get("spotify_id")
                        or payload.get("track_key")
                    )
                    artist_norm = str(payload.get("artist_norm") or "").strip()
                    candidate_id = payload.get("candidate_id")
                    if candidate_id not in (None, ""):
                        try:
                            candidate_id = int(candidate_id)
                        except (TypeError, ValueError):
                            return {"error": "candidate_id must be numeric when clearing"}, 400
                    removed = db.clear_workspace_selection(
                        workspace,
                        track_value if kind == "exact" else None,
                        candidate_id=candidate_id,
                        kind=kind,
                        artist_norm=artist_norm,
                    )
                    return jsonify({"ok": True, "cleared": removed, **_tray_payload(db, workspace)})

                row, kind, track_value, artist_norm_or_error = _candidate_row_for_request(
                    db, workspace, payload
                )
                if row is None:
                    return {"error": artist_norm_or_error}, 404
                force = bool(payload.get("force"))
                valid, error = _validate_candidate_eligibility(db, workspace, row, kind, force=force)
                if not valid:
                    return {"error": error}, 400
                data = _row_dict(row)
                selection_id = db.select_workspace_candidate(
                    workspace,
                    track_value if kind == "exact" else None,
                    data["candidate_id"],
                    kind=kind,
                    artist_norm=artist_norm_or_error if kind == "expansion" else "",
                    score=data.get("score"),
                    explanation=data.get("breakdown_json") or "",
                    eligibility=("ELIGIBLE_REVIEW" if force else (data.get("eligibility") or "")),
                    force=force,
                )
                return jsonify(
                    {"ok": True, "selection_id": selection_id, **_tray_payload(db, workspace)}
                )
            finally:
                db.close()
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.post("/api/spotify/select-recommended")
    @app.post("/api/spotify/selection/recommended")
    @app.post("/api/discovery/select-recommended")
    def spotify_select_recommended():
        payload = request.get_json(silent=True) or {}
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
            kind = str(payload.get("kind") or "exact").casefold()
            if kind not in {"exact", "expansion"}:
                return {"error": "kind must be exact or expansion"}, 400
            track_ids = payload.get("track_ids")
            if isinstance(track_ids, (str, bytes)):
                track_ids = [track_ids]
            minimum = payload.get("minimum_score")
            minimum_score = float(minimum) if minimum not in (None, "") else None
            db = Database(config.database_path)
            try:
                selected = _select_recommended_safe(
                    db,
                    workspace,
                    kind=kind,
                    track_ids=track_ids,
                    minimum_score=minimum_score,
                )
                return jsonify(
                    {"ok": True, "selected": len(selected), "selection_ids": selected, **_tray_payload(db, workspace)}
                )
            finally:
                db.close()
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.post("/api/spotify/select-all")
    @app.post("/api/spotify/selection/all")
    @app.post("/api/spotify/select-all-recommended")
    @app.post("/api/spotify/selections/clear")
    @app.post("/api/discovery/select-all")
    def spotify_select_all():
        payload = request.get_json(silent=True) or {}
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
            kind = str(payload.get("kind") or "exact").casefold()
            clear = bool(payload.get("clear")) or str(payload.get("action", "")).casefold() in {
                "clear", "deselect", "remove"
            }
            folder_id = payload.get("folder_id") or payload.get("playlist_id")
            liked = bool(payload.get("liked")) or str(folder_id or "").casefold() in {
                "liked", "liked songs", "liked_songs"
            }
            db = Database(config.database_path)
            try:
                if clear:
                    if folder_id is not None or liked:
                        cleared = db.clear_workspace_folder_selection(
                            workspace, folder_id, liked=liked
                        )
                    else:
                        cleared = db.clear_workspace_selection(workspace, kind=kind)
                    return jsonify({"ok": True, "cleared": cleared, **_tray_payload(db, workspace)})
                minimum = payload.get("minimum_score")
                minimum_score = float(minimum) if minimum not in (None, "") else None
                if kind == "exact" and (folder_id is not None or liked):
                    track_rows = db.get_library_track_rows(
                        folder_id,
                        liked=liked,
                        workspace_id=workspace,
                    )
                    selected = _select_recommended_safe(
                        db,
                        workspace,
                        kind=kind,
                        track_ids=[row["id"] for row in track_rows],
                        minimum_score=minimum_score,
                    )
                else:
                    selected = _select_recommended_safe(
                        db,
                        workspace,
                        kind=kind,
                        minimum_score=minimum_score,
                    )
                return jsonify(
                    {"ok": True, "selected": len(selected), "selection_ids": selected, **_tray_payload(db, workspace)}
                )
            finally:
                db.close()
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 400

    # --------------------------------------------------------------- paths
    @app.get("/api/paths/common-songs")
    @app.get("/api/paths/songs")
    @app.get("/api/common-songs")
    @app.get("/api/songs-folders")
    def common_songs_folders():
        candidates: list[dict[str, Any]] = []
        try:
            configured = config.songs_folder_candidates()
        except (AttributeError, OSError, TypeError, ValueError):
            configured = []
        for path in configured:
            try:
                resolved = path.expanduser().resolve()
                exists = resolved.is_dir()
            except OSError:
                continue
            candidates.append(
                {
                    "path": str(resolved),
                    "name": resolved.name or str(resolved),
                    "exists": exists,
                    "is_songs_folder": resolved.name.casefold() == "songs",
                }
            )
        existing = [item for item in candidates if item["exists"]]
        return jsonify({"folders": existing, "candidates": candidates})

    @app.post("/api/paths/validate")
    @app.post("/api/spotify/path/check")
    @app.post("/api/path/check")
    def spotify_path_validate():
        raw = str((request.get_json(silent=True) or {}).get("path", ""))
        ok, error, resolved = _validate_spotify_folder(raw)
        return jsonify({"ok": ok, "error": error, "resolved": resolved})

    @app.post("/api/paths/pick")
    @app.post("/api/folder-picker")
    @app.post("/api/output-path/pick")
    @app.post("/api/spotify/path/pick")
    def native_folder_picker():
        if not _loopback_request():
            return {"error": "folder picking runs on the server. Use Download ZIP to save on this device."}, 403
        if os.name != "nt":
            return {"error": "native folder picking is only supported on Windows"}, 501
        selected = ""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = str(
                filedialog.askdirectory(
                    parent=root,
                    title="Choose a Clone Hero / YARG Songs folder",
                    mustexist=False,
                )
                or ""
            )
            root.destroy()
        except Exception as exc:  # noqa: BLE001 - native UI is optional
            return {"error": _safe_message(exc)}, 500
        if not selected:
            return jsonify({"selected": False, "path": ""})
        ok, error, resolved = _validate_spotify_folder(selected)
        if not ok:
            return {"error": error}, 400
        return jsonify({"selected": True, "path": resolved, "resolved": resolved})

    # --------------------------------------------------------------- exporting
    def _validated_export_selections(db: Database, workspace: int | str) -> list[dict[str, Any]]:
        rows = db.list_workspace_selections(workspace)
        if not rows:
            raise ValueError("select at least one eligible chart before exporting")
        selections: list[dict[str, Any]] = []
        for row in rows:
            data = _row_dict(row)
            kind = str(data.get("kind") or "exact").casefold()
            candidate_rows = db.get_workspace_candidates(
                workspace,
                track_id=data.get("track_id") or data.get("spotify_id") if kind == "exact" else None,
                kind=kind,
                include_rejected=True,
            )
            matching_row = next(
                (
                    candidate
                    for candidate in candidate_rows
                    if int(_row_dict(candidate).get("candidate_id") or 0)
                    == int(data.get("candidate_id") or 0)
                    and (
                        kind != "expansion"
                        or str(_row_dict(candidate).get("artist_norm") or "")
                        == str(data.get("artist_norm") or "")
                    )
                ),
                None,
            )
            if matching_row is None:
                raise ValueError("a persisted selection is no longer part of this discovery workspace")
            valid, error = _validate_candidate_eligibility(db, workspace, matching_row, kind)
            if not valid:
                raise ValueError(error)
            candidate = _candidate_from_payload(_json_object(data.get("candidate_json"), {}))
            if candidate is None:
                candidate = db.get_candidate(int(data.get("candidate_id") or 0))
            if candidate is None:
                raise ValueError("a persisted selection has no chart candidate")
            selection = dict(data)
            selection["candidate_json"] = json.dumps(
                {item.name: getattr(candidate, item.name) for item in fields(candidate)},
                ensure_ascii=True,
                default=str,
            )
            selection["candidate_id"] = data.get("candidate_id")
            selection["status"] = "selected"
            selections.append(selection)
        return selections

    def _export_result_payload(result: Any) -> dict[str, Any]:
        def item_payload(item: Any) -> dict[str, Any]:
            return {
                "selection_index": getattr(item, "selection_index", None),
                "selection_id": getattr(item, "selection_id", None),
                "source": str(getattr(item, "source", "") or ""),
                "chart_id": getattr(item, "chart_id", None),
                "target_relpath": str(getattr(item, "target_relpath", "") or ""),
                "path": str(getattr(item, "target_path", "") or "")
                if getattr(item, "target_path", None) is not None
                else "",
                "status": str(getattr(item, "status", "") or ""),
                "result": str(getattr(item, "result", "") or ""),
                "bytes": int(getattr(item, "bytes_count", 0) or 0),
                "sha256": str(getattr(item, "sha256", "") or ""),
                "source_hash": str(getattr(item, "source_hash", "") or ""),
                "source_hash_algorithm": str(getattr(item, "source_hash_algorithm", "") or ""),
                "artifact_key": str(getattr(item, "artifact_key", "") or ""),
                "error": _safe_message(getattr(item, "failure", "") or ""),
            }

        preflight = getattr(result, "preflight", None)
        manifest = getattr(result, "manifest", None)
        sync = getattr(result, "sync", None)
        payload: dict[str, Any] = {
            "kind": str(getattr(result, "kind", "") or ""),
            "destination": str(getattr(result, "destination", "") or ""),
            "success": bool(getattr(result, "success", False)),
            "status": "success" if bool(getattr(result, "success", False)) else "failed",
            "published": int(getattr(result, "published_count", 0) or 0),
            "failed": int(getattr(result, "failed_count", 0) or 0),
            "bytes": int(
                getattr(result, "output_bytes", 0)
                or getattr(result, "bytes_count", 0)
                or 0
            ),
            "errors": [_safe_message(error) for error in getattr(result, "errors", ())],
            "items": [item_payload(item) for item in getattr(result, "items", ())],
        }
        if preflight is not None:
            payload["preflight"] = {
                "ok": bool(getattr(preflight, "ok", False)),
                "estimated_bytes": int(getattr(preflight, "estimated_bytes", 0) or 0),
                "available_bytes": getattr(preflight, "available_bytes", None),
                "errors": [
                    _safe_message(getattr(issue, "message", issue))
                    for issue in getattr(preflight, "errors", ())
                ],
                "warnings": [
                    _safe_message(getattr(issue, "message", issue))
                    for issue in getattr(preflight, "warnings", ())
                ],
            }
        if manifest is not None:
            try:
                payload["manifest"] = manifest.to_dict()
            except Exception:
                payload["manifest"] = None
        manifest_path = getattr(result, "manifest_path", None)
        payload["manifest_path"] = str(manifest_path) if manifest_path is not None else ""
        if sync is not None:
            payload["sync"] = {
                "stale_paths": list(getattr(sync, "stale_paths", ()) or ()),
                "removed_paths": list(getattr(sync, "removed_paths", ()) or ()),
                "preserved_paths": list(getattr(sync, "preserved_paths", ()) or ()),
                "errors": [_safe_message(error) for error in getattr(sync, "errors", ())],
            }
        return payload

    def _prepare_export(
        kind: str,
        payload: Mapping[str, Any],
        *,
        allow_server_staging: bool = False,
    ) -> tuple[int | str | None, str, tuple[dict[str, Any], int] | None]:
        """Validate workspace + destination + remote gate before any export."""
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
        except (OSError, ValueError) as exc:
            return None, "", ({"error": _safe_message(exc)}, 404)

        if kind == "folder":
            raw_destination = str(
                payload.get("path")
                or payload.get("destination")
                or payload.get("output_dir")
                or config.library_root
            )
            ok, error, destination = _validate_spotify_folder(raw_destination)
        else:
            raw_destination = str(payload.get("path") or payload.get("destination") or "")
            ok, error, destination = _validate_zip_path(raw_destination)
        if not ok:
            return None, "", ({"error": error}, 400)
        if not _loopback_request() and not allow_server_staging:
            # Remote users may only write into the explicitly linked library.
            linked = _linked_library_root()
            if not (kind == "folder" and linked is not None and Path(destination) == linked):
                return None, "", ({"error": "remote exports can only write to the linked Clone Hero library. Link it first, or use Download ZIP."}, 403)
        return workspace, destination, None

    def _run_export(
        kind: str,
        payload: Mapping[str, Any],
        *,
        allow_server_staging: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[dict[str, Any], int]:
        busy = _busy_response()
        if busy is not None:
            return busy
        workspace, destination, error = _prepare_export(
            kind, payload, allow_server_staging=allow_server_staging
        )
        if error is not None:
            return error
        return _perform_export(kind, payload, workspace, destination, progress=progress)

    def _perform_export(
        kind: str,
        payload: Mapping[str, Any],
        workspace: int | str,
        destination: str,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[dict[str, Any], int]:

        db = Database(config.database_path)
        sources: list[ChartSource] = []
        service: ExportService | None = None
        export_id: int | None = None
        export_key = f"web-{secrets.token_hex(12)}"
        try:
            selections = _validated_export_selections(db, workspace)
            sources = list(source_factory(config))
            source_map = {
                str(getattr(source, "name", "") or ""): source for source in sources
            }
            tracks = db.get_spotify_tracks(True)
            track_map: dict[Any, Track] = {}
            for track in tracks:
                if track.db_id is not None:
                    track_map[track.db_id] = track
                if track.spotify_id:
                    track_map[track.spotify_id] = track
                if track.track_key:
                    track_map[track.track_key] = track
            options = ExportOptions(
                managed_sync=bool(
                    payload.get("managed_sync", getattr(getattr(config, "export", None), "managed_sync", False))
                ),
                overwrite_owned=bool(
                    payload.get("overwrite_owned", getattr(getattr(config, "export", None), "overwrite_owned", True))
                ),
                include_manifest_in_zip=bool(
                    payload.get(
                        "include_manifest",
                        getattr(getattr(config, "export", None), "include_manifest_in_zip", True),
                    )
                ),
                minimum_free_bytes=int(
                    payload.get(
                        "minimum_free_bytes",
                        getattr(getattr(config, "export", None), "minimum_free_bytes", 0),
                    )
                    or 0
                ),
                continue_on_error=bool(
                    payload.get(
                        "continue_on_error",
                        getattr(getattr(config, "export", None), "continue_on_error", True),
                    )
                ),
                artifact_retries=int(
                    payload.get(
                        "artifact_retries",
                        getattr(getattr(config, "export", None), "artifact_retries", 2),
                    )
                    or 0
                ),
                download_workers=int(
                    payload.get(
                        "download_workers",
                        getattr(getattr(config, "export", None), "download_workers", 3),
                    )
                    or 1
                ),
            )
            provider = app.config.get("SPOTIFY_ARTIFACT_PROVIDER")
            service = ExportService(
                config,
                source_map,
                artifact_provider=provider,
                artifact_cache_dir=getattr(config, "artifact_cache_path", None),
                track_resolver=track_map,
            )
            export_id = db.create_export_record(
                workspace,
                export_key=export_key,
                kind=kind,
                destination=destination,
                status="running",
                managed=options.managed_sync,
                metadata={"workflow": "spotify_workspace"},
            )

            def eligibility_checker(selection: Any, _candidate: ChartCandidate) -> bool:
                value = _row_dict(selection)
                return str(value.get("eligibility") or "").upper() in {
                    RECOMMENDED,
                    ELIGIBLE_REVIEW,
                }

            if kind == "folder":
                result = service.export_folder(
                    selections,
                    destination,
                    managed_sync=options.managed_sync,
                    options=options,
                    eligibility_checker=eligibility_checker,
                    progress=progress,
                )
            else:
                result = service.export_zip(
                    selections,
                    destination,
                    options=options,
                    eligibility_checker=eligibility_checker,
                    progress=progress,
                )
            result_payload = _export_result_payload(result)
            result_payload["export_id"] = export_id
            db.record_export_result(export_id, result_payload)
            if result_payload.get("manifest_path") or result_payload.get("manifest"):
                db.update_export_record(
                    export_id,
                    manifest_path=result_payload.get("manifest_path", ""),
                    manifest_json=result_payload.get("manifest") or {},
                )
            try:
                generate_spotify_reports(config, db, workspace)
            except Exception:
                log.debug("Could not refresh Spotify export reports", exc_info=True)
            return result_payload, 200 if result_payload["success"] else 400
        except (OSError, ValueError, TypeError, SpotifyError) as exc:
            message = _safe_message(exc, _oauth_secret_values())
            if export_id is not None:
                try:
                    db.update_export_record(export_id, status="failed", metadata={"error": message})
                except Exception:
                    pass
            return {"error": message or "export failed"}, 400
        finally:
            if service is not None:
                service.close()
            for source in sources:
                try:
                    source.close()
                except Exception:
                    pass
            db.close()

    @app.post("/api/spotify/export/folder")
    @app.post("/api/export/folder")
    @app.post("/api/spotify/export/direct")
    def spotify_export_folder():
        return _run_export("folder", request.get_json(silent=True) or {})

    @app.post("/api/spotify/export/zip")
    @app.post("/api/export/zip")
    @app.post("/api/spotify/export/archive")
    def spotify_export_zip():
        return _run_export("zip", request.get_json(silent=True) or {})

    @app.post("/api/spotify/export/start")
    def spotify_export_start():
        """Start an export in the background so the UI can show live progress."""
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = dict(request.get_json(silent=True) or {})
        kind = str(payload.get("kind") or "folder").casefold()
        if kind not in {"folder", "zip"}:
            return {"error": "kind must be folder or zip"}, 400
        browser_download = bool(payload.get("browser_download"))
        if browser_download:
            if not app.config.get("PUBLIC_MODE"):
                return {"error": "browser downloads are only needed in public mode"}, 400
            kind = "zip"
            staging = config.state_path / "browser-downloads"
            staging.mkdir(parents=True, exist_ok=True)
            payload["path"] = str(staging / f"clone-hero-charts-{secrets.token_hex(8)}.zip")
        workspace, destination, error = _prepare_export(
            kind, payload, allow_server_staging=browser_download
        )
        if error is not None:
            return error
        with export_job.lock:
            if export_job.running:
                return {"error": "an export is already running"}, 409
            export_job.reset()
            export_job.running = True
            export_job.status = "starting"
            export_job.kind = kind
            export_job.browser_download = browser_download
            export_job.destination = destination
            export_job.started_at = time.time()
        threading.Thread(
            target=_export_worker,
            args=(kind, payload, workspace, destination, browser_download),
            daemon=True,
        ).start()
        return jsonify({"started": True, "kind": kind, "destination": destination})

    def _export_worker(kind, payload, workspace, destination, browser_download):
        export_job.log(f"Export started ({kind}) → {destination}")
        export_job.log("Validating selections and preparing export...")
        try:
            result, status = _perform_export(
                kind, payload, workspace, destination, progress=export_job.update
            )
            with export_job.lock:
                if status == 200:
                    if browser_download:
                        export_id = result.get("export_id")
                        if export_id is not None:
                            browser_exports[int(export_id)] = Path(destination)
                            result["download_url"] = (
                                f"/api/spotify/export/download/{export_id}"
                                f"?access_token={app.config.get('LAN_ACCESS_TOKEN', '')}"
                            )
                    export_job.result = result
                    export_job.status = "complete" if result.get("success") else "failed"
                    if not result.get("success"):
                        errors = result.get("errors") or []
                        export_job.error = result.get("error") or (errors[0] if errors else "export failed")
                else:
                    export_job.status = "failed"
                    errors = result.get("errors") or []
                    export_job.error = str(result.get("error") or (errors[0] if errors else "export failed"))
        except Exception as exc:  # noqa: BLE001 - job must always settle
            with export_job.lock:
                export_job.status = "failed"
                export_job.error = _safe_message(exc)
        finally:
            with export_job.lock:
                export_job.running = False
                if export_job.status == "complete":
                    published = int((export_job.result or {}).get("published") or 0)
                    export_job.events.append({"ts": time.time(), "message": f"Export complete: {published} charts published"})
                else:
                    export_job.events.append({"ts": time.time(), "message": f"Export failed: {export_job.error}"})

    @app.get("/api/spotify/export/status")
    def spotify_export_status():
        with export_job.lock:
            payload = {
                "running": export_job.running,
                "status": export_job.status,
                "kind": export_job.kind,
                "done": export_job.done,
                "total": export_job.total,
                "current": export_job.current,
                "error": export_job.error,
                "destination": export_job.destination,
                "started_at": export_job.started_at,
                "recent_events": list(export_job.events)[-8:],
            }
            if not export_job.running and export_job.result is not None:
                payload["result"] = export_job.result
        return jsonify(payload)

    @app.post("/api/spotify/export/download")
    @app.post("/api/export/download")
    def spotify_export_download():
        """Build a ZIP on the service and return a browser-download URL.

        The browser owns the final save location, which is the only safe way a
        hosted application can write to a remote computer's Desktop or Songs
        folder.
        """
        if not app.config.get("PUBLIC_MODE"):
            return {"error": "browser downloads are only needed in public mode"}, 400
        staging = config.state_path / "browser-downloads"
        staging.mkdir(parents=True, exist_ok=True)
        destination = staging / f"clone-hero-charts-{secrets.token_hex(8)}.zip"
        payload = dict(request.get_json(silent=True) or {})
        payload["path"] = str(destination)
        result, status = _run_export("zip", payload, allow_server_staging=True)
        if status != 200:
            return result, status
        export_id = int(result["export_id"])
        browser_exports[export_id] = destination
        return jsonify({
            **result,
            "download_url": f"/api/spotify/export/download/{export_id}?access_token={app.config.get('LAN_ACCESS_TOKEN', '')}",
        })

    @app.get("/api/spotify/export/download/<int:export_id>")
    def download_browser_export(export_id: int):
        target = browser_exports.get(export_id)
        if target is None or not target.is_file():
            abort(404)
        return send_file(target, as_attachment=True, download_name="clone-hero-charts.zip", max_age=0)

    @app.post("/api/spotify/export")
    @app.post("/api/export")
    def spotify_export_dispatch():
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or payload.get("format") or payload.get("kind") or "folder").casefold()
        if mode in {"zip", "archive"}:
            return _run_export("zip", payload)
        return _run_export("folder", payload)

    @app.post("/api/spotify/export/sync")
    @app.post("/api/export/sync")
    @app.post("/api/export/managed-sync")
    @app.post("/api/spotify/sync")
    def spotify_export_sync():
        if not _loopback_request():
            return {"error": "managed sync writes to the server. Use Download ZIP to save on this device."}, 403
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.get_json(silent=True) or {}
        try:
            workspace = _workspace_id(payload.get("workspace_id"))
            ok, error, destination = _validate_spotify_folder(
                str(payload.get("path") or payload.get("destination") or config.library_root)
            )
            if not ok:
                return {"error": error}, 400
            db = Database(config.database_path)
            service: ExportService | None = None
            sources: list[ChartSource] = []
            try:
                selections = _validated_export_selections(db, workspace)
                track_map = {
                    track.db_id: track
                    for track in db.get_spotify_tracks(True)
                    if track.db_id is not None
                }
                current_paths: list[str] = []
                for selection in selections:
                    candidate = _candidate_from_payload(_json_object(selection.get("candidate_json"), {}))
                    if candidate is None:
                        continue
                    track = track_map.get(selection.get("track_id"))
                    relative = str(selection.get("target_relpath") or "")
                    if not relative:
                        relative = str(
                            destination_path(
                                config,
                                candidate,
                                str(selection.get("kind") or "exact"),
                                track=track,
                            )
                        )
                    if not relative.casefold().endswith(".sng"):
                        return {"error": "a persisted selection has an unsafe target path"}, 400
                    current_paths.append(relative.replace("\\", "/"))
                sources = list(source_factory(config))
                service = ExportService(config, sources, track_resolver=track_map)
                result = service.sync_managed(destination, current_paths)
                response = {
                    "ok": not bool(result.errors),
                    "destination": destination,
                    "stale_paths": list(result.stale_paths),
                    "removed_paths": list(result.removed_paths),
                    "preserved_paths": list(result.preserved_paths),
                    "errors": [_safe_message(item) for item in result.errors],
                }
                return response, 200 if response["ok"] else 400
            finally:
                if service is not None:
                    service.close()
                for source in sources:
                    try:
                        source.close()
                    except Exception:
                        pass
                db.close()
        except (OSError, ValueError, TypeError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.get("/api/artwork/source/<source>/<artwork_id>")
    def source_artwork(source: str, artwork_id: str):
        """Proxy cached source artwork; Spotify artwork never enters this route."""
        if not _safe_local_request():
            return {"error": "artwork proxy is local-only"}, 403
        if source.casefold() != "enchor" or not re.fullmatch(r"[A-Fa-f0-9]{16,64}", artwork_id):
            abort(404)
        cache_dir = config.state_path / "artwork-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{artwork_id.casefold()}.jpg"
        try:
            if not cache_path.is_file():
                response = httpx.get(
                    f"https://files.enchor.us/{artwork_id}.jpg",
                    timeout=float(getattr(config.network, "timeout_seconds", 30.0)),
                    follow_redirects=True,
                )
                if response.status_code != 200 or not response.content or len(response.content) > 10 * 1024 * 1024:
                    abort(404)
                part = cache_path.with_name(cache_path.name + ".part")
                part.write_bytes(response.content)
                os.replace(part, cache_path)
        except (OSError, httpx.HTTPError):
            cache_path.with_name(cache_path.name + ".part").unlink(missing_ok=True)
            abort(404)
        return send_file(cache_path, mimetype="image/jpeg", max_age=86400)

    def _start_job(action: str) -> tuple[dict, int]:
        busy = _busy_response()
        if busy is not None:
            return busy
        with state.lock:
            if state.running:
                return {"error": "a job is already running"}, 409
            state.reset()
            state.running = True
            state.action = action
            state.started_at = time.time()
            state.last_activity_at = state.started_at
            state.activity = f"Starting {action}..."
        thread = threading.Thread(
            target=_run_job,
            args=(config, state, action, source_factory),
            daemon=True,
        )
        thread.start()
        return {"started": action}, 202

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return (
            _SPOTIFY_PAGE
            .replace("__OUTPUT_DEFAULT__", str(config.library_root))
            .replace("__LAN_ACCESS_TOKEN__", json.dumps(str(app.config.get("LAN_ACCESS_TOKEN") or "")))
            .replace("__PUBLIC_MODE__", "true" if app.config.get("PUBLIC_MODE") else "false")
        )

    @app.get("/advanced")
    def advanced():
        return _ADVANCED_PAGE.replace("__LIBRARY_ROOT__", str(config.library_root))

    # -------------------------------------------------------------- files
    @app.get("/api/files")
    def list_files():
        input_dir = config.input_path
        db = Database(config.database_path)
        try:
            disabled = db.get_disabled_files()
        finally:
            db.close()
        files = []
        if input_dir.exists():
            for path in sorted(input_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    files.append({"name": path.name, "size": path.stat().st_size, "enabled": path.name not in disabled})
        return jsonify(files)

    @app.post("/api/upload")
    def upload():
        uploaded = request.files.getlist("files")
        if not uploaded:
            return {"error": "no files provided"}, 400
        input_dir = config.input_path
        input_dir.mkdir(parents=True, exist_ok=True)
        saved, rejected = [], []
        for item in uploaded:
            name = Path(item.filename or "").name
            suffix = Path(name).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                rejected.append(name)
                continue
            safe_name = sanitize_component(Path(name).stem, fallback="playlist") + suffix
            item.save(input_dir / safe_name)
            saved.append(safe_name)
        return jsonify({"saved": saved, "rejected": rejected})

    @app.post("/api/files/delete")
    def delete_file():
        name = Path((request.json or {}).get("name", "")).name  # strip any path parts
        target = (config.input_path / name).resolve()
        if not str(target).startswith(str(config.input_path.resolve())):
            abort(403)
        if target.exists() and target.suffix.lower() in SUPPORTED_EXTENSIONS:
            target.unlink()
        return jsonify({"deleted": name})

    @app.post("/api/files/toggle")
    def toggle_file():
        payload = request.json or {}
        name = Path(str(payload.get("name", ""))).name
        enabled = bool(payload.get("enabled", True))
        if not name:
            return {"error": "name is required"}, 400
        target = config.input_path / name
        if not target.exists() or target.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return {"error": "file not found"}, 404
        db = Database(config.database_path)
        try:
            disabled = db.get_disabled_files()
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
            db.set_disabled_files(disabled)
        finally:
            db.close()
        return jsonify({"name": name, "enabled": enabled})

    # --------------------------------------------------------------- jobs
    @app.post("/api/run/<action>")
    def run(action: str):
        if action not in (
            "dry-run",
            "build",
            "exact-dry-run",
            "exact-build",
            "expansion-dry-run",
            "expansion-build",
        ):
            abort(404)
        return _start_job(action)

    @app.post("/api/output-path/check")
    def output_path_check():
        raw = str((request.json or {}).get("path", ""))
        ok, error, resolved = _validate_output_dir(config, raw)
        return jsonify({"ok": ok, "error": error, "resolved": resolved})

    @app.post("/api/run/simple")
    def run_simple():
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.json or {}
        output_dir = str(payload.get("output_dir", "")).strip()
        expansion = bool(payload.get("expansion", False))
        ok, error, resolved = _validate_output_dir(config, output_dir)
        if not ok:
            return {"error": error}, 400
        input_dir = config.input_path
        db = Database(config.database_path)
        try:
            disabled = db.get_disabled_files()
        finally:
            db.close()
        has_enabled_files = input_dir.exists() and any(
            p.suffix.lower() in SUPPORTED_EXTENSIONS
            and p.name not in disabled
            for p in input_dir.iterdir()
        )
        if not has_enabled_files:
            return {"error": "add enabled export files first"}, 400
        with state.lock:
            if state.running:
                return {"error": "a job is already running"}, 409
            state.reset()
            state.running = True
            state.action = "simple-build"
            state.output_dir = resolved
            state.started_at = time.time()
            state.last_activity_at = state.started_at
            state.activity = "Starting build..."
            state._log_locked(f"Output folder: {resolved}")
        thread = threading.Thread(
            target=_run_simple_job,
            args=(config, state, output_dir, expansion, source_factory),
            daemon=True,
        )
        thread.start()
        return {"started": "simple-build"}, 202

    @app.post("/api/open-folder")
    def open_folder():
        target = state.output_dir or str(config.library_root)
        path = Path(target)
        if not path.is_dir():
            return {"error": "folder does not exist yet"}, 404
        # The server runs on the user's own machine, so this opens for them.
        if hasattr(os, "startfile"):
            os.startfile(str(path))
            return jsonify({"opened": str(path)})
        return {"error": "opening folders is only supported on Windows"}, 501

    @app.get("/api/status")
    def status():
        running_jobs = _running_jobs()
        return jsonify({**state.snapshot(), "busy": bool(running_jobs), "running_jobs": running_jobs})

    @app.get("/api/estimate")
    def estimate():
        from csv_to_clonehero.overrides import load_overrides

        sources: list[ChartSource] = []
        try:
            sources = source_factory(config)
            source_names = [source.name for source in sources]
            db = Database(config.database_path)
            try:
                return jsonify(
                    _estimate_work(
                        config,
                        db,
                        source_names,
                        load_overrides(config.base_dir),
                        disabled_files=db.get_disabled_files(),
                    )
                )
            finally:
                db.close()
        finally:
            for source in sources:
                source.close()

    # -------------------------------------------------------------- review
    @app.get("/api/review")
    def review_items():
        include_rejected = request.args.get("include_rejected") == "1"
        db = Database(config.database_path)
        try:
            return jsonify(_review_payload(db, state.snapshot(), include_rejected))
        finally:
            db.close()

    @app.post("/api/review/decide")
    def review_decide():
        payload = request.json or {}
        track_key = payload.get("track_key", "")
        source = payload.get("source", "")
        try:
            chart_id = int(payload.get("chart_id", 0))
        except (TypeError, ValueError):
            chart_id = 0
        decision = payload.get("decision", "")
        if (
            decision not in ("accept", "reject", "clear")
            or not track_key
            or not source
            or chart_id <= 0
        ):
            return {"error": "invalid decision payload"}, 400
        db = Database(config.database_path)
        try:
            db.set_manual_decision(track_key, source, chart_id, decision)
        finally:
            db.close()
        return jsonify(
            {
                "ok": True,
                "track_key": track_key,
                "source": source,
                "chart_id": chart_id,
                "decision": decision,
                "message": "Saved; applies on the next exact pass/build.",
            }
        )

    @app.post("/api/review/accept-all")
    def review_accept_all():
        """Accept the best current review candidate for each track."""
        busy = _busy_response()
        if busy is not None:
            return busy
        with state.lock:
            if state.running:
                return {"error": "wait for the current job to finish"}, 409
            db = Database(config.database_path)
            try:
                result = db.accept_best_review_candidates()
            finally:
                db.close()
        return jsonify(
            {
                "ok": True,
                **result,
                "message": (
                    f"Accepted {result['accepted']} best review matches; "
                    "applies on the next exact pass/build."
                ),
            }
        )

    # ------------------------------------------------------------ reports
    @app.get("/api/summary")
    def summary():
        path = config.reports_path / "summary.json"
        if not path.exists():
            return jsonify(None)
        return app.response_class(
            path.read_text(encoding="utf-8"), mimetype="application/json"
        )

    @app.get("/reports/<name>")
    def report_file(name: str):
        target = (config.reports_path / Path(name).name).resolve()
        if not str(target).startswith(str(config.reports_path.resolve())):
            abort(403)
        if not target.exists():
            abort(404)
        return send_file(target, as_attachment=True)

    @app.get("/api/reports")
    def list_reports():
        reports_dir = config.reports_path
        files = []
        if reports_dir.exists():
            for path in sorted(reports_dir.iterdir()):
                if path.is_file():
                    files.append(path.name)
        return jsonify(files)

    return app


_SPOTIFY_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV to Clone Hero | Spotify workspace</title>
<style>
  :root { color-scheme: dark; --ink:#f7f1e8; --muted:#b4aa9b; --bg:#171514;
    --panel:#24201e; --line:#51463d; --green:#9bd36a; --pink:#f08ab0;
    --yellow:#f5cc73; --red:#ee7f75; --shadow:5px 5px 0 #0b0a09; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; color:var(--ink); background:
    radial-gradient(circle at 80% 0%, #3b2531 0, transparent 32rem), var(--bg);
    font-family: "Trebuchet MS", "Segoe UI", sans-serif; }
  body:before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.08;
    background-image:linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px);
    background-size:32px 32px; mask-image:linear-gradient(to bottom, #000, transparent 75%); }
  a { color:var(--yellow); }
  .shell { max-width:1240px; margin:auto; padding:28px 22px 70px; position:relative; }
  .topbar { display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:62px; }
  .brand { color:var(--ink); text-decoration:none; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }
  .brand span { color:var(--pink); }
  .toplinks { display:flex; align-items:center; gap:18px; font-size:.86rem; }
  .hero { max-width:850px; padding:18px 0 40px; }
  .eyebrow { color:var(--green); font-size:.76rem; font-weight:900; letter-spacing:.18em; text-transform:uppercase; }
  h1 { font-size:clamp(2.6rem, 8vw, 6.8rem); line-height:.9; letter-spacing:-.08em; margin:16px 0 22px; max-width:850px; }
  h1 em { color:var(--pink); font-style:normal; }
  h2 { font-size:1.05rem; letter-spacing:.04em; text-transform:uppercase; margin:0 0 12px; }
  h3 { margin:0; font-size:1rem; }
  p { line-height:1.55; }
  .lede { color:var(--muted); font-size:1.05rem; max-width:650px; }
  .privacy { color:var(--muted); font-size:.86rem; max-width:650px; }
  .panel { background:color-mix(in srgb, var(--panel) 94%, transparent); border:1px solid var(--line);
    box-shadow:var(--shadow); padding:18px; margin:18px 0; }
  .signin { display:flex; flex-wrap:wrap; align-items:center; gap:14px; margin-top:28px; }
  button, input, select { font:inherit; }
  button { border:1px solid var(--line); background:#332c27; color:var(--ink); padding:10px 15px;
    cursor:pointer; border-radius:2px; box-shadow:3px 3px 0 #0b0a09; }
  button:hover { transform:translate(-1px,-1px); }
  button.primary { color:#14110e; background:var(--green); border-color:#c5eb9e; font-weight:900; }
  button.accent { color:#1d1016; background:var(--pink); border-color:#ffc2d6; font-weight:900; }
  button.danger { background:#542c31; color:#ffd9d1; }
  button:disabled { opacity:.45; cursor:not-allowed; transform:none; }
  input[type=text] { min-width:0; width:100%; color:var(--ink); background:#11100f; border:1px solid var(--line); padding:10px 12px; }
  .muted { color:var(--muted); font-size:.88rem; }
  .hidden { display:none !important; }
  .workspace-grid { display:grid; grid-template-columns:250px minmax(0,1fr) 290px; gap:18px; align-items:start; }
  .folder-list, .track-list, .candidate-list { display:grid; gap:8px; }
  .track-list { max-height:68vh; min-height:240px; overflow:auto; padding-right:5px; resize:vertical; }
  .navigation-list { max-height:62vh; overflow:auto; padding-right:5px; }
  .setup-grid { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(280px,.7fr); gap:18px; align-items:start; }
  .step-label { color:var(--pink); font-size:.72rem; font-weight:900; letter-spacing:.13em; text-transform:uppercase; }
  .workspace-summary { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:12px; }
  .workspace-stat { border:1px solid var(--line); padding:9px; background:#171412; }
  .workspace-stat b { display:block; color:var(--green); font-size:1.1rem; }
  details.panel > summary { cursor:pointer; font-weight:900; font-size:1.15rem; }
  .folder { display:flex; gap:10px; align-items:center; width:100%; text-align:left; }
  .folder img { width:42px; height:42px; object-fit:cover; background:#3a302a; }
  .folder .folder-copy { min-width:0; overflow:hidden; flex:1; }
  .folder .folder-copy b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .folder .folder-copy small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .track { border:1px solid #403831; padding:12px; background:#1c1917; }
  .track-head { display:flex; justify-content:space-between; gap:12px; }
  .track-head > .state { flex-shrink:0; }
  .track-title { min-width:0; flex:1; }
  .track-title b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .track-title small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .selectbar { display:flex; flex-wrap:wrap; align-items:center; gap:10px; padding:12px;
    border:1px solid var(--line); background:#1d1916; margin:4px 0 12px; }
  .selectbar .muted { flex-basis:100%; margin:0; }
  button.big { padding:13px 22px; font-size:1.02rem; }
  .candidate { display:grid; grid-template-columns:54px minmax(0,1fr) auto; gap:10px; align-items:center;
    padding:9px; margin-top:8px; border:1px solid #4b4039; background:#29221f; }
  .candidate img { width:54px; height:54px; object-fit:cover; background:#3e322d; }
  .candidate-main { min-width:0; }
  .candidate-main b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .candidate-main small { color:var(--muted); display:block; line-height:1.4; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .candidate > button { white-space:nowrap; }
  .state { font-size:.72rem; letter-spacing:.08em; color:var(--yellow); }
  .state.RECOMMENDED { color:var(--green); }
  .state.EXCLUDED, .state.INCOMPATIBLE { color:var(--red); }
  .toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:9px; }
  .toolbar > input { flex:1 1 260px; }
  .statusline { color:var(--muted); min-height:1.4em; margin:8px 0; }
  .progress { height:8px; background:#100f0e; border:1px solid var(--line); overflow:hidden; }
  .progress > div { height:100%; width:0; background:var(--pink); transition:width .25s ease; }
  .scope-list { max-height:34rem; overflow:auto; padding-right:5px; margin-top:12px; }
  .scope-save { display:flex; justify-content:flex-end; gap:12px; align-items:center;
    border-top:1px solid var(--line); margin-top:14px; padding-top:14px; }
  .scope-save button { padding:13px 20px; font-size:1rem; }
  .scan-meta { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; }
  .recent-log { display:grid; gap:4px; margin-top:12px; min-height:1.5em; color:var(--muted); font-size:.8rem; }
  .recent-log div { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tray { position:sticky; top:16px; }
  .tray-list { max-height:320px; overflow:auto; display:grid; gap:8px; }
  .tray-item { border-bottom:1px solid #453a34; padding:7px 0; font-size:.84rem; min-width:0; }
  .tray-item b, .tray-item .muted { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .path-row { display:flex; gap:8px; }
  .path-row input { flex:1; }
  .notice { border-left:3px solid var(--yellow); padding-left:12px; color:var(--muted); }
  .error { color:var(--red); }
  .ok { color:var(--green); }
  .browse-overlay { position:fixed; inset:0; background:rgba(8,7,6,.72); z-index:50;
    display:flex; align-items:center; justify-content:center; padding:18px; }
  .browse-dialog { width:min(680px, 100%); max-height:82vh; display:flex; flex-direction:column;
    background:var(--panel); border:1px solid var(--line); box-shadow:var(--shadow); padding:18px; }
  .browse-list { display:grid; gap:6px; overflow:auto; min-height:180px; margin-top:10px; padding-right:5px; }
  .browse-list .folder { padding:8px 10px; background:#1c1917; border:1px solid #403831; }
  .browse-list .folder:hover { border-color:var(--yellow); }
  @media (max-width:950px) { .workspace-grid { grid-template-columns:210px minmax(0,1fr); }
    .setup-grid { grid-template-columns:1fr; }
    .tray { grid-column:1 / -1; position:static; } }
  @media (max-width:650px) { .shell { padding:18px 13px 50px; } .topbar { margin-bottom:36px; }
    .toplinks { gap:9px; } .workspace-grid { grid-template-columns:1fr; } h1 { font-size:3.2rem; }
    .candidate { grid-template-columns:42px minmax(0,1fr); } .candidate img { width:42px; height:42px; }
    .candidate button { grid-column:1 / -1; width:100%; } .path-row { flex-wrap:wrap; } }
</style>
</head>
<body>
<main class="shell">
  <nav class="topbar">
    <a class="brand" href="/"><span>CSV</span> to Clone Hero</a>
    <div class="toplinks"><a href="/advanced">advanced CSV mode</a><button id="signOut" class="hidden">Sign out</button></div>
  </nav>

  <section class="setup-grid">
      <div id="inventoryPanel" class="panel"><div class="step-label">1 · Clone Hero library</div><h2>Linked Songs folder</h2><p class="muted">Link your Clone Hero / YARG Songs folder on the server. Owned songs are skipped during discovery, exports go straight into the folder, and the same chart is never written twice.</p><div class="path-row"><input id="inventoryPath" type="text" placeholder="Path to the Songs folder, e.g. D:\Songs" spellcheck="false"><button id="browseServer" title="Browse the server's folders">Browse</button><button id="pickInventoryPath" title="Choose existing Songs folder">Choose folder</button><button id="inventoryStart" class="accent">Link &amp; scan</button><button id="inventoryVerify">Verify</button></div><div id="inventoryCandidates" class="toolbar" style="margin-top:8px"></div><div id="inventoryStatus" class="statusline"></div><div class="toolbar" style="margin-top:6px"><button id="inventoryIssuesButton">Check folder health</button><button id="inventoryClean" class="danger hidden">Quarantine problem files</button></div><div id="inventoryIssues" class="statusline"></div></div>
    <div class="panel"><div class="step-label">2 · Resume saved work</div><h2>Saved workspace</h2><p class="muted">Discovery results and selections autosave locally. Load a previous workspace here.</p><div class="path-row"><select id="workspacePicker" style="flex:1;min-width:0"></select><button id="loadWorkspaceButton">Load</button></div><div id="workspaceSummary" class="workspace-summary"></div><div class="toolbar" style="margin-top:10px"><button id="saveWorkspace" class="accent">Save Workspace</button></div><div id="workspaceSaveStatus" class="statusline">Loading saved workspaces...</div></div>
  </section>

  <section id="signedOut">
    <div class="hero">
      <div class="eyebrow">Local browser · Spotify first</div>
      <h1>Your library,<br><em>your picks.</em></h1>
  <p class="lede">Find charts for the music you already keep in Spotify, curate the results, and export only what you choose to Clone Hero or YARG.</p>
  <p id="remoteHint" class="notice hidden">Remote mode: link your Clone Hero Songs folder in step 1, then export straight into it from the selection tray. The ZIP download stays available as a fallback. Native folder pickers run on the server, so they are hidden here.</p>
      <div class="panel">
        <h2>Private beta</h2>
        <p class="notice">Spotify access is currently a private beta. Only allowlisted Spotify accounts can sign in while the integration is being tested.</p>
        <p class="privacy">Privacy by design: this app runs on <code>127.0.0.1</code>. Your refresh token stays in the operating system keyring when available. Tokens are never written to SQLite, ZIP exports, or browser-visible status data. Spotify artwork is used only for playlist navigation.</p>
        <div class="signin"><button id="signIn" class="primary">Sign in with Spotify</button><span id="authMessage" class="muted"></span></div>
      </div>
    </div>
  </section>

  <section id="signedIn" class="hidden">
    <div class="hero" style="padding-bottom:14px">
      <div class="eyebrow">Spotify workspace</div>
      <h1 style="font-size:clamp(2.3rem,6vw,5rem)">Pick the songs<br><em>worth keeping.</em></h1>
      <p id="accountLine" class="lede"></p>
    </div>
    <details id="discoveryPanel" class="panel" open><summary id="discoverySummary">Spotify discovery setup</summary><div style="margin-top:16px">
      <div class="toolbar"><button id="syncSpotify" class="primary">Sync Spotify library</button><button id="notifications">Enable notifications</button></div>
      <div id="syncStatus" class="statusline" aria-live="polite">Sync playlist names first. Nothing contacts Enchor until you choose a scope.</div>
       <div id="scopePanel" class="notice hidden"><h2>Search scope</h2><p class="muted">Only selected playlists and optional Liked Songs will contact Enchor. Save this scope before finding charts.</p><div class="toolbar"><button id="scopeAll">Select all</button><button id="scopeNone">Select none</button><label><input id="likedScope" type="checkbox"> Liked Songs</label></div><div id="scopeList" class="folder-list scope-list"></div><button id="showInaccessible" class="hidden" style="margin-top:10px"></button><div id="estimate" class="statusline"></div><div class="scope-save"><span id="scopeSaveHint" class="muted">Choose playlists, then save the search scope.</span><button id="saveScope" class="primary">Save Scope</button></div></div>
       <div class="toolbar"><button id="scan" class="primary">Find charts in selected scope</button><button id="scanCancel" class="danger hidden">Cancel search</button></div>
       <div class="scan-meta"><div id="scanStatus" class="statusline" aria-live="polite">Choose and save a scope, then find charts.</div><div id="scanEta" class="statusline"></div></div>
       <div class="progress" role="progressbar" aria-label="Spotify discovery progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="scanFill"></div></div>
       <div id="scanRecent" class="recent-log" aria-live="polite"></div>
    </div></details>
    <div class="workspace-grid">
      <aside class="panel"><div class="step-label">3 · Review matches</div><div class="toolbar" style="justify-content:space-between"><h2>Playlists</h2><button id="allFolder">All</button></div><div id="folders" class="folder-list navigation-list"><p class="muted">Scan to load playlists.</p></div></aside>
       <section><div class="panel"><div class="toolbar"><h2 style="margin-right:auto">Chart matches</h2><button id="clearAll" class="danger">Clear selections</button></div><div class="selectbar"><button id="selectRecommended" class="primary big">Select all recommended charts</button><span class="muted">One click picks the best eligible chart for every matched song in this workspace.</span></div><div id="reviewStatus" class="statusline"></div><div class="toolbar"><select id="trackView"><option value="eligible">Matched charts</option><option value="recommended">Recommended only</option><option value="excluded">Low-quality matches</option><option value="all">All songs</option></select><input id="filter" type="text" placeholder="filter artist or song"></div><div id="trackCount" class="statusline"></div><div id="tracks" class="track-list"><p class="muted">Choose a playlist or load a workspace.</p></div><button id="loadMoreTracks" class="hidden" style="margin-top:10px">Show 50 more</button></div>
         <details class="panel"><summary>Optional artist expansion</summary><div class="toolbar"><p class="muted" style="margin-right:auto">Search for additional songs by selected primary artists.</p><button id="expansionStart" class="accent">Search selected artists</button></div><div id="artists" class="folder-list navigation-list"><p class="muted">Scan first to see artists.</p></div><div id="expansionStatus" class="statusline"></div></details></section>
      <aside class="panel tray"><div class="step-label">4 · Export</div><h2>Selection tray</h2><div id="traySummary" class="statusline">0 charts selected</div><div id="tray" class="tray-list"></div><hr style="border-color:#453a34;border-width:1px 0 0;margin:16px 0"><div id="serverExport"><h2>Export destination</h2><div class="path-row"><input id="exportPath" type="text" value="__OUTPUT_DEFAULT__" spellcheck="false"><button id="pickPath" title="Choose folder">Choose</button></div><div id="pathStatus" class="statusline"></div><div class="toolbar"><button id="checkPath">Validate folder</button><button id="exportFolder" class="primary">Export selected charts</button></div><details style="margin-top:12px"><summary class="muted">ZIP and managed-sync options</summary><div class="path-row" style="margin-top:8px"><input id="zipPath" type="text" placeholder=".zip destination on this computer (required)" spellcheck="false"></div><button id="exportZip" style="margin-top:8px">Export ZIP</button><button id="syncExport" style="margin-top:8px">Sync managed files</button></details></div><div id="remoteExport" class="hidden"><h2>Linked library export</h2><p id="linkedExportHint" class="muted">Link a Songs folder above to enable direct export.</p><button id="exportLinked" class="primary">Export selected charts to linked library</button><hr style="border-color:#453a34;border-width:1px 0 0;margin:16px 0"><h2>Fallback: save on this device</h2><p class="muted">Your browser will download a ZIP here, so its save dialog chooses this computer's folder.</p><button id="downloadZip">Download selected charts as ZIP</button></div><div id="exportProgressWrap" class="hidden" style="margin-top:10px"><div id="exportProgressText" class="statusline">Starting export...</div><div class="progress"><div id="exportProgressFill"></div></div><div id="exportLog" class="recent-log"></div></div><div id="exportStatus" class="statusline"></div></aside>
    </div>
  </section>

  <!-- Compatibility text keeps old smoke tests harmless without putting the CSV builder back on the landing page. -->
  <template id="legacy-simple-compat">Where does your music live? iTunes / Apple Music outPath Also find more songs by my artists</template>
  <div id="browseOverlay" class="browse-overlay hidden">
    <div class="browse-dialog">
      <div class="toolbar"><h2 style="margin-right:auto">Choose a folder on the server</h2><button id="browseClose" type="button">Close</button></div>
      <p class="muted" style="margin:6px 0 0">Click a folder to open it. Use <b>Use this folder</b> to fill the Songs folder path.</p>
      <div id="browsePath" class="statusline"></div>
      <div id="browseList" class="browse-list"></div>
      <div class="toolbar" style="margin-top:12px; justify-content:flex-end"><button id="browseUse" class="primary" type="button">Use this folder</button></div>
    </div>
  </div>
</main>
<script>
const lanAccessToken = __LAN_ACCESS_TOKEN__;
const publicMode = __PUBLIC_MODE__;
const $ = id => document.getElementById(id);
 let workspaceId = null, allTracks = [], currentFolder = null, currentLiked = false, latestStatus = null, scopeData = null, scopePlaylists = [], savedWorkspaces = [], showInaccessible = false, notifiedJob = '', scopeSaved = false, scopeDirty = false, lastRenderedScored = 0, lastWorkspaceRefreshAt = 0, trackTotal = 0, trackHasMore = false, completedUiApplied = false, filterTimer = null;
const jsonHeaders = {'Content-Type':'application/json'};
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(url, options={}) { const headers = {...(options.headers || {})}; if (lanAccessToken) headers['X-CSV-To-CloneHero-Access-Token'] = lanAccessToken; const response = await fetch(url, {...options, headers}); let body = {}; try { body = await response.json(); } catch (_) {} if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`); return body; }
function body(data) { return {method:'POST', headers:jsonHeaders, body:JSON.stringify({...data, workspace_id:workspaceId})}; }
function showError(id, error) { $(id).textContent = error.message || String(error); $(id).className = 'statusline error'; }
function showOk(id, text) { $(id).textContent = text; $(id).className = 'statusline ok'; }
async function authStatus() {
  const status = await api('/api/auth/status');
  $('signedOut').classList.toggle('hidden', status.authenticated);
  $('signedIn').classList.toggle('hidden', !status.authenticated);
  $('signOut').classList.toggle('hidden', !status.authenticated);
  if (!status.authenticated) { $('authMessage').textContent = status.configuration_error || status.allowlist || ''; return; }
  const account = status.account || {};
  $('accountLine').textContent = `Signed in as ${account.display_name || 'Spotify user'} · ${publicMode ? 'downloads save through this browser.' : 'private library stays on this computer.'}`;
  await loadScope();
  await loadWorkspace(workspaceId);
}
$('signIn').addEventListener('click', async () => { try { const result = await api('/api/auth/spotify/start'); location.href = result.authorization_url; } catch (error) { showError('authMessage', error); } });
$('signOut').addEventListener('click', async () => { try { await api('/api/auth/spotify/logout', {method:'POST'}); location.reload(); } catch (error) { alert(error.message); } });
function folderHtml(folder) {
  const artwork = folder.artwork_url ? `<img src="${esc(folder.artwork_url)}" alt="" loading="lazy">` : '<span style="width:42px;height:42px;background:#3a302a;display:block"></span>';
  return `<button class="folder" data-folder="${esc(folder.id)}">${artwork}<span class="folder-copy"><b>${esc(folder.name)}</b><small class="muted">${folder.track_count || 0} tracks · created by ${esc(folder.created_by || 'Unknown Spotify user')} · ${folder.eligible_count || 0} eligible</small></span></button>`;
}
 function renderFolders(folders) { $('folders').innerHTML = folders.length ? folders.map(folderHtml).join('') : '<p class="muted">No accessible playlists yet.</p>'; document.querySelectorAll('[data-folder]').forEach(button => button.addEventListener('click', () => loadTracks(button.dataset.folder, button.dataset.folder === 'liked'))); }
function candidateHtml(track, candidate) {
  const artwork = candidate.source_artwork_url ? `<img src="${esc(candidate.source_artwork_url)}" alt="${esc(candidate.source || 'source')} artwork" loading="lazy">` : '<span style="width:54px;height:54px;background:#3e322d;display:block"></span>';
  const state = String(candidate.eligibility || '').toUpperCase();
  const eligible = ['RECOMMENDED','ELIGIBLE_REVIEW'].includes(state);
  const incompatible = state === 'INCOMPATIBLE';
  const selected = candidate.selected ? ' · selected' : '';
  let button = '';
  if (eligible) {
    button = `<button data-select-track="${esc(track.spotify_id || track.track_key)}" data-select-candidate="${candidate.candidate_id}" data-select-source="${esc(candidate.source)}" data-select-chart="${candidate.chart_id}" data-select-action="${candidate.selected ? 'clear' : 'select'}">${candidate.selected ? 'Clear' : 'Select'}</button>`;
  } else if (!incompatible) {
    button = `<button data-select-track="${esc(track.spotify_id || track.track_key)}" data-select-candidate="${candidate.candidate_id}" data-select-source="${esc(candidate.source)}" data-select-chart="${candidate.chart_id}" data-select-action="${candidate.selected ? 'clear' : 'select'}" data-force="1" style="background:#54303a" title="This chart scored below the quality threshold. It may be a live version, an alternate mix, or have playback issues — you can still try it.">${candidate.selected ? 'Clear' : 'Try anyway'}</button>`;
  } else {
    button = '<button disabled title="Wrong artist or no playable guitar chart">—</button>';
  }
  return `<div class="candidate">${artwork}<div class="candidate-main"><b>${esc(candidate.artist)} — ${esc(candidate.title)}</b><small>${esc(candidate.source)} · score ${candidate.score ?? 0} · quality ${candidate.quality ?? 0}</small><small class="state ${esc(candidate.eligibility)}">${esc(candidate.eligibility || 'unrated')}${selected}</small></div>${button}</div>`;
}
function trackHtml(track) {
  const candidates = (track.candidates || []).map(candidate => candidateHtml(track, candidate)).join('');
  const owned = track.already_owned ? '<span class="state ok">already owned</span>' : `<span class="state">${track.selected_count || 0}/${track.candidate_count || 0}</span>`;
  return `<article class="track" data-track-card="${esc(track.track_key)}"><div class="track-head"><div class="track-title"><b>${esc(track.artist || track.primary_artist)} — ${esc(track.title)}</b><small class="muted">${esc(track.album || 'Singles')} · ${esc((track.playlists || []).join(', '))}</small></div>${owned}</div><div class="candidate-list">${track.already_owned ? '<small class="muted">Matched existing chart metadata; source search suppressed.</small>' : (candidates || '<small class="muted">No eligible chart candidates.</small>')}</div></article>`;
}
 function updateTrackCard(track) { const old = document.querySelector(`[data-track-card="${CSS.escape(track.track_key)}"]`); if (!old) return; const wrap = document.createElement('div'); wrap.innerHTML = trackHtml(track); old.replaceWith(wrap.firstElementChild); }
 $('tracks').addEventListener('click', async (event) => { const button = event.target.closest('[data-select-candidate]'); if (!button || button.disabled) return; const payload = {track_id:button.dataset.selectTrack, candidate_id:Number(button.dataset.selectCandidate), source:button.dataset.selectSource, chart_id:Number(button.dataset.selectChart), action:button.dataset.selectAction}; if (button.dataset.force === '1' && payload.action === 'select' && !confirm('This chart scored below the quality threshold and may have playback issues. Use it anyway?')) return; if (button.dataset.force === '1') payload.force = true; button.disabled = true; try { await api('/api/spotify/select', body(payload)); const track = allTracks.find(item => (item.spotify_id || item.track_key) === payload.track_id); if (track) { (track.candidates || []).forEach(candidate => { if (candidate.candidate_id === payload.candidate_id) candidate.selected = payload.action === 'select'; }); track.selected_count = (track.candidates || []).filter(candidate => candidate.selected).length; updateTrackCard(track); } loadTray(); } catch (error) { alert(error.message); button.disabled = false; } });
 function renderTracks() { $('trackCount').textContent = `Showing ${allTracks.length.toLocaleString()} of ${trackTotal.toLocaleString()} songs in this view`; $('tracks').innerHTML = allTracks.length ? allTracks.map(trackHtml).join('') : '<p class="muted">No tracks match this view.</p>'; $('loadMoreTracks').classList.toggle('hidden', !trackHasMore); }
 async function loadTracks(folderId=currentFolder, liked=currentLiked, append=false) { currentFolder = folderId; currentLiked = liked; try { const query = new URLSearchParams({workspace_id:workspaceId || '', limit:'50', offset:append ? String(allTracks.length) : '0', view:$('trackView').value, search:$('filter').value.trim(), ...(folderId ? {folder_id:folderId} : {}), ...(liked ? {liked:'1'} : {})}); const result = await api('/api/spotify/library/tracks?' + query); allTracks = append ? allTracks.concat(result.tracks || []) : (result.tracks || []); trackTotal = Number(result.track_total || 0); trackHasMore = !!result.has_more; renderTracks(); } catch (error) { showError('trackCount', error); } }
function renderTray(data) { $('traySummary').textContent = `${data.count || 0} charts selected · ${(data.total_estimated_bytes || 0).toLocaleString()} estimated bytes`; $('tray').innerHTML = (data.items || []).map(item => `<div class="tray-item"><b>${esc(item.track?.artist || item.artist_norm || '')} ${esc(item.track?.title || item.candidate?.title || '')}</b><br><span class="muted">${esc(item.source)} · ${esc(item.candidate?.title || '')}</span></div>`).join(''); }
async function loadTray() { try { renderTray(await api('/api/spotify/selections?workspace_id=' + encodeURIComponent(workspaceId || ''))); } catch (_) {} }
function renderWorkspaceSummary(item) { if (!item) { $('workspaceSummary').innerHTML = ''; return; } $('workspaceSummary').innerHTML = `<div class="workspace-stat"><b>${Number(item.completed_tracks || 0).toLocaleString()}</b><span class="muted">tracks processed</span></div><div class="workspace-stat"><b>${Number(item.matched_tracks || 0).toLocaleString()}</b><span class="muted">matched songs</span></div><div class="workspace-stat"><b>${Number(item.selections || 0).toLocaleString()}</b><span class="muted">selected charts</span></div>`; }
async function refreshWorkspaces() { try { const result = await api('/api/spotify/workspaces'); savedWorkspaces = result.workspaces || []; $('workspacePicker').innerHTML = savedWorkspaces.length ? savedWorkspaces.map(item => `<option value="${item.id}" ${Number(item.id) === Number(workspaceId) ? 'selected' : ''}>${esc(item.name)} · ${esc(item.status)} · ${Number(item.completed_tracks || 0).toLocaleString()} tracks</option>`).join('') : '<option value="">No saved workspace yet</option>'; const selected = savedWorkspaces.find(item => Number(item.id) === Number(workspaceId)) || savedWorkspaces[0]; if (!workspaceId && selected) workspaceId = selected.id; renderWorkspaceSummary(selected); $('workspaceSaveStatus').textContent = selected ? `${selected.saved_explicitly ? 'Saved' : 'Autosaved'} locally · ${new Date((selected.saved_at || selected.updated_at) * 1000).toLocaleString()}` : 'A workspace appears after Spotify sync.'; } catch (error) { showError('workspaceSaveStatus', error); } }
 let linkedLibraryPath = '';
 function updateLinkedExportButton() { const button = $('exportLinked'); if (!button) return; button.disabled = !linkedLibraryPath; $('linkedExportHint').textContent = linkedLibraryPath ? `Writes directly into ${linkedLibraryPath} on the server. Existing identical charts are skipped.` : 'Link a Songs folder above to enable direct export.'; }
 async function loadInventoryStatus() { try { const result = await api('/api/spotify/inventory/status'); if (result.path) $('inventoryPath').value = result.path; linkedLibraryPath = result.linked && result.path ? result.path : ''; const status = result.status; if (result.linked && status) { const when = new Date(status.scanned_at * 1000).toLocaleString(); const free = result.free_bytes != null ? ` · ${(Number(result.free_bytes) / 1073741824).toFixed(1)} GB free` : ''; const access = result.exists ? (result.writable ? 'ready' : 'NOT writable') : 'folder missing'; $('inventoryStatus').textContent = `${Number(status.valid_count || 0).toLocaleString()} valid charts · ${Number(status.invalid_count || 0).toLocaleString()} broken · ${access}${free} · scanned ${when}`; $('inventoryStatus').className = 'statusline ' + (result.exists && result.writable ? 'ok' : 'error'); } else if (result.linked) { $('inventoryStatus').textContent = 'Linked but never scanned. Click Link & scan.'; $('inventoryStatus').className = 'statusline'; } else { $('inventoryStatus').textContent = 'No library linked yet. Paste the Songs folder path and click Link & scan.'; $('inventoryStatus').className = 'statusline'; } updateLinkedExportButton(); } catch (_) {} }
 async function loadLibraryCandidates() { try { const result = await api('/api/paths/common-songs'); const existing = (result.folders || []).filter(folder => folder.exists); if (!existing.length) return; $('inventoryCandidates').innerHTML = '<span class="muted">Found on the server:</span>' + existing.map(folder => `<button type="button" data-candidate="${esc(folder.path)}">${esc(folder.path)}</button>`).join(''); document.querySelectorAll('[data-candidate]').forEach(button => button.addEventListener('click', () => { $('inventoryPath').value = button.dataset.candidate; })); } catch (_) {} }
 let browseCurrent = '';
 async function loadBrowse(path) { try { const query = path ? '?path=' + encodeURIComponent(path) : ''; const result = await api('/api/spotify/path/browse' + query); browseCurrent = result.path || ''; $('browsePath').textContent = result.path || 'This PC'; const up = result.parent !== null && result.parent !== undefined ? `<button type="button" class="folder" data-browse="${esc(result.parent)}"><span class="folder-copy"><b>.. (up one level)</b></span></button>` : ''; const rows = (result.folders || []).map(folder => `<button type="button" class="folder" data-browse="${esc(folder.path)}"><span class="folder-copy"><b>${esc(folder.name)}</b></span></button>`).join(''); $('browseList').innerHTML = up + rows || '<p class="muted">No subfolders here.</p>'; document.querySelectorAll('[data-browse]').forEach(button => button.addEventListener('click', () => loadBrowse(button.dataset.browse))); } catch (error) { showError('browsePath', error); } }
 $('browseServer').addEventListener('click', async () => { $('browseOverlay').classList.remove('hidden'); await loadBrowse($('inventoryPath').value.trim()); });
 $('browseClose').addEventListener('click', () => $('browseOverlay').classList.add('hidden'));
 $('browseOverlay').addEventListener('click', event => { if (event.target === $('browseOverlay')) $('browseOverlay').classList.add('hidden'); });
 $('browseUse').addEventListener('click', () => { if (browseCurrent) $('inventoryPath').value = browseCurrent; $('browseOverlay').classList.add('hidden'); });
async function loadWorkspace(value=workspaceId) { try { const query = new URLSearchParams({workspace_id:value || '', limit:'50', offset:'0', view:$('trackView').value, search:$('filter').value.trim()}); const result = await api('/api/spotify/library?' + query); workspaceId = result.workspace?.id || result.workspace_id; $('workspacePicker').value = String(workspaceId || ''); renderFolders(result.folders || []); allTracks = result.tracks || []; trackTotal = Number(result.track_total || 0); trackHasMore = !!result.has_more; renderTracks(); renderTray(await api('/api/spotify/selections?workspace_id=' + workspaceId)); await loadArtists(); renderWorkspaceSummary(savedWorkspaces.find(item => Number(item.id) === Number(workspaceId))); $('reviewStatus').textContent = `Saved workspace loaded · ${trackTotal.toLocaleString()} songs in this view · no discovery scan required.`; } catch (error) { showError('reviewStatus', error); } }
 function formatDuration(seconds) { const value = Math.max(0, Math.round(Number(seconds) || 0)); if (value < 60) return `${value}s`; const minutes = Math.floor(value / 60); if (minutes < 60) return `${minutes}m`; return `${Math.floor(minutes / 60)}h ${minutes % 60}m`; }
 function renderRecentEvents(events) { const recent = (events || []).slice(-6); $('scanRecent').innerHTML = recent.map(event => `<div>${esc(new Date((event.ts || 0) * 1000).toLocaleTimeString())} · ${esc(event.message || '')}</div>`).join(''); }
 function renderStatus(status) { latestStatus = status; const progress = status.progress || {}; const total = Number(progress.total_work || (Number(progress.total_tasks || progress.total || 0) + Number(progress.scoring_tasks_total || progress.total_tracks || 0))); const done = Number(progress.completed_work || (Number(progress.completed_tasks || progress.done || 0) + Number(progress.scoring_tasks_completed || progress.completed_tracks || 0))); const percent = total ? Math.min(100, done * 100 / total) : status.running ? 4 : ['complete','partial'].includes(status.status) ? 100 : 0; $('scanFill').style.width = `${percent}%`; $('scanFill').parentElement.setAttribute('aria-valuenow', String(Math.round(percent))); const terminal = ['complete','partial','cancelled','interrupted','failed'].includes(status.status) && !status.running; const hasSavedResults = terminal && ['complete','partial'].includes(status.status) && Number(progress.completed_tracks || 0) > 0; const detail = total ? `${done.toLocaleString()} / ${total.toLocaleString()} work units` : `${status.phase || 'idle'}`; $('scanStatus').textContent = status.error || [status.message, detail].filter(Boolean).join(' · '); $('scanStatus').className = 'statusline ' + (status.error ? 'error' : terminal ? 'ok' : ''); const hasEta = status.running && Number(status.eta_upper_seconds || status.eta_seconds) > 0; const etaText = hasEta ? `${formatDuration(status.eta_lower_seconds)}–${formatDuration(status.eta_upper_seconds)} remaining` : 'Estimating time remaining'; $('scanEta').textContent = status.running ? `${etaText} · ${formatDuration(status.elapsed_seconds)} elapsed` : status.finished_at ? `Finished in ${formatDuration(status.elapsed_seconds)}` : ''; if (hasSavedResults && !completedUiApplied) { completedUiApplied = true; $('discoveryPanel').open = false; $('discoverySummary').textContent = 'Discovery complete · saved results loaded'; $('scan').textContent = 'Run discovery again'; $('reviewStatus').textContent = 'Your saved scan is loaded below. You do not need to run discovery again.'; } renderRecentEvents(status.recent_events || status.events); $('scan').disabled = !!status.running || !scopeSaved || scopeDirty; setBusyLockout(!!status.busy); }
function setBusyLockout(busy) { ['syncSpotify','saveScope','inventoryStart','inventoryVerify','inventoryClean','expansionStart','saveWorkspace','syncExport','selectRecommended','clearAll'].forEach(id => { const el = $(id); if (el) el.disabled = busy; }); }
 async function pollScan() { try { const status = await api('/api/spotify/status?workspace_id=' + encodeURIComponent(workspaceId || 'spotify')); renderStatus(status); $('scanCancel').classList.toggle('hidden', !status.running); document.title = status.running ? `Searching Spotify… ${status.progress?.completed_work || 0}/${status.progress?.total_work || 0}` : 'CSV to Clone Hero | Spotify workspace'; const scored = Number(status.progress?.completed_tracks || 0); const refreshDue = scored - lastRenderedScored >= 100 && Date.now() - lastWorkspaceRefreshAt >= 10000; if (status.running && refreshDue) { lastRenderedScored = scored; lastWorkspaceRefreshAt = Date.now(); await loadWorkspace(); } if (status.running) setTimeout(pollScan, 1200); else if (['complete','partial','cancelled','interrupted','failed'].includes(status.status)) { await loadWorkspace(); await refreshWorkspaces(); notifyTerminal(status); } } catch (error) { showError('scanStatus', error); setTimeout(pollScan, 1800); } }
 function renderScopePlaylists() { const inaccessible = scopePlaylists.filter(p => p.access_status !== 'accessible'); const visible = scopePlaylists.filter(p => p.access_status === 'accessible' || showInaccessible); $('scopeList').innerHTML = visible.map(p => `<label class="folder"><input type="checkbox" data-scope-playlist="${esc(p.spotify_id)}" ${p.selected ? 'checked' : ''} ${p.access_status !== 'accessible' ? 'disabled' : ''}><span class="folder-copy"><b>${esc(p.name || p.spotify_id)}</b><small class="muted">${p.item_count || 0} tracks · created by ${esc(p.created_by || p.owner_name || p.owner_id || 'Unknown Spotify user')}${p.access_status !== 'accessible' ? ` · ${esc(p.access_status || 'inaccessible')}` : ''}${p.access_status !== 'accessible' && p.access_reason ? ` · ${esc(p.access_reason)}` : ''}</small></span></label>`).join('') || '<p class="muted">Sync Spotify library to load playlists.</p>'; document.querySelectorAll('[data-scope-playlist]').forEach(input => input.addEventListener('change', scopeChanged)); $('showInaccessible').classList.toggle('hidden', !inaccessible.length); $('showInaccessible').textContent = showInaccessible ? `Hide inaccessible playlists (${inaccessible.length})` : `Show inaccessible playlists (${inaccessible.length})`; }
 async function loadScope(value=workspaceId) { try { const query = value ? `?workspace_id=${encodeURIComponent(value)}` : ''; const result = await api('/api/spotify/scope' + query); workspaceId = result.workspace_id; scopeData = result.scope || {}; scopePlaylists = result.playlists || []; showInaccessible = false; scopeSaved = !!(scopeData.playlist_ids?.length || scopeData.include_liked); scopeDirty = false; $('scopePanel').classList.remove('hidden'); $('likedScope').checked = !!scopeData.include_liked; renderScopePlaylists(); refreshEstimate(); renderStatus(latestStatus || {}); } catch (error) { showError('syncStatus', error); } }
 async function refreshEstimate() { if (!scopeSaved && scopeDirty) { $('estimate').textContent = 'Save Scope to calculate the search estimate.'; return; } try { const result = await api('/api/spotify/estimate?workspace_id=' + encodeURIComponent(workspaceId || '')); const e = result.estimate || result; const d = e.duration || {}; const duration = d.available ? ` · initial estimate ${formatDuration(d.lower_seconds)}–${formatDuration(d.upper_seconds)}` : ''; $('estimate').textContent = `${e.selected_tracks || 0} selected · ${e.already_owned || 0} already owned · ${e.logical_query_count || 0} deduplicated queries (${e.reusable_cached_queries || 0} cached, ${e.fresh_queries || 0} fresh) · fallback upper bound ${e.possible_fallback_queries || 0}${duration}`; } catch (_) { $('estimate').textContent = 'Save a non-empty scope to calculate the estimate.'; } }
$('syncSpotify').addEventListener('click', async () => { try { const result = await api('/api/spotify/inventory', {method:'POST'}); workspaceId = result.workspace_id; pollScan(); } catch (error) { showError('syncStatus', error); } });
 function scopeChanged() { scopeDirty = true; scopeSaved = false; $('scopeSaveHint').textContent = 'Scope changed. Save Scope before finding charts.'; $('scan').disabled = true; refreshEstimate(); }
 $('scopeAll').addEventListener('click', () => { document.querySelectorAll('[data-scope-playlist]').forEach(input => { if (!input.disabled) input.checked = true; }); scopeChanged(); }); $('scopeNone').addEventListener('click', () => { document.querySelectorAll('[data-scope-playlist]').forEach(input => input.checked = false); scopeChanged(); }); $('likedScope').addEventListener('change', scopeChanged);
$('showInaccessible').addEventListener('click', () => { showInaccessible = !showInaccessible; renderScopePlaylists(); });
 $('saveScope').addEventListener('click', async () => { try { const playlist_ids = [...document.querySelectorAll('[data-scope-playlist]:checked')].map(input => input.dataset.scopePlaylist); const result = await api('/api/spotify/scope', {method:'POST', headers:jsonHeaders, body:JSON.stringify({workspace_id:workspaceId, playlist_ids, include_liked:$('likedScope').checked})}); scopeData = result.scope; scopeSaved = !!(playlist_ids.length || $('likedScope').checked); scopeDirty = false; $('scopeSaveHint').textContent = scopeSaved ? 'Scope saved. You can now find charts.' : 'Choose at least one playlist or enable Liked Songs.'; showOk('syncStatus', scopeSaved ? 'Scope saved.' : 'Scope cleared.'); refreshEstimate(); renderStatus(latestStatus || {}); } catch (error) { showError('syncStatus', error); } });
$('scanCancel').addEventListener('click', async () => { try { await api('/api/spotify/scan/cancel', {method:'POST'}); } catch (error) { showError('scanStatus', error); } });
 $('scan').addEventListener('click', async () => { try { if (completedUiApplied && !confirm('A saved discovery already exists. Run discovery again using the selected scope?')) return; completedUiApplied = false; lastRenderedScored = 0; const result = await api('/api/spotify/scan', body({})); workspaceId = result.workspace_id; pollScan(); } catch (error) { showError('scanStatus', error); } });
$('pickInventoryPath').addEventListener('click', async () => { try { const result = await api('/api/spotify/path/pick', {method:'POST'}); if (result.path) $('inventoryPath').value = result.path; } catch (error) { showError('inventoryStatus', error); } });
$('inventoryStart').addEventListener('click', async () => { try { $('inventoryStatus').textContent = 'Scanning Songs folder...'; const result = await api('/api/spotify/library-import', {method:'POST', headers:jsonHeaders, body:JSON.stringify({path:$('inventoryPath').value})}); const createdNote = result.created ? 'Folder created and linked: ' : ''; showOk('inventoryStatus', `${createdNote}${Number(result.valid || 0).toLocaleString()} valid charts linked · ${Number(result.invalid || 0).toLocaleString()} broken. Owned songs are now skipped during discovery.`); await loadInventoryStatus(); await refreshEstimate(); } catch (error) { showError('inventoryStatus', error); } });
$('inventoryVerify').addEventListener('click', async () => { try { $('inventoryStatus').textContent = 'Verifying linked library...'; const result = await api('/api/spotify/library/verify', {method:'POST'}); showOk('inventoryStatus', `Verified: ${Number(result.valid || 0).toLocaleString()} valid · ${Number(result.invalid || 0).toLocaleString()} broken · ${result.writable ? 'writable' : 'NOT writable'}.`); await loadInventoryStatus(); await refreshEstimate(); } catch (error) { showError('inventoryStatus', error); } });
$('inventoryIssuesButton').addEventListener('click', async () => { try { const result = await api('/api/spotify/library/issues'); const total = result.broken.length + result.suspicious.length + result.duplicates.length; if (!total) { showOk('inventoryIssues', 'Folder health check: no broken, suspicious, or duplicate files found.'); $('inventoryClean').classList.add('hidden'); return; } showError('inventoryIssues', `Folder health check: ${result.broken.length} broken · ${result.suspicious.length} suspicious · ${result.duplicates.length} duplicate groups. Quarantining moves problem files to a quarantine folder beside the library; nothing is deleted.`); $('inventoryClean').classList.remove('hidden'); } catch (error) { showError('inventoryIssues', error); } });
$('inventoryClean').addEventListener('click', async () => { if (!confirm('Move broken, suspicious, and duplicate charts out of the Songs folder into a quarantine folder? Nothing is deleted.')) return; try { const result = await api('/api/spotify/library/clean', {method:'POST', headers:jsonHeaders, body:JSON.stringify({scope:'all'})}); showOk('inventoryIssues', `Quarantined ${result.moved} files to ${result.quarantine}.`); $('inventoryClean').classList.add('hidden'); await loadInventoryStatus(); await refreshEstimate(); } catch (error) { showError('inventoryIssues', error); } });
$('notifications').addEventListener('click', async () => { if (!('Notification' in window)) return showError('syncStatus', new Error('Browser notifications are not supported.')); const permission = await Notification.requestPermission(); localStorage.setItem('spotifyNotifications', permission === 'granted' ? 'enabled' : 'disabled'); $('notifications').textContent = permission === 'granted' ? 'Notifications enabled' : 'Notifications blocked'; });
function notifyTerminal(status) { const id = `${status.workspace_id || workspaceId}:${status.finished_at || status.status}`; if (notifiedJob === id) return; notifiedJob = id; if (localStorage.getItem('spotifyNotifications') === 'enabled' && window.Notification && Notification.permission === 'granted') new Notification(`Spotify discovery ${status.status}`, {body: status.message || status.error || 'Discovery finished.'}); }
 $('selectRecommended').addEventListener('click', async () => { try { await api('/api/spotify/select-recommended', body({})); await loadWorkspace(); await refreshWorkspaces(); } catch (error) { alert(error.message); } });
$('clearAll').addEventListener('click', async () => { try { await api('/api/spotify/select-all', body({clear:true})); await loadWorkspace(); await refreshWorkspaces(); } catch (error) { alert(error.message); } });
 $('filter').addEventListener('input', () => { clearTimeout(filterTimer); filterTimer = setTimeout(() => loadTracks(currentFolder, currentLiked, false), 250); }); $('trackView').addEventListener('change', () => loadTracks(currentFolder, currentLiked, false)); $('loadMoreTracks').addEventListener('click', () => loadTracks(currentFolder, currentLiked, true)); $('allFolder').addEventListener('click', () => loadTracks(null, false, false));
 $('loadWorkspaceButton').addEventListener('click', async () => { const selected = Number($('workspacePicker').value); if (!selected) return; workspaceId = selected; completedUiApplied = false; await loadScope(selected); await loadWorkspace(selected); await refreshWorkspaces(); const status = await api('/api/spotify/status?workspace_id=' + selected); renderStatus(status); showOk('workspaceSaveStatus', 'Saved workspace loaded.'); $('reviewStatus').scrollIntoView({behavior:'smooth', block:'start'}); }); $('workspacePicker').addEventListener('change', () => renderWorkspaceSummary(savedWorkspaces.find(item => Number(item.id) === Number($('workspacePicker').value))));
 $('saveWorkspace').addEventListener('click', async () => { try { $('workspaceSaveStatus').textContent = 'Checkpointing workspace...'; const result = await api('/api/spotify/workspace/save', body({})); $('workspaceSaveStatus').textContent = `${result.candidates.toLocaleString()} candidates and ${result.selections} selections saved locally.`; await refreshWorkspaces(); } catch (error) { showError('workspaceSaveStatus', error); } });
function artistHtml(artist) { return `<label class="folder"><input type="checkbox" data-artist="${esc(artist.artist_norm)}" ${artist.selected ? 'checked' : ''} ${artist.eligible ? '' : 'disabled'}><span class="folder-copy"><b>${esc(artist.artist_name)}</b><small class="muted">${artist.track_count} library songs · cap ${artist.cap}</small></span></label>`; }
async function loadArtists() { try { const result = await api('/api/spotify/expansion/artists?workspace_id=' + workspaceId); $('artists').innerHTML = (result.artists || []).map(artistHtml).join('') || '<p class="muted">No primary artists found.</p>'; } catch (_) {} }
$('expansionStart').addEventListener('click', async () => { const artists = [...document.querySelectorAll('[data-artist]:checked')].map(input => input.dataset.artist); try { await api('/api/spotify/expansion/artists/select', body({artists:artists.map(artist_norm => ({artist_norm,selected:true}))})); const result = await api('/api/spotify/expansion/start', body({artist_norms:artists})); workspaceId = result.workspace_id; $('expansionStatus').textContent = 'Expansion search started...'; pollScan(); } catch (error) { showError('expansionStatus', error); } });
async function checkPath() { try { const result = await api('/api/spotify/path/check', body({path:$('exportPath').value})); if (result.ok) showOk('pathStatus', result.resolved); else showError('pathStatus', new Error(result.error)); } catch (error) { showError('pathStatus', error); } }
$('checkPath').addEventListener('click', checkPath); $('pickPath').addEventListener('click', async () => { try { const result = await api('/api/spotify/path/pick', {method:'POST'}); if (result.path) { $('exportPath').value = result.path; checkPath(); } } catch (error) { showError('pathStatus', error); } });
function setExportButtons(disabled) { ['exportLinked','downloadZip','exportFolder','exportZip'].forEach(id => { const el = $(id); if (el) el.disabled = disabled; }); }
let exportPollTimer = null;
async function pollExport() {
  try {
    const status = await api('/api/spotify/export/status');
    $('exportLog').innerHTML = (status.recent_events || []).map(event => `<div>${esc(event.message)}</div>`).join('');
    if (status.total > 0) {
      const pct = Math.max(2, Math.round(100 * status.done / status.total));
      $('exportProgressFill').style.width = pct + '%';
      if (status.current === 'Checking download sizes') {
        $('exportProgressText').textContent = `Checking download sizes ${status.done}/${status.total} — rate-limited, this phase can take several minutes`;
      } else {
        const label = status.current ? ' · ' + status.current.split('/').pop() : '';
        $('exportProgressText').textContent = `Exporting ${status.done}/${status.total}${label}`;
      }
    } else {
      $('exportProgressText').textContent = status.status === 'starting' ? 'Starting export...' : 'Preparing export...';
    }
    if (status.running) { exportPollTimer = setTimeout(pollExport, 800); return; }
    exportPollTimer = null;
    $('exportProgressWrap').classList.add('hidden');
    setExportButtons(false);
    if (status.status === 'complete' && status.result) {
      const result = status.result;
      const failures = (result.items || []).filter(item => item.status === 'failed' || item.result === 'artifact_failed');
      if (result.success === false && failures.length === 0) throw new Error((result.errors || ['export failed']).join('; '));
      const skipped = (result.items || []).filter(item => item.result === 'already_present').length;
      if (result.download_url) {
        const link = document.createElement('a'); link.href = result.download_url; link.download = 'clone-hero-charts.zip'; document.body.appendChild(link); link.click(); link.remove();
        showOk('exportStatus', `Prepared ${result.published || 0} charts. Choose a folder in this browser's download dialog.`);
      } else {
        let message = `${result.published || 0} charts exported to ${result.destination}${skipped ? ` · ${skipped} already present` : ''}`;
        if (failures.length) message += ` · ${failures.length} failed (see log)`;
        if (failures.length) showError('exportStatus', new Error(message)); else showOk('exportStatus', message);
      }
      await loadInventoryStatus();
      await loadTray();
    } else {
      showError('exportStatus', new Error(status.error || 'export failed'));
    }
  } catch (error) {
    exportPollTimer = null;
    $('exportProgressWrap').classList.add('hidden');
    setExportButtons(false);
    showError('exportStatus', error);
  }
}
async function startExport(kind, path, browserDownload=false) {
  if (exportPollTimer) { clearTimeout(exportPollTimer); exportPollTimer = null; }
  $('exportStatus').textContent = ''; $('exportStatus').className = 'statusline';
  $('exportProgressWrap').classList.remove('hidden');
  $('exportProgressText').textContent = 'Starting export...';
  $('exportProgressFill').style.width = '2%';
  $('exportLog').innerHTML = '';
  setExportButtons(true);
  try {
    await api('/api/spotify/export/start', body({kind, path, browser_download:browserDownload}));
    pollExport();
  } catch (error) {
    showError('exportStatus', error);
    $('exportProgressWrap').classList.add('hidden');
    setExportButtons(false);
  }
}
function runExport(mode) { const path = mode === 'folder' ? $('exportPath').value : $('zipPath').value; startExport(mode, path); }
$('exportFolder').addEventListener('click', () => runExport('folder')); $('exportZip').addEventListener('click', () => runExport('zip'));
$('syncExport').addEventListener('click', async () => { try { const result = await api('/api/spotify/export/sync', body({path:$('exportPath').value})); showOk('exportStatus', `${result.removed_paths?.length || 0} stale managed files removed.`); } catch (error) { showError('exportStatus', error); } });
$('downloadZip').addEventListener('click', () => startExport('zip', '', true));
$('exportLinked').addEventListener('click', () => { if (!linkedLibraryPath) { showError('exportStatus', new Error('Link a Songs folder first.')); return; } startExport('folder', linkedLibraryPath); });
if (publicMode) { $('serverExport').classList.add('hidden'); $('remoteExport').classList.remove('hidden'); $('pickInventoryPath').classList.add('hidden'); $('remoteHint').classList.remove('hidden'); }
Promise.all([refreshWorkspaces(), loadInventoryStatus(), loadLibraryCandidates()]).then(() => authStatus()).then(() => api('/api/spotify/status?workspace_id=' + encodeURIComponent(workspaceId || 'spotify')).then(status => { workspaceId = workspaceId || status.workspace_id; renderStatus(status); if (status.running) pollScan(); else if (['complete','partial','cancelled','interrupted','failed'].includes(status.status)) notifyTerminal(status); }));
</script>
</body>
</html>'''


_ADVANCED_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV to Clone Hero - Advanced</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", system-ui, sans-serif; background: #12151c;
         color: #e6e9ef; margin: 0; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 1.6rem; margin: 8px 0 4px; }
  h2 { font-size: 1.05rem; margin: 28px 0 8px; color: #8fd3ff; }
  .card { background: #1b2130; border: 1px solid #2a3247; border-radius: 10px;
          padding: 16px; margin-top: 10px; }
  .muted { color: #9aa4b8; font-size: .9rem; }
  a { color: #8fd3ff; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 10px 18px;
           cursor: pointer; background: #2f3a56; color: #e6e9ef; }
  button.primary { background: #2f7d4f; }
  button:disabled { opacity: .45; cursor: default; }
  .file-row { display: flex; justify-content: space-between; align-items: center;
              padding: 6px 4px; border-bottom: 1px solid #2a3247; }
  .file-row button { padding: 4px 10px; background: #54303a; font-size: .85rem; }
  .stage { padding: 4px 0; }
  .stage .ok { color: #6fd08c; }
  .stage .bad { color: #ff7a7a; }
  .stage .spin { color: #ffd479; }
  .job-status { background: #0d1017; border: 1px solid #2a3247; border-radius: 8px;
                padding: 10px 12px; margin: 12px 0; }
  .job-status-head { display: flex; justify-content: space-between; gap: 12px; }
  .job-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
             background: #6fd08c; margin-right: 7px; }
  .job-dot.running { background: #ffd479; box-shadow: 0 0 0 4px #4a3e23; }
  .job-dot.failed { background: #ff7a7a; }
  .job-track { height: 7px; background: #2a3247; border-radius: 99px; margin-top: 9px; overflow: hidden; }
  .job-fill { height: 100%; width: 0; background: #8fd3ff; transition: width .25s ease; }
  .review-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  table { border-collapse: collapse; margin-top: 6px; }
  td { padding: 3px 14px 3px 0; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #drop { border: 2px dashed #3a4456; border-radius: 10px; }
  #drop.over { border-color: #8fd3ff; background: #20293d; }
  .reports a { display: inline-block; margin: 2px 10px 2px 0; font-size: .9rem; }
  code { background: #0d1017; padding: 1px 6px; border-radius: 5px; }
</style>
</head>
<body>
<main>
  <h1>🎸 CSV to Clone Hero <span class="muted" style="font-size:1rem">— advanced</span></h1>
  <p class="muted">Turn playlist exports into a Clone Hero library.
     Library folder: <code>__LIBRARY_ROOT__</code>
     · <a href="/">← simple mode</a></p>

  <h2>1 · Get your playlist CSVs</h2>
  <div class="card">
    Export each public playlist to CSV with one of these free tools, then come back:
    <ul>
      <li><a href="https://www.chosic.com/spotify-playlist-exporter/" target="_blank" rel="noopener">Chosic Spotify Playlist Exporter</a> — log in with Spotify, pick a playlist, download CSV</li>
      <li><a href="https://exportify.net/" target="_blank" rel="noopener">Exportify</a> — log in with Spotify, export playlists to CSV</li>
    </ul>
  </div>

  <h2>2 · Add your CSVs</h2>
  <div class="card" id="drop">
    <input type="file" id="picker" accept=".csv" multiple>
    <span class="muted">or drag &amp; drop CSV files anywhere on this box</span>
    <div id="files"></div>
  </div>

  <h2>3 · Build the library</h2>
  <div class="card">
    <div id="estimate" class="muted">Calculating search estimate...</div>
    <p class="muted">Exact playlist songs run first. Artist expansion is optional and uses a separate search pass.</p>
    <button id="exactDry" class="primary">Exact dry run</button>
    <button id="exactBuild">Exact build</button>
    <button id="expansionDry">Expansion dry run</button>
    <button id="expansionBuild">Expansion build</button>
    <div id="jobStatus" class="job-status" aria-live="polite">
      <div class="job-status-head">
        <span><span id="jobDot" class="job-dot"></span><b id="jobHeadline">Ready</b></span>
        <span id="jobElapsed" class="muted"></span>
      </div>
      <div id="jobActivity" class="muted" style="margin-top:5px">Start an exact dry run to search playlist songs.</div>
      <div class="job-track"><div id="jobFill" class="job-fill"></div></div>
      <div id="jobDetail" class="muted" style="margin-top:5px"></div>
    </div>
    <div id="stages" style="margin-top:12px"></div>
  </div>

  <h2>4 · Review matches <span class="muted">(optional)</span></h2>
  <div class="card">
    <p class="muted">One card represents one Spotify song. The best candidate is shown first;
       alternatives can be expanded. Accepts and rejects are saved immediately and
       apply on the next exact pass or build.</p>
    <div id="reviewProgress" class="muted"></div>
    <div class="review-toolbar">
      <button id="acceptAll" class="primary">Accept all best review matches</button>
      <span class="muted">Use this when automatic review of every found song is acceptable.</span>
    </div>
    <label class="muted" style="margin-left:12px">
      <input type="checkbox" id="showRejected"> also show rejected candidates</label>
    <input type="text" id="reviewFilter" placeholder="filter…" style="margin-left:12px; background:#0d1017; border:1px solid #2a3247; color:#e6e9ef; border-radius:6px; padding:6px 10px;">
    <div id="review" style="margin-top:10px"></div>
  </div>

  <h2>5 · Results</h2>
  <div class="card">
    <div id="summary" class="muted">No results yet — run a dry run first.</div>
    <div class="reports" id="reports" style="margin-top:10px"></div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let latestStatus = null;

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, '0')}s` : `${seconds}s`;
}

function renderJobStatus(status) {
  latestStatus = status;
  const stages = status.stages || [];
  const progress = status.search_progress || {};
  const doneStages = stages.filter(stage => stage.status === 'done').length;
  const currentStage = stages.findIndex(stage => stage.status === 'running');
  const stageTotal = Math.max(stages.length, 5);
  let percent = stages.length ? (doneStages / stageTotal) * 100 : 0;
  let detail = status.stage ? `${status.stage}` : '';
  if (status.search_active && progress.queries_total) {
    percent = (progress.queries_done / progress.queries_total) * 100;
    detail = `${progress.queries_done} / ${progress.queries_total} queries · ` +
      `${progress.scored_tracks} / ${progress.total_tracks} tracks scored · ` +
      `${progress.review_candidates} review candidates available`;
  } else if (status.running && currentStage >= 0) {
    percent = ((doneStages + 0.25) / stageTotal) * 100;
    detail = stages[currentStage].detail || detail;
  } else if (!status.running && status.finished_at) {
    percent = status.error ? Math.min(99, (doneStages / stageTotal) * 100) : 100;
    detail = status.error || 'All stages finished.';
  }
  $('jobFill').style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $('jobDot').className = 'job-dot' + (status.error ? ' failed' : status.running ? ' running' : '');
  const actionLabels = {
    'dry-run': 'Exact dry run', 'build': 'Exact build',
    'exact-dry-run': 'Exact dry run', 'exact-build': 'Exact build',
    'expansion-dry-run': 'Expansion dry run', 'expansion-build': 'Expansion build'
  };
  $('jobHeadline').textContent = status.running
    ? `Running: ${actionLabels[status.action] || status.action || 'job'}`
    : status.error ? 'Job failed' : status.finished_at ? 'Last run complete' : 'Ready';
  $('jobActivity').textContent = status.activity || detail || 'Waiting to start.';
  $('jobDetail').textContent = detail;
  const elapsed = status.running && status.started_at
    ? (Date.now() / 1000 - status.started_at)
    : status.elapsed_seconds;
  $('jobElapsed').textContent = elapsed ? formatDuration(elapsed) : '';
}

function refreshJobElapsed() {
  if (latestStatus) renderJobStatus(latestStatus);
}

setInterval(refreshJobElapsed, 1000);

async function refreshFiles() {
  const files = await (await fetch('/api/files')).json();
  $('files').innerHTML = files.length
    ? files.map(f => `<div class="file-row" style="${f.enabled ? '' : 'opacity:.5'}"><label style="cursor:pointer;flex:1;display:flex;align-items:center;gap:8px">
        <input type="checkbox" ${f.enabled ? 'checked' : ''} onchange="toggleFile('${f.name.replace(/'/g, "\\'")}', this.checked)">
        <span>📄 ${f.name} <span class="muted">(${(f.size/1024).toFixed(0)} KB)</span></span></label>
        <button onclick="delFile('${f.name.replace(/'/g, "\\'")}')">remove</button></div>`).join('')
    : '<p class="muted">No CSVs yet.</p>';
}

async function toggleFile(name, enabled) {
  await fetch('/api/files/toggle', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, enabled})});
  await refreshFiles();
  refreshEstimate();
}

async function delFile(name) {
  await fetch('/api/files/delete', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  await refreshFiles();
  refreshEstimate();
}

async function upload(fileList) {
  const data = new FormData();
  for (const f of fileList) data.append('files', f);
  const res = await (await fetch('/api/upload', {method: 'POST', body: data})).json();
  if (res.rejected && res.rejected.length)
    alert('Skipped (not .csv): ' + res.rejected.join(', '));
  await refreshFiles();
  refreshEstimate();
}

$('picker').addEventListener('change', e => upload(e.target.files));
const drop = $('drop');
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.add('over');}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.remove('over');}));
drop.addEventListener('drop', e => upload(e.dataTransfer.files));

const runButtons = ['exactDry', 'exactBuild', 'expansionDry', 'expansionBuild'];

function setRunButtons(disabled) {
  runButtons.forEach(id => $(id).disabled = disabled);
  $('acceptAll').disabled = disabled;
}

async function run(action) {
  jobRunning = true;
  setRunButtons(true);
  $('stages').innerHTML = '';
  renderJobStatus({running: true, action, stage: 'Starting', activity: `Starting ${action}...`, stages: [], search_progress: {}});
  const res = await fetch('/api/run/' + action, {method: 'POST'});
  if (res.status === 409) { alert('A job is already running.'); }
  poll();
}

async function poll() {
  const s = await (await fetch('/api/status')).json();
  $('stages').innerHTML = s.stages.map(st => {
    const icon = st.status === 'done' ? '<span class="ok">✔</span>'
               : st.status === 'failed' ? '<span class="bad">✖</span>'
               : '<span class="spin">…</span>';
    return `<div class="stage">${icon} ${st.label}
      <span class="muted">${st.detail || ''}</span></div>`;
  }).join('') + (s.error ? `<div class="stage bad" style="color:#ff7a7a">Error: ${s.error}</div>` : '');
  jobRunning = s.running;
  renderJobStatus(s);
  renderReview();
  if (s.running) {
    await loadReview(false);
    setTimeout(poll, 900);
  }
  else {
    setRunButtons(false);
    refreshSummary(); refreshReports(); refreshEstimate();
    await loadReview(false);
  }
}

async function refreshEstimate() {
  try {
    const data = await (await fetch('/api/estimate')).json();
    const exact = data.exact || {};
    const expansion = data.expansion || {};
    $('estimate').innerHTML = `Exact: <b>${exact.song_count || 0}</b> songs,
      <b>${exact.uncached_query_count || 0}</b> uncached queries ·
      Expansion: <b>${expansion.eligible_artist_count || 0}</b> eligible artists,
      <b>${expansion.uncached_query_count || 0}</b> uncached queries`;
  } catch (err) {
    $('estimate').textContent = 'Search estimate unavailable until the input is scanned.';
  }
}

async function refreshSummary() {
  const s = await (await fetch('/api/summary')).json();
  if (!s) return;
  const exact = s.exact || {};
  const expansion = s.expansion || {};
  const plan = s.plan || {};
  const downloads = s.downloads || {};
  const manual = s.manual_decisions || {};
  $('summary').innerHTML = `<table>
    <tr><td>Playlist CSVs</td><td class="num">${s.csv_files}</td></tr>
    <tr><td>Unique tracks</td><td class="num">${s.unique_tracks}</td></tr>
    <tr><td>Primary artists</td><td class="num">${s.primary_artists}</td></tr>
    <tr><td>Exact: auto / review / no match</td>
        <td class="num">${exact.auto_matches || 0} / ${exact.review || 0} / ${exact.no_match || 0}</td></tr>
    <tr><td>Expansion selected</td><td class="num">${expansion.selected || 0}</td></tr>
    <tr><td>Manual accepts / rejects</td><td class="num">${manual.accept || 0} / ${manual.reject || 0}</td></tr>
    <tr><td>Estimated library size</td><td class="num">${plan.estimated_gib || 0} GiB</td></tr>
    <tr><td>Downloaded</td><td class="num">${downloads.completed || 0} (${downloads.gib_downloaded || 0} GiB)</td></tr>
  </table>`;
}

async function refreshReports() {
  const files = await (await fetch('/api/reports')).json();
  $('reports').innerHTML = files.length
    ? 'Reports: ' + files.map(f => `<a href="/reports/${f}">${f}</a>`).join('')
    : '';
}

// ------------------------------------------------------------- review UI
let reviewData = {tracks: [], progress: {}};
let jobRunning = false;
let lastSaved = '';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadReview(showLoading = true) {
  if (showLoading && !reviewData.tracks.length) $('review').innerHTML = '<p class="muted">Loading...</p>';
  const showRejected = $('showRejected').checked;
  try {
    reviewData = await (await fetch(
      '/api/review' + (showRejected ? '?include_rejected=1' : ''))).json();
  } catch (err) {
    $('review').innerHTML = '<p class="muted">Review state unavailable.</p>';
    return;
  }
  renderReview();
}

function renderReview() {
  const progress = reviewData.progress || {};
  const allTracks = reviewData.tracks || [];
  $('acceptAll').disabled = jobRunning;
  const filter = $('reviewFilter').value.trim().toLowerCase();
  let items = allTracks;
  if (filter) {
    items = items.filter(track => {
      const text = [track.artist, track.title, track.album,
        ...(track.playlists || []), ...(track.candidates || []).flatMap(c =>
          [c.candidate_artist, c.candidate_title, c.charter, c.reference])].join(' ');
      return text.toLowerCase().includes(filter);
    });
  }
  const statusText = progress.has_search_run
    ? `Review candidates available so far: ${progress.candidate_count || 0} · ` +
      `${progress.scored_tracks || 0} / ${progress.total_tracks || 0} tracks scored · ` +
      `${progress.review_track_count || 0} tracks` +
      (progress.searching ? ' · search still in progress' :
       progress.exact_scoring_complete ? '' : ' · scoring incomplete')
    : 'No exact search has run yet.';
  $('reviewProgress').textContent = lastSaved ? `${lastSaved} | ${statusText}` : statusText;
  if (!items.length) {
    let empty = 'Search completed and there are no review candidates.';
    if (!progress.has_search_run) empty = 'No exact search has run yet.';
    else if (progress.searching) empty = 'Search is running; no scored reviews exist yet.';
    else if (!progress.exact_scoring_complete) empty = 'Search did not complete; review state may be partial.';
    else if (filter) empty = 'No review tracks match this filter.';
    $('review').innerHTML = `<p class="muted">${empty}</p>`;
    return;
  }
  const cards = items.map(track => {
    const candidates = track.candidates || [];
    const best = candidates[0];
    const alternatives = candidates.slice(1);
    return `<article class="card" style="margin-top:10px">
      <div><b>${esc(track.artist)} — ${esc(track.title)}</b>
        <span class="muted">[${esc(track.status)}]</span></div>
      <div class="muted">${esc(track.album || '')}
        ${(track.playlists || []).length ? ' · ' + esc(track.playlists.join(', ')) : ''}</div>
      ${candidateHtml(track, best, true)}
      ${alternatives.length ? `<details><summary>${alternatives.length} alternative candidate${alternatives.length === 1 ? '' : 's'}</summary>
        ${alternatives.map(candidate => candidateHtml(track, candidate, false)).join('')}</details>` : ''}
    </article>`;
  }).join('');
  $('review').innerHTML = cards;
  document.querySelectorAll('#review button[data-decision]').forEach(button => {
    button.addEventListener('click', () => decide(
      button.dataset.track, button.dataset.source,
      Number(button.dataset.chart), button.dataset.decision));
  });
}

function candidateHtml(track, candidate, best) {
  if (!candidate) return '';
  const manual = candidate.manual || '';
  const state = manual === 'accept' ? 'background:#1d3a28;' : manual === 'reject' ? 'background:#3a1d24;opacity:.7;' : '';
  const link = candidate.url ? ` <a href="${esc(candidate.url)}" target="_blank" rel="noopener">view chart</a>` : '';
  const disabled = jobRunning ? ' disabled title="Decisions are disabled while search is running"' : '';
  return `<div class="file-row" style="${state}margin-top:8px;padding:10px;border-radius:8px">
    <span title="${esc(candidate.explanation)}">
      <b>${best ? 'Best: ' : ''}${esc(candidate.candidate_artist)} — ${esc(candidate.candidate_title)}</b><br>
      <span class="muted">Score ${candidate.score} · ${esc(candidate.charter || 'unknown charter')}
        · ${esc((candidate.instruments || []).join(', '))} · ${esc(candidate.reference || '')}${link}</span><br>
      <span class="muted">${esc(candidate.explanation || '')}</span>
    </span>
    <span style="white-space:nowrap">
      <button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="accept" style="background:#2f7d4f"${disabled}>Accept</button>
      <button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="reject" style="background:#54303a"${disabled}>Reject</button>
      ${manual ? `<button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="clear"${disabled}>Clear</button>` : ''}
    </span>
  </div>`;
}

async function decide(trackKey, source, chartId, decision) {
  const response = await fetch('/api/review/decide', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({track_key: trackKey, source, chart_id: chartId, decision})});
  if (!response.ok) return;
  const track = reviewData.tracks.find(item => item.track_key === trackKey);
  if (track) {
    track.candidates.forEach(candidate => {
      if (decision === 'accept' && candidate.manual === 'accept' &&
          !(candidate.source === source && candidate.chart_id === chartId)) candidate.manual = '';
      if (candidate.source === source && candidate.chart_id === chartId) candidate.manual = decision === 'clear' ? '' : decision;
      candidate.manual_decision = candidate.manual;
    });
  }
  lastSaved = 'Saved; applies on the next exact pass/build.';
  renderReview();
  setTimeout(() => { lastSaved = ''; renderReview(); }, 4000);
}

async function acceptAllReview() {
  if (jobRunning) return;
  const count = reviewData.progress?.review_track_count || 0;
  if (!count) {
    lastSaved = 'There are no review matches to accept.';
    renderReview();
    return;
  }
  if (!confirm(`Accept the best review candidate for all ${count} review tracks?`)) return;
  const response = await fetch('/api/review/accept-all', {method: 'POST'});
  const result = await response.json();
  if (!response.ok) {
    lastSaved = result.error || 'Bulk accept failed.';
    renderReview();
    return;
  }
  lastSaved = result.message || `Accepted ${result.accepted} review matches.`;
  await loadReview(false);
  setTimeout(() => { lastSaved = ''; renderReview(); }, 5000);
}

$('showRejected').addEventListener('change', loadReview);
$('reviewFilter').addEventListener('input', renderReview);
$('acceptAll').addEventListener('click', acceptAllReview);

$('exactDry').addEventListener('click', () => run('exact-dry-run'));
$('exactBuild').addEventListener('click', () => {
  if (confirm('Download exact playlist charts into the library now?')) run('exact-build');
});
$('expansionDry').addEventListener('click', () => run('expansion-dry-run'));
$('expansionBuild').addEventListener('click', () => {
  if (confirm('Search and download artist expansion charts now?')) run('expansion-build');
});

refreshFiles(); refreshSummary(); refreshReports(); refreshEstimate(); loadReview();
fetch('/api/status').then(r => r.json()).then(s => {
  jobRunning = s.running;
  setRunButtons(s.running);
  renderJobStatus(s);
  if (s.running) poll(); else renderReview();
});
</script>
</body>
</html>
"""


_SIMPLE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV to Clone Hero</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", system-ui, sans-serif; background: #12151c;
         color: #e6e9ef; margin: 0; }
  main { max-width: 680px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 1.6rem; margin: 8px 0 4px; }
  h2 { font-size: 1.05rem; margin: 28px 0 8px; color: #8fd3ff; }
  .card { background: #1b2130; border: 1px solid #2a3247; border-radius: 10px;
          padding: 16px; margin-top: 10px; }
  .muted { color: #9aa4b8; font-size: .9rem; }
  a { color: #8fd3ff; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 10px 18px;
           cursor: pointer; background: #2f3a56; color: #e6e9ef; }
  button.primary { background: #2f7d4f; }
  button:disabled { opacity: .45; cursor: default; }
  select, input[type=text] { font: inherit; background: #0d1017; color: #e6e9ef;
           border: 1px solid #2a3247; border-radius: 8px; padding: 9px 12px; }
  input[type=text] { width: 100%; }
  .file-row { display: flex; justify-content: space-between; align-items: center;
              padding: 6px 4px; border-bottom: 1px solid #2a3247; }
  .file-row button { padding: 4px 10px; background: #54303a; font-size: .85rem; }
  #drop { border: 2px dashed #3a4456; border-radius: 10px; text-align: center; }
  #drop.over { border-color: #8fd3ff; background: #20293d; }
  .job-track { height: 9px; background: #2a3247; border-radius: 99px; margin-top: 9px; overflow: hidden; }
  .job-fill { height: 100%; width: 0; background: #8fd3ff; transition: width .25s ease; }
  #log { background: #0d1017; border: 1px solid #2a3247; border-radius: 8px;
         padding: 10px 12px; margin-top: 12px; max-height: 240px; overflow-y: auto;
         font-family: Consolas, "Cascadia Mono", monospace; font-size: .8rem;
         white-space: pre-wrap; }
  .ok { color: #6fd08c; }
  .bad { color: #ff7a7a; }
  code { background: #0d1017; padding: 1px 6px; border-radius: 5px; }
  .hidden { display: none; }
</style>
</head>
<body>
<main>
  <h1>🎸 CSV to Clone Hero</h1>
  <p class="muted">Turn your music library exports into a Clone Hero / YARG song library.
     <a href="/advanced">advanced mode →</a></p>

  <h2>1 · Where does your music live?</h2>
  <div class="card">
    <select id="service">
      <option value="spotify">Spotify</option>
      <option value="itunes">iTunes / Apple Music</option>
    </select>
    <div id="serviceHelp" class="muted" style="margin-top:10px"></div>
  </div>

  <h2>2 · Drop your export files</h2>
  <div class="card" id="drop">
    <input type="file" id="picker" accept=".csv" multiple>
    <p class="muted" id="dropHint">or drag &amp; drop CSV files anywhere on this box</p>
    <div id="files" style="text-align:left"></div>
  </div>

  <h2>3 · Choose the output folder</h2>
  <div class="card">
    <input type="text" id="outPath" value="__OUTPUT_DEFAULT__" spellcheck="false">
    <div id="pathStatus" class="muted" style="margin-top:6px"></div>
    <p class="muted">This can point straight at your Clone Hero <code>Songs</code> folder.
       The app state and reports never move.</p>
    <label class="muted"><input type="checkbox" id="expansion">
      Also find more songs by my artists</label>
  </div>

  <div class="card" style="text-align:center">
    <button id="start" class="primary" disabled>Start</button>
  </div>

  <div id="jobCard" class="card hidden">
    <div style="display:flex; justify-content:space-between; gap:12px">
      <b id="jobHeadline">Working…</b><span id="jobElapsed" class="muted"></span>
    </div>
    <div class="job-track"><div id="jobFill" class="job-fill"></div></div>
    <div id="jobActivity" class="muted" style="margin-top:6px"></div>
    <div id="log"></div>
  </div>

  <div id="doneCard" class="card hidden">
    <h2 style="margin-top:0">Done</h2>
    <div id="doneCounts"></div>
    <button id="openFolder" class="primary" style="margin-top:10px">Open folder</button>
    <span id="openFolderMsg" class="muted" style="margin-left:10px"></span>
    <p class="muted">Point Clone Hero or YARG at this folder (Settings → Songs) and scan.</p>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let files = [];
let pathOk = false;
let jobRunning = false;

const SERVICES = {
  spotify: {
    accept: '.csv',
    hint: 'or drag & drop CSV files anywhere on this box',
    help: `Export each playlist to CSV with a free tool, then drop the files below:
      <ul><li><a href="https://www.chosic.com/spotify-playlist-exporter/" target="_blank" rel="noopener">Chosic Spotify Playlist Exporter</a></li>
      <li><a href="https://exportify.net/" target="_blank" rel="noopener">Exportify</a></li></ul>`
  },
  itunes: {
    accept: '.txt,.xml',
    hint: 'or drag & drop iTunes .txt / .xml exports anywhere on this box',
    help: `In iTunes or the Music app: select a playlist, then
      <b>File → Library → Export Playlist…</b> and choose <i>Plain Text</i> (.txt).
      For your whole library use <b>File → Library → Export Library…</b> (.xml).`
  }
};

function applyService() {
  const svc = SERVICES[$('service').value];
  $('serviceHelp').innerHTML = svc.help;
  $('picker').accept = svc.accept;
  $('dropHint').textContent = svc.hint;
}
$('service').addEventListener('change', applyService);
applyService();

function updateStart() {
  const enabledCount = files.filter(f => f.enabled).length;
  $('start').disabled = !(enabledCount > 0 && pathOk) || jobRunning;
}

async function refreshFiles() {
  files = await (await fetch('/api/files')).json();
  $('files').innerHTML = files.length
    ? files.map(f => `<div class="file-row" style="${f.enabled ? '' : 'opacity:.5'}"><label style="cursor:pointer;flex:1;display:flex;align-items:center;gap:8px">
        <input type="checkbox" ${f.enabled ? 'checked' : ''} onchange="toggleFile('${f.name.replace(/'/g, "\\'")}', this.checked)">
        <span>📄 ${f.name} <span class="muted">(${(f.size/1024).toFixed(0)} KB)</span></span></label>
        <button onclick="delFile('${f.name.replace(/'/g, "\\'")}')">remove</button></div>`).join('')
    : '<p class="muted">No files yet.</p>';
  updateStart();
}

async function toggleFile(name, enabled) {
  await fetch('/api/files/toggle', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, enabled})});
  await refreshFiles();
}

async function delFile(name) {
  await fetch('/api/files/delete', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  await refreshFiles();
}

async function upload(fileList) {
  const data = new FormData();
  for (const f of fileList) data.append('files', f);
  const res = await (await fetch('/api/upload', {method: 'POST', body: data})).json();
  if (res.rejected && res.rejected.length)
    alert('Skipped (unsupported type): ' + res.rejected.join(', '));
  await refreshFiles();
}

$('picker').addEventListener('change', e => upload(e.target.files));
const drop = $('drop');
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.add('over');}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.remove('over');}));
drop.addEventListener('drop', e => upload(e.dataTransfer.files));

let pathTimer = null;
async function checkPath() {
  const path = $('outPath').value;
  const res = await (await fetch('/api/output-path/check', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path})})).json();
  pathOk = res.ok;
  $('pathStatus').innerHTML = res.ok
    ? `<span class="ok">✔</span> ${res.resolved}`
    : `<span class="bad">✖</span> ${res.error}`;
  updateStart();
}
$('outPath').addEventListener('input', () => {
  clearTimeout(pathTimer);
  pathTimer = setTimeout(checkPath, 300);
});

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, '0')}s` : `${seconds}s`;
}

function renderStatus(s) {
  jobRunning = s.running;
  const stages = s.stages || [];
  const progress = s.search_progress || {};
  const doneStages = stages.filter(st => st.status === 'done').length;
  const current = stages.findIndex(st => st.status === 'running');
  const total = Math.max(stages.length, 6);
  let percent = stages.length ? (doneStages / total) * 100 : 0;
  if (s.search_active && progress.queries_total) {
    const stageBase = (doneStages / total) * 100;
    percent = stageBase + (progress.queries_done / progress.queries_total) * (100 / total);
  } else if (s.running && current >= 0) {
    percent = ((doneStages + 0.25) / total) * 100;
  } else if (!s.running && s.finished_at) {
    percent = s.error ? Math.min(99, percent) : 100;
  }
  $('jobFill').style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $('jobHeadline').textContent = s.running ? 'Working…'
    : s.error ? 'Failed' : s.finished_at ? 'Complete' : 'Ready';
  $('jobActivity').textContent = s.activity || '';
  const elapsed = s.running && s.started_at
    ? (Date.now() / 1000 - s.started_at) : s.elapsed_seconds;
  $('jobElapsed').textContent = elapsed ? formatDuration(elapsed) : '';
  $('log').textContent = (s.events || [])
    .map(e => `[${new Date(e.ts * 1000).toLocaleTimeString()}] ${e.message}`)
    .join('\\n');
  $('log').scrollTop = $('log').scrollHeight;
  if (!s.running && s.finished_at && !s.error && s.result) showDone(s.result);
  updateStart();
}

function showDone(result) {
  $('doneCard').classList.remove('hidden');
  const rows = [
    ['Songs matched', result.matched],
    ['Songs without a chart', result.no_match],
  ];
  if (result.expansion_selected) rows.push(['Expansion songs selected', result.expansion_selected]);
  rows.push(['Charts downloaded', result.downloaded]);
  if (result.download_errors) rows.push(['Download errors', result.download_errors]);
  $('doneCounts').innerHTML = '<table>' + rows.map(r =>
    `<tr><td>${r[0]}</td><td style="text-align:right; padding-left:18px"><b>${r[1]}</b></td></tr>`
  ).join('') + '</table>';
}

async function poll() {
  const s = await (await fetch('/api/status')).json();
  renderStatus(s);
  if (s.running) setTimeout(poll, 900);
}

$('start').addEventListener('click', async () => {
  $('doneCard').classList.add('hidden');
  $('jobCard').classList.remove('hidden');
  $('log').textContent = '';
  const body = {output_dir: $('outPath').value, expansion: $('expansion').checked};
  const res = await fetch('/api/run/simple', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || 'Could not start the job.');
    return;
  }
  poll();
});

$('openFolder').addEventListener('click', async () => {
  const res = await fetch('/api/open-folder', {method: 'POST'});
  const body = await res.json();
  $('openFolderMsg').textContent = res.ok ? '' : (body.error || 'Could not open the folder.');
});

refreshFiles();
checkPath();
fetch('/api/status').then(r => r.json()).then(s => {
  if (s.running) { $('jobCard').classList.remove('hidden'); poll(); }
  else renderStatus(s);
});
</script>
</body>
</html>
"""
