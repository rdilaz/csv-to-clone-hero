"""Browser-delivered chart downloads.

Charts are NEVER stored server-side long-term. A ZIP is assembled on demand
from workspace selections into a per-user staging folder, then handed to the
browser through a short-lived, signed, single-use URL. The staging folder is
cleaned up after streaming (and swept on an interval / at startup), so the
server only ever holds: user tokens (encrypted), the query cache, job state,
and a transient ZIP during delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import ExportOptions, ExportService
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.sources.base import ChartSource

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DEFAULT_TTL_SECONDS = 15 * 60


class DownloadError(Exception):
    """Safe, user-visible download failure."""


def _signing_key(config: AppConfig, session_key: str) -> bytes:
    return hashlib.sha256(
        f"{session_key}:browser-download:{config.public_base_url}".encode("utf-8")
    ).digest()


def _sign(payload: str, key: bytes) -> str:
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


class BrowserDownloads:
    """Registry + signed-URL delivery for on-demand ZIP archives."""

    def __init__(
        self,
        config: AppConfig,
        session_key: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        sweep_interval: float = 300.0,
    ) -> None:
        self.config = config
        self.key = _signing_key(config, session_key)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.root = config.resolve(getattr(getattr(config, "paths", None), "state_dir", "state")) / "downloads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._sweep()
        self._timer: threading.Timer | None = None
        if sweep_interval > 0:
            self._timer = threading.Timer(sweep_interval, self._sweep_timer)
            self._timer.daemon = True
            self._timer.start()

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _sweep_timer(self) -> None:
        try:
            self._sweep()
        finally:
            self._timer = None

    def _revoke_meta(self, meta: dict[str, Any] | None) -> None:
        if not meta:
            return
        path = meta.get("path")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        key = meta.get("signed", "").split(".", 1)[0]
        if key:
            self._unlink_key(key)

    def _sweep(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                (key, meta)
                for key, meta in self._index.items()
                if meta.get("expires_at", 0) < now
            ]
            for key, meta in expired:
                self._index.pop(key, None)
                self._revoke_meta(meta)
        cutoff = now - self.ttl_seconds - 60
        for entry in self.root.glob("*.zip"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass

    def _unlink_key(self, key: str) -> None:
        path = self.root / _safe_basename(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def create(self, user_id: int, workspace_id: int, archive_path: Path) -> dict[str, Any]:
        stamp = secrets.token_urlsafe(24)
        key = _safe_basename(stamp)
        expires_at = time.time() + self.ttl_seconds
        expiry_int = int(expires_at)
        payload = f"{key}:{expiry_int}"
        token = f"{key}.{expiry_int}.{_sign(payload, self.key)}"
        archive_path = Path(archive_path)
        with self._lock:
            self._index[key] = {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "path": str(archive_path),
                "expires_at": expires_at,
                "size": archive_path.stat().st_size,
                "signed": f"{key}.{expiry_int}",
            }
        return {
            "token": token,
            "expires_at": expires_at,
            "ttl_seconds": self.ttl_seconds,
            "size_bytes": archive_path.stat().st_size,
        }

    def resolve(self, token: str) -> dict[str, Any] | None:
        parts = token.split(".") if token else []
        if len(parts) != 3:
            return None
        key, raw_expiry, raw_sig = parts
        try:
            expires_at = int(raw_expiry)
        except (TypeError, ValueError):
            return None
        expected = _sign(f"{key}:{expires_at}", self.key)
        if not hmac.compare_digest(expected, raw_sig):
            return None
        if expires_at < time.time():
            return None
        with self._lock:
            return self._index.get(key)

    def stream(self, token: str):
        meta = self.resolve(token)
        if meta is None:
            raise DownloadError("Download link is invalid or has expired; run the download again.")
        path = Path(meta["path"])
        if not path.is_file():
            raise DownloadError("Download no longer exists; run the download again.")
        name = f"clone-hero-charts-{meta['workspace_id']}.zip"
        try:
            with open(path, "rb") as fh:
                while True:
                    data = fh.read(1024 * 1024)
                    if not data:
                        break
                    yield data
        finally:
            self.revoke(token)

    def revoke(self, token: str) -> None:
        key = token.split(".", 1)[0] if "." in token else token
        with self._lock:
            meta = self._index.pop(key, None)
        self._revoke_meta(meta)


def _safe_basename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "", value)
    return name or "download"


def build_archive(
    config: AppConfig,
    db: Database,
    workspace_id: int,
    sources: Iterable[ChartSource] | None,
    staging_dir: Path,
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Assemble the selected charts into one ZIP and return (archive, summary).

    ``sources`` may be omitted (the service builds them). The archive is
    written to ``staging_dir`` under state; the caller is responsible for
    streaming then deleting it (BrowserDownloads does this).
    """
    selections = _validated_selections(db, workspace_id)
    source_map = {str(getattr(s, "name", "") or ""): s for s in (sources or ())}
    if not source_map:
        from csv_to_clonehero.sources import build_sources

        built = build_sources(config)
        source_map = {str(getattr(s, "name", "") or ""): s for s in built}
    tracks = db.get_spotify_tracks(True)
    track_map: dict[Any, ChartCandidate] = {}
    for track in tracks:
        if getattr(track, "db_id", None) is not None:
            track_map[track.db_id] = track
        if getattr(track, "spotify_id", None):
            track_map[track.spotify_id] = track
        if getattr(track, "track_key", None):
            track_map[track.track_key] = track

    staging_dir.mkdir(parents=True, exist_ok=True)
    archive = staging_dir / f"charts-{workspace_id}-{secrets.token_hex(8)}.zip"
    options = ExportOptions(
        managed_sync=False,
        continue_on_error=True,
        include_manifest_in_zip=True,
        download_workers=config.export.download_workers if hasattr(config.export, "download_workers") else 3,
    )

    def checker(selection: Any, _candidate: ChartCandidate | None) -> bool:
        value = dict(selection) if hasattr(selection, "get") else {}
        eligibility = str(value.get("eligibility") or "").upper()
        from csv_to_clonehero.matching import ELIGIBLE_REVIEW, RECOMMENDED

        return eligibility in {RECOMMENDED, ELIGIBLE_REVIEW}

    service = ExportService(
        config,
        source_map,
        artifact_cache_dir=getattr(config, "artifact_cache_path", None),
        track_resolver=track_map,
    )
    try:
        result = service.export_zip(
            selections,
            archive,
            options=options,
            eligibility_checker=checker,
            progress=progress,
        )
        summary = {
            "success": bool(result.success),
            "published": result.published_count,
            "failed": result.failed_count,
            "bytes": result.output_bytes,
            "errors": [str(e) for e in result.errors or ()],
        }
        if not result.success or archive.stat().st_size == 0:
            archive.unlink(missing_ok=True)
            raise DownloadError(
                "Could not assemble a download: " + ("; ".join(result.errors or ()) or "no charts exported")
            )
        return archive, summary
    finally:
        service.close()


def _validated_selections(db: Database, workspace_id: int) -> list[dict[str, Any]]:
    try:
        rows = db.list_workspace_selections(workspace_id)
    except ValueError as exc:
        raise DownloadError("No charts selected yet; pick at least one chart to download.") from exc
    if not rows:
        raise DownloadError("No charts selected yet; pick at least one chart to download.")
    selections: list[dict[str, Any]] = []
    for row in rows:
        from dataclasses import fields

        data = dict(row) if hasattr(row, "keys") else {}
        kind = str(data.get("kind") or "exact").casefold()
        candidate = db.get_candidate(int(data.get("candidate_id") or 0))
        if candidate is None:
            continue
        selection = dict(data)
        selection["candidate_json"] = json.dumps(
            {item.name: getattr(candidate, item.name) for item in fields(candidate)},
            ensure_ascii=True,
            default=str,
        )
        selection["candidate_id"] = data.get("candidate_id")
        selection["status"] = "selected"
        selection["eligibility"] = data.get("eligibility") or "RECOMMENDED"
        selections.append(selection)
    if not selections:
        raise DownloadError("No downloadable charts in your selections.")
    return selections