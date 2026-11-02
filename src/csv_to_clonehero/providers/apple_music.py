"""Import user-selected Music/iTunes metadata exports without account access.

Export identities are local metadata hashes, never Apple catalog or chart IDs.
Only title, artist, album and the selected export label enter the snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import io
import plistlib
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from xml.parsers.expat import ExpatError

from csv_to_clonehero.csv_import import import_playlist_file
from csv_to_clonehero.providers.base import (
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    SourceImportReport,
    merge_provider_tracks,
)

MAX_EXPORT_BYTES = 10 * 1024 * 1024
MAX_EXPORT_ROWS = 100_000
MAX_METADATA_LENGTH = 1024


class AppleMusicExportProvider:
    name = "apple_music"

    def __init__(self, snapshot: LibrarySnapshot):
        self._snapshot = snapshot

    def capabilities(self) -> Mapping[str, bool]:
        return {"official_api": False, "oauth": False, "user_export": True}

    def import_library(self, *, scope: Mapping[str, Any] | None = None) -> LibrarySnapshot:
        return self._snapshot

    def close(self) -> None:
        return None

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, extension: str, label: str = "Apple Music export",
        max_tracks: int = 50_000,
    ) -> "AppleMusicExportProvider":
        extension = extension.casefold().lstrip(".")
        if extension not in {"txt", "xml"}:
            raise ValueError("Choose a Music/iTunes TXT or XML export.")
        if not raw:
            raise ValueError("The export is empty.")
        if len(raw) > MAX_EXPORT_BYTES:
            raise ValueError("The Apple Music export exceeds the 10 MB limit.")
        label = str(label or "Apple Music export").strip()
        if not label or len(label) > 120 or any(ord(char) < 32 for char in label):
            raise ValueError("Use an export label of 1–120 characters.")

        # Music may emit UTF-16 TXT. Normalize it before reusing the existing
        # BOM-aware/legacy-encoding delimited importer. No filename is trusted.
        if extension == "txt":
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                try:
                    raw = raw.decode("utf-16").encode("utf-8")
                except UnicodeError as exc:
                    raise ValueError("The TXT export could not be decoded.") from exc
            text = _decode_text(raw)
            header = next((line for line in text.splitlines() if line.strip()), "")
            delimiter = "\t" if header.count("\t") > header.count(",") else ","
            try:
                row_count = sum(1 for _ in csv.DictReader(io.StringIO(text), delimiter=delimiter))
            except csv.Error as exc:
                raise ValueError("The TXT export is not valid delimited text.") from exc
        else:
            try:
                payload = plistlib.loads(raw)
            except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError, ExpatError, RecursionError) as exc:
                raise ValueError("Choose a valid Music/iTunes XML library export.") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("Tracks"), dict):
                raise ValueError("The XML export must contain a Music/iTunes Tracks dictionary.")
            row_count = len(payload["Tracks"])
            # Do not retain unrelated account, filesystem, artwork or URL fields.
            del payload
        if row_count > MAX_EXPORT_ROWS:
            raise ValueError("The export contains too many rows; export a smaller playlist.")

        # TemporaryDirectory uses a private, generated directory. The raw upload
        # is removed before returning and never enters the persistent workspace.
        with TemporaryDirectory(prefix="riff-music-import-") as temporary:
            path = Path(temporary) / f"export.{extension}"
            path.write_bytes(raw)
            try:
                rows = import_playlist_file(path)
            except (ValueError, csv.Error, UnicodeError) as exc:
                raise ValueError("The export needs readable Name and Artist metadata.") from exc

        parsed: list[ProviderTrack] = []
        for track in rows:
            if any(len(value) > MAX_METADATA_LENGTH for value in (track.title, track.artist_full, track.album)):
                raise ValueError("A song has unusually long metadata; check the export.")
            identity = hashlib.sha256(
                f"{track.artist_norm}\0{track.title_norm}".encode("utf-8")
            ).hexdigest()
            parsed.append(ProviderTrack(
                provider=cls.name, provider_id=f"export:{identity}",
                title=track.title, artist=track.artist_full, album=track.album,
                playlist_ids=["selected-export"], playlist_names=[label],
                metadata={"identity_kind": "export_metadata_hash"},
            ))
        merged = merge_provider_tracks(parsed)
        if not merged:
            raise ValueError("No songs with both a title and artist were found.")
        if len(merged) > int(max_tracks):
            raise ValueError("The export contains too many unique songs; choose a smaller playlist.")
        report = SourceImportReport(
            total_rows=row_count, accepted_rows=len(parsed),
            skipped_rows=max(0, row_count - len(parsed)),
            unique_tracks=len(merged), duplicate_rows=len(parsed) - len(merged),
            playlists=[label],
        )
        return cls(LibrarySnapshot(
            provider=cls.name, tracks=merged,
            playlists=[ProviderPlaylist(cls.name, "selected-export", label)],
            metadata={"import": "export", "account_sync": False}, import_report=report,
        ))


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("The TXT export could not be decoded.")
