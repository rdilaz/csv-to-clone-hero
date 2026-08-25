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
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.sources.base import ChartSource
from csv_to_clonehero.server.auth import build_oauth, build_spotify_client
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
    progress: dict[str, Any] = None  # type: ignore[assignment]
    result: dict[str, Any] = None  # type: ignore[assignment]
    started_at: float | None = None
    finished_at: float | None = None


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
        started_at=payload["started_at"],
        finished_at=payload["finished_at"],
    )


class JobManager:
    """Owns the durable queue, the single worker, and job execution."""

    def __init__(
        self,
        config: AppConfig,
        cipher: TokenCipher,
        *,
        source_factory: Callable[[AppConfig], list[ChartSource]] | None = None,
        sender: EmailSender | None = None,
        poll_interval: float = 1.0,
        auto_start: bool = True,
    ) -> None:
        self.config = config
        self.cipher = cipher
        self.source_factory = source_factory or _default_source_factory
        self.sender = sender or EmailSender(config.notifications)
        self.poll_interval = max(0.1, float(poll_interval))
        self._cv = threading.Condition()
        self._shutdown_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handles: dict[int, JobResult] = {}
        if auto_start:
            self.start()

    # ---- lifecycle ---------------------------------------------------------
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
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---- public API --------------------------------------------------------
    def enqueue(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
    ) -> JobResult:
        db = Database(self.config.database_path)
        try:
            row = db.create_job(
                user_id, kind, workspace_key=workspace_key, notify_email=notify_email
            )
            result = _from_row(row)
        finally:
            db.close()
        with self._cv:
            self._handles[result.id] = result
            self._cv.notify_all()
        return result

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
        with global_gate().acquire(tag=f"job-{job.id}"):
            if self._shutdown_event.is_set():
                self._mark(job.id, status="cancelled", error="server shutdown")
                return
            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001 - surfaced per-job
                log.exception("job %s failed", job.id)
                self._mark(job.id, status="failed", error=str(exc)[:1000])
            finally:
                self._maybe_notify(job.id)

    def _execute(self, job: JobResult) -> None:
        if job.kind == "discovery":
            self._run_discovery(job)
        else:
            raise ValueError(f"unknown job kind: {job.kind}")

    def _run_discovery(self, job: JobResult) -> None:
        from csv_to_clonehero.spotify_pipeline import run_spotify_discovery

        self._mark(job.id, status="running", phase="starting", message="Starting discovery")
        db = Database(self.config.database_path)
        sources: list[ChartSource] = []
        try:
            user = db.get_user(job.user_id)
            if user is None:
                raise RuntimeError("user no longer exists")
            self._mark(
                job.id,
                status="running",
                phase="starting",
                message=f"Building Spotify session for {user['display_name'] or user['spotify_user_id']}",
            )
            oauth = build_oauth(self.config, job.user_id, self.cipher)
            client = build_spotify_client(self.config, oauth)
            sources = self.source_factory(self.config)

            def on_progress(payload: Mapping[str, Any]) -> None:
                data = dict(payload)
                message = str(data.get("message") or data.get("phase") or "").strip()
                self._mark(
                    job.id,
                    status="running",
                    phase=str(data.get("phase") or "running"),
                    message=message,
                    progress=data,
                )

            def on_log(message: Any) -> None:
                if message:
                    self._mark(job.id, message=str(message)[:1000])

            result = run_spotify_discovery(
                self.config,
                db,
                sources,
                client,
                workspace_key=job.workspace_key or None,
                workspace_name=user["spotify_user_id"],
                on_progress=on_progress,
                on_log=on_log,
            )
            stats = getattr(result, "stats", None)
            self._mark(
                job.id,
                status=str(result.status or "complete"),
                phase=str(result.phase or result.status or "complete"),
                error=str(result.error or "") or None,
                message=_completion_message(result),
                progress=_stats_payload(stats),
                result=_stats_payload(stats),
            )
        finally:
            for source in sources:
                try:
                    source.close()
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
    ) -> None:
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
            )
        finally:
            db.close()
        if row is not None:
            self._handles[job_id] = _from_row(row)

    def _maybe_notify(self, job_id: int) -> None:
        job = self.get(job_id)
        if job is None or not job.notify_email:
            return
        subject = f"CSV to Clone Hero: your job {job.kind} finished"
        body = _completion_email(job)
        self.sender.send(subject, body, job.notify_email)


def _default_source_factory(config: AppConfig) -> list[ChartSource]:
    from csv_to_clonehero.sources import build_sources

    return build_sources(config)


def _stats_payload(stats: Any) -> dict[str, Any]:
    if stats is None:
        return {}
    from dataclasses import fields, is_dataclass

    if is_dataclass(stats):
        return {field.name: getattr(stats, field.name) for field in fields(stats)}
    values = getattr(stats, "__dict__", {})
    return dict(values) if isinstance(values, dict) else {}


def _completion_message(result: Any) -> str:
    status = str(getattr(result, "status", "") or "")
    if status == "complete":
        return "Discovery complete; your matches are ready."
    if status == "partial":
        return "Discovery finished with some errors; matches are ready."
    return f"Discovery ended ({status})"


def _completion_email(job: JobResult) -> str:
    return (
        f"Your chart search job #{job.id} ({job.kind}) finished with status "
        f"{job.status!r}.\n\n"
        f"Sign back in to https://your-host to review and download your charts.\n\n"
        f"---\nCSV to Clone Hero\n"
    )