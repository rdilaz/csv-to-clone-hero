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
            return cls.from_csv(file_path.read_text(encoding="utf-8-sig", newline=""))
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
    "row_type": ("type",),
    "isrc": ("isrc",),
    "provider_id": ("soundcloud - id", "id", "track_id", "urn"),
    "liked": ("liked", "favorited", "favorite", "favourite"),
}


def _snapshot_from_rows(rows: list[Any]) -> LibrarySnapshot:
    report = SourceImportReport(total_rows=len(rows))
    parsed: list[ProviderTrack] = []
    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, Mapping):
            _skip(report, row_number, "invalid row")
            continue
        normalized = {_header(key): value for key, value in row.items() if key is not None}
        title = _value(normalized, "title")
        artist = _value(normalized, "artist")
        if not artist and " - " in title:
            artist, title = title.split(" - ", 1)
            artist, title = artist.strip(), title.strip()
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
        track = _track_from_mapping({
            "title": title,
            "artist": artist,
            "album": _value(normalized, "album"),
            "playlist_name": _value(normalized, "playlist"),
            "id": provider_id,
            "isrc": _value(normalized, "isrc"),
            "row_type": _value(normalized, "row_type"),
            "liked": _value(normalized, "liked"),
        })
        if track is None:
            _skip(report, row_number, "invalid track")
            continue
        report.accepted_rows += 1
        report.playlists.extend(track.playlist_names)
        report.liked_count += int(track.liked)
        parsed.append(track)
    merged = merge_provider_tracks(parsed)
    report.unique_tracks = len(merged)
    report.duplicate_rows = max(0, len(parsed) - len(merged))
    report.playlists = list(dict.fromkeys(report.playlists))
    return LibrarySnapshot("soundcloud", merged, metadata={"import": "export", "report": report.as_dict()}, import_report=report)


def _header(value: Any) -> str:
    return str(value or "").strip().lstrip("\ufeff").casefold()


def _value(row: Mapping[str, Any], field: str) -> str:
    for alias in _ALIASES[field]:
        value = row.get(alias)
        if value not in (None, ""):
            if isinstance(value, Mapping):
                value = value.get("username") or value.get("name") or ""
            return str(value).strip()
    return ""


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
    playlist_id = row.get("playlist_id") or ""
    return ProviderTrack(
        provider="soundcloud",
        provider_id=provider_id,
        title=title,
        artist=artist,
        album=str(row.get("album") or "").strip(),
        playlist_ids=[str(playlist_id).strip()] if str(playlist_id).strip() else [],
        playlist_names=[str(playlist).strip()] if str(playlist).strip() else [],
        liked=(str(row.get("liked") or row.get("favorited") or "").casefold() in {"1", "true", "yes"}
               or str(row.get("row_type") or "").casefold() in {"favorite", "favourite"}
               or str(playlist).casefold() == "liked songs"),
        duration_ms=_optional_int(row.get("duration_ms") or row.get("duration")),
        url=str(row.get("permalink_url") or row.get("url") or "").strip(),
        isrc=str(row.get("isrc") or "").strip(),
        metadata={"export_row": True},
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
