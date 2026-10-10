"""Background job queue for the multi-user web app.

The engine persists resumable discovery progress in ``discovery_workspaces``
/``discovery_progress`` and the query cache survives restarts. This module adds
the missing multi-user pieces:

  * A durable ``background_jobs`` table (per-user, resumable status).
  * ONE global worker thread runs jobs FIFO across all users; Enchor-bound work
    is additionally gated by the global network gate so a public app sharing one
    server IP never trips Enchor's ~1-2 req/s hard cap.
  * Completion email is optional and best-effort (see notifications).

A single worker is deliberate: being slow is fine, correctness and completion
messaging matter far more than throughput. Async web threads keep serving while
the worker advances one job at a time.
"""

from __future__ import annotations

import logging
import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.sources.base import ChartSource
from csv_to_clonehero.server.auth import build_oauth, build_soundcloud_oauth, build_spotify_client
from csv_to_clonehero.server.downloads import BrowserDownloads, build_archive, selection_snapshot
from csv_to_clonehero.server.notifications import EmailSender
from csv_to_clonehero.server.security import TokenCipher
from csv_to_clonehero.server.throttle import global_gate

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    """Lightweight in-process handle the web layer reads for one job."""

    id: int
    user_id: int
    kind: str
    workspace_key: str = ""
    notify_email: str = ""
    status: str = "queued"
    phase: str = "queued"
    message: str = ""
    error: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    heartbeat_at: float | None = None


def _row_value(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            try:
                return getattr(row, name)
            except AttributeError:
                continue
    return default


def _job_payload(row: Any) -> dict[str, Any]:
    from csv_to_clonehero.database import _load_json

    progress = _load_json(_row_value(row, "progress_json", ""), {}) or {}
    result = _load_json(_row_value(row, "result_json", ""), {}) or {}
    if not isinstance(progress, dict):
        progress = {}
    if not isinstance(result, dict):
        result = {}
    for key, value in list(progress.items()):
        try:
            progress[key] = int(value)
        except (TypeError, ValueError):
            try:
                progress[key] = float(value)
            except (TypeError, ValueError):
                pass
    return {
        "id": _row_value(row, "id"),
        "kind": _row_value(row, "kind", ""),
        "status": _row_value(row, "status", ""),
        "phase": _row_value(row, "phase", ""),
        "message": _row_value(row, "message", "") or "",
        "error": _row_value(row, "error", None),
        "progress": progress,
        "result": result,
        "workspace_key": _row_value(row, "workspace_key", "") or "",
        "notify_email": _row_value(row, "notify_email", "") or "",
        "started_at": _row_value(row, "started_at", None),
        "finished_at": _row_value(row, "finished_at", None),
        "cancel_requested": bool(_row_value(row, "cancel_requested", default=0)),
        "heartbeat_at": _row_value(row, "heartbeat_at", default=None),
        "created_at": _row_value(row, "created_at", None),
        "updated_at": _row_value(row, "updated_at", None),
    }


def _from_row(row: Any) -> JobResult:
    payload = _job_payload(row)
    return JobResult(
        id=int(payload["id"]),
        user_id=int(row["user_id"]),
        kind=payload["kind"],
        workspace_key=payload["workspace_key"],
        notify_email=payload["notify_email"],
        status=payload["status"],
        phase=payload["phase"],
        message=payload["message"],
        error=payload["error"],
        progress=payload["progress"],
        result=payload["result"],
        created_at=payload["created_at"],
        started_at=payload["started_at"],
        finished_at=payload["finished_at"],
        cancel_requested=payload["cancel_requested"],
        heartbeat_at=payload["heartbeat_at"],
    )


class JobManager:
    """Owns the durable queue, the single worker, and job execution."""

    def __init__(
        self,
        config: AppConfig,
        cipher: TokenCipher,
        *,
        source_factory: Callable[[AppConfig], list[ChartSource]] | None = None,
        downloads: BrowserDownloads | None = None,
        sender: EmailSender | None = None,
        poll_interval: float = 1.0,
        auto_start: bool = True,
    ) -> None:
        self.config = config
        self.cipher = cipher
        self.source_factory = source_factory or _default_source_factory
        self.downloads = downloads
        self.sender = sender or EmailSender(config.notifications)
        self.poll_interval = max(0.1, float(poll_interval))
        self._cv = threading.Condition()
        self._shutdown_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handles: dict[int, JobResult] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._availability_lock = threading.Lock()
        self._availability_cache: tuple[tuple, dict[str, Any]] | None = None
        self._recover_orphaned_jobs()
        if auto_start:
            self.start()

    # ---- lifecycle ---------------------------------------------------------
    def _recover_orphaned_jobs(self) -> None:
        db = Database(self.config.database_path)
        try:
            recovered = db.recover_running_jobs()
            recovered_discovery = db.recover_interrupted_discovery()
            db.recover_public_expansion_changes()
        finally:
            db.close()
        if recovered:
            log.warning("marked %s orphaned background job(s) interrupted", recovered)
        if recovered_discovery:
            log.warning(
                "marked %s orphaned discovery workspace(s) interrupted",
                recovered_discovery,
            )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown_event.clear()
        thread = threading.Thread(
            target=self._loop, name="c2ch-job-worker", daemon=True
        )
        thread.start()
        self._thread = thread

    def shutdown(self, timeout: float = 5.0) -> None:
        self._shutdown_event.set()
        for event in self._cancel_events.values():
            event.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---- public API --------------------------------------------------------
    def _availability_key(self, db: Database) -> tuple:
        from csv_to_clonehero.catalog import MAX_CATALOG_HEADER_BYTES

        path = self.config.catalog_path
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            # A bounded header read catches atomic/content replacements even
            # when an operator preserves the file's size and modification time.
            with path.open("rb") as stream:
                header = stream.readline(MAX_CATALOG_HEADER_BYTES + 1)
            header_identity = hashlib.sha256(header).hexdigest()
        except OSError:
            identity = None
            header_identity = None
        return (str(path), identity, header_identity, db.chart_validation_fingerprint())

    def discovery_availability(self, db: Database) -> dict[str, Any]:
        """A request reads readiness; only the worker parses a cold catalog."""
        from csv_to_clonehero.server.safeguards import get_enchor_preflight

        key = self._availability_key(db)
        with self._availability_lock:
            cached = self._availability_cache
        if cached is not None and cached[0] == key:
            result = dict(cached[1])
            if result.get("mode") == "catalog" or float(result.get("expires_at") or 0) > time.time():
                return result
        if key[1] is None:
            result = get_enchor_preflight(db)
            result.update(mode="live_fallback", catalog_available=False,
                          catalog_diagnostic="The local chart catalog is unavailable.")
            return result
        return {"available": False, "fresh": False, "usable": False,
                "status": "check_required", "busy": False, "retryable": True,
                "retry_after_seconds": 0, "source": "enchor",
                "message": "Check chart readiness before finding charts."}

    def _save_discovery_availability(self, db: Database, result: dict[str, Any], key: tuple) -> dict[str, Any]:
        if key != self._availability_key(db):
            return {"available": False, "fresh": False, "usable": False,
                    "status": "stale", "busy": False, "retryable": True,
                    "message": "The chart catalog changed during its check. Check readiness again."}
        with self._availability_lock:
            self._availability_cache = (key, dict(result))
        return result

    def enqueue(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
        initial_progress: Mapping[str, Any] | None = None,
    ) -> JobResult:
        result, _created = self.enqueue_unique(
            user_id,
            kind,
            workspace_key=workspace_key,
            notify_email=notify_email,
            initial_progress=initial_progress,
        )
        return result

    def enqueue_unique(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
        initial_progress: Mapping[str, Any] | None = None,
    ) -> tuple[JobResult, bool]:
        with self._cv:
            db = Database(self.config.database_path)
            try:
                row, created = db.create_or_get_active_job(
                    user_id, kind, workspace_key=workspace_key, notify_email=notify_email
                )
                if created and initial_progress is not None:
                    row = db.update_job(int(row["id"]), progress=dict(initial_progress))
                result = _from_row(row)
            finally:
                db.close()
            self._handles[result.id] = result
            self._cv.notify_all()
        return result, created

    def get(self, job_id: int) -> JobResult | None:
        db = Database(self.config.database_path)
        try:
            row = db.get_job(job_id)
            if row is None:
                return None
            job = _from_row(row)
        finally:
            db.close()
        self._handles[job_id] = job
        return job

    def list_for_user(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        db = Database(self.config.database_path)
        try:
            return [_job_payload(row) for row in db.list_user_jobs(user_id, limit=limit)]
        finally:
            db.close()

    def latest_for_user_workspace(self, user_id: int, *, workspace_key: str, kind: str) -> JobResult | None:
        db = Database(self.config.database_path)
        try:
            row = db.get_latest_job(user_id, workspace_key=workspace_key, kind=kind)
            return _from_row(row) if row is not None else None
        finally:
            db.close()

    # ---- worker ------------------------------------------------------------
    def _loop(self) -> None:
        global_gate()  # ensure the shared gate exists
        while not self._shutdown_event.is_set():
            next_job = self._dequeue_next()
            if next_job is None:
                with self._cv:
                    if self._shutdown_event.is_set():
                        break
                    self._cv.wait(self.poll_interval)
                continue
            self._run_job(next_job)

    def _dequeue_next(self) -> JobResult | None:
        with self._cv:
            db = Database(self.config.database_path)
            try:
                for row in db.get_running_jobs():
                    job = _from_row(row)
                    if job.status in ("queued", "running"):
                        claimed = db.claim_job(job.id)
                        if claimed is not None:
                            return _from_row(claimed)
                return None
            finally:
                db.close()

    def _run_job(self, job: JobResult) -> None:
        self._cancel_events.setdefault(job.id, threading.Event())
        try:
            if self._cancelled(job.id):
                self._mark(job.id, status="cancelled", phase="cancelled", message="Job cancelled")
                return
            if job.kind == "preflight":
                self._run_preflight(job)
                return
            if job.kind == "discovery":
                from csv_to_clonehero.server.safeguards import (
                    get_discovery_availability,
                    refresh_enchor_preflight,
                )

                db = Database(self.config.database_path)
                try:
                    self._mark(job.id, status="running", phase="checking_catalog",
                               message="Checking chart readiness")
                    key = self._availability_key(db)
                    if not get_discovery_availability(self.config, db).get("fresh"):
                        result = refresh_enchor_preflight(self.config, db, self.source_factory)
                    else:
                        result = get_discovery_availability(self.config, db)
                    self._save_discovery_availability(db, result, key)
                finally:
                    db.close()
            if self._cancelled(job.id):
                self._mark(job.id, status="cancelled", phase="cancelled", message="Job cancelled")
                return
            with global_gate().acquire(tag=f"job-{job.id}"):
                if self._shutdown_event.is_set() or self._cancelled(job.id):
                    self._mark(job.id, status="cancelled", error="server shutdown")
                    return
                self._execute(job)
        except Exception as exc:  # noqa: BLE001 - surfaced per-job
            log.exception("job %s failed", job.id)
            if self._cancelled(job.id):
                self._mark(job.id, status="cancelled", phase="cancelled", message="Job cancelled")
            else:
                self._mark(job.id, status="failed", error=str(exc)[:1000])
        finally:
            self._maybe_notify(job.id)
            self._cancel_events.pop(job.id, None)

    def _cancelled(self, job_id: int) -> bool:
        event = self._cancel_events.setdefault(job_id, threading.Event())
        if event.is_set():
            return True
        db = Database(self.config.database_path)
        try:
            row = db.get_job(job_id)
            requested = row is not None and bool(row["cancel_requested"])
        finally:
            db.close()
        if requested:
            event.set()
        return requested

    def _execute(self, job: JobResult) -> None:
        if job.kind == "discovery":
            self._run_discovery(job)
        elif job.kind == "expansion":
            self._run_expansion(job)
        elif job.kind == "download":
            self._run_download(job)
        else:
            raise ValueError(f"unknown job kind: {job.kind}")

    def _run_preflight(self, job: JobResult) -> None:
        """Catalog verification/provider probes share the existing FIFO worker."""
        from csv_to_clonehero.server.safeguards import refresh_enchor_preflight

        self._mark(job.id, status="running", phase="checking_catalog", message="Checking chart catalog readiness")
        db = Database(self.config.database_path)
        try:
            user = db.get_user(job.user_id)
            if user is None or job.workspace_key != Database.workspace_key_for_user(str(user["spotify_user_id"])):
                raise RuntimeError("This chart-readiness check is no longer available.")
            key = self._availability_key(db)
            result = refresh_enchor_preflight(self.config, db, self.source_factory)
            result = self._save_discovery_availability(db, result, key)
            if self._cancelled(job.id):
                self._mark(job.id, status="cancelled", phase="cancelled", message="Chart-readiness check cancelled")
                return
            self._mark(job.id, status="complete", phase="complete",
                       message="Charts ready" if result.get("usable") else "Chart-readiness check finished",
                       result={"preflight": result})
        finally:
            db.close()

    def _run_download(self, job: JobResult) -> None:
        """Prepare one user-owned ZIP off the request thread.

        The durable job records status and a safe report. Signed delivery
        tokens and staging paths remain solely in BrowserDownloads memory.
        """
        if self.downloads is None:
            raise RuntimeError("Download preparation is unavailable.")
        db = Database(self.config.database_path)
        archive: Path | None = None
        registered_token: str | None = None
        try:
            workspace = db.get_workspace(job.workspace_key)
            user = db.get_user(job.user_id)
            if (
                workspace is None
                or user is None
                or str(workspace["workspace_key"]) != Database.workspace_key_for_user(
                    str(user["spotify_user_id"])
                )
            ):
                raise RuntimeError("The selected workspace is no longer available.")
            workspace_id = int(workspace["id"])
            current_snapshot = selection_snapshot(db, workspace_id)
            if (
                not current_snapshot["selection_count"]
                or current_snapshot["generation_id"] != job.progress.get("generation_id")
                or current_snapshot["selection_fingerprint"] != job.progress.get("selection_fingerprint")
            ):
                raise RuntimeError("Chart choices changed while the download was queued. Review and prepare again.")

            self._mark(
                job.id, status="running", phase="preparing",
                message="Validating and packaging selected charts",
                progress={**job.progress, "completed": 0, "total": current_snapshot["selection_count"]},
            )
            cancel_event = self._cancel_events[job.id]
            last_update = 0.0

            def on_progress(completed: int, total: int, label: str) -> None:
                nonlocal last_update
                now = time.monotonic()
                if now - last_update < 0.5 and completed < total:
                    return
                last_update = now
                packaging = label == "Writing ZIP archive"
                self._mark(
                    job.id, status="running", phase="packaging" if packaging else "validating",
                    message="Packaging ZIP archive" if packaging else "Validating selected charts",
                    progress={**job.progress, "completed": completed, "total": total},
                    heartbeat_at=time.time(),
                )

            with self.downloads.building(workspace_id):
                archive, summary = build_archive(
                    self.config, db, workspace_id, None, self.downloads.root,
                    progress=on_progress, cancellation_event=cancel_event,
                )
                after_build = selection_snapshot(db, workspace_id)
                if (
                    after_build["generation_id"] != current_snapshot["generation_id"]
                    or after_build["selection_count"] != current_snapshot["selection_count"]
                    or after_build["selection_fingerprint"] != current_snapshot["selection_fingerprint"]
                ):
                    raise RuntimeError("Chart choices changed while the ZIP was prepared. Review and prepare again.")
                if self._cancelled(job.id) or self._shutdown_event.is_set():
                    self._mark(job.id, status="cancelled", phase="cancelled", message="Download preparation cancelled")
                    return
                download = self.downloads.create(job.user_id, workspace_id, archive)
                registered_token = download["token"]
                archive = None  # BrowserDownloads now owns and sweeps the ZIP.
                if self._cancelled(job.id) or self._shutdown_event.is_set():
                    self.downloads.revoke(registered_token)
                    registered_token = None
                    self._mark(job.id, status="cancelled", phase="cancelled", message="Download preparation cancelled")
                    return
            partial = bool(summary.get("failed") or summary.get("remaining") or summary.get("errors"))
            finished = db.finish_download_job_if_active(
                job.id,
                status="partial" if partial else "complete",
                message="ZIP ready with exceptions" if partial else "ZIP ready",
                progress={**job.progress, "completed": summary.get("selected", 0), "total": summary.get("selected", 0)},
                result={
                    "summary": summary,
                    "archive_id": download["archive_id"],
                    "archive_created_at": download["created_at"],
                    "size_bytes": download["size_bytes"],
                },
            )
            if finished is None:
                self.downloads.revoke(registered_token)
                registered_token = None
                if self._cancelled(job.id):
                    self._mark(job.id, status="cancelled", phase="cancelled", message="Download preparation cancelled")
                return
            self._handles[job.id] = _from_row(finished)
            registered_token = None
        except Exception:
            if registered_token is not None:
                self.downloads.revoke(registered_token)
            raise
        finally:
            if archive is not None:
                archive.unlink(missing_ok=True)
            db.close()

    def _run_expansion(self, job: JobResult) -> None:
        """Build a confirmed expansion solely from the validated local catalog."""
        from csv_to_clonehero.catalog import load_enchor_catalog
        from csv_to_clonehero.combined_pipeline import expand_combined

        if not self.config.server.enable_expansion:
            raise RuntimeError("Artist expansion is disabled.")
        artists = [str(value) for value in job.progress.get("artists", ())]
        coverage_mode = str(job.progress.get("coverage_mode") or "recommended")
        if coverage_mode not in {"all", "recommended"}:
            raise RuntimeError("coverage_mode must be all or recommended")
        expected_generation = int(job.progress.get("exact_generation_id") or 0)
        if not artists:
            raise RuntimeError("Choose at least one artist to expand.")

        self._mark(
            job.id,
            status="running",
            phase="catalog_expansion",
            message="Preparing artist recommendations",
            progress={**job.progress, "completed_artists": 0, "total_artists": len(artists)},
        )
        db = Database(self.config.database_path)
        restore_context = None
        restore_workspace = None
        try:
            workspace = db.get_workspace(job.workspace_key)
            if workspace is None:
                raise RuntimeError("discovery workspace no longer exists")
            if int(workspace["id"]) <= 0:
                raise RuntimeError("discovery workspace is invalid")
            user = db.get_user(job.user_id)
            if user is None or str(workspace["workspace_key"]) != Database.workspace_key_for_user(
                str(user["spotify_user_id"])
            ):
                raise RuntimeError("artist expansion workspace is not owned by this user")
            metadata = _json_object(workspace["metadata_json"])
            generation = db.get_current_exact_generation(workspace["id"])
            if (
                generation is None
                or int(generation["id"]) != expected_generation
                or metadata.get("workflow") != "public_catalog"
            ):
                raise RuntimeError(
                    "Artist expansion requires the current completed catalog-backed exact match."
                )
            catalog = load_enchor_catalog(
                self.config.catalog_path,
                excluded_identities=db.get_blocked_chart_identities(),
                validation_fingerprint=db.chart_validation_fingerprint(),
            )
            if not catalog.available:
                raise RuntimeError(catalog.diagnostic or "The local chart catalog is unavailable.")
            restore_workspace = int(workspace["id"])
            with db.atomic_workspace_selection():
                expected_revision = job.progress.get("selection_revision")
                if expected_revision is not None and int(expected_revision) != db.public_selection_revision(restore_workspace):
                    raise RuntimeError("The choices changed before artist expansion started; review them and try again.")
                restore_context = db.begin_public_expansion_change(restore_workspace, expected_generation)

            def on_progress(payload: Mapping[str, Any]) -> None:
                if self._cancelled(job.id):
                    raise RuntimeError("Artist expansion was cancelled.")
                self._mark(
                    job.id,
                    status="running",
                    phase="catalog_expansion",
                    message=str(payload.get("message") or "Preparing artist recommendations"),
                    progress={**job.progress, **dict(payload)},
                    heartbeat_at=time.time(),
                )

            result = expand_combined(
                self.config,
                db,
                int(workspace["id"]),
                artists,
                catalog=catalog,
                exact_generation_id=expected_generation,
                on_progress=on_progress,
                coverage_mode=coverage_mode,
            )
            current = db.get_current_exact_generation(workspace["id"])
            if current is None or int(current["id"]) != expected_generation:
                db.clear_workspace_expansion_candidates(workspace["id"])
                raise RuntimeError("Exact matching changed while expansion was running; choose artists again.")
            selected = set(artists)
            for row in db.get_workspace_expansion_artists(workspace["id"]):
                db.set_expansion_artist_selected(
                    workspace["id"], str(row["artist_norm"]), str(row["artist_norm"]) in selected
                )
            cancelled = self._cancelled(job.id)
            payload = {
                "artists": artists,
                "exact_generation_id": expected_generation,
                "total_artists": len(artists),
                "completed_artists": len(artists),
                "recommendations": int(result.candidates),
                "coverage_mode": coverage_mode,
                "all_artists": bool(job.progress.get("all_artists", False)),
                "catalog_snapshot_id": result.catalog_snapshot_id,
                "network_requests": 0,
            }
            with db.atomic_workspace_selection():
                db.finish_public_expansion_change(restore_workspace, expected_generation, restore_context)
            restore_context = None
            payload.update(db.public_selection_receipt(restore_workspace))
            self._mark(
                job.id,
                status="cancelled" if cancelled else "complete",
                phase="cancelled" if cancelled else "complete",
                message="Job cancelled" if cancelled else "Artist recommendations are ready.",
                progress=payload,
                result=payload,
            )
        finally:
            if restore_context is not None:
                # A failed/cancelled replacement may already have changed some
                # artists. Its owned restore point survives the terminal job.
                with db.atomic_workspace_selection():
                    db.finish_public_expansion_change(restore_workspace, expected_generation, restore_context)
            db.close()

    def _run_discovery(self, job: JobResult) -> None:
        from csv_to_clonehero.catalog import load_enchor_catalog
        from csv_to_clonehero.combined_pipeline import (
            discover_combined,
            enforce_library_limit,
            workspace_snapshots,
        )
        from csv_to_clonehero.spotify_pipeline import run_spotify_discovery
        from csv_to_clonehero.server.safeguards import validate_discovery_start

        self._mark(job.id, status="running", phase="starting", message="Starting discovery")
        db = Database(self.config.database_path)
        sources: list[ChartSource] = []
        client: Any | None = None
        soundcloud_client: Any | None = None
        try:
            validation = validate_discovery_start(
                db,
                self.config,
                user_id=job.user_id,
                workspace_key=job.workspace_key,
                current_job_id=job.id,
            )
            if not validation.ok:
                raise RuntimeError(" ".join(validation.errors))
            user = db.get_user(job.user_id)
            if user is None:
                raise RuntimeError("user no longer exists")
            workspace = db.get_workspace(job.workspace_key)
            workspace_id = int(workspace["id"]) if workspace is not None else None
            metadata = _json_object(workspace["metadata_json"] if workspace is not None else "{}")
            catalog = load_enchor_catalog(
                self.config.catalog_path,
                excluded_identities=db.get_blocked_chart_identities(),
                validation_fingerprint=db.chart_validation_fingerprint(),
            )
            spotify_scope = (
                db.get_workspace_scope_data(workspace_id)
                if workspace_id is not None
                else None
            ) or metadata.get("spotify_scope")
            soundcloud_scope = metadata.get("soundcloud_scope")
            persisted_tracks = _provider_tracks_from_provenance(db, workspace_id)
            has_persisted_soundcloud = any(
                track.provider == "soundcloud" for track in persisted_tracks
            )
            has_uploaded_soundcloud = any(
                str(row["provider"]) == "soundcloud"
                and str(row["source_key"]) != "api"
                for row in (
                    db.get_source_summary(workspace_id)
                    if workspace_id is not None
                    else []
                )
            )
            combined_provider_discovery = (
                has_persisted_soundcloud or _scope_selected(soundcloud_scope)
            )
            pending_snapshots: list[tuple[Any, Mapping[str, Any] | None]] = []
            if (catalog.available or combined_provider_discovery) and _scope_selected(spotify_scope):
                from csv_to_clonehero.providers.spotify import SpotifyLibraryProvider

                self._mark(job.id, status="running", phase="provider_import", message="Loading current Spotify selection")
                oauth = build_oauth(self.config, job.user_id, self.cipher)
                client = build_spotify_client(self.config, oauth)
                spotify_snapshot = SpotifyLibraryProvider(client).import_library(scope=spotify_scope)
                if workspace_id is None:
                    raise RuntimeError("discovery workspace no longer exists")
                pending_snapshots.append((spotify_snapshot, spotify_scope))

            if _scope_selected(soundcloud_scope) and not has_uploaded_soundcloud:
                from csv_to_clonehero.providers.soundcloud_api import SoundCloudAPIProvider, SoundCloudClient

                self._mark(job.id, status="running", phase="provider_import", message="Loading current SoundCloud selection")
                soundcloud_oauth = build_soundcloud_oauth(self.config, job.user_id)
                soundcloud_client = SoundCloudClient(
                    soundcloud_oauth,
                    timeout=float(self.config.network.timeout_seconds or 20),
                )
                soundcloud_snapshot = SoundCloudAPIProvider(soundcloud_client).import_library(scope=soundcloud_scope)
                if workspace_id is None:
                    raise RuntimeError("discovery workspace no longer exists")
                pending_snapshots.append((soundcloud_snapshot, soundcloud_scope))

            if workspace_id is None:
                raise RuntimeError("discovery workspace no longer exists")
            if pending_snapshots:
                replaced = {str(snapshot.provider).casefold() for snapshot, _ in pending_snapshots}
                prospective = [
                    snapshot for snapshot in workspace_snapshots(db, workspace_id)
                    if str(snapshot.provider).casefold() not in replaced
                ] + [snapshot for snapshot, _ in pending_snapshots]
                enforce_library_limit(prospective, self.config.server.max_library_tracks)
                for snapshot, scope_value in pending_snapshots:
                    db.upsert_library_snapshot(workspace_id, snapshot, scope=scope_value)
            snapshots = workspace_snapshots(db, workspace_id)
            if snapshots and (catalog.available or combined_provider_discovery):
                enforce_library_limit(snapshots, self.config.server.max_library_tracks)
            current_provider_tracks = _provider_tracks_from_provenance(db, workspace_id)
            current_provider_playlists = [
                playlist
                for snapshot in snapshots
                for playlist in snapshot.playlists
            ]
            prefetched_library = (
                _provider_library_result(
                    current_provider_tracks,
                    current_provider_playlists,
                )
                if current_provider_tracks and (catalog.available or combined_provider_discovery)
                else None
            )

            self._mark(
                job.id,
                status="running",
                phase="starting",
                message=f"Preparing discovery for {user['display_name'] or user['spotify_user_id']}",
            )
            if not catalog.available and prefetched_library is None:
                oauth = build_oauth(self.config, job.user_id, self.cipher)
                client = build_spotify_client(self.config, oauth)

            def on_progress(payload: Mapping[str, Any]) -> None:
                data = dict(payload)
                message = str(data.get("message") or data.get("phase") or "").strip()
                self._mark(
                    job.id,
                    status="running",
                    phase=str(data.get("phase") or "running"),
                    message=message,
                    progress=data,
                    heartbeat_at=time.time(),
                )
                db.update_workspace_progress(
                    workspace_id,
                    status="running",
                    phase=str(data.get("phase") or "running"),
                    total_tracks=int(data.get("total_tracks") or 0),
                    completed_tracks=int(data.get("processed_tracks") or data.get("completed_tracks") or 0),
                    message=message,
                    metadata={
                        key: data[key]
                        for key in (
                            "elapsed_seconds",
                            "estimated_remaining_seconds",
                            "estimated_lower_seconds",
                            "estimated_upper_seconds",
                        )
                        if key in data
                    },
                )

            def on_log(message: Any) -> None:
                if message:
                    self._mark(job.id, message=str(message)[:1000])

            if catalog.available:
                result = discover_combined(
                    self.config,
                    db,
                    snapshots,
                    workspace_key=job.workspace_key,
                    workspace_name=str(user["spotify_user_id"]),
                    catalog=catalog,
                    on_progress=on_progress,
                    operation_id=str(job.id),
                    cancellation_event=self._cancel_events[job.id],
                )
            else:
                sources = self.source_factory(self.config)
                result = run_spotify_discovery(
                    self.config,
                    db,
                    sources,
                    client,
                    prefetched_library=prefetched_library,
                    workspace_id=workspace_id,
                    workspace_name=user["spotify_user_id"],
                    on_progress=on_progress,
                    on_log=on_log,
                    cancellation_event=self._cancel_events[job.id],
                )
            stats = getattr(result, "stats", None)
            cancelled = self._cancelled(job.id)
            final_payload = _stats_payload(stats)
            final_payload["discovery_mode"] = "catalog" if catalog.available else "live_fallback"
            final_payload["catalog_diagnostic"] = catalog.diagnostic
            if not catalog.available:
                final_payload.update(_live_yield_audit(self.config, db, workspace_id, stats))
            elif isinstance(final_payload.get("yield_histogram"), Mapping):
                final_payload["yield_total"] = sum(
                    int(value or 0) for value in final_payload["yield_histogram"].values()
                )
            previous_progress = self.get(job.id).progress if self.get(job.id) is not None else {}
            terminal_progress = {**previous_progress, **final_payload}
            workspace_row = db.get_workspace(workspace_id)
            workspace_metadata = _json_object(workspace_row["metadata_json"] if workspace_row is not None else "{}")
            workspace_metadata.update({
                "workflow": "public_catalog" if catalog.available else "public_live_fallback",
                "partial": str(result.status or "complete") != "complete",
                "stats": final_payload,
            })
            db.update_workspace(
                workspace_id,
                status="cancelled" if cancelled else str(result.status or "complete"),
                phase="cancelled" if cancelled else str(result.phase or result.status or "complete"),
                total_tracks=int(final_payload.get("tracks") or final_payload.get("library_tracks") or 0),
                completed_tracks=int(final_payload.get("tracks") or final_payload.get("library_tracks") or 0),
                metadata=workspace_metadata,
            )
            self._mark(
                job.id,
                status="cancelled" if cancelled else str(result.status or "complete"),
                phase="cancelled" if cancelled else str(result.phase or result.status or "complete"),
                error=str(result.error or "") or None,
                message="Job cancelled" if cancelled else _completion_message(result),
                progress=terminal_progress,
                result=final_payload,
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            for source in sources:
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    pass
            if soundcloud_client is not None:
                try:
                    soundcloud_client.close()
                    soundcloud_client.oauth.close()
                except Exception:  # noqa: BLE001
                    pass
            db.close()

    def _mark(
        self,
        job_id: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        message: str | None = None,
        error: str | None = None,
        progress: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        heartbeat_at: float | None = None,
    ) -> None:
        if status == "running" and heartbeat_at is None:
            heartbeat_at = time.time()
        db = Database(self.config.database_path)
        try:
            row = db.update_job(
                job_id,
                status=status,
                phase=phase,
                message=message,
                error=error,
                progress=progress,
                result=result,
                started_at=time.time() if status == "running" else None,
                finished_at=time.time() if status in ("complete", "failed", "partial", "cancelled") else None,
                heartbeat_at=heartbeat_at,
            )
        finally:
            db.close()
        if row is not None:
            self._handles[job_id] = _from_row(row)

    def cancel(self, job_id: int, user_id: int) -> JobResult | None:
        db = Database(self.config.database_path)
        try:
            row = db.get_job(job_id)
            if row is None or int(row["user_id"]) != int(user_id):
                return None
            row = db.request_job_cancel(job_id)
            result = _from_row(row) if row is not None else None
        finally:
            db.close()
        self._cancel_events.setdefault(int(job_id), threading.Event()).set()
        with self._cv:
            self._cv.notify_all()
        return result

    def _maybe_notify(self, job_id: int) -> None:
        job = self.get(job_id)
        if job is None or not job.notify_email:
            return
        subject = f"CSV to Clone Hero: your job {job.kind} finished"
        body = _completion_email(job, self.config.public_base_url)
        self.sender.send(subject, body, job.notify_email)


def _default_source_factory(config: AppConfig) -> list[ChartSource]:
    from csv_to_clonehero.sources import build_sources

    return build_sources(config)


def _scope_selected(scope: Any) -> bool:
    return isinstance(scope, Mapping) and bool(
        scope.get("playlist_ids") or scope.get("include_liked")
    )


def _provider_tracks_from_provenance(
    db: Database, workspace_id: int | None
) -> list[Any]:
    if workspace_id is None:
        return []
    from csv_to_clonehero.combined_pipeline import workspace_snapshots

    return [
        track
        for snapshot in workspace_snapshots(db, workspace_id)
        for track in snapshot.tracks
    ]


def _provider_library_result(
    provider_tracks: list[Any], provider_playlists: list[Any] | tuple[Any, ...] = ()
) -> Any:
    """Adapt approved provider records to the shared discovery import shape."""
    from csv_to_clonehero.providers.base import (
        merge_provider_tracks,
        provider_track_playlist_memberships,
    )
    from csv_to_clonehero.spotify import LibraryCounts, LibraryImportResult, SpotifyPlaylist

    ordered = sorted(
        provider_tracks,
        key=lambda track: (
            track.dedupe_key,
            track.provider != "spotify",
            track.provider,
            track.provider_id,
        ),
    )
    provenance_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for track in ordered:
        provenance_by_key.setdefault(track.dedupe_key, []).append({
            "provider": track.provider,
            "provider_id": track.provider_id,
            "playlist_ids": list(track.playlist_ids),
            "playlist_names": list(track.playlist_names),
            "playlist_memberships": provider_track_playlist_memberships(track),
            "liked": bool(track.liked),
            "album": track.album,
            "isrc": track.isrc,
            "url": track.url,
            "metadata": dict(track.metadata),
        })

    records: list[dict[str, Any]] = []
    playlist_models: dict[str, str] = {}
    for playlist in provider_playlists:
        raw_id = str(getattr(playlist, "provider_id", "") or "")
        provider = str(getattr(playlist, "provider", "") or "")
        playlist_id = raw_id if provider == "spotify" else f"{provider}:{raw_id}"
        if playlist_id:
            playlist_models[playlist_id] = str(getattr(playlist, "name", "") or raw_id)
    playlist_item_count = 0
    liked_count = 0
    for track in merge_provider_tracks(ordered):
        provenance = provenance_by_key[track.dedupe_key]
        spotify = next(
            (item for item in provenance if item["provider"] == "spotify"),
            None,
        )
        identity = spotify or provenance[0]
        provider_id = str(identity["provider_id"])
        record_id = (
            provider_id
            if identity["provider"] == "spotify"
            else f"{identity['provider']}:{provider_id}"
        )
        playlists: list[dict[str, str]] = []
        seen_playlists: set[str] = set()
        for item in provenance:
            for raw_id, raw_name in item["playlist_memberships"]:
                raw_id = raw_id or raw_name
                playlist_id = (
                    raw_id
                    if item["provider"] == "spotify"
                    else f"{item['provider']}:{raw_id}"
                )
                if playlist_id in seen_playlists:
                    continue
                seen_playlists.add(playlist_id)
                playlist = {
                    "id": playlist_id,
                    "name": raw_name or raw_id,
                }
                playlists.append(playlist)
                playlist_models.setdefault(playlist_id, playlist["name"])
        metadata = dict(track.metadata)
        metadata["provenance"] = provenance
        metadata["providers"] = {
            item["provider"]: {
                "id": item["provider_id"],
                "liked": item["liked"],
                "playlist_ids": item["playlist_ids"],
                "playlist_names": item["playlist_names"],
                "url": item["url"],
                "metadata": item["metadata"],
            }
            for item in provenance
        }
        liked = any(bool(item["liked"]) for item in provenance)
        liked_count += int(liked)
        playlist_item_count += len(playlists)
        records.append({
            "spotify_id": record_id,
            "track_key": f"{track.artist_norm}|{track.title_norm}",
            "title": track.title,
            "artists": [track.artist],
            "album": track.album,
            "playlists": playlists,
            "liked": liked,
            "duration_ms": track.duration_ms,
            "isrc": track.isrc,
            "url": str(spotify["url"] if spotify else ""),
            "origin": str(identity["provider"]),
            "origin_metadata": metadata,
        })
    counts = LibraryCounts(
        playlist_count=len(playlist_models),
        playlist_item_count=playlist_item_count,
        liked_item_count=liked_count,
        included_count=len(records),
    )
    return LibraryImportResult(
        records=records,
        playlists=[
            SpotifyPlaylist(playlist_id, name)
            for playlist_id, name in playlist_models.items()
        ],
        counts=counts,
    )


def _stats_payload(stats: Any) -> dict[str, Any]:
    if stats is None:
        return {}
    from dataclasses import fields, is_dataclass

    if is_dataclass(stats):
        return {field.name: getattr(stats, field.name) for field in fields(stats)}
    values = getattr(stats, "__dict__", {})
    return dict(values) if isinstance(values, dict) else {}


def _live_yield_audit(
    config: AppConfig,
    db: Database,
    workspace_id: int,
    stats: Any,
) -> dict[str, Any]:
    """Partition fallback results without converting source failures into absence."""
    from csv_to_clonehero.combined_pipeline import TERMINAL_YIELD_CLASSES, _primary_rejection_reason
    from csv_to_clonehero.matching import BLOCKED, SELECTABLE_RESULT_TIERS, score_candidate

    histogram = {name: 0 for name in TERMINAL_YIELD_CLASSES}
    persisted = db.get_discovery_song_yield_summary(workspace_id)
    workspace_track_ids = db.get_workspace_library_track_ids(workspace_id)
    mapping = {
        "strong": "recommended_strong",
        "review": "recommended_review",
        "no_catalog_candidate": "no_plausible_catalog_candidate",
        "not_playable": "hard_rejected",
        "no_suitable": "soft_rejected",
        "incomplete_evidence": "incomplete_evidence",
    }
    if persisted["total"] == len(workspace_track_ids) and all(
        classification in mapping
        for classification in persisted["classifications"]
    ):
        for classification, count in persisted["classifications"].items():
            histogram[mapping[classification]] += int(count)
        return {
            "yield_histogram": histogram,
            "rejection_song_reasons": dict(persisted["reasons"]),
            "yield_total": sum(histogram.values()),
        }
    reasons: dict[str, int] = {}
    generation = db.get_current_exact_generation(workspace_id)
    generation_id = int(generation["id"]) if generation is not None else -1
    rows = db.get_workspace_candidates(
        workspace_id,
        kind="exact",
        include_rejected=True,
        generation_id=generation_id,
    )
    by_track: dict[int, list[Any]] = {}
    for row in rows:
        if row["track_id"]:
            by_track.setdefault(int(row["track_id"]), []).append(row)
    tracks = {
        int(track.db_id): track
        for track in db.get_spotify_tracks(True)
        if track.db_id is not None
        and int(track.db_id) in workspace_track_ids
    }
    incomplete = any(
        bool(getattr(stats, name, 0))
        for name in ("partial", "query_errors", "partial_queries", "cap_reached_queries", "catalog_unavailable_tracks")
    )
    for track_id, track in tracks.items():
        scored = []
        for row in by_track.get(track_id, ()):
            candidate = db.get_candidate(int(row["candidate_id"]))
            if candidate is not None:
                scored.append((candidate, score_candidate(track, candidate, config)))
        if any(str(value.eligibility).upper() == "RECOMMENDED"
               and value.result_tier in SELECTABLE_RESULT_TIERS for _, value in scored):
            histogram["recommended_strong"] += 1
            continue
        if any(str(value.eligibility).upper() == "ELIGIBLE_REVIEW"
               and value.result_tier in SELECTABLE_RESULT_TIERS for _, value in scored):
            histogram["recommended_review"] += 1
            continue
        if scored:
            hard = all(bool(value.gate_factors.get("hard_gate")) or value.result_tier == BLOCKED
                       for _, value in scored)
            bucket = "hard_rejected" if hard else "soft_rejected"
            classification = "not_playable" if hard else "no_suitable"
            reason = _primary_rejection_reason(classification, scored)
        elif incomplete:
            bucket = "incomplete_evidence"
            reason = "live provider failure or truncated evidence"
        else:
            bucket = "no_plausible_catalog_candidate"
            reason = "no plausible catalog candidate"
        histogram[bucket] += 1
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "yield_histogram": histogram,
        "rejection_song_reasons": reasons,
        "yield_total": sum(histogram.values()),
    }


def _completion_message(result: Any) -> str:
    status = str(getattr(result, "status", "") or "")
    if status == "complete":
        return "Discovery complete; your matches are ready."
    if status == "partial":
        return "Discovery finished with some errors; matches are ready."
    return f"Discovery ended ({status})"


def _json_object(value: Any) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _completion_email(job: JobResult, public_base_url: str = "") -> str:
    destination = public_base_url.rstrip("/") or "the application"
    return (
        f"Your chart search job #{job.id} ({job.kind}) finished with status "
        f"{job.status!r}.\n\n"
        f"Sign back in to {destination} to review and download your charts.\n\n"
        f"---\nCSV to Clone Hero\n"
    )
