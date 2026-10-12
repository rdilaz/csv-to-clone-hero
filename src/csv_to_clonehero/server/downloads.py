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
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import ExportOptions, ExportService, SNG_VALIDATION_VERSION
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.sources.base import ChartSource

log = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 15 * 60
_MAX_RETAINED_PER_WORKSPACE = 3


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
        # Public staging follows the database volume, not the local app's
        # configurable artifact state directory. In Docker this is /data.
        self.root = config.state_path / "downloads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._active_builds: set[int] = set()
        self._sweep_interval = max(0.0, float(sweep_interval))
        self._closed = False
        self._sweep(include_crash_state=True)
        self._timer: threading.Timer | None = None
        self._schedule_sweep()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_sweep(self) -> None:
        with self._lock:
            if self._closed or self._sweep_interval <= 0:
                return
            timer = threading.Timer(self._sweep_interval, self._sweep_timer)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _sweep_timer(self) -> None:
        try:
            self._sweep()
        finally:
            with self._lock:
                self._timer = None
            self._schedule_sweep()

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

    def _sweep(self, *, include_crash_state: bool = False) -> None:
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
        if include_crash_state:
            # This is called during registry construction, before this process
            # can own an assembly. Periodic sweeps must not race a long build.
            for entry in self.root.glob(".assembly-*"):
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)

    def _unlink_key(self, key: str) -> None:
        path = self.root / _safe_basename(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def create(self, user_id: int, workspace_id: int, archive_path: Path) -> dict[str, Any]:
        stamp = secrets.token_urlsafe(24)
        key = _safe_basename(stamp)
        created_at = time.time()
        expires_at = created_at + self.ttl_seconds
        expiry_int = int(expires_at)
        payload = f"{key}:{expiry_int}"
        token = f"{key}.{expiry_int}.{_sign(payload, self.key)}"
        # Receipt identity is independent of the bearer URL and wall clock.
        # It associates a job with its archive and never authorizes delivery.
        archive_id = secrets.token_hex(16)
        archive_path = Path(archive_path)
        with self._lock:
            self._index[key] = {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "path": str(archive_path),
                "expires_at": expires_at,
                "size": archive_path.stat().st_size,
                "signed": f"{key}.{expiry_int}",
                "token": token,
                "archive_id": archive_id,
                "created_at": created_at,
            }
            # Registry insertion order survives equal or backward clock ticks.
            retained = [
                (old_key, meta)
                for old_key, meta in reversed(self._index.items())
                if int(meta.get("user_id", -1)) == int(user_id)
                and int(meta.get("workspace_id", -1)) == int(workspace_id)
            ]
            for old_key, meta in retained[_MAX_RETAINED_PER_WORKSPACE:]:
                self._index.pop(old_key, None)
                self._revoke_meta(meta)
        return {
            "token": token,
            "archive_id": archive_id,
            "created_at": created_at,
            "expires_at": expires_at,
            "ttl_seconds": self.ttl_seconds,
            "size_bytes": archive_path.stat().st_size,
        }

    @contextmanager
    def building(self, workspace_id: int):
        """Allow at most one archive assembly per workspace."""
        workspace_id = int(workspace_id)
        with self._lock:
            if workspace_id in self._active_builds:
                raise DownloadError("A library download is already being prepared.")
            self._active_builds.add(workspace_id)
        try:
            yield
        finally:
            with self._lock:
                self._active_builds.discard(workspace_id)

    def resolve(self, token: str) -> dict[str, Any] | None:
        parsed = self._parse_token(token)
        if parsed is None:
            return None
        key, expires_at = parsed
        with self._lock:
            meta = self._index.get(key)
            return dict(meta) if meta is not None and expires_at >= time.time() else None

    def _parse_token(self, token: str) -> tuple[str, int] | None:
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
        return key, expires_at

    def consume(self, token: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        """Atomically claim a valid token without deleting its ZIP yet."""
        parsed = self._parse_token(token)
        if parsed is None:
            return None
        key, expires_at = parsed
        with self._lock:
            meta = self._index.get(key)
            if meta is None or expires_at < time.time():
                return None
            if user_id is not None and int(meta.get("user_id", -1)) != int(user_id):
                return None
            return self._index.pop(key)

    def stream(self, token: str, *, user_id: int | None = None):
        meta = self.consume(token, user_id=user_id)
        if meta is None:
            raise DownloadError("Download link is invalid or has expired; run the download again.")
        path = Path(meta["path"])
        if not path.is_file():
            self._revoke_meta(meta)
            raise DownloadError("Download no longer exists; run the download again.")

        def content():
            try:
                with open(path, "rb") as fh:
                    while True:
                        data = fh.read(1024 * 1024)
                        if not data:
                            break
                        yield data
            finally:
                self._revoke_meta(meta)

        return content()

    def latest_for_user_workspace(
        self, user_id: int, workspace_id: int
    ) -> dict[str, Any] | None:
        """Return safe metadata for the newest unconsumed owned download."""
        self._sweep()
        with self._lock:
            matches = [
                meta for meta in self._index.values()
                if int(meta.get("user_id", -1)) == int(user_id)
                and int(meta.get("workspace_id", -1)) == int(workspace_id)
            ]
            if not matches:
                return None
            meta = matches[-1]
            return {
                "token": meta["token"],
                "archive_id": meta["archive_id"],
                "expires_at": meta["expires_at"],
                "ttl_seconds": self.ttl_seconds,
                "size_bytes": meta["size"],
                "created_at": meta["created_at"],
            }

    def revoke(self, token: str) -> None:
        key = token.split(".", 1)[0] if "." in token else token
        with self._lock:
            meta = self._index.pop(key, None)
        self._revoke_meta(meta)

    def revoke_workspace(self, workspace_id: int) -> int:
        """Revoke all transient archives belonging to one workspace."""
        with self._lock:
            keys = [
                key for key, meta in self._index.items()
                if int(meta.get("workspace_id", -1)) == int(workspace_id)
            ]
            metas = [self._index.pop(key) for key in keys]
        for meta in metas:
            self._revoke_meta(meta)
        return len(metas)


def _safe_basename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "", value)
    return name or "download"


def selection_snapshot(db: Database, workspace_id: int) -> dict[str, Any]:
    """Bind a queued archive to one exact generation and set of choices.

    Persist only the digest and counts in the job; artist/title data and the
    full selection list stay in the workspace tables.
    """
    current = db.get_current_exact_generation(workspace_id)
    latest = db.get_latest_exact_generation(workspace_id)
    generation_id = int(current["id"]) if current is not None else -1 if latest is not None else 0
    choices = []
    rows = db.list_workspace_selections(workspace_id)
    if rows and all(row["kind"] == "catalog" for row in rows):
        generation_id = 0
    for row in rows:
        item = dict(row)
        choices.append(tuple(str(item.get(key) or "") for key in (
            "kind", "track_id", "candidate_id", "artist_norm", "metadata_json"
        )))
    choices.sort()
    encoded = json.dumps(choices, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "generation_id": generation_id,
        "selection_count": len(choices),
        "selection_fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def build_archive(
    config: AppConfig,
    db: Database,
    workspace_id: int,
    sources: Iterable[ChartSource] | None,
    staging_dir: Path,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancellation_event: threading.Event | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Assemble the selected charts into one ZIP and return (archive, summary).

    ``sources`` may be omitted (the service builds them). The archive is
    written to ``staging_dir`` under state; the caller is responsible for
    streaming then deleting it (BrowserDownloads does this).
    """
    selections = _validated_selections(db, workspace_id, config=config)
    source_map = {str(getattr(s, "name", "") or ""): s for s in (sources or ())}
    owned_sources: list[ChartSource] = []
    if not source_map:
        from csv_to_clonehero.sources import build_sources

        owned_sources = build_sources(config, include_video_background=False)
        source_map = {str(getattr(s, "name", "") or ""): s for s in owned_sources}
    tracks = db.get_spotify_tracks(True)
    track_map: dict[Any, Any] = {}
    for track in tracks:
        if getattr(track, "db_id", None) is not None:
            track_map[track.db_id] = track
        if getattr(track, "spotify_id", None):
            track_map[track.spotify_id] = track
        if getattr(track, "track_key", None):
            track_map[track.track_key] = track

    staging_dir.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_hex(8)
    assembly_dir = staging_dir / f".assembly-{workspace_id}-{nonce}"
    artifact_cache = assembly_dir / "artifacts"
    assembly_dir.mkdir(parents=True, exist_ok=False)
    temporary_archive = assembly_dir / "charts.zip"
    archive = staging_dir / f"charts-{workspace_id}-{nonce}.zip"
    options = ExportOptions(
        managed_sync=False,
        continue_on_error=True,
        include_manifest_in_zip=True,
        minimum_free_bytes=max(
            0, int(getattr(config.export, "minimum_free_bytes", 0) or 0)
        ),
        download_workers=min(
            16,
            max(1, int(getattr(config.export, "download_workers", 3) or 3)),
        ),
        artifact_retries=int(getattr(config.export, "artifact_retries", 2) or 0),
        retry_backoff_seconds=float(
            getattr(config.export, "retry_backoff_seconds", 2.0) or 0
        ),
    )

    def checker(selection: Any, _candidate: ChartCandidate | None) -> bool:
        value = dict(selection) if hasattr(selection, "get") else {}
        tier = str(value.get("result_tier") or "").upper()
        metadata = json.loads(str(value.get("metadata_json") or "{}"))
        from csv_to_clonehero.matching import POSSIBLE_MATCH, SELECTABLE_RESULT_TIERS

        return tier in (SELECTABLE_RESULT_TIERS | {"CATALOG_CHOICE"}) and (
            tier != POSSIBLE_MATCH
            or bool(metadata.get("possible_confirmed"))
            or not metadata.get("selection_origin")
        )

    service: ExportService | None = None
    try:
        service = ExportService(
            config,
            source_map,
            artifact_cache_dir=artifact_cache,
            track_resolver=track_map,
        )
        result = service.export_zip(
            selections,
            temporary_archive,
            options=options,
            eligibility_checker=checker,
            progress=progress,
            cancellation_event=cancellation_event,
        )
        artifact_refs = {
            (str(item.get("source") or "").casefold(), int(item.get("chart_id") or 0)):
                str((json.loads(item.get("candidate_json") or "{}") or {}).get("md5") or "")
            for item in selections
        }

        for item in result.items:
            try:
                identity = (str(item.source).casefold(), int(item.chart_id))
            except (TypeError, ValueError):
                continue
            artifact_ref = artifact_refs.get(identity, "")
            if item.status == "published":
                db.record_chart_artifact_validation(
                    *identity,
                    artifact_ref=artifact_ref,
                    state="VERIFIED",
                    validator_version=item.validation_version,
                    artifact_sha256=item.sha256,
                    size_bytes=item.bytes_count,
                    playable_note_count=item.playable_note_count,
                    validation={
                        "valid": True,
                        "validation_version": item.validation_version,
                        "playable_note_count": item.playable_note_count,
                        "note_counts": dict(item.note_counts),
                    },
                )
            elif item.status == "invalid":
                db.record_chart_artifact_validation(
                    *identity,
                    artifact_ref=artifact_ref,
                    state="REJECTED",
                    validator_version=SNG_VALIDATION_VERSION,
                    reason_code="ARTIFACT_VALIDATION_FAILED",
                    reason=item.failure,
                )
            elif item.status == "failed":
                existing = db.get_chart_artifact_validation(*identity)
                if existing is None or str(existing["state"]) != "VERIFIED":
                    lowered = str(item.failure or "").casefold()
                    unavailable = "http 404" in lowered or "http 410" in lowered
                    db.record_chart_artifact_validation(
                        *identity,
                        artifact_ref=artifact_ref,
                        state="UNAVAILABLE" if unavailable else "UNVERIFIED",
                        reason_code="SOURCE_UNAVAILABLE" if unavailable else "TRANSIENT_DOWNLOAD_FAILURE",
                        reason=item.failure,
                    )
        summary = {
            "success": bool(result.success),
            "published": result.published_count,
            "failed": result.failed_count,
            "invalid": result.invalid_count,
            "download_failures": result.download_failure_count,
            "already_present": sum(
                item.result == "already_present" for item in result.items
            ),
            "ready": result.ready_count,
            "selected": len(result.items),
            "remaining": result.remaining_count,
            "status": "cancelled" if result.cancelled else "success" if result.success else "failed",
            "bytes": result.output_bytes,
            "errors": [str(e) for e in result.errors or ()],
        }
        if not result.success or temporary_archive.stat().st_size == 0:
            raise DownloadError(
                "Could not assemble a download: " + ("; ".join(result.errors or ()) or "no charts exported")
            )
        os.replace(temporary_archive, archive)
        return archive, summary
    finally:
        if service is not None:
            service.close()
        shutil.rmtree(assembly_dir, ignore_errors=True)
        for source in owned_sources:
            try:
                source.close()
            except Exception:
                pass


def _validated_selections(
    db: Database,
    workspace_id: int,
    *,
    config: AppConfig | None = None,
) -> list[dict[str, Any]]:
    try:
        rows = db.list_workspace_selections(workspace_id)
    except ValueError as exc:
        raise DownloadError("No charts selected yet; pick at least one chart to download.") from exc
    if not rows:
        raise DownloadError("No charts selected yet; pick at least one chart to download.")
    track_map = {
        int(track.db_id): track
        for track in db.get_spotify_tracks(True)
        if track.db_id is not None
    }
    workspace_track_ids = db.get_workspace_library_track_ids(workspace_id)
    minimum_score = db.get_workspace_minimum_score(workspace_id) if config is not None else 0.0
    latest_generation = db.get_latest_exact_generation(workspace_id)
    current_generation = db.get_current_exact_generation(workspace_id)
    generation_id = (
        int(current_generation["id"])
        if current_generation is not None
        else -1 if latest_generation is not None else None
    )
    imported_identities = {
        (str(row["normalized_artist"]), str(row["normalized_title"]))
        for row in db.get_neutral_library_tracks(workspace_id)
    }
    imported_identities.update({
        (track.primary_artist_norm, track.title_norm)
        for track in track_map.values()
        if track.db_id in workspace_track_ids
    })
    selections: list[dict[str, Any]] = []
    seen_expansion_songs: set[tuple[str, str]] = set()
    # Membership is workspace-wide. Loading it for every selection turns a
    # large expansion setlist into repeated scans of the same candidate rows.
    exact_memberships: set[tuple[int, int]] | None = None
    expansion_memberships: dict[tuple[int, str], Any] | None = None
    for row in reversed(rows):
        from dataclasses import fields

        data = dict(row) if hasattr(row, "keys") else {}
        kind = str(data.get("kind") or "exact").casefold()
        if kind not in {"exact", "expansion", "catalog"}:
            continue
        if kind == "expansion" and (config is None or not config.server.enable_expansion):
            continue
        candidate = db.get_candidate(int(data.get("candidate_id") or 0))
        if candidate is None:
            continue
        if not db.is_chart_artifact_eligible(candidate.source, candidate.chart_id):
            continue
        current_eligibility = str(data.get("eligibility") or "RECOMMENDED")
        current_score = data.get("score")
        selection_metadata = json.loads(str(data.get("metadata_json") or "{}"))
        current_tier = ""
        if kind == "catalog":
            from csv_to_clonehero.server.catalog_browse import current_catalog_choice
            if config is None or not current_catalog_choice(config, db, workspace_id, data, candidate):
                continue
            current_eligibility, current_tier, current_score = "ELIGIBLE_REVIEW", "CATALOG_CHOICE", 0
        elif kind == "exact":
            track_id = int(data.get("track_id") or 0)
            if track_id not in workspace_track_ids:
                continue
            if exact_memberships is None:
                exact_memberships = {
                    (int(current["track_id"] or 0), int(current["candidate_id"] or 0))
                    for current in db.get_workspace_candidates(
                        workspace_id,
                        kind="exact",
                        include_rejected=True,
                        include_candidate_data=False,
                        generation_id=generation_id,
                    )
                }
            if (track_id, int(data.get("candidate_id") or 0)) not in exact_memberships:
                continue
            if config is not None:
                from csv_to_clonehero.matching import BLOCKED, SELECTABLE_RESULT_TIERS, classify_result_tier, score_candidate

                track = track_map.get(track_id)
                if track is None:
                    continue
                breakdown = score_candidate(track, candidate, config)
                current_eligibility = str(getattr(breakdown, "eligibility", "") or "")
                current_tier = classify_result_tier(breakdown)[0]
                if current_tier == BLOCKED or current_tier not in SELECTABLE_RESULT_TIERS:
                    continue
                if current_tier == "POSSIBLE_MATCH" and not selection_metadata.get("possible_confirmed"):
                    continue
                current_score = breakdown.final_score
        else:
            if generation_id is None or generation_id < 0:
                continue
            artist_norm = str(data.get("artist_norm") or "")
            if expansion_memberships is None:
                expansion_memberships = {}
                for item in db.get_workspace_candidates(
                    workspace_id,
                    kind="expansion",
                    include_rejected=True,
                    include_candidate_data=False,
                ):
                    # Preserve the first row chosen by the previous ordered
                    # lookup if multiple candidate rows share an identity.
                    expansion_memberships.setdefault(
                        (int(item["candidate_id"] or 0), str(item["artist_norm"] or "")),
                        item,
                    )
            current = expansion_memberships.get((int(data.get("candidate_id") or 0), artist_norm))
            if current is None:
                continue
            metadata = json.loads(str(current["metadata_json"] or "{}"))
            if int(metadata.get("exact_generation_id") or 0) != generation_id:
                continue
            from csv_to_clonehero.matching import BLOCKED, SELECTABLE_RESULT_TIERS, classify_result_tier, score_expansion_candidate
            from csv_to_clonehero.normalization import normalize_artist, normalize_title

            if (
                normalize_artist(str(candidate.artist or "")),
                normalize_title(str(candidate.name or "")),
            ) in imported_identities:
                continue
            expansion_identity = (
                normalize_artist(str(candidate.artist or "")),
                normalize_title(str(candidate.name or "")),
            )
            breakdown = score_expansion_candidate(
                artist_norm, candidate, config,
                include_compatible=metadata.get("coverage_mode") == "all",
            )
            current_eligibility = str(getattr(breakdown, "eligibility", "") or "")
            current_tier = classify_result_tier(breakdown, kind="expansion")[0]
            if (
                bool(getattr(breakdown, "gate_factors", {}).get("hard_gate"))
                or current_eligibility not in {"RECOMMENDED", "ELIGIBLE_REVIEW"}
                or current_tier == BLOCKED
                or current_tier not in SELECTABLE_RESULT_TIERS
            ):
                continue
            current_score = breakdown.final_score
        if config is not None and float(current_score or 0) < minimum_score and not current_tier:
            continue
        selection = dict(data)
        selection["candidate_json"] = json.dumps(
            {item.name: getattr(candidate, item.name) for item in fields(candidate)},
            ensure_ascii=True,
            default=str,
        )
        selection["candidate_id"] = data.get("candidate_id")
        selection["status"] = "selected"
        selection["eligibility"] = current_eligibility
        selection["result_tier"] = current_tier
        selection["score"] = current_score
        selections.append(selection)
    if not selections:
        raise DownloadError("No downloadable charts in your selections.")
    return list(reversed(selections))
