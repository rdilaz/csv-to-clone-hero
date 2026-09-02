"""Shared preflight and start validation for public discovery jobs."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from csv_to_clonehero.config import AppConfig
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
            "message": "Check Enchor before starting discovery.",
        }
    current = time.time() if now is None else float(now)
    data = dict(row)
    fresh = bool(
        float(data.get("expires_at") or 0) > current
        and data.get("request_version") == PREFLIGHT_VERSION
    )
    usable = bool(
        fresh
        and data.get("success")
        and data.get("supported")
        and data.get("authoritative")
    )
    if usable:
        message = f"Enchor reported {int(data.get('out_of') or 0):,} charts."
    elif not fresh:
        message = "The Enchor catalog check is missing or stale."
    else:
        message = str(data.get("error") or "Enchor could not provide an authoritative broad catalog total.")
    return {
        "available": True,
        "fresh": fresh,
        "usable": usable,
        "source": data.get("source_name"),
        "endpoint": data.get("endpoint"),
        "request_version": data.get("request_version"),
        "checked_at": data.get("checked_at"),
        "expires_at": data.get("expires_at"),
        "success": bool(data.get("success")),
        "supported": bool(data.get("supported")),
        "authoritative": bool(data.get("authoritative")),
        "found": data.get("found"),
        "out_of": data.get("out_of"),
        "returned_count": int(data.get("returned_count") or 0),
        "page": data.get("page"),
        "error": data.get("error") or "",
        "metadata": _load_json(data.get("metadata_json"), {}) or {},
        "message": message,
    }


def get_enchor_preflight(db: Database, *, now: float | None = None) -> dict[str, Any]:
    return preflight_public(db.get_source_preflight("enchor"), now=now)


def refresh_enchor_preflight(
    config: AppConfig,
    db: Database,
    source_factory: Callable[[AppConfig], list[ChartSource]],
) -> dict[str, Any]:
    cached = get_enchor_preflight(db)
    if cached.get("fresh"):
        return cached

    sources: list[ChartSource] = []
    result: EnchorCatalogPreflight | None = None
    try:
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
                with global_gate().acquire(tag="enchor-preflight"):
                    # Another request may have completed while this one waited.
                    cached = get_enchor_preflight(db)
                    if cached.get("fresh"):
                        return cached
                    result = method()
                    row = db.record_source_preflight(
                        asdict(result),
                        ttl_seconds=(
                            config.server.enchor_preflight_ttl_seconds
                            if result.success
                            else min(60, config.server.enchor_preflight_ttl_seconds)
                        ),
                    )
                    return preflight_public(row)
    except Exception as exc:  # noqa: BLE001 - persisted as actionable status
        result = EnchorCatalogPreflight(checked_at=time.time(), error=str(exc)[:1000])
    finally:
        for source in sources:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass

    assert result is not None
    row = db.record_source_preflight(
        asdict(result),
        ttl_seconds=(
            config.server.enchor_preflight_ttl_seconds
            if result.success
            else min(60, config.server.enchor_preflight_ttl_seconds)
        ),
    )
    return preflight_public(row)


def validate_discovery_start(
    db: Database,
    config: AppConfig,
    *,
    user_id: int,
    workspace_key: str,
    current_job_id: int | None = None,
) -> DiscoveryValidation:
    errors: list[str] = []
    user = db.get_user(int(user_id))
    workspace_row = db.get_workspace(workspace_key) if workspace_key else None
    has_neutral_library = bool(workspace_row and db.get_neutral_library_tracks(int(workspace_row["id"])))
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

    preflight = get_enchor_preflight(db)
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
