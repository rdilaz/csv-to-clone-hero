"""Neutral contract for music-library providers.

Provider adapters normalize their own payloads here. Matching, planning, and
chart downloads must consume the resulting snapshot and never branch on a
provider name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from csv_to_clonehero.models import Track
from csv_to_clonehero.normalization import normalize_artist, normalize_title, split_artists


@dataclass
class ProviderPlaylist:
    provider: str
    provider_id: str
    name: str
    liked: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderTrack:
    provider: str
    provider_id: str
    title: str
    artist: str
    album: str = ""
    playlist_ids: list[str] = field(default_factory=list)
    playlist_names: list[str] = field(default_factory=list)
    liked: bool = False
    duration_ms: int | None = None
    isrc: str = ""
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_norm(self) -> str:
        return normalize_artist(self.artist)

    @property
    def title_norm(self) -> str:
        return normalize_title(self.title)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return self.artist_norm, self.title_norm

    @property
    def playlist_memberships(self) -> list[tuple[str, str]]:
        return provider_track_playlist_memberships(self)


@dataclass
class LibrarySnapshot:
    provider: str
    tracks: list[ProviderTrack] = field(default_factory=list)
    playlists: list[ProviderPlaylist] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    import_report: "SourceImportReport | None" = None

    @property
    def unique_track_count(self) -> int:
        return len(self.tracks)

    def normalized_tracks(self) -> list[Track]:
        """Return engine tracks after cross-provider union and normalization."""
        return [provider_track_to_track(track) for track in self.tracks]


@dataclass
class SourceImportReport:
    """Structured diagnostics returned by file-backed source imports."""

    total_rows: int = 0
    accepted_rows: int = 0
    skipped_rows: int = 0
    skipped_reasons: list[dict[str, Any]] = field(default_factory=list)
    unique_tracks: int = 0
    duplicate_rows: int = 0
    playlists: list[str] = field(default_factory=list)
    liked_count: int = 0
    missing_artist_count: int = 0
    missing_provider_id_count: int = 0
    inaccessible_count: int = 0
    error_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted_rows,
            "skipped_rows": self.skipped_rows,
            "skipped_reasons": list(self.skipped_reasons),
            "unique_tracks": self.unique_tracks,
            "duplicate_rows": self.duplicate_rows,
            "playlists": list(self.playlists),
            "liked_count": self.liked_count,
            "missing_artist_count": self.missing_artist_count,
            "missing_provider_id_count": self.missing_provider_id_count,
            "inaccessible_count": self.inaccessible_count,
            "error_count": self.error_count,
        }


@runtime_checkable
class LibraryProvider(Protocol):
    """Approved provider operations used by the public workflow."""

    name: str

    def capabilities(self) -> Mapping[str, bool]:
        """Report whether OAuth/API and export fallback are available."""
        ...

    def import_library(self, *, scope: Mapping[str, Any] | None = None) -> LibrarySnapshot:
        """Import only an explicitly selected library scope."""
        ...

    def close(self) -> None:
        ...


def provider_track_to_track(value: ProviderTrack) -> Track:
    """Convert one neutral provider record to the engine's existing Track."""
    primary, featured = split_artists(value.artist)
    key = f"{normalize_artist(primary)}|{normalize_title(value.title)}"
    metadata = dict(value.metadata)
    provenance = metadata.setdefault("providers", {})
    provider_data = provenance.setdefault(value.provider, {})
    provider_data.update(
        {
            "id": value.provider_id,
            "playlist_ids": list(value.playlist_ids),
            "playlist_names": list(value.playlist_names),
            "liked": bool(value.liked),
        }
    )
    return Track(
        track_key=key,
        title=value.title,
        title_norm=value.title_norm,
        artist_full=value.artist,
        artist_norm=value.artist_norm,
        primary_artist=primary,
        primary_artist_norm=normalize_artist(primary),
        featured_artists=featured,
        album=value.album,
        album_norm=normalize_title(value.album) if value.album else "",
        playlists=list(dict.fromkeys([*value.playlist_names, *value.playlist_ids])),
        # The legacy storage/read APIs use this field as a required library
        # identity. Provider-qualified IDs keep SoundCloud-only tracks visible
        # without pretending they are Spotify IDs.
        spotify_id=value.provider_id if value.provider == "spotify" else f"{value.provider}:{value.provider_id}",
        duration_ms=value.duration_ms,
        isrc=value.isrc,
        spotify_url=value.url if value.provider == "spotify" else "",
        origin=value.provider,
        origin_metadata=metadata,
    )


def playlist_identity(provider_id: Any, name: Any) -> str:
    """Return a stable source-local identity for an observed playlist."""
    identity = str(provider_id or "").strip()
    if identity:
        return identity
    label = str(name or "").strip()
    return f"name:{label.casefold()}" if label else ""


def playlist_membership_pairs(
    playlist_ids: Iterable[Any] | None,
    playlist_names: Iterable[Any] | None,
) -> list[tuple[str, str]]:
    """Pair parallel membership fields without dropping unmatched values."""
    ids = [str(value or "").strip() for value in (playlist_ids or ())]
    names = [str(value or "").strip() for value in (playlist_names or ())]
    pairs: list[tuple[str, str]] = []
    for index in range(max(len(ids), len(names))):
        pair = (
            ids[index] if index < len(ids) else "",
            names[index] if index < len(names) else "",
        )
        if (pair[0] or pair[1]) and pair not in pairs:
            pairs.append(pair)
    return pairs


def _coerce_membership_tuples(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    pairs: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            pair = (
                str(item.get("provider_id") or item.get("playlist_id") or item.get("id") or "").strip(),
                str(item.get("name") or item.get("playlist_name") or "").strip(),
            )
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2:
            pair = (str(item[0] or "").strip(), str(item[1] or "").strip())
        else:
            continue
        if (pair[0] or pair[1]) and pair not in pairs:
            pairs.append(pair)
    return pairs


def provider_track_playlist_memberships(value: ProviderTrack) -> list[tuple[str, str]]:
    """Read exact provenance memberships before pairing aggregate fields."""
    provenance = value.metadata.get("provenance")
    pairs: list[tuple[str, str]] = []
    if isinstance(provenance, Sequence) and not isinstance(provenance, (str, bytes)):
        for entry in provenance:
            if not isinstance(entry, Mapping):
                continue
            entry_provider = str(entry.get("provider") or "").casefold()
            if entry_provider and entry_provider != value.provider.casefold():
                continue
            exact = _coerce_membership_tuples(entry.get("playlist_memberships"))
            if not exact:
                exact = playlist_membership_pairs(
                    entry.get("playlist_ids"), entry.get("playlist_names")
                )
            for pair in exact:
                if pair not in pairs:
                    pairs.append(pair)
    if pairs:
        return pairs
    exact = _coerce_membership_tuples(value.metadata.get("playlist_memberships"))
    return exact or playlist_membership_pairs(value.playlist_ids, value.playlist_names)


def merge_provider_tracks(values: Iterable[ProviderTrack]) -> list[ProviderTrack]:
    """Union providers by normalized artist/title while preserving provenance."""
    merged: dict[tuple[str, str], ProviderTrack] = {}
    for incoming in values:
        if not incoming.title.strip() or not incoming.artist.strip():
            continue
        current = merged.get(incoming.dedupe_key)
        entry = {
            "provider": incoming.provider,
            "provider_id": incoming.provider_id,
            "playlist_ids": list(incoming.playlist_ids),
            "playlist_names": list(incoming.playlist_names),
            "playlist_memberships": playlist_membership_pairs(
                incoming.playlist_ids, incoming.playlist_names
            ),
            "liked": bool(incoming.liked),
        }
        if current is None:
            copy = ProviderTrack(
                **{name: (list(value) if isinstance(value, list) else value)
                   for name, value in vars(incoming).items()}
            )
            copy.metadata = dict(copy.metadata)
            copy.metadata["provenance"] = [entry]
            merged[incoming.dedupe_key] = copy
            continue
        current.playlist_ids = list(dict.fromkeys([*current.playlist_ids, *incoming.playlist_ids]))
        current.playlist_names = list(dict.fromkeys([*current.playlist_names, *incoming.playlist_names]))
        current.liked = current.liked or incoming.liked
        if not current.album and incoming.album:
            current.album = incoming.album
        if not current.isrc and incoming.isrc:
            current.isrc = incoming.isrc
        if not current.url and incoming.url:
            current.url = incoming.url
        providers = current.metadata.setdefault("provenance", [])
        if not isinstance(providers, list):
            providers = current.metadata["provenance"] = []
        if entry not in providers:
            # Keep the newest provider first for existing callers while
            # retaining every source record.
            providers.insert(0, entry)
    return list(merged.values())
