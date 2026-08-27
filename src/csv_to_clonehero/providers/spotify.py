"""Adapter from the existing Spotify client to the neutral provider contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderPlaylist, ProviderTrack, merge_provider_tracks


class SpotifyLibraryProvider:
    name = "spotify"

    def __init__(self, client: Any):
        self.client = client

    def capabilities(self) -> Mapping[str, bool]:
        return {"official_api": True, "oauth": True, "user_export": False, "public_playlist": False}

    def import_library(self, *, scope: Mapping[str, Any] | None = None) -> LibrarySnapshot:
        scope = scope or {}
        playlist_ids = {str(value) for value in scope.get("playlist_ids", []) if str(value).strip()}
        include_liked = bool(scope.get("include_liked"))
        playlists = list(self.client.list_playlists())
        selected = [playlist for playlist in playlists if not playlist_ids or playlist.id in playlist_ids]
        result = self.client.fetch_library(include_liked=include_liked, playlists=selected)
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
            )
            for record in result.records
        ]
        provider_playlists = [
            ProviderPlaylist(
                provider="spotify",
                provider_id=playlist.id,
                name=playlist.name,
                metadata={"url": playlist.url},
            )
            for playlist in result.playlists
        ]
        return LibrarySnapshot("spotify", merge_provider_tracks(tracks), provider_playlists, {"import": "api"})

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
