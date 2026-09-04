"""SoundCloud provider adapters.

This module intentionally supports approved API clients and user-provided
exports only. It never accepts credentials, scrapes pages, or calls private
SoundCloud endpoints.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from csv_to_clonehero.providers.base import (
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    SourceImportReport,
    merge_provider_tracks,
    playlist_identity,
    provider_track_playlist_memberships,
)


class SoundCloudExportProvider:
    name = "soundcloud"

    def __init__(self, snapshot: LibrarySnapshot):
        self._snapshot = snapshot

    def capabilities(self) -> Mapping[str, bool]:
        return {"official_api": False, "oauth": False, "user_export": True, "public_playlist": True}

    def import_library(self, *, scope: Mapping[str, Any] | None = None) -> LibrarySnapshot:
        return self._snapshot

    def close(self) -> None:
        return None

    @classmethod
    def from_file(cls, path: str | Path) -> "SoundCloudExportProvider":
        file_path = Path(path)
        if file_path.suffix.casefold() == ".json":
            return cls.from_json(file_path.read_text(encoding="utf-8-sig"))
        if file_path.suffix.casefold() == ".csv":
            with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                return cls.from_csv(handle.read())
        raise ValueError("SoundCloud imports support only JSON or CSV exports")

    @classmethod
    def from_json(cls, text: str) -> "SoundCloudExportProvider":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("SoundCloud export is not valid JSON") from exc
        if isinstance(payload, Mapping):
            rows = payload.get("tracks", payload.get("items", payload.get("collection", [])))
        else:
            rows = payload
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("SoundCloud JSON export must contain a track list")
        return cls(_snapshot_from_rows(list(rows)))

    @classmethod
    def from_csv(cls, text: str) -> "SoundCloudExportProvider":
        reader = csv.DictReader(io.StringIO(text, newline=""))
        rows = []
        for row in reader:
            rows.append(row)
        return cls(_snapshot_from_rows(rows))


_ALIASES = {
    "title": ("track name", "title", "name", "track_title"),
    "artist": ("artist name", "artist", "user", "username", "creator"),
    "album": ("album",),
    "playlist": ("playlist name", "playlist", "playlist_name"),
    "playlist_id": (
        "playlist id",
        "playlist_id",
        "playlistid",
        "soundcloud playlist id",
        "soundcloud - playlist id",
        "set id",
        "set_id",
        "playlist urn",
        "playlist_urn",
    ),
    "row_type": ("type",),
    "isrc": ("isrc",),
    "provider_id": ("soundcloud - id", "id", "track_id", "urn"),
    "liked": ("liked", "favorited", "favorite", "favourite"),
    "url": ("permalink_url", "permalink", "url", "link"),
}


def _snapshot_from_rows(rows: list[Any]) -> LibrarySnapshot:
    report = SourceImportReport(total_rows=len(rows))
    parsed: list[ProviderTrack] = []
    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, Mapping):
            _skip(report, row_number, "invalid row")
            continue
        normalized = {_header(key): value for key, value in row.items() if key is not None}
        raw_title = _value(normalized, "title")
        raw_artist = _value(normalized, "artist")
        title = raw_title
        artist = raw_artist
        row_type = _value(normalized, "row_type").casefold()
        # SoundCloud exports include collection/entity rows alongside tracks.
        # They are not songs even when their display label contains " - ".
        if row_type in {"album", "artist"}:
            _skip(report, row_number, "non-track row")
            continue
        artist_source = "explicit" if artist else ""
        if not artist:
            prefix = _parse_title_prefix(raw_title)
            if prefix is not None:
                artist, title = prefix
                artist_source = "title_prefix_provisional"
        if not artist:
            report.missing_artist_count += 1
            _skip(report, row_number, "missing artist")
            continue
        if not title:
            _skip(report, row_number, "missing title")
            continue
        provider_id = _value(normalized, "provider_id")
        if not provider_id:
            report.missing_provider_id_count += 1
        url = _value(normalized, "url")
        if not url and provider_id.startswith(("http://", "https://")):
            url = provider_id
        track = _track_from_mapping({
            "title": title,
            "artist": artist,
            "album": _value(normalized, "album"),
            "playlist_name": _value(normalized, "playlist"),
            "playlist_id": _value(normalized, "playlist_id"),
            "id": provider_id,
            "isrc": _value(normalized, "isrc"),
            "row_type": _value(normalized, "row_type"),
            "liked": _value(normalized, "liked"),
            "url": url,
            "metadata": {
                "export_row": True,
                "raw_title": raw_title,
                "raw_artist": raw_artist,
                "row_type": row_type,
                "artist_source": artist_source,
                "artist_is_provisional": artist_source == "title_prefix_provisional",
                "provider_id": provider_id,
                "isrc": _value(normalized, "isrc"),
            },
        })
        if track is None:
            _skip(report, row_number, "invalid track")
            continue
        report.accepted_rows += 1
        report.playlists.extend(track.playlist_names)
        parsed.append(track)
    merged = merge_provider_tracks(parsed)
    playlists = _observed_playlists(parsed)
    report.unique_tracks = len(merged)
    report.duplicate_rows = max(0, len(parsed) - len(merged))
    report.playlists = [playlist.name for playlist in playlists if playlist.name]
    report.liked_count = sum(track.liked for track in merged)
    return LibrarySnapshot(
        "soundcloud",
        merged,
        playlists,
        metadata={"import": "export", "report": report.as_dict()},
        import_report=report,
    )


def _observed_playlists(tracks: list[ProviderTrack]) -> list[ProviderPlaylist]:
    observations = [
        membership
        for track in tracks
        for membership in provider_track_playlist_memberships(track)
    ]
    ids_by_name: dict[str, set[str]] = {}
    for provider_id, name in observations:
        if provider_id and name:
            ids_by_name.setdefault(name.casefold(), set()).add(provider_id)
    playlists: dict[str, ProviderPlaylist] = {}
    for provider_id, name in observations:
        if not provider_id and name and len(ids_by_name.get(name.casefold(), ())) == 1:
            provider_id = next(iter(ids_by_name[name.casefold()]))
        identity = playlist_identity(provider_id, name)
        if not identity:
            continue
        current = playlists.get(identity)
        if current is None:
            playlists[identity] = ProviderPlaylist(
                provider="soundcloud",
                provider_id=identity,
                name=name,
                liked=name.casefold() == "liked songs",
                metadata={"inferred_from_membership": True},
            )
        elif not current.name and name:
            current.name = name
    return list(playlists.values())


def _header(value: Any) -> str:
    return str(value or "").strip().lstrip("\ufeff").casefold()


def _value(row: Mapping[str, Any], field: str) -> str:
    for alias in _ALIASES[field]:
        value = row.get(alias)
        if value not in (None, ""):
            if isinstance(value, Mapping):
                if field == "playlist_id":
                    value = value.get("id") or value.get("urn") or ""
                elif field == "playlist":
                    value = value.get("title") or value.get("name") or ""
                else:
                    value = value.get("username") or value.get("name") or ""
            return str(value).strip()
    if field == "playlist_id":
        for alias in _ALIASES["playlist"]:
            value = row.get(alias)
            if isinstance(value, Mapping):
                return str(value.get("id") or value.get("urn") or "").strip()
    return ""


def _parse_title_prefix(value: str) -> tuple[str, str] | None:
    """Parse the export's documented ASCII ``artist - title`` fallback.

    This is intentionally structural and conservative: Unicode dashes and
    other separators remain part of the title, and the caller records that the
    resulting artist is provisional rather than authoritative.
    """
    text = str(value or "").strip()
    if " - " not in text:
        return None
    artist, title = (part.strip() for part in text.split(" - ", 1))
    if not artist or not title:
        return None
    return artist, title


def _skip(report: SourceImportReport, row_number: int, reason: str) -> None:
    report.skipped_rows += 1
    report.skipped_reasons.append({"row": row_number, "reason": reason})


def _track_from_mapping(row: Any) -> ProviderTrack | None:
    if not isinstance(row, Mapping):
        return None
    title = str(row.get("title") or row.get("name") or row.get("track_title") or "").strip()
    artist_value = row.get("artist") or row.get("user") or row.get("username") or row.get("creator") or ""
    if isinstance(artist_value, Mapping):
        artist_value = artist_value.get("username") or artist_value.get("name") or ""
    artist = str(artist_value).strip()
    if not title or not artist:
        return None
    provider_id = str(row.get("id") or row.get("track_id") or row.get("urn") or row.get("permalink_url") or "").strip()
    if not provider_id:
        provider_id = f"export:{artist}:{title}"
    playlist = row.get("playlist") or row.get("playlist_name") or ""
    playlist_id = (
        row.get("playlist_id")
        or row.get("playlistId")
        or row.get("playlist id")
        or row.get("set_id")
        or row.get("playlist_urn")
        or ""
    )
    if isinstance(playlist, Mapping):
        playlist_id = playlist_id or playlist.get("id") or playlist.get("urn") or ""
        playlist = playlist.get("title") or playlist.get("name") or ""
    playlist_name = str(playlist).strip()
    playlist_provider_id = str(playlist_id).strip()
    metadata = row.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    metadata.setdefault("export_row", True)
    if playlist_provider_id or playlist_name:
        metadata["playlist_memberships"] = [(playlist_provider_id, playlist_name)]
    return ProviderTrack(
        provider="soundcloud",
        provider_id=provider_id,
        title=title,
        artist=artist,
        album=str(row.get("album") or "").strip(),
        playlist_ids=[playlist_provider_id] if playlist_provider_id else [],
        playlist_names=[playlist_name] if playlist_name else [],
        liked=(str(row.get("liked") or row.get("favorited") or "").casefold() in {"1", "true", "yes"}
               or str(row.get("row_type") or "").casefold() in {"favorite", "favourite"}
               or playlist_name.casefold() == "liked songs"),
        duration_ms=_optional_int(row.get("duration_ms") or row.get("duration")),
        url=str(row.get("permalink_url") or row.get("url") or "").strip(),
        isrc=str(row.get("isrc") or "").strip(),
        metadata=metadata,
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
