"""Shared preflight and start validation for public discovery jobs."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.catalog import load_enchor_catalog
from csv_to_clonehero.database import Database, _load_json
from csv_to_clonehero.server.auth import user_workspace_key
from csv_to_clonehero.server.throttle import global_gate
from csv_to_clonehero.sources.base import ChartSource, source_is_exportable
from csv_to_clonehero.sources.enchor import EnchorCatalogPreflight, PREFLIGHT_VERSION


@dataclass(frozen=True)
class DiscoveryValidation:
    ok: bool
    errors: tuple[str, ...]
    preflight: dict[str, Any]
    active_job_id: int | None = None


def preflight_public(row: Any, *, now: float | None = None) -> dict[str, Any]:
    if row is None:
        return {
            "available": False,
            "fresh": False,
            "usable": False,
            "status": "check_required",
            "busy": False,
            "retryable": True,
            "retry_after_seconds": 0,
            "message": "Check Enchor before starting discovery.",
        }
    current = time.time() if now is None else float(now)
    data = dict(row)
    metadata = _load_json(data.get("metadata_json"), {}) or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    verified = bool(data.get("success") and data.get("supported") and data.get("authoritative"))
    expires_at = float(data.get("expires_at") or 0)
    if not verified:
        # Old deployments cached HTTP-successful but unusable probes for the
        # success TTL too. Interpret those observations with the short failure
        # policy without rewriting their original operator evidence.
        expires_at = min(expires_at, float(data.get("checked_at") or 0) + 60)
    fresh = bool(
        expires_at > current
        and data.get("request_version") == PREFLIGHT_VERSION
    )
    usable = bool(fresh and verified)
    if usable:
        message = f"Enchor reported {int(data.get('out_of') or 0):,} charts."
    elif not fresh:
        message = "The Enchor catalog check is missing or stale."
    else:
        # Raw adapter errors stay in the persisted operator evidence. Public
        # readiness must not expose internal paths or request diagnostics.
        message = (
            "The community chart service is limiting requests. Your music choices are kept; try again shortly."
            if metadata.get("rate_limited")
            else "The community chart service could not be verified. Your music choices are kept; try again shortly."
        )
    return {
        "available": True,
        "fresh": fresh,
        "usable": usable,
        "status": "ready" if usable else "stale" if not fresh else "unavailable",
        "busy": False,
        "retryable": not usable,
        "retry_after_seconds": max(1, math.ceil(expires_at - current)) if fresh and not usable else 0,
        "source": data.get("source_name"),
        "endpoint": data.get("endpoint"),
        "request_version": data.get("request_version"),
        "checked_at": data.get("checked_at"),
        "expires_at": expires_at,
        "success": bool(data.get("success")),
        "supported": bool(data.get("supported")),
        "authoritative": bool(data.get("authoritative")),
        "found": data.get("found"),
        "out_of": data.get("out_of"),
        "returned_count": int(data.get("returned_count") or 0),
        "page": data.get("page"),
        "error": "" if usable else message,
        "metadata": {
            key: metadata[key]
            for key in ("http_status", "rate_limited")
            if key in metadata
        },
        "message": message,
    }


def get_enchor_preflight(db: Database, *, now: float | None = None) -> dict[str, Any]:
    return preflight_public(db.get_source_preflight("enchor"), now=now)


def get_discovery_availability(config: AppConfig, db: Database) -> dict[str, Any]:
    """Prefer a verified persistent catalog; report live fallback otherwise."""
    catalog = load_enchor_catalog(
        config.catalog_path,
        excluded_identities=db.get_blocked_chart_identities(),
        validation_fingerprint=db.chart_validation_fingerprint(),
    )
    if catalog.available:
        return {
            "available": True,
            "fresh": True,
            "usable": True,
            "status": "ready",
            "busy": False,
            "retryable": False,
            "retry_after_seconds": 0,
            "mode": "catalog",
            "catalog_available": True,
            "catalog_diagnostic": "",
            "source": "enchor",
            "authoritative": True,
            "success": True,
            "supported": True,
            "out_of": catalog.known_chart_count,
            "eligible_charts": catalog.chart_count,
            "eligible_songs": getattr(catalog, "song_count", None),
            "snapshot_id": catalog.snapshot_id,
            "catalog_sha256": catalog.sha256,
            "message": f"Local catalog ready with {catalog.chart_count:,} eligible charts.",
        }
    live = get_enchor_preflight(db)
    live["mode"] = "live_fallback"
    live["catalog_available"] = False
    live["catalog_diagnostic"] = "The local chart catalog is unavailable."
    return live


def refresh_enchor_preflight(
    config: AppConfig,
    db: Database,
    source_factory: Callable[[AppConfig], list[ChartSource]],
) -> dict[str, Any]:
    availability = get_discovery_availability(config, db)
    if availability.get("mode") == "catalog" or availability.get("fresh"):
        return availability

    sources: list[ChartSource] = []
    result: EnchorCatalogPreflight | None = None
    try:
        # A request must not wait behind an entire discovery/export job. The
        # worker queue owns that wait; a public probe reports transient busy
        # status without recording a failed provider observation.
        with global_gate().acquire(timeout=0, tag="enchor-preflight") as acquired:
            if not acquired:
                return {
                    **availability,
                    "status": "busy",
                    "busy": True,
                    "retryable": True,
                    "retry_after_seconds": 1,
                    "message": "The chart service is busy. Your music choices are kept; try finding charts again shortly.",
                }
            # Another request may have completed between the first read and
            # acquisition. Keep the same catalog/fallback response contract.
            cached = get_discovery_availability(config, db)
            if cached.get("mode") == "catalog" or cached.get("fresh"):
                return cached
            sources = source_factory(config)
            source = next((item for item in sources if getattr(item, "name", "") == "enchor"), None)
            if source is None:
                result = EnchorCatalogPreflight(
                    checked_at=time.time(),
                    error="Enchor is not enabled in the configured chart sources.",
                )
            elif not source_is_exportable(source):
                result = EnchorCatalogPreflight(
                    checked_at=time.time(),
                    error="The configured Enchor source is not exportable.",
                )
            else:
                method = getattr(source, "preflight_catalog", None)
                if not callable(method):
                    result = EnchorCatalogPreflight(
                        checked_at=time.time(),
                        error="The configured Enchor adapter does not support catalog preflight.",
                    )
                else:
                    result = method()
                # Record while holding the request budget, so another public
                # probe reuses this observation instead of issuing a duplicate.
            db.record_source_preflight(
                asdict(result),
                ttl_seconds=(
                    config.server.enchor_preflight_ttl_seconds
                    if result.success and result.supported and result.authoritative
                    else min(60, config.server.enchor_preflight_ttl_seconds)
                ),
            )
            return get_discovery_availability(config, db)
    except Exception as exc:  # noqa: BLE001 - persisted as actionable status
        result = EnchorCatalogPreflight(checked_at=time.time(), error=str(exc)[:1000])
    finally:
        for source in sources:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass

    assert result is not None
    db.record_source_preflight(
        asdict(result),
        ttl_seconds=(
            config.server.enchor_preflight_ttl_seconds
            if result.success and result.supported and result.authoritative
            else min(60, config.server.enchor_preflight_ttl_seconds)
        ),
    )
    return get_discovery_availability(config, db)


def validate_discovery_start(
    db: Database,
    config: AppConfig,
    *,
    user_id: int,
    workspace_key: str,
    current_job_id: int | None = None,
    availability: dict[str, Any] | None = None,
) -> DiscoveryValidation:
    errors: list[str] = []
    user = db.get_user(int(user_id))
    workspace_row = db.get_workspace(workspace_key) if workspace_key else None
    neutral_tracks = db.get_neutral_library_tracks(int(workspace_row["id"])) if workspace_row else []
    has_neutral_library = bool(neutral_tracks)
    expected_key = ""
    if user is None:
        errors.append("The signed-in user no longer exists.")
    else:
        expected_key = user_workspace_key(user["spotify_user_id"])
        if workspace_key != expected_key:
            errors.append("The discovery workspace is not owned by this user.")
        token = db.get_user_token(int(user_id))
        if not has_neutral_library and (token is None or not str(token["encrypted_refresh_token"] or "").strip()):
            errors.append("Reconnect Spotify before starting discovery.")

    enabled = [str(name).strip() for name in config.sources.enabled]
    if "enchor" not in enabled:
        errors.append("Enchor must be enabled for public discovery.")
    unknown = sorted(set(enabled) - {"enchor"})
    if unknown:
        errors.append(f"Unsupported chart source configuration: {', '.join(unknown)}.")

    workspace = workspace_row
    scope = db.get_workspace_scope_data(int(workspace["id"])) if workspace is not None else None
    if not has_neutral_library and (scope is None or not (scope.get("playlist_ids") or scope.get("include_liked"))):
        errors.append("Choose at least one Spotify playlist or Liked Songs before starting.")
    elif scope is not None and len(scope.get("playlist_ids") or []) > config.server.max_scope_playlists:
        errors.append("The selected playlist scope is too large.")
    elif scope is not None and len(scope.get("track_ids") or []) > config.server.max_scope_tracks:
        errors.append("The selected track scope is too large.")
    # Public request threads pass the worker's current observation. Worker
    # execution independently performs the full current catalog verification.
    preflight = dict(availability) if availability is not None else get_discovery_availability(config, db)
    if not preflight.get("usable"):
        errors.append(str(preflight.get("message") or "Check Enchor before starting discovery."))

    active = db.get_active_job(
        int(user_id),
        workspace_key=workspace_key,
        exclude_job_id=current_job_id,
    )
    if active is not None:
        errors.append("A discovery job is already active for this workspace.")
    return DiscoveryValidation(
        ok=not errors,
        errors=tuple(errors),
        preflight=preflight,
        active_job_id=int(active["id"]) if active is not None else None,
    )
