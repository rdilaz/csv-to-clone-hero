"""Adapter from the existing Spotify client to the neutral provider contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from csv_to_clonehero.providers.base import (
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    SourceImportReport,
    merge_provider_tracks,
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _playlist_model(value: Any) -> ProviderPlaylist:
    total_tracks = _field(
        value,
        "total_tracks",
        _field(value, "item_count", _field(value, "track_count")),
    )
    owner = _field(value, "owner", {})
    owner = owner if isinstance(owner, Mapping) else {}
    metadata = {
        "url": str(_field(value, "url", "") or ""),
        "artwork_url": str(_field(value, "artwork_url", "") or ""),
        "total_tracks": total_tracks,
        "item_count": total_tracks,
        "access_status": str(_field(value, "access_status", "accessible") or "accessible"),
        "access_reason": str(_field(value, "access_reason", "") or ""),
        "owner_id": str(_field(value, "owner_id", "") or owner.get("id") or ""),
        "owner_name": str(
            _field(value, "owner_name", "")
            or owner.get("display_name")
            or owner.get("name")
            or ""
        ),
        "is_owned": _field(value, "is_owned"),
    }
    return ProviderPlaylist(
        provider="spotify",
        provider_id=str(_field(value, "id", "") or ""),
        name=str(_field(value, "name", "") or ""),
        metadata=metadata,
    )


def _nonnegative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _import_report(
    result: Any,
    tracks: list[ProviderTrack],
    playlists: list[ProviderPlaylist],
) -> SourceImportReport:
    records = list(_field(result, "records", ()) or ())
    counts = _field(result, "counts")
    raw_total = _nonnegative_int(_field(counts, "total_item_count"))
    included = _nonnegative_int(_field(counts, "included_count"))
    provider_duplicates = _nonnegative_int(_field(counts, "duplicate_count"))
    safely_accounted = (
        raw_total is not None
        and included is not None
        and provider_duplicates is not None
        and included == len(records)
        and raw_total >= included + provider_duplicates
    )
    if safely_accounted:
        accepted = included + provider_duplicates
        skipped = raw_total - accepted
    else:
        raw_total = len(records)
        accepted = len(records)
        skipped = 0
    unique_tracks = len(tracks)
    return SourceImportReport(
        total_rows=raw_total,
        accepted_rows=accepted,
        skipped_rows=skipped,
        unique_tracks=unique_tracks,
        duplicate_rows=max(0, accepted - unique_tracks),
        playlists=[playlist.name for playlist in playlists],
        liked_count=sum(track.liked for track in tracks),
        inaccessible_count=_nonnegative_int(_field(counts, "inaccessible_count")) or 0,
        error_count=_nonnegative_int(_field(counts, "error_count")) or 0,
    )


class SpotifyLibraryProvider:
    name = "spotify"

    def __init__(self, client: Any):
        self.client = client

    def capabilities(self) -> Mapping[str, bool]:
        return {"official_api": True, "oauth": True, "user_export": False, "public_playlist": False}

    def import_library(self, *, scope: Mapping[str, Any] | None = None, on_progress: Any | None = None) -> LibrarySnapshot:
        scope = scope or {}
        playlist_ids_specified = "playlist_ids" in scope
        playlist_ids = {str(value) for value in scope.get("playlist_ids", []) if str(value).strip()}
        include_liked = bool(scope.get("include_liked"))
        playlists = list(self.client.list_playlists())
        selected = [
            playlist
            for playlist in playlists
            if not playlist_ids_specified or str(_field(playlist, "id", "")) in playlist_ids
        ]
        result = self.client.fetch_library(include_liked=include_liked, playlists=selected, on_progress=on_progress)
        tracks = [
            ProviderTrack(
                provider="spotify",
                provider_id=record.spotify_id,
                title=record.title,
                artist=record.artist,
                album=record.album,
                playlist_ids=record.playlist_ids,
                playlist_names=record.playlist_names,
                liked=record.liked,
                duration_ms=record.duration_ms,
                isrc=record.isrc,
                url=record.url,
                metadata={
                    "playlist_memberships": [
                        (str(_field(playlist, "id", "") or ""), str(_field(playlist, "name", "") or ""))
                        for playlist in (_field(record, "playlists", ()) or ())
                    ]
                },
            )
            for record in result.records
        ]
        fetched = {
            str(_field(playlist, "id", "") or ""): playlist
            for playlist in (_field(result, "playlists", ()) or ())
            if str(_field(playlist, "id", "") or "")
        }
        selected_ids = [str(_field(playlist, "id", "") or "") for playlist in selected]
        playlist_values = [fetched.pop(playlist_id, selected[index]) for index, playlist_id in enumerate(selected_ids)]
        playlist_values.extend(fetched.values())
        provider_playlists = [_playlist_model(playlist) for playlist in playlist_values]
        merged = merge_provider_tracks(tracks)
        report = _import_report(result, merged, provider_playlists)
        return LibrarySnapshot(
            "spotify",
            merged,
            provider_playlists,
            {
                "import": "api",
                "report": report.as_dict(),
                "partial": bool(report.error_count or report.inaccessible_count),
            },
            import_report=report,
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
