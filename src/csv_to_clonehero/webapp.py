"""Browser interface with local, LAN, and authenticated public-URL modes.

Lets a nontechnical user:

1. get CSVs (links to Chosic / Exportify),
2. upload them into input_playlists,
3. run a dry run or a full build with on-screen progress,
4. read the summary and download reports.

No terminal required.
"""

from __future__ import annotations

import hmac
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import string
import threading
import time
import uuid
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
from csv_to_clonehero.catalog import load_enchor_catalog
from csv_to_clonehero.database import Database
from csv_to_clonehero.diagnostics import RunDiagnostics, diagnostics_path
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
)
from csv_to_clonehero.models import ChartCandidate, Track
from csv_to_clonehero.spotify import (
    SpotifyAuthError,
    SpotifyCancellationError,
    SpotifyClient,
    SpotifyError,
    SpotifyOAuth,
    SpotifyPermissionError,
)
from csv_to_clonehero.spotify_pipeline import (
    build_source_query_key,
    build_spotify_estimate,
    estimate_spotify_duration,
    run_expansion_discovery,
    run_spotify_discovery,
)
from csv_to_clonehero.providers.soundcloud_api import SoundCloudClient, SoundCloudOAuth
from csv_to_clonehero.pipeline import (
    cleanup_empty_folders,
    run_downloads,
    run_plan,
    run_relocation,
    run_scan,
    run_search,
)
from csv_to_clonehero.reporting import generate_reports, generate_spotify_reports
from csv_to_clonehero.review_presentation import build_review_presentation
from csv_to_clonehero.sources import build_sources
from csv_to_clonehero.sources.base import (
    ChartSource,
    source_catalog_available,
    source_catalog_contains_chart,
)
from csv_to_clonehero.normalization import normalize_artist, normalize_title

log = logging.getLogger(__name__)

_SPOTIFY_WORKSPACE_KEY = "spotify"


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
            self.run_id = ""
            self.started_at: float | None = None
            self.finished_at: float | None = None
            self.progress: dict[str, Any] = {
                "total_tasks": 0,
                "completed_tasks": 0,
                "failed_tasks": 0,
                "total_tracks": 0,
                "catalog_eligible_tracks": 0,
                "catalog_unavailable_tracks": 0,
                "completed_tracks": 0,
                "cached_queries": 0,
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
                "estimated_cached_queries": 0,
                "estimated_completed_seconds": 0.0,
                "expansion_artists_total": 0,
                "expansion_artists_completed": 0,
                "expansion_songs_total": 0,
                "expansion_catalog_unavailable_songs": 0,
                "expansion_songs_with_charts": 0,
                "expansion_songs_without_charts": 0,
            }
            self.events: deque = deque(maxlen=LOG_EVENT_LIMIT)
            self.result: dict[str, Any] | None = None
            self._last_counter_at: float | None = None
            self._last_task_done = 0
            self._last_cached_done = 0
            self._last_scoring_done = 0
            self._query_seconds_samples: deque[float] = deque(maxlen=25)
            self._scoring_seconds_samples: deque[float] = deque(maxlen=50)
            self._cached_seconds_samples: deque[float] = deque(maxlen=20)
            self.worker: threading.Thread | None = None

    def _log_locked(self, message: str) -> None:
        self.events.append({"ts": time.time(), "message": _safe_message(message)})

    def log(self, message: str) -> None:
        with self.lock:
            self._log_locked(message)

    def begin(
        self,
        workspace_id: int,
        workspace_key: str = "spotify",
        scan_key: str = "exact",
        run_id: str | None = None,
    ) -> None:
        with self.lock:
            self.reset()
            self.running = True
            self.status = "running"
            self.phase = "starting"
            self.message = "Starting Spotify discovery..."
            self.workspace_id = workspace_id
            self.workspace_key = workspace_key
            self.scan_key = str(scan_key or "exact")
            self.run_id = str(run_id or secrets.token_urlsafe(12))
            self.started_at = time.time()
            self._last_counter_at = self.started_at
            self._log_locked(self.message)

    def attach_worker(self, run_id: str, worker: threading.Thread) -> bool:
        """Associate the thread before it can be observed by another request."""
        with self.lock:
            if not self.running or self.run_id != str(run_id):
                return False
            self.worker = worker
            return True

    def cleanup(self, run_id: str, worker: threading.Thread) -> bool:
        """Release only this run's guard, preserving recorded terminal state."""
        with self.lock:
            if self.run_id != str(run_id) or self.worker is not worker:
                return False
            self.worker = None
            if self.running:
                self.running = False
                self.status = "interrupted"
                self.phase = "interrupted"
                self.message = "Spotify discovery was interrupted; cached work can be resumed."
                self.finished_at = time.time()
                self._log_locked(self.message)
            return True

    def recover_stale(self, run_id: str) -> bool:
        """Clear a dead worker's in-memory ownership token."""
        with self.lock:
            if self.run_id != str(run_id) or not self.running:
                return False
            if self.worker is not None and self.worker.is_alive():
                return False
            self.worker = None
            self.running = False
            self.status = "interrupted"
            self.phase = "interrupted"
            self.message = "Spotify discovery was interrupted; cached work can be resumed."
            self.finished_at = time.time()
            self._log_locked(self.message)
            return True

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
                elif cached_delta:
                    self._cached_seconds_samples.append(duration / cached_delta)
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
                        ("catalog_eligible_tracks", "catalog_eligible_tracks"),
                        ("catalog_unavailable_tracks", "catalog_unavailable_tracks"),
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
                        ("expansion_artists_total", "expansion_artists_total"),
                        ("expansion_artists_completed", "expansion_artists_completed"),
                        ("expansion_songs_total", "expansion_songs_total"),
                        ("expansion_catalog_unavailable_songs", "expansion_catalog_unavailable_songs"),
                        ("expansion_songs_with_charts", "expansion_songs_with_charts"),
                        ("expansion_songs_without_charts", "expansion_songs_without_charts"),
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
            cached_seconds = (
                sorted(self._cached_seconds_samples)[len(self._cached_seconds_samples) // 2]
                if self._cached_seconds_samples
                else 0.02
            )
            if not self.running:
                eta = 0.0
                estimate_source = "complete" if self.finished_at is not None else "unknown"
            elif total_work == 0:
                eta = 0.0
                estimate_source = "unknown"
            else:
                cached_done = int(progress.get("cached_queries") or 0)
                estimated_cached = int(progress.get("estimated_cached_queries") or 0)
                remaining_cached = max(0, estimated_cached - cached_done)
                remaining_fresh = max(0, remaining_queries - remaining_cached)
                eta = (
                    remaining_fresh * query_seconds
                    + remaining_cached * cached_seconds
                    + remaining_scoring * scoring_seconds
                )
                observed = (
                    len(self._query_seconds_samples)
                    + len(self._scoring_seconds_samples)
                    + len(self._cached_seconds_samples)
                )
                if not remaining_queries and not remaining_scoring and total_work > completed_work:
                    eta = max(0.0, estimated_total - elapsed)
                estimate_source = "live_phase_rate" if observed >= 2 else "calibrated"
            sample_count = (
                len(self._query_seconds_samples)
                + len(self._scoring_seconds_samples)
                + len(self._cached_seconds_samples)
            )
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
                "run_id": self.run_id,
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
                "cached_seconds_per_item": round(cached_seconds, 4),
                "eta_sample_count": sample_count,
                "estimate_source": estimate_source,
                "events": list(self.events),
                "recent_events": list(self.events)[-6:],
                "result": self.result,
                "worker_alive": bool(self.worker and self.worker.is_alive()),
                "worker_known": self.worker is not None,
                "last_activity_at": self._last_counter_at,
            }


class _SoundCloudImportJobState:
    """Thread-safe state for the local SoundCloud export import."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.running = False
            self.status = "idle"
            self.phase = "idle"
            self.message = ""
            self.error = ""
            self.workspace_id: int | None = None
            self.workspace_key = ""
            self.run_id = ""
            self.filename = ""
            self.started_at: float | None = None
            self.finished_at: float | None = None
            self.progress: dict[str, Any] = {
                "total_bytes": 0,
                "bytes_read": 0,
                "total_rows": 0,
                "accepted_rows": 0,
                "skipped_rows": 0,
                "duplicate_rows": 0,
                "total_tracks": 0,
                "persisted_tracks": 0,
            }
            self.events: deque = deque(maxlen=LOG_EVENT_LIMIT)
            self.result: dict[str, Any] | None = None
            self.worker: threading.Thread | None = None
            self._last_message = ""

    def begin(
        self,
        *,
        workspace_id: int,
        workspace_key: str,
        run_id: str,
        filename: str,
        total_bytes: int,
    ) -> None:
        with self.lock:
            self.reset()
            self.running = True
            self.status = "running"
            self.phase = "queued"
            self.message = "Upload accepted; preparing the SoundCloud import..."
            self.workspace_id = int(workspace_id)
            self.workspace_key = str(workspace_key)
            self.run_id = str(run_id)
            self.filename = str(filename)
            self.started_at = time.time()
            self.progress["total_bytes"] = int(total_bytes)
            self._log_locked(self.message)

    def attach_worker(self, worker: threading.Thread) -> None:
        with self.lock:
            self.worker = worker

    def update(self, payload: Mapping[str, Any] | None = None) -> None:
        with self.lock:
            data = dict(payload or {})
            self.phase = str(data.get("phase") or self.phase)
            self.status = str(data.get("status") or self.status or "running")
            if data.get("message"):
                self.message = _safe_message(data["message"])
            for key in self.progress:
                if key not in data:
                    continue
                try:
                    self.progress[key] = int(data[key] or 0)
                except (TypeError, ValueError):
                    self.progress[key] = data[key]
            if data.get("error"):
                self.error = _safe_message(data["error"])
            if self.message and self.message != self._last_message:
                self._log_locked(self.message)
                self._last_message = self.message

    def finish(self, result: Mapping[str, Any]) -> None:
        with self.lock:
            self.running = False
            self.status = str(result.get("status") or "complete")
            self.phase = str(result.get("phase") or "complete")
            self.message = str(result.get("message") or "SoundCloud import complete.")
            self.error = _safe_message(result.get("error") or "")
            self.finished_at = time.time()
            self.result = dict(result)
            self._log_locked(self.message)

    def fail(self, error: Any) -> None:
        with self.lock:
            self.running = False
            self.status = "failed"
            self.phase = "failed"
            self.error = _safe_message(error)
            self.message = f"SoundCloud import failed: {self.error}"
            self.finished_at = time.time()
            self._log_locked(self.message)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            end = now if self.running else (self.finished_at or now)
            elapsed = (
                max(0.0, end - self.started_at)
                if self.started_at is not None
                else 0.0
            )
            return {
                "running": self.running,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "workspace_id": self.workspace_id,
                "workspace_key": self.workspace_key,
                "run_id": self.run_id,
                "filename": self.filename,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": round(elapsed, 3),
                "progress": dict(self.progress),
                "events": list(self.events),
                "recent_events": list(self.events)[-6:],
                "result": self.result,
                "worker_alive": bool(self.worker and self.worker.is_alive()),
                "worker_known": self.worker is not None,
            }

    def _log_locked(self, message: str) -> None:
        self.events.append({"ts": time.time(), "message": _safe_message(message)})


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
            self.workspace_id: int | str | None = None
            self.started_at: float | None = None
            self.result: dict[str, Any] | None = None
            self.events: deque = deque(maxlen=200)
            self.item_states: dict[int, dict[str, Any]] = {}
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

    def update_item(self, item: Any) -> None:
        """Persist an item-level view for live recovery/progress displays."""
        with self.lock:
            index = int(getattr(item, "selection_index", 0) or 0)
            self.item_states[index] = {
                "selection_index": index,
                "selection_id": getattr(item, "selection_id", None),
                "source": str(getattr(item, "source", "") or ""),
                "chart_id": getattr(item, "chart_id", None),
                "target_relpath": str(getattr(item, "target_relpath", "") or ""),
                "status": str(getattr(item, "status", "") or ""),
                "result": str(getattr(item, "result", "") or ""),
                "error": _safe_message(getattr(item, "failure", "") or ""),
                "bytes": int(getattr(item, "bytes_count", 0) or 0),
                "playable_note_count": int(getattr(item, "playable_note_count", 0) or 0),
            }

    def item_snapshot(self) -> dict[str, Any]:
        with self.lock:
            states = list(self.item_states.values())
            terminal = {"published", "duplicate", "failed", "invalid", "preflight_failed"}
            return {
                "selected": len(states),
                "completed": sum(item["status"] in {"published", "duplicate"} for item in states),
                "downloading": sum(item["status"] == "downloading" for item in states),
                "retrying": sum(item["status"] == "retrying" for item in states),
                "failed": sum(item["status"] in {"failed", "preflight_failed"} for item in states),
                "invalid": sum(item["status"] == "invalid" for item in states),
                "cancelled": sum(item["status"] == "cancelled" for item in states),
                "remaining": sum(item["status"] not in terminal for item in states),
                "items": states,
            }


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
        "tracks": getattr(result, "tracks", 0),
        "exact_hits": getattr(result, "exact_hits", 0),
        "exact_misses": getattr(result, "exact_misses", 0),
        "candidates": getattr(result, "candidates", 0),
        "rejected_candidates": getattr(result, "rejected_candidates", 0),
        "eligible_candidates": getattr(result, "eligible_candidates", 0),
        "rejection_reasons": getattr(result, "rejection_reasons", {}),
        "catalog_available": getattr(result, "catalog_available", False),
        "catalog_diagnostic": _safe_message(getattr(result, "catalog_diagnostic", "") or ""),
        "catalog_snapshot_id": getattr(result, "catalog_snapshot_id", ""),
        "catalog_chart_count": getattr(result, "catalog_chart_count", 0),
        "catalog_load_seconds": getattr(result, "catalog_load_seconds", 0.0),
        "artist_found_title_different": getattr(result, "artist_found_title_different", 0),
        "title_found_artist_different": getattr(result, "title_found_artist_different", 0),
        "broader_retrieval_tracks": getattr(result, "broader_retrieval_tracks", 0),
        "zero_candidate_tracks": getattr(result, "zero_candidate_tracks", 0),
        "candidate_charts_evaluated": getattr(result, "candidate_charts_evaluated", 0),
        "unique_charts_evaluated": getattr(result, "unique_charts_evaluated", 0),
        "strong_match_tracks": getattr(result, "strong_match_tracks", 0),
        "review_match_tracks": getattr(result, "review_match_tracks", 0),
        "hard_rejected_only_tracks": getattr(result, "hard_rejected_only_tracks", 0),
        "soft_rejected_only_tracks": getattr(result, "soft_rejected_only_tracks", 0),
        "no_plausible_tracks": getattr(result, "no_plausible_tracks", 0),
        "persisted_candidates": getattr(result, "persisted_candidates", 0),
        "retrieval_seconds": getattr(result, "retrieval_seconds", 0.0),
        "scoring_seconds": getattr(result, "scoring_seconds", 0.0),
        "discovery_seconds": getattr(result, "discovery_seconds", 0.0),
        "funnel": getattr(result, "funnel", {}),
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


REVIEW_DEFAULT_PAGE_SIZE = 50
REVIEW_MAX_PAGE_SIZE = 100


def _review_page_options(args: Mapping[str, Any]) -> dict[str, Any]:
    """Parse bounded card-level review paging and inspection filters."""
    try:
        limit = int(args.get("limit", REVIEW_DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        limit = REVIEW_DEFAULT_PAGE_SIZE
    limit = max(1, min(REVIEW_MAX_PAGE_SIZE, limit))
    try:
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    if args.get("page") not in (None, "") and args.get("offset") in (None, ""):
        try:
            offset = max(0, int(args.get("page")) - 1) * limit
        except (TypeError, ValueError):
            offset = 0
    return {
        "offset": max(0, offset),
        "limit": limit,
        "search": str(args.get("search", "") or "").strip(),
        "state": str(args.get("state", "all") or "all").strip().casefold(),
        "sort": str(args.get("sort", "artist") or "artist").strip().casefold(),
    }


def _review_card_matches(card: Mapping[str, Any], state: str) -> bool:
    if state in {"", "all"}:
        return True
    review_state = str(card.get("review_state", "") or "").casefold()
    status = str(card.get("status", "") or "").casefold()
    if state in {"eligible", "matched"}:
        return bool(card.get("has_eligible_match")) or review_state in {"strong", "needs_review"}
    if state in {"strong", "recommended", "auto_accept"}:
        return bool(card.get("has_strong_match")) or review_state == "strong" or status in {"accepted", "auto_accept"}
    if state in {"review", "needs_review"}:
        return bool(card.get("has_review_match")) or review_state == "needs_review" or status == "review"
    if state in {"selected", "chosen"}:
        return bool(card.get("selected"))
    if state in {"unselected", "unchosen"}:
        return not bool(card.get("selected"))
    if state in {"not_playable", "incompatible"}:
        return review_state == "not_playable"
    return True


def _review_card_search_text(card: Mapping[str, Any]) -> str:
    if card.get("_search_text"):
        return str(card["_search_text"])
    values = [
        card.get("artist"),
        card.get("title"),
        card.get("album"),
        *(card.get("playlists") or ()),
    ]
    for candidate in card.get("candidates") or ():
        values.extend(
            candidate.get(name)
            for name in ("candidate_artist", "candidate_title", "charter", "reference")
            if isinstance(candidate, Mapping)
        )
    return " ".join(str(value or "") for value in values).casefold()


def _review_row_state(row: Any) -> str:
    """Translate persisted eligibility into the lightweight card state."""
    data = _row_dict(row)
    eligibility = str(data.get("eligibility") or "").strip().upper()
    decision = str(data.get("decision") or "").strip().upper()
    if eligibility == INCOMPATIBLE or decision == INCOMPATIBLE:
        return "not_playable"
    if eligibility in {RECOMMENDED, "AUTO_ACCEPT"} or decision == "AUTO_ACCEPT":
        return "strong"
    if eligibility in {ELIGIBLE_REVIEW, "REVIEW"} or decision == "REVIEW":
        return "needs_review"
    return "not_recommended"


def _review_row_search_text(row: Any, track: Track) -> str:
    data = _row_dict(row)
    candidate = _json_object(data.get("candidate_json"), {})
    candidate = candidate if isinstance(candidate, Mapping) else {}
    values = [
        track.artist_full,
        track.title,
        track.album,
        *(track.playlists or ()),
        candidate.get("artist"),
        candidate.get("name"),
        candidate.get("charter"),
        candidate.get("url"),
        candidate.get("md5"),
        data.get("source"),
        data.get("chart_id"),
    ]
    return " ".join(str(value or "") for value in values).casefold()


def _paginate_review_cards(
    cards: list[dict],
    *,
    offset: int = 0,
    limit: int = REVIEW_DEFAULT_PAGE_SIZE,
    search: str = "",
    state: str = "all",
    sort: str = "artist",
) -> dict[str, Any]:
    """Filter and page complete cards while retaining every card alternative."""
    filtered = [
        card
        for card in cards
        if _review_card_matches(card, state)
        and (not search or search.casefold() in _review_card_search_text(card))
    ]
    if sort in {"score", "best"}:
        filtered.sort(
            key=lambda card: (
                -float(
                    card.get(
                        "_best_score",
                        (card.get("candidates") or [{}])[0].get("score") or 0,
                    )
                    or 0
                ),
                str(card.get("artist") or "").casefold(),
                str(card.get("title") or "").casefold(),
            )
        )
    else:
        filtered.sort(
            key=lambda card: (
                str(card.get("artist") or "").casefold(),
                str(card.get("title") or "").casefold(),
                str(card.get("track_key") or ""),
            )
        )
    total = len(filtered)
    offset = max(0, int(offset))
    limit = max(1, min(REVIEW_MAX_PAGE_SIZE, int(limit)))
    page_cards = filtered[offset:offset + limit]
    page_count = (total + limit - 1) // limit if total else 0
    page = {
        "offset": offset,
        "limit": limit,
        "page": (offset // limit) + 1 if total and offset < total else 0,
        "page_count": page_count,
        "total": total,
        "returned": len(page_cards),
        "has_more": offset + len(page_cards) < total,
        "search": search,
        "state": state or "all",
        "sort": sort or "artist",
    }
    return {"tracks": page_cards, "page": page}


def _review_payload(
    db: Database,
    state_snapshot: dict,
    include_rejected: bool = False,
    *,
    offset: int = 0,
    limit: int = REVIEW_DEFAULT_PAGE_SIZE,
    search: str = "",
    state: str = "all",
    sort: str = "artist",
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
        breakdown: dict[str, Any] = {}
        try:
            loaded_breakdown = json.loads(row["breakdown_json"])
            if isinstance(loaded_breakdown, dict):
                breakdown = loaded_breakdown
                explanation = str(breakdown.get("explanation", ""))
        except (TypeError, ValueError):
            pass
        item = {
            "candidate_artist": candidate.artist,
            "candidate_title": candidate.name,
            "source": candidate.source,
            "chart_id": candidate.chart_id,
            "score": round(row["score"], 1),
            "decision": row["decision"],
            "automatic_decision": row["decision"],
            "explanation": explanation,
            "breakdown": breakdown,
            "base_score": float(breakdown.get("base_score") or 0),
            "bonuses": list(breakdown.get("bonuses") or []),
            "penalties": list(breakdown.get("penalties") or []),
            "gate_factors": dict(breakdown.get("gate_factors") or {}),
            "quality_factors": dict(breakdown.get("quality_factors") or {}),
            "charter": candidate.charter,
            "album": candidate.album,
            "pack_name": candidate.pack_name,
            "instruments": list(candidate.instruments or ()),
            "diff_guitar": candidate.diff_guitar,
            "guitar_difficulty": candidate.diff_guitar,
            "url": candidate.url,
            "reference": candidate.reference,
            "chart_reference": candidate.reference,
            "supports_download": bool(candidate.supports_download),
            "manual": manual,
            "manual_decision": manual,
            # A local manual accept is the persisted current choice for this
            # review surface, even before the next exact pass materializes it.
            "selected": manual == "accept",
        }
        item["review"] = build_review_presentation(item)
        item["review_state"] = item["review"]["state"]
        item["review_label"] = item["review"]["label"]
        item["review_summary"] = item["review"]["summary"]
        item["selectable"] = item["review"]["selectable"]
        item["selection_action"] = item["review"]["action"]
        grouped.setdefault(row["track_id"], []).append(item)

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
        automatic_best = next(
            (
                candidate
                for candidate in candidates
                if candidate["review"]["state"] in {"strong", "needs_review"}
            ),
            candidates[0],
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
        selected_candidate = next(
            (candidate for candidate in candidates if candidate.get("selected")),
            None,
        )
        for candidate in candidates:
            candidate["best"] = candidate is automatic_best
        primary = selected_candidate or automatic_best
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
                "best_candidate_id": automatic_best["chart_id"],
                "selected_candidate_id": selected_candidate["chart_id"] if selected_candidate else None,
                "selected": bool(selected_candidate),
                "review": primary["review"],
                "review_state": primary["review"]["state"],
                "review_label": primary["review"]["label"],
                "review_summary": primary["review"]["summary"],
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
    page = _paginate_review_cards(
        cards,
        offset=offset,
        limit=limit,
        search=search,
        state=state,
        sort=sort,
    )
    return {
        "tracks": page["tracks"],
        "progress": metadata,
        **metadata,
        "page": page["page"],
        "track_total": page["page"]["total"],
        "track_offset": page["page"]["offset"],
        "track_limit": page["page"]["limit"],
        "has_more": page["page"]["has_more"],
    }


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
        recovery_db.mark_active_expansion_batches_interrupted()
        recovery_db.recover_running_exports()
        recovery_db.delete_workspace_by_key("spotify-demo")
    finally:
        recovery_db.close()
    state = JobState()
    spotify_job = _SpotifyJobState()
    soundcloud_import_job = _SoundCloudImportJobState()
    export_job = _ExportJobState()
    auth_lock = threading.RLock()
    auth_client: Any = None
    spotify_client: Any = None
    pending_authorization: dict[str, Any] | None = None
    pending_soundcloud_authorization: dict[str, Any] | None = None
    soundcloud_oauth: Any = None
    auth_authenticated = False
    spotify_cancel_event: threading.Event | None = None
    export_cancel_event = threading.Event()
    secure_store = keyring if keyring is not None else _default_keyring()
    browser_exports: dict[int, Path] = {}

    # Expose only injectable handles and non-secret state for application
    # integration tests.  Tokens are deliberately absent from this mapping.
    app.extensions["spotify_job"] = spotify_job
    app.extensions["soundcloud_import_job"] = soundcloud_import_job
    app.extensions["export_job"] = export_job
    app.extensions["export_cancel_event"] = export_cancel_event
    app.extensions["spotify_oauth_factory"] = oauth_factory
    app.extensions["spotify_client_factory"] = spotify_factory

    def _recover_stale_spotify_job() -> bool:
        """Recover only a dead in-process worker whose durable run still owns the workspace."""
        memory = spotify_job.snapshot()
        if not memory.get("running") or not memory.get("worker_known") or memory.get("worker_alive"):
            return False
        workspace = memory.get("workspace_id")
        run_id = str(memory.get("run_id") or "")
        if workspace is None or not run_id:
            return False
        db = Database(config.database_path)
        try:
            db.recover_discovery_run(workspace, run_id)
        finally:
            db.close()
        return spotify_job.recover_stale(run_id)

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

    def _get_soundcloud_oauth() -> Any:
        nonlocal soundcloud_oauth
        with auth_lock:
            if soundcloud_oauth is not None:
                return soundcloud_oauth
            if not getattr(config.soundcloud, "oauth_configured", False):
                raise RuntimeError("SoundCloud OAuth is not configured")
            soundcloud_oauth = SoundCloudOAuth(
                config.soundcloud.effective_client_id,
                config.soundcloud.effective_client_secret,
                config.soundcloud.redirect_uri_value,
                scopes=tuple(config.soundcloud.scopes),
                store=secure_store,
                timeout=float(config.network.timeout_seconds or 20),
            )
            return soundcloud_oauth

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
        row = _account_row()
        if row is None:
            return False
        owner = auth_client
        token = (
            getattr(owner, "token", None) or getattr(owner, "_token", None)
            if owner is not None
            else None
        )
        if token is not None:
            return True
        if _secure_refresh_token_available(owner):
            return True
        return bool(auth_authenticated and spotify_client is not None)

    def _verified_spotify_client() -> Any:
        """Return the client only after its OAuth token is usable."""
        client = _get_spotify_client()
        oauth = getattr(client, "_oauth", None) or auth_client
        token_method = getattr(oauth, "access_token", None) or getattr(
            oauth, "get_access_token", None
        )
        if callable(token_method):
            token_method()
        return client

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
        with soundcloud_import_job.lock:
            if soundcloud_import_job.running:
                running.append("SoundCloud import")
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
            requested = _SPOTIFY_WORKSPACE_KEY
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

    def _scan_summary(row: Any) -> dict[str, Any]:
        data = _row_dict(row)
        metadata = _json_object(data.get("metadata_json"), {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        stats = metadata.get("stats", {})
        if not isinstance(stats, Mapping):
            stats = {}

        def count(name: str) -> int:
            try:
                return int(stats.get(name, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "id": data.get("id"),
            "scan_key": str(data.get("scan_key") or ""),
            "kind": str(data.get("kind") or "exact"),
            "status": str(data.get("status") or "created"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "partial": bool(metadata.get("partial") or stats.get("partial")),
            "message": str(metadata.get("error") or ""),
            "stats": dict(stats),
            "total_queries": count("total_queries"),
            "completed_queries": count("completed_queries"),
            "failed_queries": count("failed_queries"),
            "total_tracks": count("total_tracks"),
            "catalog_eligible_tracks": count("catalog_eligible_tracks"),
            "catalog_unavailable_tracks": count("catalog_unavailable_tracks"),
            "completed_tracks": count("completed_tracks"),
            "exact_candidates": count("exact_candidates"),
            "exact_eligible": count("exact_eligible"),
            "exact_recommended": count("exact_recommended"),
            "expansion_artists_requested": count("expansion_artists_requested"),
            "expansion_artists_total": count("expansion_artists_total")
            or count("expansion_artists_requested"),
            "expansion_artists_scanned": count("expansion_artists_scanned"),
            "expansion_artists_completed": count("expansion_artists_completed")
            or count("expansion_artists_scanned"),
            "expansion_songs_total": count("expansion_songs_total"),
            "expansion_catalog_unavailable_songs": count("expansion_catalog_unavailable_songs"),
            "expansion_songs_with_charts": count("expansion_songs_with_charts"),
            "expansion_songs_without_charts": count("expansion_songs_without_charts"),
            "expansion_candidates": count("expansion_candidates"),
            "expansion_eligible": count("expansion_eligible"),
        }

    def _scan_history(db: Database, workspace: int | str) -> list[dict[str, Any]]:
        summaries = [_scan_summary(row) for row in db.get_discovery_scans(workspace)]
        return sorted(
            summaries,
            key=lambda item: float(item.get("started_at") or item.get("finished_at") or 0),
        )

    def _expansion_groups_payload(
        db: Database,
        workspace: int | str,
    ) -> list[dict[str, Any]]:
        artist_rows = {
            str(row["artist_norm"]): row
            for row in db.get_workspace_expansion_artists(workspace)
        }
        groups: dict[str, dict[str, Any]] = {}
        for row in db.get_workspace_candidates(
            workspace,
            kind="expansion",
            include_rejected=True,
        ):
            data = _row_dict(row)
            artist_norm = str(data.get("artist_norm") or "")
            artist_row = artist_rows.get(artist_norm)
            candidate_metadata = _json_object(data.get("metadata_json"), {})
            if not isinstance(candidate_metadata, Mapping):
                candidate_metadata = {}
            song_title = str(candidate_metadata.get("discography_song_title") or "").strip()
            song_artist = str(candidate_metadata.get("discography_song_artist") or "").strip()
            song_album = str(candidate_metadata.get("spotify_album") or "").strip()
            group_key = (
                f"{artist_norm}::{normalize_title(song_title)}"
                if song_title
                else artist_norm
            )
            group = groups.setdefault(
                group_key,
                {
                    "id": group_key,
                    "kind": "expansion",
                    "artist_norm": artist_norm,
                    "artist_name": str(
                        artist_row["artist_name"]
                        if artist_row
                        else data.get("track_artist") or ""
                    ),
                    "song_artist": song_artist,
                    "song_title": song_title,
                    "album": song_album,
                    "configured_cap": int(artist_row["cap"] or 0) if artist_row else 0,
                    "candidates": [],
                    "candidate_count": 0,
                    "eligible_count": 0,
                    "selected_count": 0,
                },
            )
            candidate = _candidate_payload(row)
            group["candidates"].append(candidate)
            group["candidate_count"] += 1
            if str(candidate.get("eligibility") or "").upper() in {
                "RECOMMENDED",
                "ELIGIBLE_REVIEW",
            }:
                group["eligible_count"] += 1
            if candidate.get("selected"):
                group["selected_count"] += 1

        for group in groups.values():
            group["candidates"].sort(
                key=lambda item: (
                    -float(item.get("score") or 0),
                    -float(item.get("quality") or 0),
                    str(item.get("title") or "").casefold(),
                )
            )
        return sorted(groups.values(), key=lambda item: str(item["artist_name"]).casefold())

    def _expansion_summary_payload(
        db: Database,
        workspace: int | str,
        groups: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Count unique expansion songs separately from chart variants."""

        def song_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
            metadata = candidate.get("metadata")
            if isinstance(metadata, Mapping):
                target_artist = metadata.get("discography_song_artist")
                target_title = metadata.get("discography_song_title")
                if target_artist and target_title:
                    return normalize_artist(str(target_artist)), normalize_title(str(target_title))
            return (
                normalize_artist(str(candidate.get("artist") or "")),
                normalize_title(str(candidate.get("title") or candidate.get("name") or "")),
            )

        exact_songs = {
            song_key(_candidate_payload(row))
            for row in db.get_workspace_candidates(
                workspace,
                kind="exact",
                include_rejected=True,
            )
        }
        expansion_songs = {
            song_key(candidate)
            for group in groups
            for candidate in group["candidates"]
        }
        expansion_songs.discard(("", ""))
        preview_songs: set[tuple[str, str]] = set()
        preview_full_count = 0
        preview_owned_count = 0
        preview_errors = 0
        for row in db.get_workspace_expansion_artists(workspace, selected_only=True):
            metadata = _json_object(row["metadata_json"], {})
            preview = metadata.get("discography_preview", {}) if isinstance(metadata, Mapping) else {}
            if not isinstance(preview, Mapping):
                continue
            preview_full_count += int(preview.get("catalog_song_count", 0) or 0)
            preview_owned_count += int(preview.get("already_in_library", 0) or 0)
            preview_errors += bool(preview.get("error"))
            for item in preview.get("tracks", []) if isinstance(preview.get("tracks", []), list) else []:
                if not isinstance(item, Mapping):
                    continue
                key = (
                    normalize_artist(str(item.get("artist") or "")),
                    normalize_title(str(item.get("title") or "")),
                )
                if key[0] and key[1]:
                    preview_songs.add(key)
        return {
            "groups": len(groups),
            "candidates": sum(group["candidate_count"] for group in groups),
            "unique_songs": len(expansion_songs),
            # An artist-expansion duplicate of an exact match is a chart
            # variant, not a newly discovered song for the user.
            "new_songs": len(expansion_songs - exact_songs),
            "eligible": sum(group["eligible_count"] for group in groups),
            "selected": sum(group["selected_count"] for group in groups),
            "searched_songs": len(preview_songs) or len({
                (
                    normalize_artist(str(item.get("song_artist") or "")),
                    normalize_title(str(item.get("song_title") or "")),
                )
                for item in groups
                if item.get("song_artist") and item.get("song_title")
            }),
            "preview_songs": len(preview_songs),
            "preview_full_count": preview_full_count,
            "preview_owned_count": preview_owned_count,
            "preview_errors": preview_errors,
        }

    def _workspace_status(value: Any = None) -> dict[str, Any]:
        _recover_stale_spotify_job()
        memory = spotify_job.snapshot()
        workspace_id = value if value not in (None, "") else memory.get("workspace_id") or "spotify"
        db: Database | None = None
        scan_history: list[dict[str, Any]] = []
        expansion_batch: dict[str, Any] | None = None
        try:
            db = Database(config.database_path)
            row = db.get_workspace(workspace_id)
            progress_row = db.get_workspace_progress(workspace_id) if row is not None else None
            if row is not None:
                scan_history = _scan_history(db, int(row["id"]))
                expansion_batch = _expansion_batch_payload(db, int(row["id"]))
        except (ValueError, OSError, sqlite3.OperationalError):
            row = None
            progress_row = None
        finally:
            if db is not None:
                db.close()
        if row is not None:
            workspace_id = int(row["id"])
        persisted = _row_dict(progress_row)
        progress_metadata = _json_object(persisted.get("metadata_json"), {})
        if not isinstance(progress_metadata, Mapping):
            progress_metadata = {}
        progress = {
            "total_tasks": int(persisted.get("total_tasks", row["total_tasks"] if row else 0) or 0),
            "completed_tasks": int(persisted.get("completed_tasks", row["completed_tasks"] if row else 0) or 0),
            "failed_tasks": int(persisted.get("failed_tasks", row["failed_tasks"] if row else 0) or 0),
            "total_tracks": int(persisted.get("total_tracks", row["total_tracks"] if row else 0) or 0),
            "completed_tracks": int(persisted.get("completed_tracks", row["completed_tracks"] if row else 0) or 0),
        }
        progress["done"] = progress["completed_tasks"]
        progress["total"] = progress["total_tasks"]
        active = bool(
            memory.get("running")
            and (row is None or int(memory.get("workspace_id") or 0) == int(workspace_id))
        )
        progress.update({
            key: value
            for key, value in memory.get("progress", {}).items()
            if active and value is not None
})
        if not active:
            for key in (
                "expansion_artists_total",
                "expansion_artists_completed",
                "cached_queries",
                "estimated_cached_queries",
            ):
                if key in progress_metadata:
                    try:
                        progress[key] = int(progress_metadata[key] or 0)
                    except (TypeError, ValueError):
                        pass
        if not active and row is not None:
            metadata = _json_object(row["metadata_json"] if "metadata_json" in row.keys() else "", {})
            stats = metadata.get("stats", {}) if isinstance(metadata, Mapping) else {}
            if isinstance(stats, Mapping):
                scoring_total = int(stats.get("scoring_tasks_total", 0) or 0)
                scoring_done = int(stats.get("scoring_tasks_completed", 0) or 0)
                cached_done = int(stats.get("cached_queries", 0) or 0)
                estimated_cached = int(stats.get("total_queries", 0) or 0) - int(stats.get("network_queries", 0) or 0)
                progress.update({
                    "scoring_tasks_total": scoring_total,
                    "scoring_tasks_completed": scoring_done,
                    "cached_queries": cached_done,
                    "estimated_cached_queries": max(cached_done, estimated_cached),
                    "total_work": progress["total_tasks"] + scoring_total,
                    "completed_work": progress["completed_tasks"] + scoring_done,
                    "expansion_songs_total": int(stats.get("expansion_songs_total", 0) or 0),
                    "expansion_songs_with_charts": int(stats.get("expansion_songs_with_charts", 0) or 0),
                    "expansion_songs_without_charts": int(stats.get("expansion_songs_without_charts", 0) or 0),
                    "remaining_work": max(
                        0,
                        progress["total_tasks"] + scoring_total
                        - progress["completed_tasks"] - scoring_done,
                    ),
                })
        result = dict(memory)
        workspace_metadata = _json_object(row["metadata_json"] if row else "", {})
        if not isinstance(workspace_metadata, Mapping):
            workspace_metadata = {}
        persisted_status = str(row["status"] if row else "created")
        if not active and persisted_status == "running":
            persisted_status = "interrupted"
        result.update({
            "running": active,
            "workspace_id": workspace_id if row is not None or memory.get("workspace_id") else None,
            "workspace_key": str(row["workspace_key"] if row else memory.get("workspace_key", "spotify")),
            "status": str(memory.get("status") if active else persisted_status),
            "phase": str(persisted.get("phase") or (row["phase"] if row else memory.get("phase", "idle"))),
            "progress": progress,
            "message": _safe_message(
                memory.get("message", "") if active else persisted.get("message") or ""
            ),
            "error": _safe_message(
                memory.get("error", "") if active else persisted.get("error") or ""
            ),
            "partial": bool((memory.get("partial") if active else False) or workspace_metadata.get("partial", False)),
            "scan_history": scan_history,
        })
        current_run_id = ""
        if row is not None:
            try:
                db = Database(config.database_path)
                current_run_id = db.get_meta("local_current_run_id")
            finally:
                if db is not None:
                    db.close()
        if current_run_id:
            try:
                artifact = diagnostics_path(config.state_path, current_run_id)
                if artifact.is_file():
                    result["diagnostics"] = json.loads(artifact.read_text(encoding="utf-8"))
                    result["diagnostics_url"] = f"/api/diagnostics/{current_run_id}"
                    result["diagnostics_file"] = str(artifact)
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                pass
        heartbeat_at = persisted.get("updated_at") if persisted else None
        if expansion_batch is not None:
            heartbeat_at = expansion_batch.get("heartbeat_at") or heartbeat_at
        result["heartbeat_at"] = heartbeat_at
        result["last_activity_at"] = memory.get("last_activity_at") if active else heartbeat_at
        result["recovered"] = bool(workspace_metadata.get("interrupted")) and not active
        persisted_scan_key = str(
            workspace_metadata.get("scan_key")
            or progress_metadata.get("scan_key")
            or ("inventory" if str(result["phase"]).startswith(("spotify_inventory", "ready_for_scope")) else "")
        )
        latest_scan = scan_history[-1] if scan_history else {}
        batch_relevant = bool(
            expansion_batch
            and (
                str(memory.get("scan_key") or "").startswith("expansion")
                or persisted_scan_key.startswith("expansion")
            )
        )
        if batch_relevant and expansion_batch is not None:
            progress.update({
                "expansion_artists_total": int(expansion_batch.get("total_artists") or 0),
                "expansion_artists_completed": int(expansion_batch.get("completed_artists") or 0),
                "expansion_artists_failed": int(expansion_batch.get("failed_artists") or 0),
                "expansion_artists_skipped": int(expansion_batch.get("skipped_artists") or 0),
                "network_requests": int(expansion_batch.get("network_requests") or 0),
                "current_artist": str(expansion_batch.get("current_artist") or ""),
            })
            if not active:
                result["status"] = str(expansion_batch.get("status") or result["status"])
                result["phase"] = str(expansion_batch.get("phase") or result["phase"])
                result["run_id"] = str(expansion_batch.get("run_id") or result.get("run_id") or "")
                result["error"] = _safe_message(expansion_batch.get("error") or result.get("error") or "")
            result["expansion_batch"] = expansion_batch
            result["expansion_artist_errors"] = [
                {"artist_name": item.get("artist_name"), "artist_norm": item.get("artist_norm"), "error": item.get("error")}
                for item in expansion_batch.get("artists", [])
                if item.get("error")
            ]
        result["scan_key"] = (
            str(memory.get("scan_key") or "exact")
            if active
            else persisted_scan_key or str(latest_scan.get("scan_key") or "exact")
        )
        result["run_id"] = str(
            memory.get("run_id") if active else workspace_metadata.get("run_id") or progress_metadata.get("run_id") or ""
        )
        if batch_relevant and expansion_batch is not None and not active:
            result["run_id"] = str(expansion_batch.get("run_id") or result["run_id"])
        if not active and latest_scan:
            result["started_at"] = latest_scan.get("started_at")
            result["finished_at"] = latest_scan.get("finished_at")
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
                "candidate_artist": data.get("artist", ""),
                "candidate_title": data.get("title", ""),
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
            "candidate_artist": str(getattr(candidate, "artist", "") or ""),
            "candidate_title": str(getattr(candidate, "name", "") or ""),
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
            "breakdown": dict(breakdown),
            "base_score": float(
                breakdown.get("base_score")
                or _json_object(breakdown.get("factors"), {}).get("score", {}).get("base", 0)
                or 0
            ),
            "bonuses": list(breakdown.get("bonuses") or []),
            "penalties": list(breakdown.get("penalties") or []),
            "gate_factors": dict(breakdown.get("gate_factors") or {}),
            "quality_factors": dict(breakdown.get("quality_factors") or {}),
            "selected": bool(data.get("selected")),
            "kind": str(data.get("kind") or "exact"),
            "artist_norm": str(data.get("artist_norm") or ""),
            "metadata": _json_object(data.get("metadata_json"), {}),
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
            all_workspace_rows = db.get_library_track_rows(workspace_id=workspace)
            matched_track_ids = {
                int(item["track_id"])
                for item in db.get_workspace_candidates(
                    workspace,
                    kind="exact",
                    include_rejected=False,
                )
                if item["track_id"] is not None
            }
            owned_track_ids = {
                int(item["id"])
                for item in all_workspace_rows
                if bool(item["already_owned"])
            }
            expansion_groups = _expansion_groups_payload(db, workspace)
            expansion_summary = _expansion_summary_payload(db, workspace, expansion_groups)
            scan_history = _scan_history(db, workspace)
            exact_scans = [item for item in scan_history if item.get("kind") == "exact"]
            exact_summary = exact_scans[-1] if exact_scans else {}
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
            "expansion_groups": expansion_groups,
            "scan_history": scan_history,
            "expansion_summary": expansion_summary,
            "discovery_summary": {
                "total_songs": len(all_workspace_rows),
                "matched_songs": len(matched_track_ids | owned_track_ids),
                "catalog_eligible_songs": int(exact_summary.get("catalog_eligible_tracks") or 0),
                "catalog_unavailable_songs": int(exact_summary.get("catalog_unavailable_tracks") or 0),
            },
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
        catalog_sources: list[ChartSource] | None = None,
    ) -> tuple[bool, str]:
        data = _row_dict(row)
        if kind == "exact":
            owns_catalog_sources = catalog_sources is None
            if owns_catalog_sources:
                catalog_sources = list(source_factory(config))
            try:
                relevant = [
                    source
                    for source in catalog_sources
                    if str(getattr(source, "name", "")) == str(data.get("source") or "")
                ]
                chart_memberships = [
                    source_catalog_contains_chart(
                        source,
                        int(data.get("chart_id")),
                        source_name=str(data.get("source") or ""),
                    )
                    for source in relevant
                    if data.get("chart_id") not in (None, "")
                ]
                if chart_memberships and any(value is True for value in chart_memberships):
                    pass
                elif chart_memberships and all(value is False for value in chart_memberships):
                    return False, "song is unavailable in the local chart catalog"
                else:
                    memberships = [
                        source_catalog_available(
                            source,
                            str(data.get("track_artist") or ""),
                            str(data.get("track_title") or ""),
                        )
                        for source in relevant
                    ]
                    if memberships and all(value is False for value in memberships):
                        return False, "song is unavailable in the local chart catalog"
            finally:
                if owns_catalog_sources:
                    for source in catalog_sources or ():
                        try:
                            source.close()
                        except Exception:
                            pass
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
        saver = getattr(oauth, "save_token", None)
        if callable(saver) and not isinstance(token, Mapping):
            saver(token)
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
        expansion_batch_id: int | None = None
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        busy = _busy_response()
        if busy is not None:
            return busy
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a Spotify discovery job is already running"}, 409
        job_client: Any = None
        try:
            workspace = _workspace_id(payload.get("workspace_id"), required=False)
            if workspace is None:
                db = Database(config.database_path)
                try:
                    workspace = db.create_workspace(
                        workspace_key=_SPOTIFY_WORKSPACE_KEY,
                        name="Spotify library",
                        kind="exact",
                        status="created",
                        phase="idle",
                    )
                finally:
                    db.close()
            workspace = int(workspace)
            db = Database(config.database_path)
            try:
                scope = db.get_workspace_scope_data(workspace)
            finally:
                db.close()
            if kind == "exact":
                if scope is None or (not scope.get("playlist_ids") and not scope.get("include_liked")):
                    return {"error": "choose at least one Spotify playlist or enable Liked Songs first"}, 400
            if not _is_authenticated():
                return {"error": "sign in with Spotify first"}, 401
            job_client = _verified_spotify_client()
            selected_norms = payload.get("artist_norms") or payload.get("artists") or None
            if kind == "expansion":
                db = Database(config.database_path)
                try:
                    all_artist_rows = db.get_workspace_expansion_artists(workspace)
                    artists_by_norm = {
                        str(row["artist_norm"]): row for row in all_artist_rows
                    }
                    if selected_norms is not None:
                        requested_values = (
                            [selected_norms]
                            if isinstance(selected_norms, (str, bytes, Mapping))
                            else list(selected_norms)
                        )
                        requested_norms: list[str] = []
                        for value in requested_values:
                            if isinstance(value, Mapping):
                                raw_norm = value.get("artist_norm") or value.get("artist") or value.get("name")
                                if value.get("selected") is False or value.get("enabled") is False:
                                    continue
                            else:
                                raw_norm = value
                            norm = normalize_artist(str(raw_norm or "").strip())
                            if norm and norm not in requested_norms:
                                requested_norms.append(norm)
                        invalid = [norm for norm in requested_norms if norm not in artists_by_norm]
                        if invalid:
                            raise ValueError("artist is not a primary artist in the Spotify library")
                        selected_rows = [artists_by_norm[norm] for norm in requested_norms]
                    else:
                        selected_rows = [
                            row for row in all_artist_rows if bool(row["selected"])
                        ]
                finally:
                    db.close()
                if not selected_rows:
                    return {"error": "select at least one expansion artist first"}, 400
                if len(selected_rows) > config.expansion.max_artists_per_batch:
                    return {
                        "error": f"artist expansion batches are limited to {config.expansion.max_artists_per_batch} primary artists; received {len(selected_rows)}"
                    }, 400
                if any(int(row["cap"] or 0) <= 0 for row in selected_rows):
                    return {"error": "every expansion artist must be eligible for expansion"}, 400
                missing_preview = []
                successful_preview = []
                for row in selected_rows:
                    metadata = _json_object(row["metadata_json"], {})
                    preview = metadata.get("discography_preview") if isinstance(metadata, Mapping) else None
                    if not isinstance(preview, Mapping) or "discovered_at" not in preview:
                        missing_preview.append(str(row["artist_name"] or row["artist_norm"]))
                    elif not preview.get("error"):
                        successful_preview.append(str(row["artist_norm"]))
                if missing_preview:
                    return {
                        "error": "build the Spotify discography preview before searching Enchor",
                        "missing_preview_artists": missing_preview,
                    }, 409
                if not successful_preview:
                    return {"error": "at least one selected artist needs a successful Spotify preview"}, 409
        except SpotifyError as exc:
            return {"error": _safe_message(exc, _oauth_secret_values())}, 401
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 400

        with spotify_job.lock:
            scan_key = "expansion" if kind == "expansion" else "exact"
            spotify_job.begin(workspace, scan_key=scan_key)
            run_id = spotify_job.run_id
            spotify_cancel_event = threading.Event()
            active_cancel_event = spotify_cancel_event
        status_db = Database(config.database_path)
        try:
            if kind == "expansion":
                selected_payload = [_row_dict(row) for row in selected_rows]
                fingerprint = _expansion_selection_fingerprint(selected_payload)
                batch = status_db.get_expansion_batch(workspace_id=workspace)
                if batch is not None and str(batch["selection_fingerprint"]) == fingerprint:
                    batch = status_db.resume_expansion_batch(
                        int(batch["id"]),
                        run_id=run_id,
                        phase="search",
                        retry_failed=bool(payload.get("retry_failed")),
                    )
                    expansion_batch_id = int(batch["id"])
                else:
                    expansion_batch_id = status_db.create_expansion_batch(
                        workspace,
                        selected_payload,
                        run_id=run_id,
                        selection_fingerprint=fingerprint,
                        status="running",
                        phase="search",
                    )
                    for row in selected_rows:
                        metadata = _json_object(row["metadata_json"], {})
                        preview = metadata.get("discography_preview", {}) if isinstance(metadata, Mapping) else {}
                        status_db.checkpoint_expansion_batch_artist(
                            expansion_batch_id,
                            str(row["artist_norm"]),
                            phase="preview",
                            status="failed" if preview.get("error") else "complete",
                            error=str(preview.get("error") or "") or None,
                        )
                batch_rows = status_db.get_expansion_batch_artists(expansion_batch_id)
                selected_norms = [
                    str(row["artist_norm"])
                    for row in batch_rows
                    if str(row["search_status"]) in {"pending", "running", "interrupted"}
                    or (bool(payload.get("retry_failed")) and str(row["search_status"]) == "failed")
                ]
            run_metadata = {
                "workflow": "spotify_discovery",
                "scan_key": scan_key,
                "run_id": run_id,
                "expansion_batch_id": expansion_batch_id,
            }
            status_db.update_workspace(
                workspace,
                status="running",
                phase="expansion_starting" if kind == "expansion" else "starting",
                metadata=run_metadata,
            )
            status_db.update_workspace_progress(
                workspace,
                status="running",
                phase="expansion_starting" if kind == "expansion" else "starting",
                message=(
                    "Starting artist expansion..."
                    if kind == "expansion"
                    else "Starting Spotify discovery..."
                ),
                error="",
                metadata={
                    **run_metadata,
                    "expansion_artists_total": 0,
                    "expansion_artists_completed": 0,
                },
            )
        finally:
            status_db.close()

        def worker() -> None:
            nonlocal spotify_cancel_event
            db = Database(config.database_path)
            sources: list[ChartSource] = []
            soundcloud_client: Any = None
            result: Any = None
            try:
                client = job_client

                def on_progress(value: Any) -> None:
                    if isinstance(value, Mapping):
                        spotify_job.progress_update(value)

                def on_log(value: Any, *_args: Any) -> None:
                    spotify_job.log(_safe_message(value, _oauth_secret_values()))

                workspace_row = db.get_workspace(workspace)
                workspace_metadata = _json_object(workspace_row["metadata_json"], {}) if workspace_row is not None else {}
                combined_scope = workspace_metadata.get("soundcloud_scope")
                if kind == "exact" and isinstance(combined_scope, Mapping) and (
                    combined_scope.get("playlist_ids") or combined_scope.get("include_liked")
                ):
                    from csv_to_clonehero.combined_pipeline import discover_combined
                    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
                    from csv_to_clonehero.providers.soundcloud import _track_from_mapping
                    from csv_to_clonehero.providers.soundcloud_api import SoundCloudAPIProvider, SoundCloudClient

                    spotify_tracks = [
                        ProviderTrack(
                            provider="spotify", provider_id=track.spotify_id,
                            title=track.title, artist=track.artist_full, album=track.album,
                            playlist_names=list(track.playlists), duration_ms=track.duration_ms,
                            isrc=track.isrc, url=track.spotify_url,
                        )
                        for track in db.get_library_tracks(workspace_id=workspace)
                    ]
                    soundcloud_client = SoundCloudClient(_get_soundcloud_oauth(), timeout=float(config.network.timeout_seconds or 20))
                    soundcloud_snapshot = SoundCloudAPIProvider(soundcloud_client).import_library(scope=dict(combined_scope))
                    result = discover_combined(
                        config, db,
                        [LibrarySnapshot("spotify", spotify_tracks), soundcloud_snapshot],
                        workspace_key=_SPOTIFY_WORKSPACE_KEY,
                        workspace_name="Combined library",
                    )
                elif kind == "expansion" and isinstance(combined_scope, Mapping) and (
                    combined_scope.get("playlist_ids") or combined_scope.get("include_liked")
                ):
                    from csv_to_clonehero.combined_pipeline import expand_combined
                    result = expand_combined(config, db, workspace, selected_norms or [])
                elif kind == "expansion":
                    sources = list(source_factory(config))
                    norms = selected_norms
                    if isinstance(norms, str):
                        norms = [norms]
                    result = run_expansion_discovery(
                        config,
                        db,
                        workspace,
                        sources,
                        selected_artist_norms=list(norms) if norms is not None else None,
                        expansion_batch_id=expansion_batch_id,
                        cancellation_event=active_cancel_event,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                else:
                    sources = list(source_factory(config))
                    library_result = None
                    if client is None:
                        tracks = db.get_library_tracks(workspace_id=workspace)
                        if tracks:
                            library_result = tracks
                    result = run_spotify_discovery(
                        config,
                        db,
                        sources,
                        client,
                        workspace_id=workspace,
                        include_liked=bool(payload.get("include_liked", True)),
                        library_result=library_result,
                        cancellation_event=active_cancel_event,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                spotify_job.finish(result)
                if kind == "expansion" and expansion_batch_id is not None:
                    db.update_expansion_batch(
                        expansion_batch_id,
                        status=str(getattr(result, "status", "complete") or "complete"),
                        phase="search_complete" if str(getattr(result, "status", "")) != "cancelled" else "search",
                        current_artist="",
                        error=str(getattr(result, "error", "") or "") or None,
                    )
                try:
                    _save_workspace_snapshot(workspace)
                except Exception:
                    log.warning("Could not write automatic Spotify workspace snapshot", exc_info=True)
            except Exception as exc:  # noqa: BLE001 - surfaced through status
                message = _safe_message(exc, _oauth_secret_values())
                log.error("Spotify discovery job failed")
                try:
                    if kind == "expansion" and expansion_batch_id is not None:
                        db.update_expansion_batch(expansion_batch_id, status="failed", phase="search", error=message)
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
                if soundcloud_client is not None:
                    try:
                        soundcloud_client.close()
                    except Exception:
                        pass
                db.recover_discovery_run(workspace, run_id)
                spotify_job.cleanup(run_id, threading.current_thread())
                db.close()

        worker_thread = threading.Thread(target=worker, daemon=True, name=f"spotify-{run_id}")
        with spotify_job.lock:
            spotify_job.attach_worker(run_id, worker_thread)
            worker_thread.start()
        return {
            "started": True,
            "job": "spotify-discovery",
            "kind": kind,
            "workspace_id": workspace,
            "workspace_key": "spotify",
            "scan_key": "expansion" if kind == "expansion" else "exact",
            "run_id": run_id,
            "batch_id": expansion_batch_id,
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
            access_token = getattr(oauth, "access_token", None) or getattr(
                oauth, "get_access_token", None
            )
            if callable(access_token):
                access_token()
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
                # Do not retain a client created before this OAuth exchange.
                # The next request must use the newly persisted token.
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

    # ------------------------------------------------------- SoundCloud auth
    @app.get("/api/auth/soundcloud/status")
    def soundcloud_auth_status():
        configured = bool(getattr(config.soundcloud, "oauth_configured", False))
        authenticated = False
        if configured and secure_store is not None:
            try:
                getter = getattr(secure_store, "get_password", None)
                service = "soundcloud"
                username = "soundcloud"
                authenticated = bool(getter(service, username)) if callable(getter) else False
            except Exception:
                authenticated = False
        return jsonify({"configured": configured, "authenticated": authenticated, "export_available": bool(config.soundcloud.allow_user_exports)})

    @app.get("/api/auth/soundcloud/start")
    def soundcloud_auth_start():
        nonlocal pending_soundcloud_authorization
        if not _loopback_request():
            return {"error": "SoundCloud sign-in must be started on the host computer"}, 403
        try:
            oauth = _get_soundcloud_oauth()
            request_obj = oauth.begin_authorization()
            with auth_lock:
                pending_soundcloud_authorization = {"oauth": oauth, "state": request_obj.state, "request": request_obj, "created_at": time.time()}
            return jsonify({"authorization_url": request_obj.url, "url": request_obj.url, "state": request_obj.state, "redirect_uri": request_obj.redirect_uri})
        except Exception as exc:
            return {"error": _safe_message(exc)}, 503

    @app.get("/api/auth/soundcloud/callback")
    def soundcloud_auth_callback():
        nonlocal pending_soundcloud_authorization
        pending = pending_soundcloud_authorization
        if pending is None or time.time() - float(pending.get("created_at", 0)) > 600:
            return {"error": "SoundCloud OAuth state expired; start sign-in again."}, 400
        if request.args.get("error"):
            pending_soundcloud_authorization = None
            return {"error": "SoundCloud sign-in was not completed."}, 400
        if not hmac.compare_digest(str(pending.get("state", "")), str(request.args.get("state", ""))):
            return {"error": "SoundCloud OAuth state validation failed; start sign-in again."}, 400
        try:
            pending["oauth"].exchange_code(
                str(request.args.get("code", "")),
                returned_state=str(request.args.get("state", "")),
                expected_state=str(pending["state"]),
                code_verifier=pending["request"].code_verifier,
            )
            pending_soundcloud_authorization = None
            return redirect("/", code=303)
        except Exception:
            pending_soundcloud_authorization = None
            return {"error": "SoundCloud sign-in failed."}, 400

    @app.post("/api/auth/soundcloud/logout")
    def soundcloud_auth_logout():
        nonlocal soundcloud_oauth
        try:
            if soundcloud_oauth is not None:
                soundcloud_oauth.logout()
        finally:
            soundcloud_oauth = None
        return jsonify({"ok": True, "authenticated": False})

    @app.get("/api/soundcloud/scope")
    def soundcloud_scope():
        try:
            client = SoundCloudClient(_get_soundcloud_oauth(), timeout=float(config.network.timeout_seconds or 20))
            try:
                playlists = client.playlists()
                liked = len(client.liked_tracks())
                return jsonify({"playlists": playlists, "liked_count": liked, "configured": True})
            finally:
                client.close()
        except Exception as exc:
            return {"error": _safe_message(exc) or "SoundCloud is unavailable."}, 503

    @app.post("/api/soundcloud/scope")
    def save_soundcloud_scope():
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        payload = request.get_json(silent=True) or {}
        playlist_ids = sorted({str(value).strip() for value in payload.get("playlist_ids", []) if str(value).strip()})
        include_liked = bool(payload.get("include_liked"))
        if not playlist_ids and not include_liked:
            return {"error": "choose a SoundCloud playlist or Liked Tracks first"}, 400
        workspace = _workspace_id(payload.get("workspace_id"), required=False)
        if workspace is None:
            return {"error": "choose a Spotify workspace first"}, 400
        db = Database(config.database_path)
        try:
            row = db.get_workspace(workspace)
            metadata = _json_object(row["metadata_json"], {}) if row is not None else {}
            metadata["soundcloud_scope"] = {"playlist_ids": playlist_ids, "include_liked": include_liked}
            db.update_workspace(workspace, metadata=metadata)
            return jsonify({"ok": True, "scope": metadata["soundcloud_scope"]})
        finally:
            db.close()

    # -------------------------------------------------------- Spotify discovery
    def _start_local_catalog_job(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        busy = _busy_response()
        if busy is not None:
            return busy
        workspace, workspace_key = _get_local_library_workspace()
        discovery_run_id = ""
        db = Database(config.database_path)
        try:
            discovery_run_id = db.get_meta("local_current_run_id")
        finally:
            db.close()
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a discovery job is already running"}, 409
            spotify_job.begin(workspace, workspace_key=workspace_key, scan_key="catalog", run_id=discovery_run_id or None)
            run_id = spotify_job.run_id

        def worker() -> None:
            db = Database(config.database_path)
            diagnostics = None
            try:
                from csv_to_clonehero.combined_pipeline import discover_combined
                from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
                grouped: dict[str, list[ProviderTrack]] = {}
                for row in db.get_track_provenance(workspace):
                    provider = str(row["provider"])
                    grouped.setdefault(provider, []).append(ProviderTrack(
                        provider=provider, provider_id=str(row["provider_id"]), title=str(row["title"]),
                        artist=str(row["artist"]), album=str(row["album"] or ""), liked=bool(row["liked"]),
                        isrc=str(row["isrc"] or ""), url=str(row["url"] or ""),
                        metadata=_json_object(row["metadata_json"], {}),
                    ))
                last_persisted = 0.0
                def on_progress(value: Mapping[str, Any]) -> None:
                    nonlocal last_persisted
                    payload = dict(value)
                    spotify_job.progress_update(payload)
                    now = time.monotonic()
                    if now - last_persisted >= 0.25 or payload.get("phase") == "complete":
                        db.update_workspace_progress(
                            workspace,
                            status="running",
                            phase=str(payload.get("phase") or "finding_charts"),
                            message=str(payload.get("message") or "Matching your songs..."),
                            error="",
                            total_tracks=int(payload.get("total_tracks", 0) or 0),
                            completed_tracks=int(payload.get("processed_tracks", 0) or 0),
                            metadata={"run_id": spotify_job.run_id, "scan_key": "catalog", **payload},
                        )
                        last_persisted = now
                on_progress({"phase": "combine", "message": "Combining imported sources locally...", "total_tracks": sum(map(len, grouped.values())), "processed_tracks": 0})
                run_id = spotify_job.run_id
                try:
                    diagnostics = RunDiagnostics(config.state_path, run_id)
                    diagnostics.update("source_setup", workspace_id=workspace, workspace_key=workspace_key)
                    db.set_meta("local_current_run_id", run_id)
                except OSError:
                    diagnostics = None
                result = discover_combined(config, db, [LibrarySnapshot(name, tracks) for name, tracks in grouped.items()], workspace_key=workspace_key, workspace_name="Local combined library", expansion_artists=payload.get("expansion_artists", ()), diagnostics=diagnostics, on_progress=on_progress)
                spotify_job.finish(result)
                db.update_workspace(workspace, status=result.status, phase=result.phase, message="Catalog matching complete.", error=result.error)
                db.update_workspace_progress(
                    workspace,
                    status=result.status,
                    phase=result.phase,
                    message="Catalog matching complete.",
                    error=result.error,
                    metadata={"run_id": run_id, "scan_key": "catalog"},
                )
                if diagnostics:
                    diagnostics.update("selection", counters={"selected_charts": 0})
                    diagnostics.update("export", counters={"export_attempts": 0, "exported_charts": 0})
                    diagnostics.finish(
                        result.status,
                        workspace_id=workspace,
                        workspace_key=workspace_key,
                        counters={
                            "exact_hits": result.exact_hits,
                            "exact_misses": result.exact_misses,
                            "artist_found_title_different": result.artist_found_title_different,
                            "title_found_artist_different": result.title_found_artist_different,
                            "broader_retrieval_tracks": result.broader_retrieval_tracks,
                            "zero_candidate_tracks": result.zero_candidate_tracks,
                            "tracks_with_candidates": result.tracks_with_candidates,
                            "candidate_charts_evaluated": result.candidate_charts_evaluated,
                            "unique_charts_evaluated": result.unique_charts_evaluated,
                            "strong_match_tracks": result.strong_match_tracks,
                            "review_match_tracks": result.review_match_tracks,
                            "hard_rejected_only_tracks": result.hard_rejected_only_tracks,
                            "soft_rejected_only_tracks": result.soft_rejected_only_tracks,
                            "no_plausible_tracks": result.no_plausible_tracks,
                            "candidate_charts_created": result.persisted_candidates,
                            "eligible_candidates": result.eligible_candidates,
                            "rejected_candidates": result.rejected_candidates,
                            "retrieval_seconds": round(result.retrieval_seconds, 3),
                            "scoring_seconds": round(result.scoring_seconds, 3),
                            "discovery_seconds": round(result.discovery_seconds, 3),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                message = _safe_message(exc)
                spotify_job.fail(message)
                try:
                    db.update_workspace(
                        workspace,
                        status="failed",
                        phase="catalog_match",
                        message="Matching stopped before it finished.",
                        error=message,
                    )
                    db.update_workspace_progress(
                        workspace,
                        status="failed",
                        phase="catalog_match",
                        message="Matching stopped before it finished.",
                        error=message,
                        metadata={"run_id": run_id, "scan_key": "catalog"},
                    )
                except Exception:
                    log.warning("Could not persist local matching failure", exc_info=True)
                if diagnostics:
                    try:
                        diagnostics.finish("failed", error=message)
                    except OSError:
                        log.warning("Could not persist local matching diagnostics", exc_info=True)
            finally:
                db.close()

        thread = threading.Thread(target=worker, daemon=True)
        spotify_job.attach_worker(run_id, thread)
        thread.start()
        return {"started": "catalog", "workspace_id": workspace, "workspace_key": workspace_key, "run_id": run_id}, 202

    @app.post("/api/spotify/scan")
    @app.post("/api/discovery/start")
    def spotify_scan_start():
        payload = request.get_json(silent=True) or {}
        if payload.get("source") in {"soundcloud", "combined", "local"} or payload.get("workspace_key", "").startswith("local:"):
            return _start_local_catalog_job(payload)
        return _start_spotify_job("exact", payload)

    @app.get("/api/spotify/status")
    @app.get("/api/spotify/scan/status")
    @app.get("/api/discovery/status")
    @app.get("/api/discovery/progress")
    def spotify_scan_status():
        payload = request.args.get("workspace_id")
        status = _workspace_status(payload)
        workspace_id = status.get("workspace_id")
        if workspace_id is not None:
            db = Database(config.database_path)
            try:
                local_key = db.get_meta("local_workspace_key")
                local_workspace = db.get_workspace(local_key) if local_key else None
                try:
                    requested_workspace_id = int(workspace_id)
                except (TypeError, ValueError):
                    requested_workspace_id = None
                if local_workspace is not None and requested_workspace_id == int(local_workspace["id"]):
                    status["journey"] = _local_journey_payload(db, int(workspace_id))
            finally:
                db.close()
        return jsonify(status)

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
                workspace_key=_SPOTIFY_WORKSPACE_KEY, name="Spotify library", kind="exact",
                status="created", phase="idle", metadata={"workflow": "spotify_staged"},
            ))
        finally:
            db.close()

    @app.post("/api/spotify/reset")
    @app.post("/api/spotify/workspace/reset")
    def spotify_workspace_reset():
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        payload = request.get_json(silent=True) or {}
        try:
            workspace = _workspace_id(payload.get("workspace_id"), required=False)
            db = Database(config.database_path)
            try:
                if workspace is None:
                    row = db.get_workspace(_SPOTIFY_WORKSPACE_KEY)
                    workspace = int(row["id"]) if row is not None else None
                if workspace is not None:
                    workspace = int(workspace)
                    db.clear_workspace_candidates(workspace)
                    db.clear_workspace_expansion_artists(workspace)
                    db.replace_workspace_library_tracks(workspace, [])
                    db.set_workspace_scope(workspace, playlist_ids=[], include_liked=False)
                    db.update_workspace(
                        workspace,
                        status="created",
                        phase="idle",
                        message="Workspace reset to initial clean state.",
                        error="",
                    )
                    db.update_workspace_progress(
                        workspace,
                        status="created",
                        phase="idle",
                        message="Workspace reset.",
                        error="",
                        metadata={"total_work": 0, "completed_work": 0},
                    )
            finally:
                db.close()
            return jsonify({"ok": True, "workspace_id": workspace, "status": "reset"})
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.post("/api/spotify/inventory")
    def spotify_inventory_sync():
        if not _safe_local_request():
            return {"error": "Spotify workspace is local-only"}, 403
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        _recover_stale_spotify_job()
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.get_json(silent=True) or {}
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a Spotify job is already running"}, 409
            workspace = _workspace_id(payload.get("workspace_id"), required=False)
            workspace = int(workspace) if workspace is not None else _get_or_create_spotify_workspace()
            spotify_job.begin(workspace, scan_key="inventory")
            run_id = spotify_job.run_id
        status_db = Database(config.database_path)
        try:
            inventory_metadata = {
                "workflow": "spotify_inventory",
                "scan_key": "inventory",
                "run_id": run_id,
            }
            status_db.update_workspace(
                workspace,
                status="running",
                phase="spotify_inventory",
                metadata=inventory_metadata,
            )
            status_db.update_workspace_progress(
                workspace,
                status="running",
                phase="spotify_inventory",
                message="Starting Spotify inventory sync...",
                error="",
                metadata=inventory_metadata,
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
                        **inventory_metadata,
                        "workflow": "spotify_staged",
                        "playlist_count": len(playlists),
                    })
                spotify_job.finish(type("InventoryResult", (), {
                    "status": "ready_for_scope", "phase": "ready_for_scope", "error": "", "partial": False,
                })())
            except Exception as exc:
                message = _safe_message(exc, _oauth_secret_values())
                try:
                    db.update_workspace(workspace, status="failed", phase="spotify_inventory", metadata={
                        **inventory_metadata,
                        "error": message,
                    })
                except Exception:
                    pass
                spotify_job.fail(message)
            finally:
                db.recover_discovery_run(workspace, run_id)
                spotify_job.cleanup(run_id, threading.current_thread())
                db.close()

        worker_thread = threading.Thread(target=inventory_worker, daemon=True, name=f"spotify-{run_id}")
        with spotify_job.lock:
            spotify_job.attach_worker(run_id, worker_thread)
            worker_thread.start()
        return jsonify({"started": True, "workspace_id": workspace, "phase": "spotify_inventory", "scan_key": "inventory", "run_id": run_id}), 202

    @app.post("/api/spotify/workspace/new")
    def spotify_workspace_new():
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        busy = _busy_response()
        if busy is not None:
            return busy
        db = Database(config.database_path)
        try:
            workspace = db.create_workspace(
                workspace_key=f"spotify:{secrets.token_hex(8)}",
                name="New Spotify library",
                kind="exact",
                status="created",
                phase="idle",
                metadata={"workflow": "spotify_staged"},
            )
        finally:
            db.close()
        return jsonify({"ok": True, "workspace_id": workspace})

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
                liked_available = False
                if _is_authenticated():
                    try:
                        client = _get_spotify_client()
                        count_method = getattr(client, "get_saved_tracks_count", None)
                        if callable(count_method):
                            liked_count = max(0, int(count_method()))
                            liked_available = True
                    except Exception:
                        log.info("Could not fetch Spotify Liked Songs count", exc_info=True)
            finally:
                db.close()
            return jsonify({"workspace_id": workspace, "scope": scope, "playlists": playlists,
                            "liked_count": liked_count, "liked_available": liked_available,
                            "accessible_count": sum(p.get("access_status") == "accessible" for p in playlists),
                            "inaccessible_count": sum(p.get("access_status") != "accessible" for p in playlists)})
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.post("/api/spotify/import")
    def spotify_import_selected():
        """Import the selected Spotify scope without starting chart matching."""
        if not _safe_local_request():
            return {"error": "Spotify workspace is local-only"}, 403
        if not _is_authenticated():
            return {"error": "sign in with Spotify first"}, 401
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.get_json(silent=True) or {}
        workspace = _workspace_id(payload.get("workspace_id"), required=False)
        if workspace is None:
            workspace, _ = _get_local_library_workspace()
        db = Database(config.database_path)
        try:
            scope = db.get_workspace_scope_data(workspace) or {}
            if not scope.get("playlist_ids") and not scope.get("include_liked"):
                return {"error": "choose Liked Songs or at least one playlist first"}, 400
        finally:
            db.close()
        with spotify_job.lock:
            if spotify_job.running:
                return {"error": "a Spotify operation is already running"}, 409
            spotify_job.begin(workspace, workspace_key="local", scan_key="spotify_import")
            run_id = spotify_job.run_id
        status_db = Database(config.database_path)
        try:
            status_db.update_workspace(workspace, status="running", phase="spotify_import", metadata={"run_id": run_id, "scan_key": "spotify_import"})
            status_db.update_workspace_progress(workspace, status="running", phase="spotify_import", message="Reading selected Spotify music...", error="", metadata={"run_id": run_id, "scan_key": "spotify_import"})
        finally:
            status_db.close()

        def worker() -> None:
            db = Database(config.database_path)
            provider = None
            try:
                from csv_to_clonehero.library_pipeline import run_library_import
                from csv_to_clonehero.providers.spotify import SpotifyLibraryProvider
                client = _verified_spotify_client()
                provider = SpotifyLibraryProvider(client)
                started_at = time.monotonic()
                progress_state: dict[str, Any] = {"playlist_items": {}, "liked_items": 0, "liked_total": None, "playlist_totals": {}}
                last_persisted = 0.0

                def progress(value: Mapping[str, Any]) -> None:
                    nonlocal last_persisted
                    update = dict(value)
                    phase = str(update.get("phase") or "spotify_import")
                    if phase == "playlist":
                        index = int(update.get("playlist_index", 0) or 0)
                        name = str(update.get("playlist_name") or "playlist")
                        progress_state["playlist_items"][index] = int(update.get("playlist_items_read", progress_state["playlist_items"].get(index, 0)) or 0)
                        if update.get("playlist_total_items") is not None:
                            progress_state["playlist_totals"][index] = int(update["playlist_total_items"])
                        message = f"Reading playlist {index} / {int(update.get('playlist_total', 0) or 0)}: {name}"
                    elif phase == "liked":
                        progress_state["liked_items"] = int(update.get("liked_items_read", 0) or 0)
                        if update.get("liked_total_items") is not None:
                            progress_state["liked_total"] = int(update["liked_total_items"])
                        message = "Reading Liked Songs"
                    else:
                        message = str(update.get("message") or "Preparing Spotify import...")
                    processed = int(progress_state["liked_items"]) + sum(progress_state["playlist_items"].values())
                    expected_values = [value for value in [progress_state["liked_total"], *progress_state["playlist_totals"].values()] if value is not None]
                    expected = sum(expected_values) if len(expected_values) == (1 if progress_state["liked_total"] is not None else 0) + len(progress_state["playlist_totals"]) else None
                    elapsed = max(0.0, time.monotonic() - started_at)
                    update.update({"phase": phase, "message": message, "spotify_items_processed": processed, "spotify_items_expected": expected, "elapsed_seconds": round(elapsed, 1)})
                    if expected and processed >= 100 and elapsed >= 2:
                        remaining = max(0, expected - processed)
                        rate = processed / elapsed
                        if rate > 0:
                            update["eta_seconds"] = int(remaining / rate)
                    spotify_job.progress_update(update)
                    if time.monotonic() - last_persisted >= 0.25 or phase not in {"playlist", "liked"}:
                        db.update_workspace_progress(workspace, status="running", phase=phase, message=message, error="", total_tracks=expected or 0, completed_tracks=processed, metadata={"run_id": run_id, "scan_key": "spotify_import", **update})
                        last_persisted = time.monotonic()

                imported = run_library_import(config, db, workspace, {"spotify": provider}, scopes={"spotify": scope}, on_progress=progress)
                provider = None
                summary = db.get_combined_summary(workspace)
                source_summary = {str(row["provider"]): row for row in db.get_source_summary(workspace)}
                spotify_unique = int(source_summary.get("spotify", {}).get("tracks", 0) or 0)
                soundcloud_unique = int(source_summary.get("soundcloud", {}).get("tracks", 0) or 0)
                overlap = max(0, spotify_unique + soundcloud_unique - int(summary.get("tracks", 0)))
                progress({"phase": "merge", "message": f"Your Music updated: {spotify_unique} Spotify unique songs; {overlap} in both; {summary.get('tracks', 0)} canonical songs."})
                db.update_workspace(workspace, status="ready_for_matching", phase="ready_for_matching", message="Spotify music added to Your Music.", error="")
                db.update_workspace_progress(workspace, status="complete", phase="ready_for_matching", message="Spotify import complete.", error="", metadata={"run_id": run_id, "scan_key": "spotify_import", "spotify_unique": spotify_unique, "soundcloud_unique": soundcloud_unique, "canonical_count": summary.get("tracks", 0), "overlap": overlap, "elapsed_seconds": round(time.monotonic() - started_at, 1)})
                spotify_job.finish(type("SpotifyImportResult", (), {"status": "complete", "phase": "ready_for_matching", "error": "", "partial": False})())
            except Exception as exc:
                message = _safe_message(exc, _oauth_secret_values())
                db.update_workspace(workspace, status="failed", phase="spotify_import_failed", metadata={"run_id": run_id, "error": message})
                db.update_workspace_progress(workspace, status="failed", phase="spotify_import_failed", message="Spotify import failed.", error=message, metadata={"run_id": run_id, "scan_key": "spotify_import"})
                spotify_job.fail(message)
            finally:
                if provider is not None:
                    provider.close()
                db.close()
                spotify_job.cleanup(run_id, threading.current_thread())

        thread = threading.Thread(target=worker, daemon=True, name=f"spotify-import-{run_id}")
        spotify_job.attach_worker(run_id, thread)
        thread.start()
        return jsonify({"started": True, "workspace_id": workspace, "run_id": run_id, "phase": "spotify_import"}), 202

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
            workspace = spotify_job.workspace_id
        batch_payload = None
        if workspace is not None:
            db = Database(config.database_path)
            try:
                batch = db.get_expansion_batch(workspace_id=workspace)
                if batch is not None:
                    terminal = str(batch["status"]) in {"complete", "completed", "partial", "failed", "cancelled", "interrupted"}
                    if not terminal:
                        db.request_expansion_batch_cancel(int(batch["id"]))
                    batch_payload = _row_dict(db.get_expansion_batch(int(batch["id"])))
            finally:
                db.close()
        if event is not None:
            event.set()
        return jsonify({
            "ok": True,
            "status": "cancelling" if running and event is not None else "terminal",
            "batch": batch_payload,
        })

    @app.post("/api/spotify/expansion/start")
    @app.post("/api/discovery/expansion/start")
    @app.post("/api/spotify/expansion/scan")
    @app.post("/api/discovery/expansion")
    def spotify_expansion_start():
        return _start_spotify_job("expansion", request.get_json(silent=True) or {})

    @app.post("/api/spotify/expansion/clear")
    @app.post("/api/discovery/expansion/clear")
    def spotify_expansion_clear():
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        if spotify_job.running:
            return {"error": "cancel the running Spotify job before clearing expansion"}, 409
        try:
            payload = request.get_json(silent=True) or {}
            workspace = _workspace_id(payload.get("workspace_id"))
            db = Database(config.database_path)
            try:
                db.clear_workspace_expansion_state(workspace)
            finally:
                db.close()
            return jsonify({"ok": True, "workspace_id": workspace})
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 400

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
            if kind == "expansion" and track_value in (None, "") and folder_value in (None, ""):
                group_list = _expansion_groups_payload(db, workspace)
                return {
                    "workspace_id": workspace,
                    "kind": kind,
                    "folder_id": folder_value,
                    "groups": group_list,
                    "candidate_groups": group_list,
                    "tracks": [],
                    "artists": group_list,
                }
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
    def _expansion_selection_fingerprint(artists: Iterable[Mapping[str, Any]]) -> str:
        values = [
            {
                "artist_norm": str(item.get("artist_norm") or ""),
                "track_count": int(item.get("track_count") or 0),
                "cap": int(item.get("cap") or 0),
            }
            for item in artists
        ]
        payload = json.dumps(sorted(values, key=lambda item: item["artist_norm"]), sort_keys=True)
        return hashlib.sha256(("discography-preview-v1\n" + payload).encode("utf-8")).hexdigest()

    def _artist_preview_fingerprint(
        artist_norm: str, existing_songs: Iterable[tuple[str, str]]
    ) -> str:
        titles = sorted(title for norm, title in existing_songs if norm == artist_norm)
        payload = "discography-preview-v1\n" + artist_norm + "\n" + "\n".join(titles)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _expansion_batch_payload(db: Database, workspace: int | str) -> dict[str, Any] | None:
        batch = db.get_expansion_batch(workspace_id=workspace)
        if batch is None:
            return None
        artists = []
        for row in db.get_expansion_batch_artists(int(batch["id"])):
            item = _row_dict(row)
            item["metadata"] = _json_object(item.pop("metadata_json", "{}"), {})
            preview_status = str(item.get("preview_status") or "pending")
            search_status = str(item.get("search_status") or "pending")
            if search_status in {"complete", "completed", "success"}:
                state = "Search complete"
            elif search_status == "running":
                state = "Searching"
            elif preview_status == "running":
                state = "Previewing"
            elif "failed" in {preview_status, search_status}:
                state = "Failed"
            elif preview_status in {"complete", "completed", "success", "skipped"}:
                state = "Previewed"
            else:
                state = "Not previewed"
            item["state"] = state
            artists.append(item)
        data = _row_dict(batch)
        data["metadata"] = _json_object(data.pop("metadata_json", "{}"), {})
        data["artists"] = artists
        return data

    def _expansion_artist_payload(workspace: int | str) -> list[dict[str, Any]]:
        db = Database(config.database_path)
        catalog_sources: list[ChartSource] = []
        try:
            try:
                catalog_sources = list(source_factory(config))
            except Exception:
                catalog_sources = []
            tracks = db.get_library_tracks(workspace_id=workspace)
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
                metadata = _json_object(old.get("metadata_json"), {})
                preview = metadata.get("discography_preview", {}) if isinstance(metadata, Mapping) else {}
                memberships = [
                    source_catalog_available(source, entry["artist_name"])
                    for source in catalog_sources
                ]
                catalog_excluded = bool(
                    memberships and all(value is False for value in memberships)
                )
                entries.append(
                    {
                        **entry,
                        "cap": max(0, int(cap)),
                        "selected": bool(old.get("selected")),
                        "eligible": max(0, int(cap)) > 0 and not catalog_excluded,
                        "catalog_available": (
                            None
                            if not memberships or any(value is None for value in memberships)
                            else not catalog_excluded
                        ),
                        "catalog_excluded": catalog_excluded,
                        "metadata": metadata,
                        "preview_song_count": len(preview.get("tracks", [])) if isinstance(preview, Mapping) else 0,
                        "preview_album_count": int(preview.get("albums", 0) or 0) if isinstance(preview, Mapping) else 0,
                        "preview_error": str(preview.get("error", "") or "") if isinstance(preview, Mapping) else "",
                    }
                )
            # Keep the database's explicit artist list in sync with the current
            # Spotify snapshot; only primary artists are included here.
            db.replace_workspace_expansion_artists(workspace, entries)
            batch = _expansion_batch_payload(db, workspace)
            batch_artists = {
                str(item["artist_norm"]): item
                for item in (batch or {}).get("artists", [])
            }
            for entry in entries:
                lifecycle = batch_artists.get(str(entry["artist_norm"]), {})
                entry["state"] = lifecycle.get("state", "Not previewed")
                entry["batch_error"] = str(lifecycle.get("error") or "")
            return entries
        finally:
            for source in catalog_sources:
                try:
                    source.close()
                except Exception:
                    pass
            db.close()

    @app.get("/api/spotify/expansion/artists")
    @app.get("/api/discovery/expansion/artists")
    def spotify_expansion_artists():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            artists = _expansion_artist_payload(workspace)
            db = Database(config.database_path)
            try:
                batch = _expansion_batch_payload(db, workspace)
            finally:
                db.close()
            return jsonify({
                "workspace_id": workspace,
                "max_artists_per_batch": config.expansion.max_artists_per_batch,
                "artists": artists,
                "unavailable_artist_count": sum(
                    bool(item.get("catalog_excluded")) for item in artists
                ),
                "batch": batch,
            })
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 404

    def _discography_preview_payload(workspace: int | str) -> dict[str, Any]:
        db = Database(config.database_path)
        try:
            artists = []
            tracks: list[dict[str, Any]] = []
            errors = 0
            successful_previews = 0
            full_discography_count = 0
            already_in_library_count = 0
            for row in db.get_workspace_expansion_artists(workspace, selected_only=True):
                data = _row_dict(row)
                metadata = _json_object(data.get("metadata_json"), {})
                preview = metadata.get("discography_preview", {}) if isinstance(metadata, Mapping) else {}
                preview_tracks = list(preview.get("tracks", [])) if isinstance(preview, Mapping) else []
                error = str(preview.get("error", "") or "") if isinstance(preview, Mapping) else ""
                errors += bool(error)
                successful_previews += bool(
                    isinstance(preview, Mapping)
                    and preview.get("discovered_at")
                    and not error
                )
                catalog_count = int(preview.get("catalog_song_count", len(preview_tracks)) or 0) if isinstance(preview, Mapping) else 0
                already_count = int(preview.get("already_in_library", 0) or 0) if isinstance(preview, Mapping) else 0
                full_discography_count += catalog_count
                already_in_library_count += already_count
                artists.append({"artist_name": data.get("artist_name"), "artist_norm": data.get("artist_norm"), "albums": int(preview.get("albums", 0) or 0) if isinstance(preview, Mapping) else 0, "songs": len(preview_tracks), "catalog_song_count": catalog_count, "already_in_library": already_count, "error": error})
                tracks.extend(preview_tracks)
            unique = {(normalize_artist(str(item.get("artist") or "")), normalize_title(str(item.get("title") or ""))): item for item in tracks}
            unique_tracks = list(unique.values())
            sources: list[Any] = []
            source_error = ""
            try:
                sources = list(source_factory(config))
            except Exception as exc:
                source_error = _safe_message(exc)
            cached_queries = 0
            query_count = len(artists) * len(sources)
            cache = getattr(config, "cache", None)
            try:
                ttl_days = int(getattr(cache, "ttl_days", 7) or 7)
            except (TypeError, ValueError):
                ttl_days = 7
            cache_usable = bool(getattr(cache, "enabled", True)) and bool(
                getattr(getattr(config, "scan", None), "resume", True)
            )
            try:
                if cache_usable:
                    for item in artists:
                        artist = str(item.get("artist_name") or "").strip()
                        for source in sources:
                            key = build_source_query_key(db, config, source, "artist", artist, "")
                            if db.cached_query_is_fresh(key, ttl_days):
                                cached_queries += 1
            finally:
                for source in sources:
                    try:
                        source.close()
                    except Exception:
                        pass
            duration = estimate_spotify_duration(
                config,
                query_count=query_count,
                track_count=len(unique_tracks),
                cached_query_count=cached_queries,
            )
            selected_rows = db.get_workspace_expansion_artists(workspace, selected_only=True)
            ready = bool(successful_previews) and len(selected_rows) == len(artists)
            if ready:
                for row in selected_rows:
                    metadata = _json_object(row["metadata_json"], {})
                    preview = metadata.get("discography_preview") if isinstance(metadata, Mapping) else None
                    if not isinstance(preview, Mapping) or "discovered_at" not in preview:
                        ready = False
                        break
            batch = _expansion_batch_payload(db, workspace)
            lifecycle = {
                str(item["artist_norm"]): item for item in (batch or {}).get("artists", [])
            }
            for item in artists:
                row = lifecycle.get(str(item.get("artist_norm") or ""), {})
                item["state"] = row.get("state", "Not previewed")
                item["batch_error"] = str(row.get("error") or "")
            return {"workspace_id": int(workspace), "artists": artists, "artist_count": len(artists), "song_count": len(unique_tracks), "full_discography_count": full_discography_count, "already_in_library_count": already_in_library_count, "errors": errors, "successful_previews": successful_previews, "source_error": source_error, "ready": ready, "tracks": unique_tracks, "expected_queries": query_count, "cached_queries": cached_queries, "duration": duration, "max_artists_per_batch": config.expansion.max_artists_per_batch, "batch": batch}
        finally:
            db.close()

    @app.get("/api/spotify/expansion/preview")
    @app.get("/api/discovery/expansion/preview")
    def spotify_expansion_preview_get():
        try:
            workspace = _workspace_id(request.args.get("workspace_id"))
            return jsonify(_discography_preview_payload(workspace))
        except (OSError, ValueError) as exc:
            return {"error": _safe_message(exc)}, 400

    @app.post("/api/spotify/expansion/preview")
    @app.post("/api/discovery/expansion/preview")
    @app.post("/api/spotify/expansion/preview/resume")
    @app.post("/api/spotify/expansion/preview/retry")
    def spotify_expansion_preview_start():
        nonlocal spotify_cancel_event
        if not _safe_local_request():
            return {"error": "the Spotify workspace is local-only"}, 403
        _recover_stale_spotify_job()
        busy = _busy_response()
        if busy is not None:
            return busy
        if spotify_job.running:
            return {"error": "a Spotify job is already running"}, 409
        payload = request.get_json(silent=True) or {}
        workspace = _workspace_id(payload.get("workspace_id"))
        action = str(payload.get("action") or "").strip().casefold()
        if request.path.endswith("/resume"):
            action = "resume"
        elif request.path.endswith("/retry"):
            action = "retry"
        try:
            job_client = _verified_spotify_client()
        except SpotifyError as exc:
            return {"error": _safe_message(exc, _oauth_secret_values())}, 401
        available_rows = _expansion_artist_payload(workspace)
        available = {str(item["artist_norm"]): item for item in available_rows}
        db = Database(config.database_path)
        try:
            requested = payload.get("artist_norms") or payload.get("artists")
            if isinstance(requested, (str, bytes, Mapping)):
                requested = [requested]
            if requested is not None:
                requested_norms = []
                for value in requested:
                    if isinstance(value, Mapping):
                        value = value.get("artist_norm") or value.get("artist") or value.get("name")
                    norm = normalize_artist(str(value or "").strip())
                    if norm and norm not in requested_norms:
                        requested_norms.append(norm)
                invalid = [norm for norm in requested_norms if norm not in available]
                if invalid:
                    return {"error": "every expansion artist must be an eligible primary artist in the current Spotify library"}, 400
                ineligible = [norm for norm in requested_norms if not available[norm]["eligible"]]
                if ineligible:
                    return {"error": "every expansion artist must be eligible for expansion"}, 400
                if len(requested_norms) > config.expansion.max_artists_per_batch:
                    return {"error": f"artist expansion batches are limited to {config.expansion.max_artists_per_batch} primary artists; received {len(requested_norms)}"}, 400
                requested_set = set(requested_norms)
                for row in db.get_workspace_expansion_artists(workspace):
                    norm = str(row["artist_norm"])
                    db.set_expansion_artist_selected(workspace, norm, norm in requested_set)
            artists = [_row_dict(row) for row in db.get_workspace_expansion_artists(workspace, selected_only=True)]
            if len(artists) > config.expansion.max_artists_per_batch:
                return {"error": f"artist expansion batches are limited to {config.expansion.max_artists_per_batch} primary artists; received {len(artists)}"}, 400
        finally:
            db.close()
        if not artists:
            return {"error": "select at least one expansion artist first"}, 400
        with spotify_job.lock:
            spotify_job.begin(workspace, scan_key="expansion_preview")
            run_id = spotify_job.run_id
            spotify_cancel_event = threading.Event()
            active_cancel_event = spotify_cancel_event
        db = Database(config.database_path)
        try:
            fingerprint = _expansion_selection_fingerprint(artists)
            batch = db.get_expansion_batch(workspace_id=workspace)
            can_resume = bool(
                action in {"resume", "retry"}
                and batch is not None
                and str(batch["selection_fingerprint"]) == fingerprint
            )
            if can_resume:
                batch = db.resume_expansion_batch(
                    int(batch["id"]), run_id=run_id, phase="preview", retry_failed=action == "retry"
                )
                batch_id = int(batch["id"])
            else:
                batch_id = db.create_expansion_batch(
                    workspace,
                    artists,
                    run_id=run_id,
                    selection_fingerprint=fingerprint,
                    status="running",
                    phase="preview",
                    metadata={"algorithm": "discography-preview-v1"},
                )
            db.update_workspace(
                workspace,
                status="running",
                phase="expansion_preview",
                metadata={"scan_key": "expansion_preview", "run_id": run_id, "expansion_batch_id": batch_id},
            )
        finally:
            db.close()

        def preview_worker() -> None:
            nonlocal spotify_cancel_event
            worker_db = Database(config.database_path)
            try:
                client = job_client
                library_rows = worker_db.get_library_track_rows(workspace_id=workspace)
                existing_songs = {(str(row["primary_artist_norm"] or row["artist_norm"]), str(row["title_norm"])) for row in library_rows}
                seen: set[tuple[str, str]] = set()
                total_new = 0
                failures = 0
                skipped = 0
                requests = 0
                batch_artist_rows = worker_db.get_expansion_batch_artists(batch_id)
                artists_by_norm = {str(item["artist_norm"]): item for item in artists}
                total = len(batch_artist_rows)
                spotify_job.progress_update({"phase": "expansion_preview", "message": "Loading selected artist discographies from Spotify", "total_tasks": total, "total_work": total, "completed_tasks": 0, "completed_work": 0, "expansion_artists_total": total})
                preview_started = time.monotonic()
                completed = 0
                for batch_artist in batch_artist_rows:
                    if active_cancel_event.is_set() or bool(worker_db.get_expansion_batch(batch_id)["cancel_requested"]):
                        raise SpotifyCancellationError("Spotify expansion preview cancelled")
                    artist = artists_by_norm.get(str(batch_artist["artist_norm"]), _row_dict(batch_artist))
                    name = str(artist.get("artist_name") or batch_artist["artist_name"] or "")
                    norm = str(artist.get("artist_norm") or batch_artist["artist_norm"] or normalize_artist(name))
                    metadata = _json_object(artist.get("metadata_json"), {})
                    preview_fingerprint = _artist_preview_fingerprint(norm, existing_songs)
                    existing_preview = metadata.get("discography_preview", {}) if isinstance(metadata, Mapping) else {}
                    if str(batch_artist["preview_status"]) in {"complete", "completed", "success", "skipped"} or (
                        isinstance(existing_preview, Mapping)
                        and not existing_preview.get("error")
                        and existing_preview.get("fingerprint") == preview_fingerprint
                    ):
                        skipped += 1
                        completed += 1
                        worker_db.checkpoint_expansion_batch_artist(batch_id, norm, phase="preview", status="skipped", request_count=0, metadata={"fingerprint": preview_fingerprint, "reused": True})
                        spotify_job.progress_update({"phase": "expansion_preview", "message": f"Reused saved preview: {name}", "current_artist": name, "total_tasks": total, "completed_tasks": completed, "total_work": total, "completed_work": completed, "expansion_artists_completed": completed, "expansion_artists_skipped": skipped, "network_requests": requests})
                        continue
                    if str(batch_artist["preview_status"]) == "failed" and action != "retry":
                        failures += 1
                        completed += 1
                        continue
                    worker_db.checkpoint_expansion_batch_artist(batch_id, norm, phase="preview", status="running")
                    worker_db.update_expansion_batch(batch_id, status="running", phase="preview", current_artist=name)
                    try:
                        requests += 1
                        discography_method = client.get_artist_discography
                        try:
                            discography_parameters = inspect.signature(discography_method).parameters
                        except (TypeError, ValueError):
                            discography_parameters = {}
                        kwargs = (
                            {"cancellation_event": active_cancel_event}
                            if "cancellation_event" in discography_parameters
                            or any(
                                parameter.kind == inspect.Parameter.VAR_KEYWORD
                                for parameter in discography_parameters.values()
                            )
                            else {}
                        )
                        catalog = discography_method(name, **kwargs)
                        catalog_tracks = [
                            dict(item)
                            for item in catalog.get("tracks", [])
                            if isinstance(item, Mapping)
                        ]
                        catalog_keys = {
                            (
                                normalize_artist(str(item.get("artist") or name)),
                                normalize_title(str(item.get("title") or "")),
                            )
                            for item in catalog_tracks
                            if str(item.get("title") or "").strip()
                        }
                        new_tracks = []
                        for item in catalog.get("tracks", []):
                            key = (normalize_artist(str(item.get("artist") or name)), normalize_title(str(item.get("title") or "")))
                            if not key[1] or key in existing_songs or key in seen:
                                continue
                            seen.add(key)
                            new_tracks.append(dict(item))
                        preview = {"artist_id": catalog.get("artist_id", ""), "albums": catalog.get("albums", 0), "catalog_song_count": len(catalog_keys), "already_in_library": len(catalog_keys & existing_songs), "duplicate_count": max(0, len(catalog_tracks) - len(catalog_keys)), "tracks": new_tracks, "error": "", "fingerprint": preview_fingerprint, "discovered_at": time.time()}
                        total_new += len(new_tracks)
                    except SpotifyCancellationError:
                        raise
                    except (SpotifyAuthError, SpotifyPermissionError):
                        raise
                    except Exception as exc:
                        failures += 1
                        preview = {"artist_id": "", "albums": 0, "catalog_song_count": 0, "already_in_library": 0, "duplicate_count": 0, "tracks": [], "error": _safe_message(exc), "fingerprint": preview_fingerprint, "discovered_at": time.time()}
                    metadata["discography_preview"] = preview
                    worker_db.update_workspace_expansion_artist_metadata(
                        workspace, norm, metadata
                    )
                    completed += 1
                    worker_db.checkpoint_expansion_batch_artist(batch_id, norm, phase="preview", status="failed" if preview["error"] else "complete", request_count=1, error=preview["error"] or None, metadata={"fingerprint": preview_fingerprint, "songs": len(preview["tracks"]), "albums": preview["albums"]})
                    elapsed = time.monotonic() - preview_started
                    remaining_seconds = int((total - completed) * (elapsed / completed)) if completed else 0
                    eta_text = f"Spotify catalog step · about {remaining_seconds // 60}m {remaining_seconds % 60}s remaining" if remaining_seconds >= 60 else f"Spotify catalog step · about {remaining_seconds}s remaining"
                    spotify_job.progress_update({"phase": "expansion_preview", "message": f"Loaded discography {completed} / {total}: {name} ({len(preview['tracks'])} new songs)", "current_artist": name, "eta_text": eta_text, "total_tasks": total, "completed_tasks": completed, "total_work": total, "completed_work": completed, "expansion_artists_completed": completed, "expansion_artists_failed": failures, "expansion_artists_skipped": skipped, "network_requests": requests})
                row = worker_db.get_workspace(workspace)
                workspace_meta = _json_object(row["metadata_json"] if row else "", {})
                preview_status = "partial" if failures else "complete"
                workspace_meta.update({"scan_key": "expansion_preview", "run_id": run_id, "expansion_batch_id": batch_id, "partial": bool(failures), "expansion_preview": {"artists": total, "songs": total_new, "errors": failures, "skipped": skipped, "network_requests": requests}})
                worker_db.update_workspace_progress(
                    workspace,
                    {
                        "status": preview_status,
                        "phase": "expansion_preview_complete",
                        "total_tasks": total,
                        "completed_tasks": completed,
                        "total_work": total,
                        "completed_work": completed,
                        "metadata": workspace_meta,
                    },
                )
                worker_db.update_workspace(workspace, status=preview_status, phase="expansion_preview_complete", metadata=workspace_meta)
                worker_db.update_expansion_batch(batch_id, status=preview_status, phase="preview_complete", current_artist="", error=None)
                spotify_job.finish(type("PreviewResult", (), {"status": preview_status, "phase": "expansion_preview_complete", "partial": bool(failures)})())
            except SpotifyCancellationError as exc:
                message = _safe_message(exc)
                worker_db.update_expansion_batch(batch_id, status="cancelled", phase="preview", current_artist="", error=message)
                worker_db.update_workspace(workspace, status="cancelled", phase="expansion_preview_cancelled", metadata={"scan_key": "expansion_preview", "run_id": run_id, "expansion_batch_id": batch_id, "cancelled": True})
                spotify_job.finish(type("PreviewResult", (), {"status": "cancelled", "phase": "expansion_preview_cancelled", "partial": True, "error": ""})())
            except Exception as exc:
                message = _safe_message(exc)
                try:
                    worker_db.update_expansion_batch(batch_id, status="failed", phase="preview", error=message)
                    worker_db.update_workspace(workspace, status="failed", phase="expansion_preview_failed", metadata={"scan_key": "expansion_preview", "run_id": run_id, "error": message})
                except Exception:
                    pass
                spotify_job.fail(message)
            finally:
                with spotify_job.lock:
                    if spotify_cancel_event is active_cancel_event:
                        spotify_cancel_event = None
                worker_db.recover_discovery_run(workspace, run_id)
                spotify_job.cleanup(run_id, threading.current_thread())
                worker_db.close()

        worker_thread = threading.Thread(target=preview_worker, daemon=True, name=f"spotify-{run_id}")
        with spotify_job.lock:
            spotify_job.attach_worker(run_id, worker_thread)
            worker_thread.start()
        return jsonify({"started": True, "workspace_id": workspace, "scan_key": "expansion_preview", "run_id": run_id, "batch_id": batch_id}), 202

    @app.post("/api/spotify/score-preview")
    def spotify_score_preview():
        """Evaluate sandbox metadata through the production matcher."""
        payload = request.get_json(silent=True) or {}
        track_data = dict(payload.get("track") or {})
        chart_data = dict(payload.get("chart") or {})
        artist = str(track_data.get("artist") or "").strip()
        title = str(track_data.get("title") or "").strip()
        chart_artist = str(chart_data.get("artist") or "").strip()
        chart_title = str(chart_data.get("title") or chart_data.get("name") or "").strip()
        if not all((artist, title, chart_artist, chart_title)):
            return {"error": "track and chart artist/title are required"}, 400
        raw_instruments = chart_data.get("instruments") or []
        if isinstance(raw_instruments, str):
            raw_instruments = raw_instruments.split(",")
        instruments = [str(value).strip().casefold() for value in raw_instruments if str(value).strip()]
        difficulty = chart_data.get("difficulty")
        try:
            difficulty_value = int(difficulty) if difficulty not in (None, "") else None
        except (TypeError, ValueError):
            return {"error": "difficulty must be a number"}, 400
        track = Track(
            track_key="sandbox",
            title=title,
            title_norm=normalize_title(title),
            artist_full=artist,
            artist_norm=normalize_artist(artist),
            primary_artist=artist,
            primary_artist_norm=normalize_artist(artist),
            album=str(track_data.get("album") or ""),
            album_norm=normalize_title(str(track_data.get("album") or "")),
        )
        chart = ChartCandidate(
            source="sandbox",
            chart_id=1,
            md5="sandbox",
            name=chart_title,
            artist=chart_artist,
            name_norm=normalize_title(chart_title),
            artist_norm=normalize_artist(chart_artist),
            album=str(chart_data.get("album") or ""),
            charter=str(chart_data.get("charter") or ""),
            icon=str(chart_data.get("icon") or ""),
            pack_name=str(chart_data.get("pack_name") or ""),
            instruments=instruments,
            diff_guitar=difficulty_value if "guitar" in instruments else None,
            diff_bass=difficulty_value if "bass" in instruments else None,
            diff_drums=difficulty_value if "drums" in instruments else None,
            diff_vocals=difficulty_value if "vocals" in instruments else None,
        )
        breakdown = score_candidate(track, chart, config)
        eligibility = eligibility_for(breakdown, config)
        result = {
            item.name: getattr(breakdown, item.name)
            for item in fields(breakdown)
        }
        result.update(
            final_score=breakdown.final_score,
            quality=breakdown.quality,
            eligibility=eligibility.state,
            eligibility_reasons=eligibility.reasons,
            explanation=breakdown.explain(),
        )
        return jsonify(result)

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
                    norm = normalize_artist(value.strip())
                    selected = True
                else:
                    data = dict(value or {})
                    name = str(data.get("artist_norm") or data.get("artist") or data.get("name") or "").strip()
                    norm = normalize_artist(name)
                    selected = bool(data.get("selected", data.get("enabled", True)))
                if norm not in available:
                    return {"error": "artist is not a primary artist in the Spotify library"}, 400
                if available[norm]["cap"] <= 0 and selected:
                    return {"error": "artist is not eligible for expansion"}, 400
                updates.append((norm, selected))
            db = Database(config.database_path)
            try:
                selected_norms = {
                    str(row["artist_norm"])
                    for row in db.get_workspace_expansion_artists(workspace, selected_only=True)
                }
                for norm, selected in updates:
                    if selected:
                        selected_norms.add(norm)
                    else:
                        selected_norms.discard(norm)
                if len(selected_norms) > config.expansion.max_artists_per_batch:
                    return {
                        "error": f"artist expansion batches are limited to {config.expansion.max_artists_per_batch} primary artists; received {len(selected_norms)}"
                    }, 400
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
        scope: str = "strong",
        search_text: str = "",
    ) -> list[int]:
        """Select one safe chart per song for an explicit bulk action.

        Strong recommendations are the default. Review-tier rows are included
        only when the caller explicitly asks for the eligible/current result
        scope; merely being eligible never opts a review chart into a build.
        """
        scope = str(scope or "strong").casefold()
        if scope in {"recommended", "auto", "auto_accept", "strong"}:
            allowed_states = {"strong"}
            scope = "strong"
        elif scope in {"eligible", "current", "filter", "all_eligible"}:
            allowed_states = {"strong", "needs_review"}
            scope = "eligible"
        else:
            raise ValueError("bulk selection scope must be strong or eligible")
        requested = None
        if track_ids is not None:
            requested = {str(value) for value in track_ids}
        needle = str(search_text or "").strip().casefold()
        catalog_sources = list(source_factory(config)) if kind == "exact" else None
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
            if _review_row_state(row) not in allowed_states:
                continue
            if needle and needle not in " ".join(
                str(data.get(key) or "")
                for key in ("track_artist", "track_title", "source", "chart_id")
            ).casefold():
                candidate_text = _json_object(data.get("candidate_json"), {})
                candidate_text = candidate_text if isinstance(candidate_text, Mapping) else {}
                if needle not in " ".join(
                    str(candidate_text.get(key) or "")
                    for key in ("artist", "name", "charter", "url")
                ).casefold():
                    continue
            valid, _error = _validate_candidate_eligibility(
                db,
                workspace,
                row,
                kind,
                catalog_sources=catalog_sources,
            )
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
        for source in catalog_sources or ():
            try:
                source.close()
            except Exception:
                pass
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
            if kind != "exact":
                return {"error": "bulk selection currently supports exact songs only"}, 400
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
                scope = str(
                    payload.get("scope")
                    or payload.get("selection_scope")
                    or payload.get("filter")
                    or "strong"
                ).casefold()
                if scope in {"current", "filter"}:
                    scope = "eligible"
                track_ids = payload.get("track_ids")
                if isinstance(track_ids, (str, bytes)):
                    track_ids = [track_ids]
                if folder_id is not None or liked:
                    track_rows = db.get_library_track_rows(
                        folder_id,
                        liked=liked,
                        workspace_id=workspace,
                    )
                    folder_track_ids = [row["id"] for row in track_rows]
                    if track_ids is None:
                        track_ids = folder_track_ids
                    else:
                        requested = {str(value) for value in track_ids}
                        track_ids = [
                            track_id
                            for track_id in folder_track_ids
                            if str(track_id) in requested
                        ]
                    selected = _select_recommended_safe(
                        db,
                        workspace,
                        kind=kind,
                        track_ids=track_ids,
                        minimum_score=minimum_score,
                        scope=scope,
                    )
                else:
                    selected = _select_recommended_safe(
                        db,
                        workspace,
                        kind=kind,
                        track_ids=track_ids,
                        minimum_score=minimum_score,
                        scope=scope,
                        search_text=str(payload.get("search") or payload.get("query") or ""),
                    )
                return jsonify(
                    {
                        "ok": True,
                        "selected": len(selected),
                        "selection_ids": selected,
                        "scope": scope,
                        **_tray_payload(db, workspace),
                    }
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
    def _validated_export_selections(
        db: Database,
        workspace: int | str,
        *,
        strict: bool = True,
        catalog_sources: list[ChartSource] | None = None,
    ) -> list[dict[str, Any]]:
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
                message = "a persisted selection is no longer part of this discovery workspace"
                if strict:
                    raise ValueError(message)
                selection = dict(data)
                selection["status"] = "selected"
                selection["eligible"] = False
                selection["eligibility"] = "NOT_EXPORTABLE"
                selection["selection_error"] = message
                selections.append(selection)
                continue
            valid, error = _validate_candidate_eligibility(
                db,
                workspace,
                matching_row,
                kind,
                catalog_sources=catalog_sources,
            )
            if not valid:
                if strict:
                    raise ValueError(error)
                selection = dict(data)
                selection["status"] = "selected"
                selection["eligible"] = False
                selection["eligibility"] = "NOT_EXPORTABLE"
                selection["selection_error"] = error
                candidate = _candidate_from_payload(_json_object(data.get("candidate_json"), {}))
                if candidate is None:
                    candidate = db.get_candidate(int(data.get("candidate_id") or 0))
                if candidate is not None:
                    selection["candidate_json"] = json.dumps(
                        {item.name: getattr(candidate, item.name) for item in fields(candidate)},
                        ensure_ascii=True,
                        default=str,
                    )
                selections.append(selection)
                continue
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
                "playable_note_count": int(getattr(item, "playable_note_count", 0) or 0),
                "metadata": {
                    "result": str(getattr(item, "result", "") or ""),
                    "playable_note_count": int(
                        getattr(item, "playable_note_count", 0) or 0
                    ),
                },
            }

        preflight = getattr(result, "preflight", None)
        manifest = getattr(result, "manifest", None)
        sync = getattr(result, "sync", None)
        payload: dict[str, Any] = {
            "kind": str(getattr(result, "kind", "") or ""),
            "destination": str(getattr(result, "destination", "") or ""),
            "success": bool(getattr(result, "success", False)),
            "status": (
                "cancelled"
                if bool(getattr(result, "cancelled", False))
                else "success" if bool(getattr(result, "success", False)) else "failed"
            ),
            "published": int(getattr(result, "published_count", 0) or 0),
            "failed": int(getattr(result, "failed_count", 0) or 0),
            "added": sum(
                1
                for item in getattr(result, "items", ())
                if getattr(item, "status", "") == "published"
                and getattr(item, "result", "") != "already_present"
            ),
            "already_present": sum(
                1
                for item in getattr(result, "items", ())
                if getattr(item, "result", "") == "already_present"
            ),
            "invalid": int(getattr(result, "invalid_count", 0) or 0),
            "download_failures": int(getattr(result, "download_failure_count", 0) or 0),
            "cancelled_items": len(getattr(result, "cancelled_items", ()) or ()),
            "ready": int(getattr(result, "ready_count", 0) or 0),
            "selected": len(getattr(result, "items", ()) or ()),
            "remaining": int(getattr(result, "remaining_count", 0) or 0),
            "bytes": int(
                getattr(result, "output_bytes", 0)
                or getattr(result, "bytes_count", 0)
                or 0
            ),
            "errors": [_safe_message(error) for error in getattr(result, "errors", ())],
            "items": [item_payload(item) for item in getattr(result, "items", ())],
        }
        if payload["status"] == "success" and (
            payload["failed"] or payload["invalid"] or payload["cancelled_items"]
        ):
            payload["status"] = "partial"
        if preflight is not None:
            payload["preflight"] = {
                "ok": bool(getattr(preflight, "ok", False)),
                "estimated_bytes": int(getattr(preflight, "estimated_bytes", 0) or 0),
                "reusable_count": int(getattr(preflight, "reusable_count", 0) or 0),
                "already_present_count": int(getattr(preflight, "already_present_count", 0) or 0),
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
                or _linked_library_root()
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
        cancellation_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], int]:

        db = Database(config.database_path)
        sources: list[ChartSource] = []
        service: ExportService | None = None
        export_id: int | None = None
        export_key = f"web-{secrets.token_hex(12)}"
        try:
            sources = list(source_factory(config))
            selections = _validated_export_selections(
                db,
                workspace,
                strict=False,
                catalog_sources=sources,
            )
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
                retry_backoff_seconds=float(
                    payload.get(
                        "retry_backoff_seconds",
                        getattr(getattr(config, "export", None), "retry_backoff_seconds", 2.0),
                    )
                    or 0
                ),
                cancellation_event=cancellation_event,
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

            checkpoint_lock = threading.Lock()

            def checkpoint(item: Any) -> None:
                """Write item progress through a short-lived DB connection.

                Materialization may run in several worker threads, while the
                owning export connection belongs to the web worker thread.
                Separate connections keep every checkpoint durable and SQLite
                thread-safe without allowing a failed checkpoint to interrupt
                the build.
                """
                item_data = {
                    "selection_id": getattr(item, "selection_id", None),
                    "source": str(getattr(item, "source", "") or ""),
                    "chart_id": getattr(item, "chart_id", None),
                    "target_relpath": str(getattr(item, "target_relpath", "") or ""),
                    "path": str(getattr(item, "target_path", "") or "")
                    if getattr(item, "target_path", None) is not None
                    else "",
                    "status": str(getattr(item, "status", "") or "pending"),
                    "result": str(getattr(item, "result", "") or ""),
                    "bytes": int(getattr(item, "bytes_count", 0) or 0),
                    "sha256": str(getattr(item, "sha256", "") or ""),
                    "source_hash": str(getattr(item, "source_hash", "") or ""),
                    "source_hash_algorithm": str(getattr(item, "source_hash_algorithm", "") or ""),
                    "artifact_key": str(getattr(item, "artifact_key", "") or ""),
                    "error": _safe_message(getattr(item, "failure", "") or ""),
                    "metadata": {
                        "result": str(getattr(item, "result", "") or ""),
                        "playable_note_count": int(
                            getattr(item, "playable_note_count", 0) or 0
                        )
                    },
                }
                with checkpoint_lock:
                    try:
                        checkpoint_db = Database(config.database_path)
                        try:
                            checkpoint_db.record_export_item(export_id, item_data)
                            item_rows = checkpoint_db.get_export_items(export_id)
                            progress_data = {
                                "selected": len(item_rows),
                                "completed": sum(
                                    row["status"] in {"published", "duplicate"}
                                    for row in item_rows
                                ),
                                "downloading": sum(row["status"] == "downloading" for row in item_rows),
                                "retrying": sum(row["status"] == "retrying" for row in item_rows),
                                "invalid": sum(row["status"] == "invalid" for row in item_rows),
                                "failed": sum(row["status"] in {"failed", "preflight_failed"} for row in item_rows),
                                "cancelled": sum(row["status"] == "cancelled" for row in item_rows),
                                "remaining": sum(
                                    row["status"] not in {
                                        "published",
                                        "duplicate",
                                        "failed",
                                        "preflight_failed",
                                        "invalid",
                                    }
                                    for row in item_rows
                                ),
                            }
                            checkpoint_db.update_export_record(
                                export_id,
                                status="running",
                                metadata={
                                    "workflow": "spotify_workspace",
                                    "progress": progress_data,
                                },
                            )
                        finally:
                            checkpoint_db.close()
                    except Exception:
                        log.debug("Could not persist export item checkpoint", exc_info=True)
                export_job.update_item(item)

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
                    cancellation_event=cancellation_event,
                    item_progress=checkpoint,
                )
            else:
                result = service.export_zip(
                    selections,
                    destination,
                    options=options,
                    eligibility_checker=eligibility_checker,
                    progress=progress,
                    cancellation_event=cancellation_event,
                    item_progress=checkpoint,
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
            return result_payload, 200 if result_payload["success"] or result_payload.get("status") == "cancelled" else 400
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

    def _preview_export(
        kind: str,
        payload: Mapping[str, Any],
        workspace: int | str,
        destination: str,
    ) -> tuple[dict[str, Any], int]:
        """Build a no-write preview from the persisted selection set."""
        db = Database(config.database_path)
        sources: list[ChartSource] = []
        service: ExportService | None = None
        try:
            sources = list(source_factory(config))
            selections = _validated_export_selections(
                db,
                workspace,
                strict=False,
                catalog_sources=sources,
            )
            if not selections:
                raise ValueError("select at least one eligible chart before building the library")
            source_map = {
                str(getattr(source, "name", "") or ""): source for source in sources
            }
            track_map: dict[Any, Track] = {}
            for track in db.get_spotify_tracks(True):
                if track.db_id is not None:
                    track_map[track.db_id] = track
                if track.spotify_id:
                    track_map[track.spotify_id] = track
                if track.track_key:
                    track_map[track.track_key] = track
            options = ExportOptions(
                managed_sync=bool(payload.get("managed_sync", False)),
                overwrite_owned=bool(payload.get("overwrite_owned", True)),
                include_manifest_in_zip=bool(payload.get("include_manifest", True)),
                minimum_free_bytes=int(payload.get("minimum_free_bytes", 0) or 0),
                continue_on_error=True,
                artifact_retries=int(payload.get("artifact_retries", 2) or 0),
                download_workers=int(payload.get("download_workers", 3) or 1),
            )
            service = ExportService(
                config,
                source_map,
                artifact_cache_dir=getattr(config, "artifact_cache_path", None),
                track_resolver=track_map,
            )

            def eligibility_checker(selection: Any, _candidate: ChartCandidate) -> bool:
                value = _row_dict(selection)
                return str(value.get("eligibility") or "").upper() in {
                    RECOMMENDED,
                    ELIGIBLE_REVIEW,
                }

            root = Path(destination).parent if kind == "zip" else Path(destination)
            preflight = service.preflight(
                selections,
                root,
                mode=kind,
                options=options,
                eligibility_checker=eligibility_checker,
            )
            items = []
            for item in preflight.items:
                issues = [issue.message for issue in item.issues if issue.fatal]
                items.append({
                    "selection_index": item.index,
                    "source": item.source_name,
                    "chart_id": item.chart_id,
                    "target_relpath": item.target_relpath,
                    "status": "invalid" if issues else "duplicate" if item.duplicate_of is not None else "ready",
                    "error": "; ".join(issues),
                })
            return {
                "ok": bool(preflight.eligible_items) and not any(
                    issue.selection_index is None for issue in preflight.errors
                ),
                "kind": kind,
                "destination": str(Path(destination).expanduser().resolve()),
                "selected_chart_count": len(selections),
                "selected": len(selections),
                "eligible_chart_count": len(preflight.eligible_items),
                "reusable_count": preflight.reusable_count,
                "already_present_count": preflight.already_present_count,
                "estimated_bytes": preflight.estimated_bytes,
                "estimated_size_bytes": preflight.estimated_bytes,
                "errors": [_safe_message(issue.message) for issue in preflight.errors],
                "items": items,
            }, 200
        finally:
            if service is not None:
                service.close()
            for source in sources:
                try:
                    source.close()
                except Exception:
                    pass
            db.close()

    @app.post("/api/spotify/export/preview")
    @app.post("/api/export/preview")
    def spotify_export_preview():
        busy = _busy_response()
        if busy is not None:
            return busy
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or payload.get("mode") or "folder").casefold()
        if kind not in {"folder", "zip"}:
            return {"error": "kind must be folder or zip"}, 400
        workspace, destination, error = _prepare_export(kind, payload)
        if error is not None:
            return error
        try:
            return jsonify(_preview_export(kind, payload, workspace, destination)[0])
        except (OSError, ValueError, TypeError, SpotifyError) as exc:
            return {"error": _safe_message(exc)}, 400

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
            export_cancel_event.clear()
            export_job.running = True
            export_job.status = "starting"
            export_job.kind = kind
            export_job.browser_download = browser_download
            export_job.destination = destination
            export_job.workspace_id = workspace
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
                kind,
                payload,
                workspace,
                destination,
                progress=export_job.update,
                cancellation_event=export_cancel_event,
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
                    result_status = str(result.get("status") or "")
                    export_job.status = (
                        "partial"
                        if result.get("success") and result_status == "partial"
                        else "complete"
                        if result.get("success")
                        else result_status or "failed"
                    )
                    if not result.get("success") and result_status != "cancelled":
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
                if export_job.status in {"complete", "success", "partial"}:
                    published = int((export_job.result or {}).get("published") or 0)
                    export_job.events.append({"ts": time.time(), "message": f"Export complete: {published} charts published"})
                elif export_job.status == "cancelled":
                    export_job.events.append({"ts": time.time(), "message": "Export cancelled; validated charts were kept and remaining items can be retried"})
                else:
                    export_job.events.append({"ts": time.time(), "message": f"Export failed: {export_job.error}"})

    def _latest_export_history() -> dict[str, Any] | None:
        """Rehydrate the last durable export for a fresh web process."""
        db = Database(config.database_path)
        try:
            workspace = export_job.workspace_id
            records = db.list_export_records(workspace_id=workspace) if workspace is not None else db.list_export_records()
            if not records:
                return None
            record = records[0]
            rows = db.get_export_items(record["id"])
            items: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                metadata = _json_object(row["metadata_json"], {})
                if not isinstance(metadata, Mapping):
                    metadata = {}
                items.append({
                    "selection_index": index,
                    "selection_id": row["selection_id"],
                    "source": row["source"],
                    "chart_id": row["chart_id"],
                    "target_relpath": row["target_relpath"],
                    "path": row["path"],
                    "status": row["status"],
                    "result": metadata.get("result", ""),
                    "error": _safe_message(row["error"] or ""),
                    "bytes": int(row["bytes"] or 0),
                    "playable_note_count": int(metadata.get("playable_note_count", 0) or 0),
                })
            terminal = {"published", "duplicate", "failed", "invalid", "preflight_failed"}
            published = [item for item in items if item["status"] == "published"]
            duplicates = [item for item in items if item["status"] == "duplicate"]
            invalid = [item for item in items if item["status"] == "invalid"]
            failed = [item for item in items if item["status"] in {"failed", "preflight_failed"}]
            cancelled = [item for item in items if item["status"] == "cancelled"]
            ready = len(published) + len(duplicates)
            record_status = str(record["status"] or "interrupted")
            result = {
                "export_id": int(record["id"]),
                "kind": record["kind"],
                "destination": record["destination"],
                "success": record_status in {"success", "complete", "completed", "partial"} and bool(ready),
                "status": "cancelled" if record_status == "cancelled" else "partial" if record_status == "partial" else "success" if record_status in {"success", "complete", "completed"} else record_status,
                "published": len(published),
                "added": sum(
                    item["status"] == "published" and item["result"] != "already_present"
                    for item in items
                ),
                "already_present": sum(item["result"] == "already_present" for item in items),
                "failed": len(failed) + len(invalid),
                "invalid": len(invalid),
                "download_failures": len(failed),
                "cancelled_items": len(cancelled),
                "ready": ready,
                "selected": len(items),
                "remaining": sum(item["status"] not in terminal for item in items),
                "bytes": sum(item["bytes"] for item in published),
                "errors": [item["error"] for item in items if item["error"]],
                "items": items,
            }
            return {
                "export_id": int(record["id"]),
                "status": "complete" if result["success"] else record_status,
                "kind": record["kind"],
                "destination": record["destination"],
                "items": {
                    "selected": len(items),
                    "completed": ready,
                    "downloading": sum(item["status"] == "downloading" for item in items),
                    "retrying": sum(item["status"] == "retrying" for item in items),
                    "failed": len(failed),
                    "invalid": len(invalid),
                    "cancelled": len(cancelled),
                    "remaining": result["remaining"],
                    "items": items,
                },
                "result": result,
                "resume_available": bool(
                    result["remaining"]
                    or result["failed"]
                    or result["invalid"]
                    or result["cancelled_items"]
                ),
            }
        finally:
            db.close()

    @app.get("/api/spotify/export/status")
    @app.get("/api/export/status")
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
                "items": export_job.item_snapshot(),
            }
            if not export_job.running and export_job.result is not None:
                payload["result"] = export_job.result
            elif not export_job.running:
                history = _latest_export_history()
                if history is not None:
                    payload.update({
                        "status": history["status"],
                        "kind": history["kind"],
                        "destination": history["destination"],
                        "export_id": history["export_id"],
                        "items": history["items"],
                        "result": history["result"],
                        "resume_available": history["resume_available"],
                    })
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
                str(
                    payload.get("path")
                    or payload.get("destination")
                    or _linked_library_root()
                    or config.library_root
                )
            )
            if not ok:
                return {"error": error}, 400
            db = Database(config.database_path)
            service: ExportService | None = None
            sources: list[ChartSource] = []
            try:
                sources = list(source_factory(config))
                selections = _validated_export_selections(
                    db,
                    workspace,
                    catalog_sources=sources,
                )
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
    def _get_local_library_workspace() -> tuple[int, str]:
        db = Database(config.database_path)
        try:
            key = db.get_meta("local_workspace_key")
            if not key:
                key = f"local:{uuid.uuid4()}"
                db.set_meta("local_workspace_key", key)
            row = db.get_workspace(key)
            workspace = int(row["id"]) if row is not None else int(db.create_workspace(
                workspace_key=key, name="Local music library", kind="exact", status="created", phase="source_setup",
                metadata={"workflow": "source_neutral"},
            ))
            return workspace, key
        finally:
            db.close()

    def _local_journey_payload(
        db: Database,
        workspace: int | str,
    ) -> dict[str, Any]:
        """Build the local workflow summary from authoritative persisted state."""
        neutral_tracks = db.get_neutral_library_tracks(workspace)
        workspace_track_ids = db.get_workspace_library_track_ids(workspace)
        candidate_rows = db.get_workspace_candidates(
            workspace,
            kind="exact",
            include_rejected=True,
            include_candidate_data=False,
        )
        if not workspace_track_ids:
            # Older local snapshots may not have the workspace projection. The
            # candidate rows are already workspace-scoped, so use their engine
            # track ids as a safe fallback.
            workspace_track_ids = {
                int(row["track_id"])
                for row in candidate_rows
                if row["track_id"] is not None
            }
        current_keys = {
            (str(track["normalized_artist"]), str(track["normalized_title"]))
            for track in neutral_tracks
        }
        current_engine_ids = {
            int(track.db_id)
            for track in db.get_spotify_tracks(True)
            if track.db_id is not None
            and (str(track.artist_norm), str(track.title_norm)) in current_keys
        }
        if current_engine_ids:
            workspace_track_ids &= current_engine_ids
        by_track: dict[int, set[str]] = {}
        for row in candidate_rows:
            track_id = row["track_id"]
            if track_id is None or int(track_id) not in workspace_track_ids:
                continue
            by_track.setdefault(int(track_id), set()).add(
                str(row["eligibility"] or "").upper()
            )

        option_tracks = set(by_track)
        strong_tracks = {
            track_id
            for track_id, states in by_track.items()
            if RECOMMENDED in states
        }
        review_tracks = {
            track_id
            for track_id, states in by_track.items()
            if ELIGIBLE_REVIEW in states
        }
        not_playable_tracks = {
            track_id
            for track_id, states in by_track.items()
            if INCOMPATIBLE in states
            and not states.intersection({RECOMMENDED, ELIGIBLE_REVIEW})
        }

        workspace_row = db.get_workspace(workspace)
        progress_row = db.get_workspace_progress(workspace)
        workspace_status = str(
            workspace_row["status"] if workspace_row is not None else ""
        ).casefold()
        progress_status = str(
            progress_row["status"] if progress_row is not None else ""
        ).casefold()
        metadata = _json_object(
            workspace_row["metadata_json"] if workspace_row is not None else {},
            {},
        )
        progress_metadata = _json_object(
            progress_row["metadata_json"] if progress_row is not None else {},
            {},
        )
        metadata = metadata if isinstance(metadata, Mapping) else {}
        progress_metadata = (
            progress_metadata if isinstance(progress_metadata, Mapping) else {}
        )
        spotify_import_active = progress_metadata.get("scan_key") == "spotify_import" and progress_status == "running"
        partial = bool(
            metadata.get("partial")
            or progress_metadata.get("partial")
            or (workspace_row is not None and int(workspace_row["error_count"] or 0) > 0)
        )
        with soundcloud_import_job.lock:
            import_active = bool(
                soundcloud_import_job.running
                and int(soundcloud_import_job.workspace_id or 0) == int(workspace)
            )
        with spotify_job.lock:
            matching_active = bool(
                spotify_job.running
                and int(spotify_job.workspace_id or 0) == int(workspace)
            )
        stale_active = (
            (workspace_status in {"pending", "running"} or progress_status in {"pending", "running"})
            and not import_active
            and not matching_active
        )
        active = import_active or matching_active
        failed = workspace_status in {"failed", "error"} or progress_status in {
            "failed",
            "error",
        }
        incomplete = stale_active or partial or workspace_status in {
            "partial",
            "interrupted",
            "cancelled",
        } or progress_status in {"partial", "interrupted", "cancelled"}
        matching_started = bool(
            active
            or workspace_status not in {"", "created", "ready_for_matching", "source_setup", "idle"}
            and (
                candidate_rows
                or metadata.get("run_id")
                or progress_metadata.get("run_id")
                or workspace_status
                in {"complete", "completed", "success", "failed", "error", "partial", "interrupted", "cancelled"}
            )
        )
        if import_active:
            matching_status = "running"
            headline = "Import in progress"
            message = "Your music is still being imported. Matching will be available when the import finishes."
        elif spotify_import_active:
            matching_status = "running"
            headline = "Reading Spotify music"
            message = str(progress_row["message"] if progress_row is not None else "Reading selected Spotify music...")
        elif matching_active:
            matching_status = "running"
            headline = "Matching in progress"
            message = "We are checking your songs for playable charts. You can leave this page and return later."
        elif failed:
            matching_status = "failed"
            headline = "Matching stopped"
            message = "Matching did not finish. Run Find chart matches again before deciding about the remaining songs."
        elif incomplete:
            matching_status = "incomplete"
            headline = "Matching incomplete"
            message = "Some songs could not be checked. Review the results, then run Find chart matches again."
        elif workspace_status in {"complete", "completed", "success"} or (
            candidate_rows and workspace_status not in {"created", "ready_for_matching", "source_setup", "idle"}
        ):
            matching_status = "complete"
            # Persisted local candidates already prove that the catalog was
            # available for this completed run. Avoid reloading the full
            # 94k-chart snapshot just to paint the review page.
            if candidate_rows:
                catalog_available = True
            else:
                catalog = load_enchor_catalog(config.catalog_path)
                catalog_available = catalog.available
            if catalog_available:
                headline = "Matching complete"
                message = "Your songs have been checked. Review the recommended charts before exporting."
            else:
                headline = "Chart catalog unavailable"
                message = "Your music was imported, but the local chart catalog is unavailable. No recommendations were made."
        elif neutral_tracks:
            matching_status = "ready"
            headline = "Ready to find charts"
            message = "Your music is imported. Find chart matches when you are ready."
        else:
            matching_status = "empty"
            headline = "Add your music"
            message = "Import a SoundCloud export to start finding playable charts."

        if matching_status not in {"complete"}:
            catalog_available = None
            catalog_diagnostic = ""
        else:
            if candidate_rows:
                catalog_available = True
                catalog_diagnostic = ""
            else:
                catalog = load_enchor_catalog(config.catalog_path)
                catalog_available = catalog.available
                catalog_diagnostic = str(getattr(catalog, "diagnostic", "") or "")

        selected = len(db.list_workspace_selections(workspace, kind="exact"))
        total = len(neutral_tracks)
        source_rows = [dict(row) for row in db.get_source_summary(workspace)]
        source_by_provider = {str(row["provider"]).casefold(): row for row in source_rows}
        provenance = db.get_track_provenance(workspace)
        source_sets: dict[tuple[str, str], set[str]] = {}
        display_names: dict[tuple[str, str], tuple[str, str]] = {}
        for track in neutral_tracks:
            key = (str(track["normalized_artist"]), str(track["normalized_title"]))
            display_names[key] = (str(track["artist"]), str(track["title"]))
            source_sets.setdefault(key, set())
        for row in provenance:
            key = (str(row["normalized_artist"]), str(row["normalized_title"]))
            source_sets.setdefault(key, set()).add(str(row["provider"]).casefold())
        music = [
            {
                "artist": display_names[key][0],
                "title": display_names[key][1],
                "sources": sorted(source_sets[key]),
                "source_label": "both" if len(source_sets[key]) > 1 else (next(iter(source_sets[key]), "unknown")),
            }
            for key in sorted(source_sets, key=lambda value: display_names.get(value, value))
        ]
        combined_sources = {
            "soundcloud_count": int(source_by_provider.get("soundcloud", {}).get("tracks", 0) or 0),
            "spotify_count": int(source_by_provider.get("spotify", {}).get("tracks", 0) or 0),
            "shared_count": sum(len(value) > 1 for value in source_sets.values()),
            "duplicates_merged": sum(int(source_by_provider.get(provider, {}).get("duplicate_rows", 0) or 0) for provider in ("soundcloud", "spotify")),
            "canonical_count": total,
        }
        progress_payload = dict(progress_row) if progress_row is not None else {}
        progress_metadata = _json_object(progress_payload.get("metadata_json", "{}"), {})
        catalog_chart_count = int(progress_metadata.get("catalog_chart_count", 0) or 0)
        if matching_status == "complete" and not catalog_chart_count:
            catalog = load_enchor_catalog(config.catalog_path)
            catalog_chart_count = int(catalog.chart_count if catalog.available else 0)
        activity_rows = [dict(row) for row in db.get_discovery_song_progress(workspace, limit=8, offset=0)]
        matching_total = int(progress_payload.get("total_tracks", 0) or 0)
        progress_summary = db.get_discovery_song_progress_summary(workspace)
        effective_matching_total = matching_total or progress_summary["total"]
        matching_total = effective_matching_total
        if matching_status == "complete" and progress_summary["total"] == total and progress_summary["completed"] < total:
            matching_status = "incomplete"
            headline = "Matching incomplete"
            message = "Matching stopped before every song received a final classification."
        current_song = next((row for row in activity_rows if str(row.get("status") or "") == "checking"), None)
        safe_tracks = strong_tracks | review_tracks
        if matching_status in {"running", "complete", "incomplete", "failed"} and effective_matching_total == total and progress_summary["total"] == total:
            option_count = progress_summary["strong"] + progress_summary["review"]
            strong_count = progress_summary["strong"]
            review_count = progress_summary["review"]
            not_playable_count = progress_summary["not_playable"]
            no_candidate_count = progress_summary["no_catalog_candidate"]
            no_suitable_count = progress_summary["no_suitable"]
            completed_count = progress_summary["completed"]
        else:
            option_count = len(strong_tracks | review_tracks)
            strong_count = len(strong_tracks)
            review_count = len(review_tracks)
            not_playable_count = len(not_playable_tracks)
            no_candidate_count = max(0, total - len(option_tracks))
            no_suitable_count = max(0, total - len(safe_tracks))
            completed_count = int(progress_payload.get("completed_tracks", 0) or 0)
        terminal = matching_status in {"complete", "incomplete", "failed"}
        run_id = str(
            progress_metadata.get("run_id")
            or metadata.get("run_id")
            or db.get_meta("local_current_run_id", "")
            or ""
        )
        return {
            "source_neutral": True,
            "has_imported_music": bool(neutral_tracks),
            "workspace_id": int(workspace),
            "matching_status": matching_status,
            "headline": headline,
            "message": message,
            "matching_started": matching_started,
            "matching_complete": matching_status == "complete",
            "review_available": terminal,
            "catalog_available": catalog_available,
            "catalog_diagnostic": catalog_diagnostic,
            "run_id": run_id,
            "review_url": f"/advanced?workspace_id={int(workspace)}",
                "counts": {
                    "imported_songs": total,
                    "chart_options": option_count,
                "candidate_charts": sum(
                    1
                    for row in candidate_rows
                    if row["track_id"] is not None
                    and int(row["track_id"]) in workspace_track_ids
                ),
                    "strong_matches": strong_count,
                    "needs_review": review_count,
                    "no_candidate_songs": no_candidate_count,
                    "not_playable_songs": not_playable_count,
                    "no_suitable_songs": no_suitable_count,
                "selected_charts": selected,
            },
            "sources": {
                "soundcloud": {"state": "imported" if "soundcloud" in source_by_provider else "not_imported", "count": combined_sources["soundcloud_count"], "raw_rows": int(source_by_provider.get("soundcloud", {}).get("total_rows", 0) or 0), "accepted_rows": int(source_by_provider.get("soundcloud", {}).get("accepted_rows", 0) or 0), "skipped": int(source_by_provider.get("soundcloud", {}).get("skipped_rows", 0) or 0), "duplicates": int(source_by_provider.get("soundcloud", {}).get("duplicate_rows", 0) or 0)},
                "spotify": {"state": "connected" if _is_authenticated() else "not_connected", "imported": "spotify" in source_by_provider, "count": combined_sources["spotify_count"], "raw_rows": int(source_by_provider.get("spotify", {}).get("total_rows", 0) or 0), "accepted_rows": int(source_by_provider.get("spotify", {}).get("accepted_rows", 0) or 0), "skipped": int(source_by_provider.get("spotify", {}).get("skipped_rows", 0) or 0), "duplicates": int(source_by_provider.get("spotify", {}).get("duplicate_rows", 0) or 0)},
            },
            "combined": combined_sources,
            "music": music[:250],
            "music_total": len(music),
            "catalog_chart_count": catalog_chart_count,
            "activity": activity_rows,
            "visualization": {
                "spotify_count": combined_sources["spotify_count"],
                "soundcloud_count": combined_sources["soundcloud_count"],
                "shared_count": combined_sources["shared_count"],
                "canonical_count": total,
                "catalog_chart_count": catalog_chart_count,
                "processed_count": int(progress_payload.get("completed_tracks", 0) or 0),
                "chartable_count": len(option_tracks),
                "strong_count": len(strong_tracks),
                "review_count": len(review_tracks),
                "phase": str(progress_payload.get("phase") or matching_status),
                "complete": matching_status == "complete",
                "spotify_items_processed": int(progress_metadata.get("spotify_items_processed", 0) or 0),
                "spotify_items_expected": progress_metadata.get("spotify_items_expected"),
                "spotify_playlist_index": progress_metadata.get("playlist_index"),
                "spotify_playlist_total": progress_metadata.get("playlist_total"),
                "spotify_playlist_name": progress_metadata.get("playlist_name", ""),
                "spotify_playlist_items_read": progress_metadata.get("playlist_items_read"),
                "spotify_playlist_total_items": progress_metadata.get("playlist_total_items"),
                "elapsed_seconds": progress_metadata.get("elapsed_seconds"),
                "eta_seconds": progress_metadata.get("eta_seconds"),
                "eta_seconds": progress_metadata.get("eta_seconds"),
            },
            "matching_dataset": {
                "canonical_count": total,
                "matching_total": matching_total,
                "consistent": matching_status not in {"complete", "incomplete", "failed"} or matching_total == total,
            },
            "progress": {
                "phase": str(progress_payload.get("phase") or matching_status),
                "processed": completed_count,
                "total": effective_matching_total or total,
                "elapsed_seconds": progress_metadata.get("elapsed_seconds"),
                "current_song": {"artist": str(current_song["artist"]), "title": str(current_song["title"])} if current_song else None,
                "recent": activity_rows,
            },
        }

    @app.get("/")
    def index():
        return (
            _UNIFIED_PAGE
            .replace("__OUTPUT_DEFAULT__", str(config.library_root))
            .replace("__LAN_ACCESS_TOKEN__", json.dumps(str(app.config.get("LAN_ACCESS_TOKEN") or "")))
            .replace("__PUBLIC_MODE__", "true" if app.config.get("PUBLIC_MODE") else "false")
        )

    @app.get("/advanced")
    def advanced():
        linked = _linked_library_root() or config.library_root
        return _ADVANCED_PAGE.replace("__LIBRARY_ROOT__", str(linked))

    # -------------------------------------------------------------- files
    @app.get("/browser-exporter.js")
    def browser_exporter_asset():
        asset = config.base_dir / "browser_export" / "soundcloud-exporter.user.js"
        if not asset.is_file():
            abort(404)
        return send_file(asset, mimetype="text/javascript", as_attachment=True, download_name=asset.name)

    @app.get("/api/sources")
    def source_status():
        workspace, key = _get_local_library_workspace()
        db = Database(config.database_path)
        try:
            summaries = [dict(row) for row in db.get_source_summary(workspace)]
            return jsonify({"workspace_id": workspace, "workspace_key": key, "sources": summaries,
                            "combined": db.get_combined_summary(workspace),
                            "journey": _local_journey_payload(db, workspace)})
        finally:
            db.close()

    @app.post("/api/sources/soundcloud/import")
    def import_soundcloud_export():
        uploaded = request.files.get("file") or request.files.get("files")
        if uploaded is None or not uploaded.filename:
            return {"error": "upload a SoundCloud CSV or JSON export"}, 400
        filename = Path(uploaded.filename).name
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".csv", ".json"}:
            return {"error": "SoundCloud imports support only CSV or JSON"}, 400
        busy = _busy_response()
        if busy is not None:
            return busy
        try:
            raw = uploaded.read()
            if len(raw) > int(config.server.max_content_length):
                return {"error": "SoundCloud export is too large"}, 413
            workspace, key = _get_local_library_workspace()
            with soundcloud_import_job.lock:
                if soundcloud_import_job.running:
                    return {"error": "a SoundCloud import is already running"}, 409
                busy = _busy_response()
                if busy is not None:
                    return busy
                diagnostics = RunDiagnostics(config.state_path)
                diagnostics.update(
                    "source_setup",
                    workspace_id=workspace,
                    workspace_key=key,
                    uploaded_files=[{"name": filename, "bytes": len(raw)}],
                )
                soundcloud_import_job.begin(
                    workspace_id=workspace,
                    workspace_key=key,
                    run_id=diagnostics.run_id,
                    filename=filename,
                    total_bytes=len(raw),
                )
                run_id = soundcloud_import_job.run_id

            def worker() -> None:
                db: Database | None = None
                provider: Any | None = None
                terminal: dict[str, Any] | None = None
                try:
                    soundcloud_import_job.update({
                        "phase": "parse",
                        "message": "Reading the SoundCloud export...",
                        "bytes_read": len(raw),
                    })
                    from csv_to_clonehero.providers.soundcloud import SoundCloudExportProvider

                    text = raw.decode("utf-8-sig")
                    provider = (
                        SoundCloudExportProvider.from_json(text)
                        if suffix == ".json"
                        else SoundCloudExportProvider.from_csv(text)
                    )
                    snapshot = provider.import_library()
                    report = snapshot.import_report
                    report_data = report.as_dict() if report else {}
                    total_rows = int(report_data.get("total_rows", 0) or 0)
                    accepted_rows = int(report_data.get("accepted_rows", 0) or 0)
                    skipped_rows = int(report_data.get("skipped_rows", 0) or 0)
                    duplicate_rows = int(report_data.get("duplicate_rows", 0) or 0)
                    soundcloud_import_job.update({
                        "phase": "parsed",
                        "message": f"Read {total_rows} rows; {len(snapshot.tracks)} usable tracks found.",
                        "total_rows": total_rows,
                        "accepted_rows": accepted_rows,
                        "skipped_rows": skipped_rows,
                        "duplicate_rows": duplicate_rows,
                        "total_tracks": len(snapshot.tracks),
                    })
                    diagnostics.update(
                        "import",
                        counters={
                            "total_input_rows": total_rows,
                            "accepted_input_rows": accepted_rows,
                            "skipped_input_rows": skipped_rows,
                            "duplicate_input_rows": duplicate_rows,
                            "unique_imported_tracks": len(snapshot.tracks),
                            "unique_imported_artists": len({track.artist_norm for track in snapshot.tracks}),
                        },
                        skipped_reasons=(report.skipped_reasons[:200] if report else []),
                        samples={
                            "tracks": [
                                {"artist": track.artist_norm, "title": track.title_norm}
                                for track in snapshot.tracks
                            ]
                        },
                    )
                    if not snapshot.tracks:
                        error = "SoundCloud export contained no usable tracks"
                        diagnostics.finish("failed", error=error)
                        terminal = {
                            "ok": False,
                            "status": "failed",
                            "phase": "failed",
                            "error": error,
                            "workspace_id": workspace,
                            "workspace_key": key,
                            "run_id": run_id,
                            "diagnostics_url": f"/api/diagnostics/{run_id}",
                            "report": report_data,
                        }
                        return

                    db = Database(config.database_path)
                    db.update_workspace(
                        workspace,
                        status="running",
                        phase="source_import",
                        error="",
                    )

                    def progress(payload: Mapping[str, Any]) -> None:
                        data = dict(payload)
                        if data.get("phase") == "import":
                            data["phase"] = "persist"
                            data.setdefault("message", "Preparing the imported tracks...")
                        elif data.get("phase") == "combine":
                            data["message"] = "Finishing the combined library summary..."
                        soundcloud_import_job.update(data)

                    from csv_to_clonehero.library_pipeline import run_library_import

                    imported = run_library_import(
                        config,
                        db,
                        workspace,
                        {"soundcloud": provider},
                        on_progress=progress,
                    )
                    provider = None  # run_library_import closes each provider.
                    # A new source snapshot invalidates old exact results and
                    # selections; otherwise a fresh import could display a
                    # recommendation for a song no longer in the export.
                    db.clear_workspace_candidates(workspace, kind="exact")
                    source_result = imported.sources.get("soundcloud")
                    source_payload = source_result.__dict__ if source_result else {}
                    combined = imported.combined_summary
                    sources = [dict(row) for row in db.get_source_summary(workspace)]
                    db.set_meta("local_current_run_id", run_id)
                    diagnostics.update(
                        "combine",
                        counters={
                            "persisted_neutral_tracks": combined.get("tracks", 0),
                            "persisted_provenance_rows": report_data.get("unique_tracks", 0),
                        },
                    )
                    diagnostics.finish(
                        imported.status,
                        workspace_id=workspace,
                        workspace_key=key,
                        counters={"combined_tracks": combined.get("tracks", 0)},
                    )
                    terminal = {
                        "ok": imported.status in {"complete", "partial"},
                        "status": imported.status,
                        "phase": "complete",
                        "message": f"Imported {source_result.tracks if source_result else 0} SoundCloud songs.",
                        "workspace_id": workspace,
                        "workspace_key": key,
                        "run_id": run_id,
                        "diagnostics_url": f"/api/diagnostics/{run_id}",
                        "source": source_payload,
                        "sources": sources,
                        "combined": combined,
                    }
                    db.update_workspace(
                        workspace,
                        status="ready_for_matching",
                        phase="ready_for_matching",
                        message="Music imported; ready to find charts.",
                        error="",
                    )
                    db.update_workspace_progress(
                        workspace,
                        status="ready_for_matching",
                        phase="ready_for_matching",
                        message="Music imported; ready to find charts.",
                        error="",
                        metadata={"run_id": run_id, "scan_key": "import"},
                    )
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    message = _safe_message(exc)
                    diagnostics.finish("failed", error=message)
                    terminal = {
                        "ok": False,
                        "status": "failed",
                        "phase": "failed",
                        "error": message,
                        "workspace_id": workspace,
                        "workspace_key": key,
                        "run_id": run_id,
                        "diagnostics_url": f"/api/diagnostics/{run_id}",
                    }
                except Exception as exc:  # noqa: BLE001
                    message = _safe_message(exc)
                    log.exception("SoundCloud import %s failed", run_id)
                    try:
                        diagnostics.finish("failed", error=message)
                    except OSError:
                        pass
                    try:
                        if db is not None:
                            db.update_workspace(
                                workspace,
                                status="failed",
                                phase="source_import",
                                error=message,
                            )
                    except Exception:
                        pass
                    terminal = {
                        "ok": False,
                        "status": "failed",
                        "phase": "failed",
                        "error": message,
                        "workspace_id": workspace,
                        "workspace_key": key,
                        "run_id": run_id,
                        "diagnostics_url": f"/api/diagnostics/{run_id}",
                    }
                finally:
                    if provider is not None:
                        try:
                            provider.close()
                        except Exception:
                            pass
                    if db is not None:
                        db.close()
                    if terminal is None:
                        terminal = {
                            "ok": False,
                            "status": "failed",
                            "phase": "failed",
                            "error": "SoundCloud import stopped unexpectedly",
                            "workspace_id": workspace,
                            "workspace_key": key,
                            "run_id": run_id,
                            "diagnostics_url": f"/api/diagnostics/{run_id}",
                        }
                        try:
                            diagnostics.finish("failed", error=terminal["error"])
                        except OSError:
                            pass
                    soundcloud_import_job.finish(terminal)

            worker_thread = threading.Thread(
                target=worker,
                daemon=True,
                name=f"soundcloud-import-{run_id}",
            )
            soundcloud_import_job.attach_worker(worker_thread)
            worker_thread.start()
            return jsonify({
                "started": True,
                "status": "running",
                "workspace_id": workspace,
                "workspace_key": key,
                "run_id": run_id,
                "diagnostics_url": f"/api/diagnostics/{run_id}",
            }), 202
        except OSError as exc:
            return {"error": _safe_message(exc)}, 500

    @app.get("/api/sources/soundcloud/import/status")
    def soundcloud_import_status():
        requested = str(request.args.get("run_id") or "")
        payload = soundcloud_import_job.snapshot()
        current = str(payload.get("run_id") or "")
        if requested and current and requested != current:
            return {"error": "unknown SoundCloud import run"}, 404
        run_id = requested or current
        if run_id:
            try:
                artifact = diagnostics_path(config.state_path, run_id)
                if artifact.is_file():
                    payload["diagnostics_url"] = f"/api/diagnostics/{run_id}"
                    if not payload["running"]:
                        payload["diagnostics"] = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                pass
        return jsonify(payload)

    @app.post("/api/spotify/export/cancel")
    @app.post("/api/export/cancel")
    def spotify_export_cancel():
        with export_job.lock:
            if not export_job.running:
                return jsonify({"ok": True, "cancelled": False, "status": export_job.status})
            export_cancel_event.set()
            export_job.status = "cancelling"
            export_job.current = "Cancellation requested; finishing the active chart safely"
            export_job.events.append({"ts": time.time(), "message": export_job.current})
        return jsonify({"ok": True, "cancelled": True, "status": "cancelling"})

    @app.get("/api/diagnostics/<run_id>")
    def download_diagnostics(run_id: str):
        if not _safe_local_request():
            return {"error": "diagnostics are local-only"}, 403
        try:
            path = diagnostics_path(config.state_path, run_id)
        except ValueError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/json", as_attachment=True, download_name="diagnostics.json")

    @app.get("/api/library/summary")
    def library_summary():
        workspace, _ = _get_local_library_workspace()
        db = Database(config.database_path)
        try:
            return jsonify({"workspace_id": workspace, "sources": [dict(row) for row in db.get_source_summary(workspace)], "combined": db.get_combined_summary(workspace)})
        finally:
            db.close()

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
        return jsonify({
            **state.snapshot(),
            "busy": bool(running_jobs),
            "running_jobs": running_jobs,
            "soundcloud_import": soundcloud_import_job.snapshot(),
        })

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
    def _local_neutral_review_payload(
        db: Database,
        workspace: int | str,
        include_rejected: bool = False,
        *,
        offset: int = 0,
        limit: int = REVIEW_DEFAULT_PAGE_SIZE,
        search: str = "",
        state: str = "all",
        sort: str = "artist",
    ) -> dict[str, Any]:
        """Expose source-neutral catalog candidates on the existing review page."""
        journey = _local_journey_payload(db, workspace)
        workspace_track_ids = db.get_workspace_library_track_ids(workspace)
        tracks = {
            int(track.db_id): track
            for track in db.get_tracks(include_ignored=True)
            if track.db_id is not None
            and (not workspace_track_ids or int(track.db_id) in workspace_track_ids)
        }
        manual_decisions = db.get_manual_decisions()
        rows = db.get_workspace_candidates(
            workspace,
            kind="exact",
            # Keep hard-gated results visible so a user can understand why a
            # song has no safe recommendation. Softly rejected rows stay
            # behind the explicit rejected-results disclosure.
            include_rejected=True,
            include_candidate_data=bool(search),
        )
        grouped_rows: dict[int, list[Any]] = {}
        for row in rows:
            data = _row_dict(row)
            track_id = data.get("track_id")
            if track_id is None or int(track_id) not in tracks:
                continue
            track = tracks[int(track_id)]
            row_state = _review_row_state(row)
            if not include_rejected and row_state not in {
                "strong",
                "needs_review",
                "not_playable",
            }:
                continue
            grouped_rows.setdefault(int(track_id), []).append(row)

        summaries: list[dict[str, Any]] = []
        for track_id, row_group in grouped_rows.items():
            track = tracks[track_id]
            states = {_review_row_state(row) for row in row_group}
            selected = any(
                bool(_row_dict(row).get("selected"))
                or manual_decisions.get(
                    (
                        track.track_key,
                        str(_row_dict(row).get("source") or ""),
                        int(_row_dict(row).get("chart_id") or 0),
                    ),
                    "",
                )
                == "accept"
                for row in row_group
            )
            best_score = max(
                (float(_row_dict(row).get("score") or 0) for row in row_group),
                default=0.0,
            )
            summaries.append({
                "_track_id": track_id,
                "_best_score": best_score,
                "track_key": track.track_key,
                "artist": track.artist_full,
                "title": track.title,
                "album": track.album,
                "playlists": list(track.playlists),
                "selected": selected,
                "status": "ACCEPTED" if selected else "AUTO_ACCEPT" if "strong" in states else "REVIEW" if "needs_review" in states else "REJECT",
                "review_state": "strong" if "strong" in states else "needs_review" if "needs_review" in states else "not_playable" if "not_playable" in states else "not_recommended",
                "has_eligible_match": bool(states & {"strong", "needs_review"}),
                "has_strong_match": "strong" in states,
                "has_review_match": "needs_review" in states and "strong" not in states,
                "_search_text": (
                    ""
                    if not search
                    else " ".join(
                        [
                            track.artist_full,
                            track.title,
                            track.album,
                            *(track.playlists or ()),
                            *(_review_row_search_text(row, track) for row in row_group),
                        ]
                    ).casefold()
                ),
            })
        page = _paginate_review_cards(
            summaries,
            offset=offset,
            limit=limit,
            search=search,
            state=state,
            sort=sort,
        )

        cards: list[dict[str, Any]] = []
        page_rows = db.get_workspace_candidates_for_tracks(
            workspace,
            [int(summary["_track_id"]) for summary in page["tracks"]],
            kind="exact",
            include_rejected=True,
        )
        for summary in page["tracks"]:
            track_id = int(summary["_track_id"])
            track = tracks[track_id]
            candidates: list[dict[str, Any]] = []
            detail_rows = [
                row
                for row in page_rows.get(track_id, ())
                if include_rejected
                or _review_row_state(row)
                in {"strong", "needs_review", "not_playable"}
            ]
            for row in detail_rows:
                candidate = _candidate_payload(row)
                manual = manual_decisions.get(
                    (
                        track.track_key,
                        str(candidate.get("source") or ""),
                        int(candidate.get("chart_id") or 0),
                    ),
                    "",
                )
                candidate["manual"] = manual
                candidate["manual_decision"] = manual
                candidate["review"] = build_review_presentation(candidate)
                candidate["review_state"] = candidate["review"]["state"]
                candidate["review_label"] = candidate["review"]["label"]
                candidate["review_summary"] = candidate["review"]["summary"]
                candidate["selectable"] = candidate["review"]["selectable"]
                candidate["selection_action"] = candidate["review"]["action"]
                candidates.append(candidate)
            candidates.sort(
                key=lambda item: (
                    -float(item.get("score") or 0),
                    str(item.get("candidate_artist") or "").casefold(),
                    str(item.get("candidate_title") or "").casefold(),
                    str(item.get("source") or ""),
                    int(item.get("chart_id") or 0),
                )
            )
            best = next(
                (item for item in candidates if item["review_state"] in {"strong", "needs_review"}),
                candidates[0],
            )
            for candidate in candidates:
                candidate["best"] = candidate is best
            selected = next((item for item in candidates if item.get("selected")), None)
            primary = selected or best
            cards.append({
                "track_key": track.track_key,
                "spotify_id": track.spotify_id,
                "artist": track.artist_full,
                "title": track.title,
                "album": track.album,
                "playlists": list(track.playlists),
                "spotify_artist": track.artist_full,
                "spotify_title": track.title,
                "status": "selected" if selected else "matched",
                "review_status": primary["review_label"],
                "candidates": candidates,
                "best_candidate_id": best["candidate_id"],
                "selected_candidate_id": selected["candidate_id"] if selected else None,
                "selected": bool(selected),
                "review": primary["review"],
                "review_state": primary["review_state"],
                "review_label": primary["review_label"],
                "review_summary": primary["review_summary"],
                "has_eligible_match": summary["has_eligible_match"],
                "has_strong_match": summary["has_strong_match"],
                "has_review_match": summary["has_review_match"],
            })
        total_tracks = len(tracks)
        candidate_count = sum(len(row_group) for row_group in grouped_rows.values())
        page_metadata = page["page"]
        return {
            "workspace_id": int(workspace),
            "source_neutral": True,
            "journey": journey,
            "tracks": cards,
            "progress": {
                "has_search_run": bool(journey["matching_started"]),
                "total_tracks": total_tracks,
                "scored_tracks": total_tracks if journey["matching_complete"] else len(cards),
                "review_track_count": len(cards),
                "review_tracks": len(cards),
                "review_candidate_count": candidate_count,
                "review_candidates": candidate_count,
                "candidate_count": candidate_count,
                "page_track_count": page_metadata["returned"],
                "searching": journey["matching_status"] == "running",
                "exact_scoring_complete": journey["matching_complete"],
                "exact_complete": journey["matching_complete"],
                "matching_status": journey["matching_status"],
                "catalog_available": journey["catalog_available"],
                "no_candidate_tracks": journey["counts"]["no_candidate_songs"],
                "no_suitable_tracks": journey["counts"]["no_suitable_songs"],
                "selected_count": journey["counts"]["selected_charts"],
            },
            "page": page_metadata,
            "track_total": page_metadata["total"],
            "track_offset": page_metadata["offset"],
            "track_limit": page_metadata["limit"],
            "has_more": page_metadata["has_more"],
        }

    @app.get("/api/review")
    def review_items():
        include_rejected = request.args.get("include_rejected") == "1"
        page_options = _review_page_options(request.args)
        db = Database(config.database_path)
        try:
            local_key = db.get_meta("local_workspace_key")
            local_workspace = db.get_workspace(local_key) if local_key else None
            if local_workspace is not None:
                neutral_rows = db.get_neutral_library_tracks(int(local_workspace["id"]))
                neutral_candidates = db.get_workspace_candidates(
                    int(local_workspace["id"]),
                    kind="exact",
                    include_rejected=True,
                    include_candidate_data=False,
                )
                if neutral_rows or neutral_candidates:
                    return jsonify(_local_neutral_review_payload(
                        db,
                        int(local_workspace["id"]),
                        include_rejected,
                        **page_options,
                    ))
            return jsonify(
                _review_payload(
                    db,
                    state.snapshot(),
                    include_rejected,
                    **page_options,
                )
            )
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
            if payload.get("workspace_id") not in (None, ""):
                workspace = _workspace_id(payload.get("workspace_id"))
                row, kind, track_value, error = _candidate_row_for_request(
                    db,
                    workspace,
                    {**payload, "kind": "exact"},
                )
                if row is None:
                    return {"error": error}, 404
                data = _row_dict(row)
                if decision == "accept":
                    valid, validation_error = _validate_candidate_eligibility(
                        db, workspace, row, kind
                    )
                    if not valid:
                        return {"error": validation_error}, 400
                    selection_id = db.select_workspace_candidate(
                        workspace,
                        track_value,
                        data["candidate_id"],
                        kind="exact",
                        score=data.get("score"),
                        explanation=data.get("breakdown_json") or "",
                        eligibility=data.get("eligibility") or "",
                    )
                else:
                    selection_id = None
                    db.clear_workspace_selection(
                        workspace,
                        track_value,
                        candidate_id=data.get("candidate_id"),
                        kind="exact",
                    )
                db.set_manual_decision(track_key, source, chart_id, decision)
                return jsonify({
                    "ok": True,
                    "workspace_id": workspace,
                    "selection_id": selection_id,
                    "decision": decision,
                })
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
        payload = request.get_json(silent=True) or {}
        with state.lock:
            if state.running:
                return {"error": "wait for the current job to finish"}, 409
            db = Database(config.database_path)
            try:
                if payload.get("workspace_id") not in (None, ""):
                    workspace = _workspace_id(payload.get("workspace_id"))
                    selected = _select_recommended_safe(
                        db,
                        workspace,
                        kind="exact",
                        scope="eligible",
                    )
                    return jsonify({
                        "ok": True,
                        "accepted": len(selected),
                        "workspace_id": workspace,
                        **_tray_payload(db, workspace),
                    })
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
        db = Database(config.database_path)
        try:
            local_key = db.get_meta("local_workspace_key")
            local_workspace = db.get_workspace(local_key) if local_key else None
            if local_workspace is not None:
                workspace = int(local_workspace["id"])
                neutral_tracks = db.get_neutral_library_tracks(workspace)
                if neutral_tracks:
                    journey = _local_journey_payload(db, workspace)
                    counts = journey["counts"]
                    return jsonify({
                        "csv_files": len(db.get_source_summary(workspace)),
                        "unique_tracks": len(neutral_tracks),
                        "primary_artists": len({row["normalized_artist"] for row in neutral_tracks}),
                        "exact": {
                            "auto_matches": counts["strong_matches"],
                            "review": counts["needs_review"],
                            "no_match": counts["no_candidate_songs"],
                        },
                        "expansion": {"selected": 0},
                        "manual_decisions": {"accept": counts["selected_charts"], "reject": 0},
                        "plan": {"estimated_gib": 0},
                        "downloads": {"completed": 0, "gib_downloaded": 0},
                        "journey": journey,
                    })
        finally:
            db.close()
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


_UNIFIED_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music to Clone Hero</title><link rel="icon" href="data:,"><style>
:root{color-scheme:dark;--bg:#10131a;--panel:#1b202b;--line:#394150;--muted:#aab3c2;--accent:#8fd3ff;--success:#42d392;--warn:#ffd479}
*{box-sizing:border-box}body{font:16px system-ui,sans-serif;width:100%;max-width:980px;margin:auto;padding:24px;background:var(--bg);color:#eee;overflow-wrap:anywhere}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin:14px 0;min-width:0}
button,.primary{font:inherit;padding:10px 14px;border:1px solid #4b83bb;border-radius:7px;background:#3b82f6;color:white;cursor:pointer;margin:4px;text-decoration:none;display:inline-block}
button:disabled{opacity:.5;cursor:default}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
input{margin:8px;max-width:100%}small,.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:12px}.card{border:1px solid #465064;padding:14px;border-radius:8px;min-width:0}
.journey-status{border-top:1px solid var(--line);margin-top:16px;padding-top:16px}.journey-status h3{margin:0 0 6px}.journey-status p{margin:6px 0}.journey-status--warning{border-left:3px solid var(--warn);padding-left:12px}.journey-status--success{border-left:3px solid var(--success);padding-left:12px}
.source-breakdown{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}.source-breakdown .card{padding:12px}.source-breakdown strong{display:block;font-size:1.3rem}.source-filter{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:12px}.source-filter button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}.music-list{list-style:none;padding:0;max-height:280px;overflow:auto;border:1px solid var(--line)}.music-list li{padding:7px 10px;border-bottom:1px solid var(--line)}.music-list li:last-child{border-bottom:0}.happening-list{list-style:none;padding:0;margin:8px 0}.happening-list li{padding:7px 0;border-bottom:1px solid var(--line)}.happening-list li:last-child{border-bottom:0}.visual-placeholder{min-height:150px;display:grid;place-items:center;text-align:center;border:1px dashed var(--accent);border-radius:9px;background:#121b25}.visual-placeholder p{margin:4px}
details{margin-top:12px}summary{cursor:pointer}.diagnostics{font-size:.9rem}.diagnostics a{color:var(--accent)}
@media(max-width:600px){body{padding:14px}section{padding:15px}button,.primary{width:100%;margin:4px 0}}
</style></head>
<body>
<h1>Music to Clone Hero and YARG</h1>
<p class="muted">Turn music you already love into playable charts. Start with a SoundCloud export; Spotify is optional.</p>
<section aria-labelledby="importHeading"><h2 id="importHeading">1. Add your music</h2>
  <div class="grid"><div class="card"><h3>SoundCloud export</h3>
    <p>Upload a TuneMyMusic CSV or SoundCloud JSON export. No SoundCloud login is required.</p>
    <label for="soundcloudFile">SoundCloud export file</label><input id="soundcloudFile" type="file" accept=".csv,.json">
    <button id="soundcloudImport">Import SoundCloud export</button><p id="soundcloudMsg" class="muted" role="status" aria-live="polite"></p>
    <a href="/browser-exporter.js">Download the optional browser exporter</a>
   </div><div class="card"><h3>Spotify</h3><p id="spotifyDescription">Connect Spotify to add Liked Songs or playlists.</p>
     <button id="spotifyConnect">Connect Spotify</button><button id="spotifySync" hidden>Sync Spotify</button><p class="muted">Spotify music is not included until you choose a scope and add it.</p>
     <div id="spotifyScope" hidden><h4>Choose Spotify music</h4><label><input id="spotifyLiked" type="checkbox"> Liked Songs <span id="spotifyLikedCount" class="muted"></span></label><div id="spotifyPlaylists"></div><button id="spotifySelectAll" type="button">Select all accessible playlists</button><button id="spotifyAdd" disabled>Add selected to Your Music</button><progress id="spotifyProgress" max="100" value="0" hidden aria-label="Spotify import progress"></progress><p id="spotifyScopeMsg" class="muted" role="status" aria-live="polite"></p></div>
   </div></div>
   <div id="summary" class="muted" style="margin-top:14px">No music imported yet.</div>
</section>
<section aria-labelledby="yourMusicHeading"><h2 id="yourMusicHeading">YOUR MUSIC</h2><div id="localSourceBreakdown" class="source-breakdown"></div><div class="source-filter" role="group" aria-label="Filter songs by source"><button data-source-filter="all" aria-pressed="true">All</button><button data-source-filter="soundcloud" aria-pressed="false">SoundCloud</button><button data-source-filter="spotify" aria-pressed="false">Spotify</button><button data-source-filter="both" aria-pressed="false">Both</button><label for="musicSearch">Search</label><input id="musicSearch" type="search" placeholder="Artist or title"></div><ul id="localMusicList" class="music-list"></ul><p id="localMusicMore" class="muted"></p></section>
<section aria-labelledby="happeningHeading"><h2 id="happeningHeading">WHAT'S HAPPENING</h2><p id="localHappeningCurrent" class="muted" role="status" aria-live="polite">Loading current status...</p><ul id="localHappeningList" class="happening-list"></ul></section>
 <section aria-labelledby="matchingHeading"><h2 id="matchingHeading">2. Find playable charts</h2>
   <h3 id="matchingStateHeading">READY TO FIND CHARTS</h3><p id="matchingStateDetail" class="muted">We compare your songs with the local chart catalog without downloading anything.</p>
   <progress id="matchingProgressBar" max="1" value="0" aria-label="Chart matching progress"></progress><p id="matchingProgressText" role="status" aria-live="polite"></p><p id="matchingCurrent" class="muted"></p>
   <div id="matchingCounters" class="grid"></div><h3>RECENT</h3><ul id="matchingRecent" class="happening-list"></ul><details><summary>View all song activity</summary><input id="matchingActivitySearch" type="search" placeholder="Search songs"><select id="matchingActivityFilter"><option value="all">All statuses</option><option value="strong">Strong</option><option value="review">Needs review</option><option value="no_suitable">No suitable</option><option value="not_playable">Not playable</option><option value="no_catalog_candidate">No catalog candidate</option></select><p class="muted">Use the existing Review matches page for full paginated results.</p></details>
   <div id="localVisual" hidden></div><button id="find" disabled>Find chart matches</button><p id="progress" class="muted" hidden></p>
</section>
<section id="review" class="journey-status" hidden aria-labelledby="reviewHeading">
  <h2 id="reviewHeading">3. Review and export</h2>
  <h3 id="outcomeHeadline"></h3><p id="outcomeDetails"></p>
  <a id="reviewLink" class="primary" href="/advanced" hidden>Review matches</a>
  <details id="diagnosticsPanel" hidden><summary>Technical details</summary><p id="diagnostics" class="muted diagnostics"></p></details>
</section>
<script>
const publicMode = __PUBLIC_MODE__; const lanAccessToken = __LAN_ACCESS_TOKEN__;
const $=id=>document.getElementById(id);let workspaceId=null;let importTimer=null;let matchingTimer=null;
async function api(url,opt){const r=await fetch(url,opt);const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function number(value){return Number(value||0).toLocaleString()}
function showDiagnostics(d){const panel=$('diagnosticsPanel');if(!d||!d.run_id||!d.diagnostics_url){panel.hidden=true;return}$('diagnostics').innerHTML=`Matching details: <code>${esc(d.run_id)}</code> · <a href="${esc(d.diagnostics_url)}">Download diagnostics</a>`;panel.hidden=false}
function sourceLabel(provider){return String(provider||'').toLowerCase()==='soundcloud'?'SoundCloud export':String(provider||'Imported music')}
let localMusic=[];let localFilter='all';let localSearch='';
function spotifyScopeReady(){return $('spotifyLiked').checked||document.querySelectorAll('#spotifyPlaylists input:checked').length>0}
function updateSpotifyAdd(){if($('spotifyAdd'))$('spotifyAdd').disabled=!spotifyScopeReady()}
function renderSpotifyScope(data){$('spotifyScope').hidden=false;$('spotifyLikedCount').textContent=data.liked_available?`(${number(data.liked_count)} songs)`:'(count unavailable)';$('spotifyLiked').checked=!!data.scope?.include_liked;$('spotifyPlaylists').innerHTML=(data.playlists||[]).map(p=>{const count=p.item_count==null?'count unknown':`${number(p.item_count)} songs`;const state=p.access_status==='accessible'?count:`${p.access_status||'inaccessible'}${p.access_reason?`: ${p.access_reason}`:''}`;return `<label style="display:block"><input type="checkbox" value="${esc(p.spotify_id)}" ${p.selected?'checked':''} ${p.access_status!=='accessible'?'disabled':''}> ${esc(p.name||'Untitled playlist')} <small>(${esc(state)})</small></label>`}).join('')||'<small>No playlists found.</small>';document.querySelectorAll('#spotifyPlaylists input').forEach(input=>input.onchange=updateSpotifyAdd);updateSpotifyAdd()}
async function loadSpotifyScope(){try{const d=await api('/api/spotify/scope?workspace_id='+encodeURIComponent(workspaceId));renderSpotifyScope(d);$('localHappeningCurrent').textContent='Spotify inventory ready. Choose Liked Songs or playlists to add.'}catch(e){$('spotifyScopeMsg').textContent=e.message}}
async function syncSpotify(){if(!workspaceId)return;const button=$('spotifyConnect');button.disabled=true;$('localHappeningCurrent').textContent='Spotify connected. Fetching playlists and checking Liked Songs...';try{const d=await api('/api/spotify/inventory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId})});const poll=async()=>{const s=await api('/api/spotify/status?workspace_id='+encodeURIComponent(workspaceId));$('localHappeningCurrent').textContent=s.running?'Spotify connected. Reading playlists and Liked Songs...':'Spotify inventory ready.';if(s.running){setTimeout(poll,600);return}button.disabled=false;await loadSpotifyScope()};poll()}catch(e){button.disabled=false;$('spotifyScopeMsg').textContent=e.message}}
async function addSpotify(){const ids=Array.from(document.querySelectorAll('#spotifyPlaylists input:checked')).map(input=>input.value);const includeLiked=$('spotifyLiked').checked;const msg=$('spotifyScopeMsg');msg.textContent='Saving selected Spotify scope...';try{await api('/api/spotify/scope',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId,playlist_ids:ids,include_liked:includeLiked})});$('spotifyAdd').disabled=true;msg.textContent='Reading selected Spotify music...';$('localHappeningCurrent').textContent='Spotify import is running. Your Music will update when it finishes.';await api('/api/spotify/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId})});const poll=async()=>{const s=await api('/api/spotify/status?workspace_id='+encodeURIComponent(workspaceId));if(s.running){renderSpotifyImportProgress(s);await refresh();setTimeout(poll,600);return}$('spotifyProgress').hidden=true;msg.textContent=s.status==='failed'?s.error||'Spotify import failed.':'Spotify import complete.';await refresh()};poll()}catch(e){msg.textContent=e.message;updateSpotifyAdd()}}
function renderLocalMusic(){const rows=localMusic.filter(song=>{const source=localFilter==='both'?song.sources.length>1:localFilter==='all'||song.sources.includes(localFilter);return source&&(!localSearch||`${song.artist} ${song.title}`.toLowerCase().includes(localSearch))});$('localMusicList').innerHTML=rows.map(song=>`<li>[${esc(song.source_label.toUpperCase())}] ${esc(song.artist)} — ${esc(song.title)}</li>`).join('')||'<li class="muted">No songs match this filter.</li>';$('localMusicMore').textContent=`${number(rows.length)} shown of ${number(localMusic.length)} canonical songs.`}
function spotifyTime(seconds){if(seconds==null)return '';const value=Math.max(0,Math.round(Number(seconds)));return value>=60?` · ~${Math.floor(value/60)}m ${value%60}s remaining`:` · ~${value}s remaining`}
function renderSpotifyImportProgress(status){const p=status.progress||{};const meta=p.metadata||{};const read=Number(meta.spotify_items_processed||p.spotify_items_processed||0);const total=meta.spotify_items_expected??p.spotify_items_expected;const bar=$('spotifyProgress');bar.hidden=false;if(total!=null){bar.max=Number(total);bar.value=Math.min(read,Number(total))}else bar.removeAttribute('value');$('localHappeningCurrent').textContent=`${p.message||status.message||'Reading selected Spotify music...'} · ${number(read)}${total!=null?` / ${number(total)}`:' items read'}${meta.eta_seconds!=null?spotifyTime(meta.eta_seconds):''}`}
function renderLocalShell(j){if(!j||!j.combined)return;const c=j.combined,s=j.sources||{},v=j.visualization||{},status=j.matching_status||'empty';$('yourMusicHeading').textContent=`YOUR MUSIC — ${number(c.canonical_count)} unique songs`;$('localSourceBreakdown').innerHTML=`<div class="card"><span>SoundCloud unique songs</span><strong>${number(c.soundcloud_count)}</strong><small>${s.soundcloud?.state==='not_imported'?'Not imported':`${number(s.soundcloud.raw_rows)} raw rows · ${number(s.soundcloud.accepted_rows)} accepted · ${number(s.soundcloud.skipped)} skipped · ${number(s.soundcloud.duplicates)} duplicates merged`}</small></div><div class="card"><span>Spotify unique songs</span><strong>${number(c.spotify_count)}</strong><small>${s.spotify?.state==='not_connected'?'Not connected':s.spotify?.imported?'Imported':'Connected, not imported'}</small></div><div class="card"><span>In both</span><strong>${number(c.shared_count)}</strong></div><div class="card"><span>Duplicates merged within sources</span><strong>${number(c.duplicates_merged)}</strong></div><div class="card"><span>Canonical unique songs</span><strong>${number(c.canonical_count)}</strong></div>`;localMusic=j.music||[];renderLocalMusic();const history=[];if(s.soundcloud?.state==='imported')history.push(`<li>✓ SoundCloud import complete<br><small>${number(s.soundcloud.raw_rows)} raw rows · ${number(s.soundcloud.accepted_rows)} accepted · ${number(s.soundcloud.skipped)} skipped · ${number(s.soundcloud.duplicates)} duplicates merged</small></li>`);else history.push('<li>○ SoundCloud<br><small>Not imported</small></li>');history.push(s.spotify?.state==='not_connected'?'<li>○ Spotify<br><small>Not connected</small></li>':`<li>${s.spotify?.imported?'✓':'○'} Spotify<br><small>${s.spotify?.imported?number(c.spotify_count)+' songs loaded':'Connected, not imported'}</small></li>`);if(c.canonical_count)history.push(`<li>✓ Your Music ready<br><small>${number(c.canonical_count)} unique songs · ${number(c.duplicates_merged)} duplicates merged</small></li>`);if(v.catalog_chart_count)history.push(`<li>✓ Enchor catalog ready<br><small>${number(v.catalog_chart_count)} charts</small></li>`);if(status==='running')history.push(`<li>◐ ${esc(j.headline||'Finding chart matches')}<br><small>${number(v.processed_count)} / ${number(c.canonical_count)} songs checked</small></li>`);if(status==='complete')history.push(`<li>✓ Matching complete<br><small>${number(j.counts?.chart_options)} chartable · ${number(j.counts?.strong_matches)} strong · ${number(j.counts?.needs_review)} review</small></li>`);if(['failed','incomplete'].includes(status))history.push(`<li>× ${esc(j.headline||'Matching stopped')}<br><small>${esc(j.message||'Run matching again; existing work is preserved.')}</small></li>`);if(status==='cancelled')history.push('<li>× Matching canceled<br><small>Existing imported music is preserved.</small></li>');$('localHappeningCurrent').textContent=status==='running'?'The application is working. Progress is saved and safe to refresh.':j.message||'Your music is ready for the next step.';$('localHappeningList').innerHTML=history.concat((j.activity||[]).slice(-8).map(row=>`<li>${row.classification==='strong'?'✓':row.classification==='review'?'•':row.classification==='not_playable'?'!':row.classification==='checking'?'◐':'×'} ${esc(row.artist)} — ${esc(row.title)}<br><small>${esc(row.classification||row.status||'Waiting')}</small></li>`)).join('');$('localVisual').dataset.contract=JSON.stringify(v)}
 function renderMatchingSurface(j){if(!j)return;const c=j.counts||{},p=j.progress||{},status=j.matching_status||'ready',total=Number(p.total||c.imported_songs||0),done=Number(p.processed||0),active=status==='running';$('matchingStateHeading').textContent=status==='complete'?'MATCHING COMPLETE':status==='failed'?'MATCHING STOPPED':status==='incomplete'?'MATCHING INCOMPLETE':status==='cancelled'?'MATCHING CANCELED':active?'FINDING PLAYABLE CHARTS':'READY TO FIND CHARTS';$('matchingStateDetail').textContent=status==='complete'?`${number(done||total)} songs checked against ${number(j.catalog_chart_count||0)} Enchor charts.`:status==='failed'?`Could not continue: ${j.message||'the matching job stopped.'}`:status==='cancelled'?`${number(done)} / ${number(total)} songs checked. Results from this incomplete run are not final.`:active?'The current matching run is saved and safe to refresh.':'Your Music and the Enchor catalog are ready.';$('matchingProgressBar').max=Math.max(1,total);$('matchingProgressBar').value=Math.min(done,total);$('matchingProgressText').textContent=`${number(done)} / ${number(total)} songs checked${p.elapsed_seconds!=null?` · Elapsed ${Math.floor(p.elapsed_seconds/60).toString().padStart(2,'0')}:${Math.round(p.elapsed_seconds%60).toString().padStart(2,'0')}`:''}${p.eta_seconds!=null?` · About ${Math.floor(p.eta_seconds/60)}m ${Math.round(p.eta_seconds%60)}s remaining`:''}`;const current=p.current_song;$('matchingCurrent').textContent=current?`Currently checking: ${current.artist} — ${current.title}`:'';$('matchingCounters').innerHTML=[['Strong',c.strong_matches],['Needs review',c.needs_review],['No suitable',c.no_suitable_songs],['Not playable',c.not_playable_songs],['No candidate',c.no_candidate_songs]].map(([label,value])=>`<div class="card"><span>${label}</span><strong>${number(value)}</strong></div>`).join('');const activity=p.recent||[];$('matchingRecent').innerHTML=activity.slice(0,30).map(row=>{const mark=row.classification==='strong'?'✓':row.classification==='review'?'•':row.classification==='not_playable'?'!':'×';return `<li>${mark} ${esc(row.artist)} — ${esc(row.title)}<br><small>${esc(row.classification||row.status||'Waiting')}</small></li>`}).join('')||'<li class="muted">No current-run song activity yet.</li>';}
 function render(d){workspaceId=d.workspace_id||workspaceId;const sources=d.sources||[];const combined=d.combined||{};$('summary').innerHTML=(sources.length?sources.map(source=>{const details=[];if(source.skipped_rows)details.push(`${number(source.skipped_rows)} skipped`);if(source.duplicate_rows)details.push(`${number(source.duplicate_rows)} duplicates`);return `<div><b>${esc(sourceLabel(source.provider))}</b>: ${number(source.tracks)} songs imported${details.length?` · ${esc(details.join(' · '))}`:''}</div>`}).join(''):'No music imported yet.')+`<p><b>Your music:</b> ${number(combined.tracks)} songs from ${number(combined.artists)} artists</p>`;$('find').disabled=!combined.tracks;showDiagnostics(d);if(d.journey){renderJourney(d.journey);renderMatchingSurface(d.journey)}}
 function journeyDetails(journey){const counts=journey.counts||{};if(journey.matching_status==='complete'&&journey.catalog_available===false)return 'Your music was imported, but no charts could be checked because the local chart catalog is unavailable.';if(journey.matching_status==='complete')return `${number(counts.imported_songs)} songs checked. ${number(counts.chart_options)} songs have eligible chart options across ${number(counts.candidate_charts)} chart versions. ${number(Math.max(0,counts.imported_songs-counts.no_candidate_songs))} songs had at least one catalog candidate evaluated. ${number(counts.strong_matches)} strong matches and ${number(counts.needs_review)} that need review. ${number(counts.no_suitable_songs)} songs have no suitable match.`;if(journey.matching_status==='incomplete')return `${number(counts.imported_songs)} songs were imported. ${number(counts.chart_options)} have eligible chart options, but matching did not finish for everything.`;if(journey.matching_status==='failed')return `${number(counts.imported_songs)} songs were imported, but matching stopped before it finished.`;return journey.message||''}
 function renderJourney(journey){if(!journey)return;renderLocalShell(journey);const counts=journey.counts||{};const review=$('review');const status=journey.matching_status||'empty';const active=status==='running';$('find').disabled=!counts.imported_songs||active;$('progress').textContent=active?journey.message||'Matching your songs...':status==='ready'?journey.message||'Your music is ready to match.':'';if(['complete','incomplete','failed'].includes(status)){review.hidden=false;$('outcomeHeadline').textContent=journey.headline||'Matching complete';$('outcomeDetails').textContent=journeyDetails(journey);$('reviewLink').hidden=!journey.review_available;$('reviewLink').href=journey.review_url||'/advanced';review.classList.toggle('journey-status--success',status==='complete'&&journey.catalog_available!==false);review.classList.toggle('journey-status--warning',status!=='complete'||journey.catalog_available===false)}else{review.hidden=true}}
 const baseRenderJourney=renderJourney;renderJourney=function(journey){baseRenderJourney(journey);renderMatchingSurface(journey)};
 function importMessage(s){const p=s.progress||{};if(s.phase==='persist'&&p.total_tracks)return `${s.message||'Saving tracks...'} ${number(p.persisted_tracks)} / ${number(p.total_tracks)}`;return s.message||s.phase||'Importing SoundCloud export...'}
function pollImport(runId){if(importTimer)clearTimeout(importTimer);const poll=async()=>{try{const s=await api('/api/sources/soundcloud/import/status?run_id='+encodeURIComponent(runId));workspaceId=s.workspace_id||workspaceId;$('soundcloudMsg').textContent=importMessage(s);showDiagnostics(s);if(s.running){$('find').disabled=true;importTimer=setTimeout(poll,500);return}importTimer=null;$('soundcloudImport').disabled=false;if(s.status==='complete'||s.status==='partial'){const d=await api('/api/sources');render(d);$('soundcloudMsg').textContent=s.result?.message||`Imported ${number(s.result?.source?.tracks)} SoundCloud songs.`}else{$('find').disabled=true;$('soundcloudMsg').textContent=s.error||'SoundCloud import failed.'}}catch(e){importTimer=null;$('soundcloudImport').disabled=false;$('find').disabled=true;$('soundcloudMsg').textContent=e.message}};poll()}
async function refresh(){try{const d=await api('/api/sources');render(d);const status=await api('/api/discovery/status?workspace_id='+encodeURIComponent(d.workspace_id));showDiagnostics(status);if(status.journey)renderJourney(status.journey);if(status.running)pollMatching(d.workspace_id);const imported=await api('/api/sources/soundcloud/import/status');if(imported.running&&imported.run_id){workspaceId=imported.workspace_id||workspaceId;$('soundcloudImport').disabled=true;$('find').disabled=true;pollImport(imported.run_id)}}catch(e){$('summary').textContent=e.message}}
async function pollMatching(workspace){if(matchingTimer)clearTimeout(matchingTimer);const poll=async()=>{try{const status=await api('/api/discovery/status?workspace_id='+encodeURIComponent(workspace));showDiagnostics(status);if(status.journey)renderJourney(status.journey);if(status.running){matchingTimer=setTimeout(poll,700);return}matchingTimer=null;const d=await api('/api/sources');render(d)}catch(e){matchingTimer=null;$('progress').textContent=e.message}};poll()}
$('soundcloudImport').onclick=async()=>{const file=$('soundcloudFile').files[0];if(!file){$('soundcloudMsg').textContent='Choose a CSV or JSON export first.';return}$('soundcloudImport').disabled=true;$('find').disabled=true;$('soundcloudMsg').textContent='Uploading export...';try{const body=new FormData;body.append('file',file);const d=await api('/api/sources/soundcloud/import',{method:'POST',body});workspaceId=d.workspace_id;$('soundcloudMsg').textContent='Upload accepted; importing in the background...';showDiagnostics(d);pollImport(d.run_id)}catch(e){$('soundcloudMsg').textContent=e.message;$('soundcloudImport').disabled=false}};
  document.querySelectorAll('[data-source-filter]').forEach(button=>button.onclick=()=>{localFilter=button.dataset.sourceFilter;document.querySelectorAll('[data-source-filter]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));renderLocalMusic()});$('musicSearch').oninput=e=>{localSearch=e.target.value.trim().toLowerCase();renderLocalMusic()};$('spotifyConnect').onclick=async()=>{try{const d=await api('/api/auth/spotify/start');location.href=d.authorization_url}catch(e){$('soundcloudMsg').textContent=e.message}};$('spotifySync').onclick=syncSpotify;$('spotifyAdd').onclick=addSpotify; $('spotifySelectAll').onclick=()=>{document.querySelectorAll('#spotifyPlaylists input').forEach(input=>{input.checked=true});updateSpotifyAdd()};$('find').onclick=async()=>{if(!workspaceId)return;$('find').disabled=true;$('progress').textContent='Starting chart matching...';try{const d=await api('/api/discovery/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'combined',workspace_id:workspaceId})});workspaceId=d.workspace_id||workspaceId;pollMatching(workspaceId)}catch(e){$('find').disabled=false;$('progress').textContent=e.message}};api('/api/auth/status').then(a=>{if(a.authenticated){$('spotifyConnect').hidden=true;$('spotifySync').hidden=false;$('spotifyDescription').textContent='Spotify is connected. Sync to choose music to add.'}}).catch(()=>{});refresh();
</script></body></html>'''





_ADVANCED_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
   <title>CSV to Clone Hero | Review and export</title>
<style>
   :root { color-scheme:dark; --ink:#f4f6fa; --muted:#aab3c2; --bg:#11151c; --panel:#1a202a; --line:#354052; --accent:#7cb7ff; --success:#72d4a0; }
   * { box-sizing:border-box; }
   body { font-family:"Segoe UI",system-ui,sans-serif; background:linear-gradient(145deg,#111722,#0d1117); color:var(--ink); margin:0; }
   main { width:100%; max-width:880px; min-width:0; margin:0 auto; padding:32px 20px 72px; }
   h1 { font-size:clamp(2rem,5vw,3.5rem); letter-spacing:-.04em; margin:8px 0 4px; }
   h2 { font-size:1.1rem; margin:32px 0 8px; color:var(--ink); }
   .card { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:18px; margin-top:10px; min-width:0; overflow-wrap:anywhere; box-shadow:0 12px 30px rgba(0,0,0,.18); }
   .muted { color:var(--muted); font-size:.9rem; }
   a { color:var(--accent); }
   button { font:inherit; border:1px solid var(--line); border-radius:8px; padding:10px 18px; cursor:pointer; background:#273143; color:var(--ink); }
   button.primary { background:var(--success); border-color:var(--success); color:#07140d; font-weight:800; }
   button:disabled { opacity:.45; cursor:default; }
   button:focus-visible, input:focus-visible, a:focus-visible, summary:focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
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
   code { background: #0d1017; padding: 1px 6px; border-radius: 5px; overflow-wrap:anywhere; word-break:break-word; }
   .review-card { margin-top: 12px; min-width:0; overflow-wrap:anywhere; }
   .review-card-head, .review-option-head, .review-option-actions { display: flex; align-items: center; gap: 10px; }
   .review-card-head, .review-option-head { justify-content: space-between; }
   .review-option-head > div { min-width:0; max-width:100%; }
   .review-card h3, .review-card h4 { margin: 0; }
   .review-eyebrow { color: var(--muted); font-size: .76rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
   .review-status, .review-selected { border: 1px solid var(--line); border-radius: 999px; display: inline-block; padding: 3px 9px; font-size: .82rem; font-weight: 700; }
   .review-status-strong, .review-selected { border-color: var(--success); color: var(--success); }
   .review-status-review { border-color: #ffd479; color: #ffd479; }
   .review-status-blocked, .review-status-not-recommended { color: #ff7a7a; }
   .review-option { border: 1px solid var(--line); border-radius: 8px; margin-top: 9px; padding: 12px; min-width:0; overflow-wrap:anywhere; }
   input[type="file"] { max-width:100%; }
   .review-option-selected { border-color: var(--success); background: #101e16; }
   .review-option-blocked { background: #1b171a; }
   .review-option-meta, .review-summary { color: var(--muted); font-size: .9rem; }
   .review-option-meta { margin: 4px 0 0; }
   .review-summary { margin: 7px 0 0; }
   .review-option-actions { justify-content: flex-end; flex-wrap: wrap; margin-top: 10px; }
   .review-why { margin-top: 10px; }
   .review-why summary, .review-alternatives summary { cursor: pointer; }
   .review-why ul { margin: 9px 0; padding-left: 20px; }
   .review-concern { color: #ffd479; }
   .review-why table { border-collapse: collapse; margin-top: 8px; width: 100%; }
   .review-why th, .review-why td { border-bottom: 1px solid var(--line); padding: 5px 8px 5px 0; text-align: left; vertical-align: top; }
   .review-why th { color: var(--muted); font-weight: 600; width: 34%; }
   .review-alternatives { border-top: 1px solid var(--line); margin-top: 14px; padding-top: 11px; }
   .review-selection-note { background: #102319; border-left: 3px solid var(--success); margin: 12px 0 0; padding: 8px 11px; }
   .local-journey { border-left: 3px solid var(--accent); background: #111d2a; margin-top: 12px; padding: 10px 12px; }
   .local-journey h3 { margin: 0 0 5px; }
   .local-journey p { margin: 5px 0 0; }
   .export-feedback { min-height: 1.4em; margin: 8px 0 0; }
   .export-feedback.error { color: #ff7a7a; }
   @media (max-width: 600px) {
     .review-card-head, .review-option-head { align-items: flex-start; flex-direction: column; }
     .review-option-actions { justify-content: flex-start; }
     .review-option-actions button { width: 100%; }
   }
</style>
</head>
<body>
<main>
   <h1>Review and export charts</h1>
   <p class="muted">Add your music, preview chart matches, review the recommendation for each song, then export your choices.
       Library folder: <code id="libraryRoot">__LIBRARY_ROOT__</code>
      · <a href="/">← Back to your music</a></p>

   <h2 id="importStepHeading">1 · Add music exports</h2>
   <div class="card" id="legacyImportSources">
      Export Spotify playlists to CSV with one of these tools, or add iTunes/Music TXT or XML exports:
    <ul>
      <li><a href="https://www.chosic.com/spotify-playlist-exporter/" target="_blank" rel="noopener">Chosic Spotify Playlist Exporter</a> — log in with Spotify, pick a playlist, download CSV</li>
      <li><a href="https://exportify.net/" target="_blank" rel="noopener">Exportify</a> — log in with Spotify, export playlists to CSV</li>
    </ul>
  </div>

   <h2 id="legacyFileStepHeading">2 · Choose files to import</h2>
   <div class="card" id="drop">
     <input type="file" id="picker" accept=".csv,.txt,.xml" multiple>
     <span class="muted">Spotify CSV, iTunes/Music TXT, or iTunes/Music XML · or drag files anywhere on this box</span>
    <div id="files"></div>
  </div>

   <h2 id="matchingStepHeading">3 · Preview chart matches</h2>
   <div class="card">
     <div id="estimate" class="muted">Calculating search estimate...</div>
      <p id="matchingIntro" class="muted">Preview searches your imported songs without downloading. Review choices before downloading selected charts.</p>
      <button id="exactDry" class="primary">Preview chart matches</button>
     <button id="exactBuild" title="Download selected charts into the library">Build library</button>
      <details id="advancedSearchOptions" style="margin-top:14px"><summary class="muted">Advanced search options</summary><div style="margin-top:10px"><p class="muted">Artist expansion is optional and runs in a separate pass.</p><button id="expansionDry">Preview artist expansion</button><button id="expansionBuild">Download artist expansion charts</button></div></details>
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

   <div id="localJourney" class="local-journey" hidden role="status" aria-live="polite"><h3 id="localJourneyHeadline"></h3><p id="localJourneyMessage"></p><p id="localJourneyCounts"></p></div>

   <h2>4 · Review your matches</h2>
   <div class="card">
     <p class="muted">One card represents one song from your music list. Start with the best chart and its short explanation; open other versions only when you need to compare them. Choices save immediately and apply to the next download build.</p>
     <div id="reviewProgress" class="muted"></div>
      <div class="review-toolbar">
        <button id="acceptAll" class="primary">Choose best review matches</button>
        <button id="selectStrong" type="button">Select all Strong matches</button>
        <button id="selectEligible" type="button">Select current eligible results</button>
        <span class="muted">Use this only when the recommended chart for every song looks right.</span>
      </div>
      <div class="review-toolbar">
        <label class="muted">View
          <select id="reviewState" aria-label="Filter matches by state">
            <option value="eligible" selected>All eligible matches</option>
            <option value="strong">Strong matches</option>
            <option value="review">Needs review</option>
            <option value="selected">Selected</option>
            <option value="unselected">Unselected</option>
            <option value="not_playable">Not playable</option>
            <option value="all">All result cards</option>
          </select>
        </label>
        <label class="muted">Sort
          <select id="reviewSort" aria-label="Sort matches">
            <option value="artist" selected>Artist and title</option>
            <option value="score">Best score</option>
          </select>
        </label>
        <label class="muted" style="margin-left:12px">
          <input type="checkbox" id="showRejected"> also show charts the app did not recommend</label>
        <input type="text" id="reviewFilter" placeholder="Search artist, title, chart, or charter" aria-label="Search matches" style="background:#0d1017; border:1px solid #2a3247; color:#e6e9ef; border-radius:6px; padding:6px 10px; min-width:220px;">
      </div>
      <div id="reviewPager" class="review-toolbar" hidden>
        <button id="reviewPrevious" type="button">Previous</button>
        <span id="reviewPageStatus" class="muted"></span>
        <button id="reviewNext" type="button">Next</button>
      </div>
      <div id="review" style="margin-top:10px"></div>
  </div>

   <h2>5 · Build your library</h2>
   <div class="card">
     <div id="summary" class="muted">No results yet — run a dry run first.</div>
     <div id="buildPreview" class="local-journey" hidden>
       <p id="buildPreviewText" class="muted"></p>
       <button id="cancelExport" type="button" hidden>Cancel build</button>
       <button id="retryFailed" type="button" hidden>Retry failed</button>
     </div>
     <p id="exportFeedback" class="export-feedback" role="status" aria-live="polite"></p>
     <div class="reports" id="reports" style="margin-top:10px"></div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let latestStatus = null;
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || 'Request failed.');
  return data;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, '0')}s` : `${seconds}s`;
}

function formatBytes(value) {
  let bytes = Math.max(0, Number(value) || 0);
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++; }
  return `${bytes.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function setExportFeedback(message, isError = false) {
  const element = $('exportFeedback');
  if (!element) return;
  element.textContent = String(message || '');
  element.classList.toggle('error', Boolean(isError));
}

function renderJobStatus(status) {
  latestStatus = status;
  const stages = status.stages || [];
  const progress = status.search_progress || {};
  const localExport = typeof reviewData !== 'undefined'
    && reviewData.source_neutral
    && reviewData.journey?.has_imported_music
    && status.action === 'exact-build';
  const exportResult = status.result || {};
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
  const exportFailed = localExport && !status.running && status.status === 'failed';
  $('jobDot').className = 'job-dot' + (status.error || exportFailed ? ' failed' : status.running ? ' running' : '');
  const actionLabels = {
    'dry-run': 'Exact dry run', 'build': 'Exact build',
    'exact-dry-run': 'Exact dry run', 'exact-build': 'Exact build',
    'expansion-dry-run': 'Expansion dry run', 'expansion-build': 'Expansion build'
  };
  let headline = status.running
    ? `Running: ${actionLabels[status.action] || status.action || 'job'}`
    : status.error ? 'Job failed' : status.finished_at ? 'Last run complete' : 'Ready';
  let activity = status.activity || detail || 'Waiting to start.';
  let userDetail = detail;
  if (localExport) {
    const published = Number(exportResult.published || 0);
    const failed = Number(exportResult.failed || 0);
    const invalid = Number(exportResult.invalid || 0);
    const itemProgress = status.items || {};
    const selected = Number(exportResult.selected || itemProgress.selected || 0);
    const completed = Number(exportResult.ready || itemProgress.completed || 0);
    if (status.running) {
      headline = status.status === 'cancelling' ? 'Cancelling library build' : 'Building library';
      activity = status.current || status.activity || 'Writing your selected charts into the library.';
      userDetail = `${completed} / ${selected} ready · ${Number(itemProgress.downloading || 0)} downloading · ${Number(itemProgress.retrying || 0)} retrying · ${Number(itemProgress.failed || 0)} failed · ${Number(itemProgress.invalid || 0)} invalid · ${Number(itemProgress.remaining || 0)} remaining`;
      setExportFeedback('Export is in progress...');
    } else if (['complete', 'partial'].includes(status.status) && exportResult.success) {
      const added = Number(exportResult.added || 0);
      const already = Number(exportResult.already_present || 0);
      const ready = Number(exportResult.ready || published || 0);
      const total = Number(exportResult.selected || selected || 0);
      headline = 'Library ready';
      activity = `${added} added · ${already} already present · ${invalid} invalid · ${Number(exportResult.download_failures || failed)} download failures`;
      userDetail = `${ready} / ${total} selected charts ready`;
      setExportFeedback(`${activity}. ${userDetail}. Selected charts exported successfully.`);
      $('retryFailed').hidden = !(invalid || failed || Number(exportResult.cancelled_items || 0));
      $('cancelExport').hidden = true;
    } else if (status.status === 'cancelled' && exportResult) {
      headline = 'Build cancelled';
      activity = 'Validated charts were kept in the library.';
      userDetail = `${Number(exportResult.ready || completed || 0)} / ${Number(exportResult.selected || selected || 0)} selected charts ready; remaining charts can be retried.`;
      setExportFeedback(`${activity} ${userDetail}`);
      $('retryFailed').hidden = false;
      $('cancelExport').hidden = true;
    } else if (!status.running) {
      headline = 'Export could not complete';
      activity = status.error || 'No charts were exported.';
      userDetail = failed ? `${failed} chart${failed === 1 ? '' : 's'} failed.` : '';
      setExportFeedback(activity, true);
      $('retryFailed').hidden = false;
      $('cancelExport').hidden = true;
    }
    if (status.running) {
      $('cancelExport').hidden = false;
      $('retryFailed').hidden = true;
    }
  }
  $('jobHeadline').textContent = headline;
  $('jobActivity').textContent = activity;
  $('jobDetail').textContent = userDetail;
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
        <span>${f.name} <span class="muted">(${(f.size/1024).toFixed(0)} KB)</span></span></label>
        <button onclick="delFile('${f.name.replace(/'/g, "\\'")}')">remove</button></div>`).join('')
    : '<p class="muted">No music exports yet.</p>';
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
    alert('Skipped (supported formats are .csv, .txt, and .xml): ' + res.rejected.join(', '));
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
  $('selectStrong').disabled = disabled;
  $('selectEligible').disabled = disabled;
}

async function run(action) {
  if (isLocalJourney() && action === 'exact-build') {
    await runNeutralExport();
    return;
  }
  jobRunning = true;
  setRunButtons(true);
  $('stages').innerHTML = '';
  renderJobStatus({running: true, action, stage: 'Starting', activity: `Starting ${action}...`, stages: [], search_progress: {}});
  try {
    const res = await fetch('/api/run/' + action, {method: 'POST'});
    if (!res.ok) throw Error((await res.json().catch(() => ({}))).error || 'Could not start this job.');
    await poll();
  } catch (err) {
    jobRunning = false;
    setRunButtons(false);
    renderJobStatus({running: false, error: err.message, activity: err.message, stages: [], search_progress: {}});
  }
}

async function poll() {
  try {
    const s = await (await fetch('/api/status')).json();
    $('stages').innerHTML = (s.stages || []).map(st => {
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
    } else {
      setRunButtons(false);
      await refreshSummary(); await refreshReports(); await refreshEstimate();
      await loadReview(false);
    }
  } catch (err) {
    jobRunning = false;
    setRunButtons(false);
    renderJobStatus({running: false, error: err.message, activity: err.message, stages: [], search_progress: {}});
  }
}

async function runNeutralExport(retryOnly = false) {
  jobRunning = true;
  setRunButtons(true);
  setExportFeedback(retryOnly ? 'Retrying incomplete charts...' : 'Preparing your selected charts...');
  renderJobStatus({running: true, action: 'exact-build', activity: 'Exporting selected charts...', stages: [], search_progress: {}});
  try {
    const response = await fetch('/api/spotify/export/start', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'folder', workspace_id: reviewData.workspace_id || reviewData.journey?.workspace_id,
        path: $('libraryRoot').textContent.trim(), retry_failed: Boolean(retryOnly)})});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(result.error || 'Could not start the export.');
    await pollNeutralExport();
  } catch (err) {
    jobRunning = false;
    setRunButtons(false);
    setExportFeedback(err.message || 'Could not start the export.', true);
    renderJobStatus({running: false, error: err.message, activity: err.message, stages: [], search_progress: {}});
  }
}

async function cancelNeutralExport() {
  if (!jobRunning) return;
  try {
    $('cancelExport').disabled = true;
    await api('/api/spotify/export/cancel', {method: 'POST'});
    setExportFeedback('Cancellation requested. The active chart will finish safely.');
  } catch (error) {
    setExportFeedback(error.message || 'Could not cancel the build.', true);
    $('cancelExport').disabled = false;
  }
}

async function pollNeutralExport() {
  try {
    const response = await fetch('/api/spotify/export/status');
    const status = await response.json();
    renderJobStatus({
      running: status.running,
      action: 'exact-build',
      activity: status.current || (status.running ? 'Exporting selected charts...' : status.error || 'Export complete.'),
      error: status.error,
      status: status.status,
      result: status.result,
      items: status.items,
      started_at: status.started_at,
      finished_at: status.running ? null : (Date.now() / 1000),
      elapsed_seconds: 0,
      stages: [],
      search_progress: {},
    });
    if (status.running) {
      setTimeout(pollNeutralExport, 700);
      return;
    }
    jobRunning = false;
    setRunButtons(false);
    await refreshSummary(); await refreshReports(); await loadReview(false);
  } catch (err) {
    jobRunning = false;
    setRunButtons(false);
    setExportFeedback(err.message || 'Export status is unavailable.', true);
    renderJobStatus({running: false, error: err.message, activity: err.message, stages: [], search_progress: {}});
  }
}

async function refreshNeutralExportStatus() {
  try {
    const response = await fetch('/api/spotify/export/status');
    const status = await response.json();
    if (!status.running && !status.result && status.status === 'idle') return;
    jobRunning = Boolean(status.running);
    setRunButtons(jobRunning);
    renderJobStatus({
      running: status.running,
      action: 'exact-build',
      activity: status.current || (status.running ? 'Exporting selected charts...' : status.error || 'Export complete.'),
      error: status.error,
      status: status.status,
      result: status.result,
      items: status.items,
      started_at: status.started_at,
      finished_at: status.running ? null : (Date.now() / 1000),
      elapsed_seconds: 0,
      stages: [],
      search_progress: {},
    });
    if (status.running) setTimeout(pollNeutralExport, 700);
  } catch (error) {
    // The export status is supplementary; the review page remains usable.
  }
}

function isLocalJourney(journey = reviewData.journey) {
  return Boolean(journey && journey.source_neutral && journey.has_imported_music);
}

function renderLocalJourney(journey) {
  const local = isLocalJourney(journey);
  const panel = $('localJourney');
  if (!local) {
    panel.hidden = true;
    $('importStepHeading').hidden = false;
    $('legacyImportSources').hidden = false;
    $('legacyFileStepHeading').hidden = false;
    $('drop').hidden = false;
    $('advancedSearchOptions').hidden = false;
    $('matchingStepHeading').textContent = '3 · Preview chart matches';
    $('matchingIntro').textContent = 'Preview searches your imported songs without downloading. Review choices before downloading selected charts.';
    $('exactDry').hidden = false;
    return;
  }
  const counts = journey.counts || {};
  panel.hidden = false;
  $('localJourneyHeadline').textContent = journey.headline || 'Your music is ready';
  $('localJourneyMessage').textContent = journey.message || '';
     const selected = Number(counts.selected_charts || 0);
  $('localJourneyCounts').textContent = `${Number(counts.imported_songs || 0).toLocaleString()} songs checked · ${Number(counts.chart_options || 0).toLocaleString()} songs with chart options across ${Number(counts.candidate_charts || 0).toLocaleString()} chart versions · ${Number(counts.strong_matches || 0).toLocaleString()} strong matches · ${Number(counts.needs_review || 0).toLocaleString()} need review · ${Number(counts.no_suitable_songs || 0).toLocaleString()} without a safe recommendation · ${selected.toLocaleString()} selected for export`;
  $('importStepHeading').hidden = true;
  $('legacyImportSources').hidden = true;
  $('legacyFileStepHeading').hidden = true;
  $('drop').hidden = true;
  $('advancedSearchOptions').hidden = true;
  $('matchingStepHeading').textContent = '2 · Matching results';
  $('matchingIntro').textContent = journey.matching_status === 'running'
    ? 'Matching is still in progress. Results will appear here when the check finishes.'
    : 'Your matching results are ready. Review the recommendation for each song before exporting.';
  $('exactDry').hidden = true;
  $('exactDry').textContent = journey.matching_status === 'running'
    ? 'Matching in progress'
    : journey.matching_status === 'ready'
      ? 'Find chart matches on the home page'
      : 'Matching complete';
  $('exactDry').disabled = true;
   $('exactBuild').textContent = 'Build library';
  updateLocalExportState();
}

function updateLocalExportState() {
  const journey = reviewData.journey;
  if (!isLocalJourney(journey)) return;
  const selected = Number(journey.counts?.selected_charts || 0);
  const matching = journey.matching_status === 'running';
  $('exactBuild').disabled = jobRunning || matching || selected === 0;
  $('exactBuild').title = selected
    ? 'Export the charts currently selected below.'
    : 'Choose at least one chart below before exporting.';
  refreshExportPreview();
}

async function refreshExportPreview() {
  const panel = $('buildPreview');
  if (!panel || !isLocalJourney()) return;
  const selected = Number(reviewData.journey?.counts?.selected_charts || 0);
  if (!selected) {
    panel.hidden = true;
    return;
  }
  try {
    const data = await api('/api/spotify/export/preview', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'folder', workspace_id: reviewData.workspace_id || reviewData.journey.workspace_id,
        path: $('libraryRoot').textContent.trim()})});
    $('buildPreviewText').textContent = `${Number(data.selected_chart_count || selected).toLocaleString()} selected charts · ${Number(data.reusable_count || 0).toLocaleString()} already present/reusable · estimated ${formatBytes(data.estimated_bytes)} · destination ${data.destination}`;
    panel.hidden = false;
  } catch (error) {
    $('buildPreviewText').textContent = error.message || 'Build preview unavailable.';
    panel.hidden = false;
  }
}

async function refreshEstimate() {
  try {
    const sourceData = await api('/api/sources');
    if (sourceData.journey?.has_imported_music) {
      reviewData.journey = sourceData.journey;
      renderLocalJourney(sourceData.journey);
      $('estimate').textContent = `${Number(sourceData.journey.counts?.imported_songs || 0).toLocaleString()} imported songs. Matching results appear below.`;
      return;
    }
    const data = await api('/api/estimate');
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
  const s = await api('/api/summary');
  if (!s) return;
  if (s.journey?.has_imported_music) {
    reviewData.journey = s.journey;
    renderLocalJourney(s.journey);
    const counts = s.journey.counts || {};
    const selected = Number(counts.selected_charts || 0);
    $('summary').innerHTML = `<p><b>${selected.toLocaleString()} chart${selected === 1 ? '' : 's'} selected</b> for export.</p><p>${Number(counts.chart_options || 0).toLocaleString()} songs have chart options across ${Number(counts.candidate_charts || 0).toLocaleString()} chart versions: ${Number(counts.strong_matches || 0).toLocaleString()} strong matches and ${Number(counts.needs_review || 0).toLocaleString()} that need review. ${selected ? 'Export is ready when you are.' : 'Choose a chart above to make export available.'}</p>`;
     updateLocalExportState();
     refreshNeutralExportStatus();
     return;
  }
  renderLocalJourney(null);
  const exact = s.exact || {};
  const expansion = s.expansion || {};
  const plan = s.plan || {};
  const downloads = s.downloads || {};
  const manual = s.manual_decisions || {};
  $('summary').innerHTML = `<table>
    <tr><td>Music-list files</td><td class="num">${s.csv_files}</td></tr>
    <tr><td>Songs imported</td><td class="num">${s.unique_tracks}</td></tr>
    <tr><td>Artists represented</td><td class="num">${s.primary_artists}</td></tr>
     <tr><td>Strong / needs review / no suitable chart</td>
        <td class="num">${exact.auto_matches || 0} / ${exact.review || 0} / ${exact.no_match || 0}</td></tr>
    <tr><td>Expansion selected</td><td class="num">${expansion.selected || 0}</td></tr>
    <tr><td>Chart choices</td><td class="num">${manual.accept || 0}</td></tr>
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
let reviewData = {tracks: [], progress: {}, journey: null};
let jobRunning = false;
let lastSaved = '';
let reviewOffset = 0;
const reviewPageSize = 50;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function reviewQueryUrl() {
  const params = new URLSearchParams();
  params.set('limit', String(reviewPageSize));
  params.set('offset', String(reviewOffset));
  const state = $('reviewState').value || 'eligible';
  const sort = $('reviewSort').value || 'artist';
  const search = $('reviewFilter').value.trim();
  params.set('state', state);
  params.set('sort', sort);
  if (search) params.set('search', search);
  if ($('showRejected').checked) params.set('include_rejected', '1');
  return `/api/review?${params.toString()}`;
}

async function loadReview(showLoading = true) {
  if (showLoading && !reviewData.tracks.length) $('review').innerHTML = '<p class="muted">Loading...</p>';
  try {
    reviewData = await api(reviewQueryUrl());
  } catch (err) {
    setReviewFeedback('We could not load your matches. Refresh the page and try again.', true);
    $('review').innerHTML = '<p class="muted">Your match results are temporarily unavailable.</p>';
    return;
  }
  const page = reviewData.page || {};
  if (page.total && reviewOffset >= page.total) {
    reviewOffset = Math.max(0, (Number(page.page_count || 1) - 1) * reviewPageSize);
    return loadReview(showLoading);
  }
  if (reviewData.journey) renderLocalJourney(reviewData.journey);
  renderReview();
}

function renderReviewPager() {
  const page = reviewData.page || {};
  const total = Number(page.total || 0);
  const offset = Number(page.offset || 0);
  const returned = Number(page.returned || 0);
  const pager = $('reviewPager');
  if (!pager) return;
  pager.hidden = total <= Number(page.limit || reviewPageSize);
  $('reviewPrevious').disabled = offset <= 0;
  $('reviewNext').disabled = !page.has_more;
  if (!total) {
    $('reviewPageStatus').textContent = '';
    return;
  }
  $('reviewPageStatus').textContent = `Showing ${offset + 1}-${offset + returned} of ${total} songs`;
}

function renderReview() {
  const progress = reviewData.progress || {};
  const allTracks = reviewData.tracks || [];
  const local = isLocalJourney();
  if (local) renderLocalJourney(reviewData.journey);
  $('acceptAll').disabled = jobRunning;
  $('selectStrong').hidden = !local;
  $('selectEligible').hidden = !local;
  $('selectStrong').disabled = jobRunning;
  $('selectEligible').disabled = jobRunning;
  const items = allTracks;
  const page = reviewData.page || {};
  renderReviewPager();
  const viewCount = Number(page.total ?? (local ? reviewData.journey.counts?.chart_options : allTracks.length) ?? 0);
  const viewLabel = page.state === 'eligible' ? 'eligible songs' :
    page.state === 'strong' ? 'strong songs' :
    page.state === 'review' ? 'songs needing review' :
    page.state === 'selected' ? 'selected songs' : 'result cards';
  const statusText = local
    ? `${viewCount.toLocaleString()} ${viewLabel} · ${Number(reviewData.journey.counts?.selected_charts || 0).toLocaleString()} selected for export`
    : progress.has_search_run
      ? `${progress.review_track_count || 0} songs need a decision · ` +
        `${progress.scored_tracks || 0} / ${progress.total_tracks || 0} songs checked` +
        (progress.searching ? ' · checking is still in progress' :
         progress.exact_scoring_complete ? '' : ' · matching is incomplete')
      : 'Run Preview chart matches to start a review.';
  $('reviewProgress').textContent = lastSaved ? `${lastSaved} | ${statusText}` : statusText;
  if (!items.length) {
    let empty = 'There are no songs needing review right now.';
    if (local) {
      const journey = reviewData.journey;
      if (journey.matching_status === 'running') empty = 'Matching is still checking your songs; no decision is ready yet.';
      else if (journey.catalog_available === false) empty = 'Chart catalog is unavailable, so no recommendations were made.';
      else if (journey.counts?.no_candidate_songs) empty = `No suitable chart was found for ${Number(journey.counts.no_candidate_songs).toLocaleString()} songs.`;
      else if (journey.counts?.no_suitable_songs) empty = 'No safe chart recommendation is available for the imported songs.';
       else if (page.search || page.state !== 'all') empty = 'No songs match this filter.';
    } else if (!progress.has_search_run) empty = 'Run Preview chart matches to check your music.';
    else if (progress.searching) empty = 'Matching is still checking your songs; no decision is ready yet.';
    else if (!progress.exact_scoring_complete) empty = 'Matching did not finish; run the preview again before deciding.';
     else if (page.search || page.state !== 'all') empty = 'No songs match this filter.';
    $('review').innerHTML = `<p class="muted">${empty}</p>`;
    return;
  }
  const cards = items.map(track => {
    const candidates = track.candidates || [];
    const best = candidates.find(candidate => candidate.best) || candidates[0];
    const alternatives = candidates.filter(candidate => candidate !== best);
    const review = track.review || {state:'needs_review', label:'Needs review', summary:'Review this chart carefully.', why:[], concerns:[], details:[]};
    const stateClass = reviewStatusClass(review);
    const selected = candidates.find(candidate => candidate.selected || candidate.manual === 'accept');
    const selectedIsAlternative = Boolean(selected && selected !== best);
    const selectedNote = selected
      ? `<p class="review-selection-note"><b>Already selected:</b> ${esc(selected.candidate_artist)} — ${esc(selected.candidate_title)}. Choosing another chart will replace this choice.</p>`
      : '';
    const playlists = (track.playlists || []).length ? ` · ${esc(track.playlists.join(', '))}` : '';
    return `<article class="card review-card" data-review-state="${esc(review.state)}">
      <header class="review-card-head">
        <div><div class="review-eyebrow">My music</div><h3>${esc(track.artist)} — ${esc(track.title)}</h3>
          <div class="muted">${esc(track.album || '')}${playlists}</div></div>
        <span class="review-status ${stateClass}">${esc(review.label)}</span>
      </header>
      ${selectedNote}
      <section class="review-recommendation">
        <div class="review-eyebrow" style="margin-top:14px">Best chart</div>
        ${best ? candidateHtml(track, best, true) : `<div class="review-option review-option-blocked"><b>${esc(review.label)}</b><p class="review-summary">${esc(review.summary)}</p></div>`}
      </section>
      ${alternatives.length ? `<details class="review-alternatives"${selectedIsAlternative ? ' open' : ''}><summary>Other versions (${alternatives.length})</summary>
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

function reviewStatusClass(review) {
  if (review.state === 'strong') return 'review-status-strong';
  if (review.state === 'needs_review') return 'review-status-review';
  if (review.state === 'not_playable') return 'review-status-blocked';
  return 'review-status-not-recommended';
}

function candidateHtml(track, candidate, best) {
  if (!candidate) return '';
  const review = candidate.review || {state:'needs_review', label:'Needs review', summary:'Review this chart carefully.', why:[], concerns:[], details:[]};
  const manual = candidate.manual || '';
  const selectedClass = candidate.selected || manual === 'accept' ? ' review-option-selected' : '';
  const blockedClass = review.state === 'not_playable' ? ' review-option-blocked' : '';
  const marker = best ? '<span class="review-selected">Best match</span>' : '';
  const selectedBadge = candidate.selected || manual === 'accept' ? '<span class="review-selected">Already selected</span>' : manual === 'reject' ? '<span class="review-status review-status-not-recommended">Skipped</span>' : '';
  const instruments = (candidate.instruments || []).join(', ');
  const meta = [candidate.charter ? `Charter: ${candidate.charter}` : '', instruments ? `Instruments: ${instruments}` : '', candidate.pack_name ? `Pack: ${candidate.pack_name}` : ''].filter(Boolean).join(' · ');
  const link = candidate.url ? ` <a href="${esc(candidate.url)}" target="_blank" rel="noopener">View chart</a>` : '';
  const disabled = jobRunning ? ' disabled title="Decisions are disabled while search is running"' : '';
  let actions = '';
  if (manual === 'accept' || candidate.selected) {
    actions = `<button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="clear"${disabled}>Clear current choice</button>`;
  } else if (review.selectable) {
    actions = `<button class="primary" data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="accept"${disabled}>${esc(review.action)}</button>
      ${manual === 'reject' ? `<button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="clear"${disabled}>Undo skip</button>` : `<button data-track="${esc(track.track_key)}" data-source="${esc(candidate.source)}" data-chart="${candidate.chart_id}" data-decision="reject"${disabled}>Skip this chart</button>`}`;
  } else {
    actions = `<button disabled aria-label="${esc(review.label)}: ${esc(candidate.candidate_title)}">${esc(review.label)}</button>`;
  }
  return `<div class="review-option${selectedClass}${blockedClass}">
    <div class="review-option-head"><div><h4>${esc(candidate.candidate_artist)} — ${esc(candidate.candidate_title)}</h4>${meta ? `<p class="review-option-meta">${esc(meta)}${link ? ' ·' : ''}${link}</p>` : link}</div><div>${marker} ${selectedBadge} <span class="review-status ${reviewStatusClass(review)}">${esc(review.label)}</span></div></div>
    <p class="review-summary">${esc(review.summary || review.label)}</p>
    ${reviewDetails(candidate)}
    <div class="review-option-actions">${actions}</div>
  </div>`;
}

function reviewDetails(candidate) {
  const review = candidate.review || {};
  const why = (review.why || []).map(value => `<li>${esc(value)}</li>`).join('');
  const concerns = (review.concerns || []).map(value => `<li class="review-concern">Potential issue: ${esc(value)}</li>`).join('');
  const rows = (review.details || []).map(row => `<tr><th scope="row">${esc(row.label)}</th><td>${esc(row.value)}</td></tr>`).join('');
  const reference = candidate.reference ? `<p class="muted">Chart reference: ${esc(candidate.reference)}</p>` : '';
  const heading = review.state === 'not_playable' ? 'Why this chart is unavailable' : 'Why this match?';
  return `<details class="review-why"><summary>${heading}</summary>${why || concerns ? `<ul>${why}${concerns}</ul>` : ''}${rows ? `<table><tbody>${rows}</tbody></table>` : ''}${reference}</details>`;
}

async function decide(trackKey, source, chartId, decision) {
  try {
    const result = await api('/api/review/decide', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({track_key: trackKey, source, chart_id: chartId, decision,
        workspace_id: reviewData.workspace_id || undefined})});
    lastSaved = decision === 'clear' ? 'Current choice cleared.' : 'Chart choice saved.';
    // Reload the authoritative persisted rows instead of making the card state
    // optimistic or allowing it to drift from SQLite.
    await loadReview(false);
    await refreshSummary();
    setTimeout(() => { lastSaved = ''; renderReview(); }, 4000);
    return result;
  } catch (error) {
    lastSaved = error.message || 'Could not save that choice.';
    renderReview();
  }
}

async function acceptAllReview() {
  if (jobRunning) return;
  const count = reviewData.progress?.review_track_count || 0;
  if (!count) {
    lastSaved = 'There are no review matches to accept.';
    renderReview();
    return;
  }
  if (!confirm(`Choose the best chart for all ${count} songs that need review?`)) return;
  try {
    const result = await api('/api/review/accept-all', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace_id: reviewData.workspace_id || undefined})});
    lastSaved = `Saved ${result.accepted || 0} chart choices.`;
    await loadReview(false);
    await refreshSummary();
    setTimeout(() => { lastSaved = ''; renderReview(); }, 5000);
  } catch (error) {
    lastSaved = error.message || 'Bulk accept failed.';
    renderReview();
  }
}

async function selectBulkScope(scope) {
  if (jobRunning || !isLocalJourney()) return;
  const label = scope === 'strong' ? 'all Strong matches' : 'the current eligible result';
  if (!confirm(`Select ${label}? Existing choices for those songs will be replaced.`)) return;
  try {
    const result = await api('/api/spotify/select-all', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace_id: reviewData.workspace_id || reviewData.journey?.workspace_id,
        scope, search: $('reviewFilter').value.trim()})});
    lastSaved = `Selected ${result.selected || 0} charts.`;
    await loadReview(false);
    await refreshSummary();
    setTimeout(() => { lastSaved = ''; renderReview(); }, 5000);
  } catch (error) {
    lastSaved = error.message || 'Bulk selection failed.';
    renderReview();
  }
}

function resetReviewPage() {
  reviewOffset = 0;
  loadReview();
}

let reviewSearchTimer = null;
function scheduleReviewSearch() {
  reviewOffset = 0;
  if (reviewSearchTimer) clearTimeout(reviewSearchTimer);
  reviewSearchTimer = setTimeout(() => loadReview(false), 250);
}

$('showRejected').addEventListener('change', resetReviewPage);
$('reviewState').addEventListener('change', resetReviewPage);
$('reviewSort').addEventListener('change', resetReviewPage);
$('reviewFilter').addEventListener('input', scheduleReviewSearch);
$('reviewPrevious').addEventListener('click', () => {
  if (reviewOffset <= 0) return;
  reviewOffset = Math.max(0, reviewOffset - reviewPageSize);
  loadReview(false);
});
$('reviewNext').addEventListener('click', () => {
  if (!(reviewData.page || {}).has_more) return;
  reviewOffset += reviewPageSize;
  loadReview(false);
});
$('acceptAll').addEventListener('click', acceptAllReview);
$('selectStrong').addEventListener('click', () => selectBulkScope('strong'));
$('selectEligible').addEventListener('click', () => selectBulkScope('eligible'));
$('cancelExport').addEventListener('click', cancelNeutralExport);
$('retryFailed').addEventListener('click', () => runNeutralExport(true));

$('exactDry').addEventListener('click', () => run('exact-dry-run'));
$('exactBuild').addEventListener('click', () => {
  if (isLocalJourney()) {
    run('exact-build');
  } else if (confirm('Export the selected charts into the library now?')) {
    run('exact-build');
  }
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
