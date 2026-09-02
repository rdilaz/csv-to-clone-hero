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
from dataclasses import dataclass, field
from typing import Any

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.sources.base import ChartSource
from csv_to_clonehero.server.auth import build_oauth, build_soundcloud_oauth, build_spotify_client
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
        self._cancel_events: dict[int, threading.Event] = {}
        self._recover_orphaned_jobs()
        if auto_start:
            self.start()

    # ---- lifecycle ---------------------------------------------------------
    def _recover_orphaned_jobs(self) -> None:
        db = Database(self.config.database_path)
        try:
            recovered = db.recover_running_jobs()
            recovered_discovery = db.recover_interrupted_discovery()
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
    def enqueue(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
    ) -> JobResult:
        result, _created = self.enqueue_unique(
            user_id,
            kind,
            workspace_key=workspace_key,
            notify_email=notify_email,
        )
        return result

    def enqueue_unique(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
    ) -> tuple[JobResult, bool]:
        db = Database(self.config.database_path)
        try:
            row, created = db.create_or_get_active_job(
                user_id, kind, workspace_key=workspace_key, notify_email=notify_email
            )
            result = _from_row(row)
        finally:
            db.close()
        with self._cv:
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
        self._cancel_events.setdefault(job.id, threading.Event())
        try:
            if self._cancelled(job.id):
                self._mark(job.id, status="cancelled", phase="cancelled", message="Job cancelled")
                return
            if job.kind == "discovery":
                from csv_to_clonehero.server.safeguards import (
                    get_enchor_preflight,
                    refresh_enchor_preflight,
                )

                db = Database(self.config.database_path)
                try:
                    if not get_enchor_preflight(db).get("fresh"):
                        refresh_enchor_preflight(self.config, db, self.source_factory)
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
        else:
            raise ValueError(f"unknown job kind: {job.kind}")

    def _run_discovery(self, job: JobResult) -> None:
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

            if combined_provider_discovery and _scope_selected(spotify_scope):
                from csv_to_clonehero.providers.spotify import SpotifyLibraryProvider

                self._mark(job.id, status="running", phase="provider_import", message="Loading current Spotify selection")
                oauth = build_oauth(self.config, job.user_id, self.cipher)
                client = build_spotify_client(self.config, oauth)
                spotify_snapshot = SpotifyLibraryProvider(client).import_library(scope=spotify_scope)
                if workspace_id is None:
                    raise RuntimeError("discovery workspace no longer exists")
                db.upsert_library_snapshot(workspace_id, spotify_snapshot, scope=spotify_scope)

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
                db.upsert_library_snapshot(workspace_id, soundcloud_snapshot, scope=soundcloud_scope)

            prefetched_library = None
            if combined_provider_discovery and workspace_id is not None:
                current_provider_tracks = _provider_tracks_from_provenance(db, workspace_id)
                if current_provider_tracks:
                    prefetched_library = _provider_library_result(current_provider_tracks)

            self._mark(
                job.id,
                status="running",
                phase="starting",
                message=f"Preparing discovery for {user['display_name'] or user['spotify_user_id']}",
            )
            if prefetched_library is None:
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
                    heartbeat_at=time.time(),
                )

            def on_log(message: Any) -> None:
                if message:
                    self._mark(job.id, message=str(message)[:1000])

            result = run_spotify_discovery(
                self.config,
                db,
                sources,
                client,
                prefetched_library=prefetched_library,
                workspace_key=job.workspace_key or None,
                workspace_name=user["spotify_user_id"],
                on_progress=on_progress,
                on_log=on_log,
                cancellation_event=self._cancel_events[job.id],
            )
            stats = getattr(result, "stats", None)
            cancelled = self._cancelled(job.id)
            self._mark(
                job.id,
                status="cancelled" if cancelled else str(result.status or "complete"),
                phase="cancelled" if cancelled else str(result.phase or result.status or "complete"),
                error=str(result.error or "") or None,
                message="Job cancelled" if cancelled else _completion_message(result),
                progress=_stats_payload(stats),
                result=_stats_payload(stats),
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
    from csv_to_clonehero.providers.base import ProviderTrack

    tracks = []
    for row in db.get_track_provenance(workspace_id):
        tracks.append(ProviderTrack(
            provider=str(row["provider"]),
            provider_id=str(row["provider_id"]),
            title=str(row["title"]),
            artist=str(row["artist"]),
            album=str(row["album"] or ""),
            liked=bool(row["liked"]),
            isrc=str(row["isrc"] or ""),
            url=str(row["url"] or ""),
            metadata=_json_object(row["metadata_json"]),
        ))
    return tracks


def _provider_library_result(provider_tracks: list[Any]) -> Any:
    """Adapt approved provider records to the shared discovery import shape."""
    from csv_to_clonehero.providers.base import merge_provider_tracks
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
            "liked": bool(track.liked),
            "album": track.album,
            "isrc": track.isrc,
            "url": track.url,
            "metadata": dict(track.metadata),
        })

    records: list[dict[str, Any]] = []
    playlist_models: dict[str, str] = {}
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
            ids = list(item["playlist_ids"])
            names = list(item["playlist_names"])
            for index in range(max(len(ids), len(names))):
                raw_id = ids[index] if index < len(ids) else names[index]
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
                    "name": names[index] if index < len(names) else raw_id,
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
