"""Offline tests for the neutral music-library provider boundary."""

from __future__ import annotations

from csv_to_clonehero.providers.base import ProviderTrack, merge_provider_tracks
from csv_to_clonehero.providers.soundcloud import SoundCloudExportProvider
from csv_to_clonehero.providers.soundcloud_api import SoundCloudAPIProvider, SoundCloudClient, SoundCloudOAuth
from csv_to_clonehero.providers.spotify import SpotifyLibraryProvider
from csv_to_clonehero.spotify import (
    LibraryCounts,
    LibraryImportResult,
    SpotifyLibraryRecord,
    SpotifyPlaylist,
)
from csv_to_clonehero.server.jobs import _provider_library_result


def test_provider_union_preserves_ids_playlists_and_liked_status():
    tracks = merge_provider_tracks(
        [
            ProviderTrack("spotify", "sp-1", "Song", "Artist", playlist_ids=["p1"]),
            ProviderTrack("soundcloud", "sc-9", "Song", "Artist", playlist_names=["Favorites"], liked=True),
        ]
    )
    assert len(tracks) == 1
    assert tracks[0].playlist_ids == ["p1"]
    assert tracks[0].playlist_names == ["Favorites"]
    assert tracks[0].liked is True
    assert tracks[0].metadata["provenance"][0]["provider_id"] == "sc-9"


def test_renamed_playlist_keeps_one_atomic_provider_membership():
    result = _provider_library_result([
        ProviderTrack(
            "soundcloud", "sc-1", "Song", "Artist",
            playlist_ids=["p"], playlist_names=["Old name"],
        ),
        ProviderTrack(
            "soundcloud", "sc-1", "Song", "Artist",
            playlist_ids=["p"], playlist_names=["New name"],
        ),
    ])
    assert [playlist.id for playlist in result.playlists] == ["soundcloud:p"]
    assert [playlist["id"] for playlist in result.records[0]["playlists"]] == ["soundcloud:p"]


def test_spotify_provider_explicit_empty_playlists_imports_liked_only():
    calls = []

    class Client:
        def list_playlists(self):
            return [SpotifyPlaylist("playlist-1", "Playlist One")]

        def fetch_library(self, *, include_liked, playlists, on_progress=None):
            calls.append((include_liked, [playlist.id for playlist in playlists]))
            records = [SpotifyLibraryRecord("liked-1", "Liked Song", ["Artist"], liked=True)]
            if playlists:
                records.append(SpotifyLibraryRecord("playlist-track", "Playlist Song", ["Artist"]))
            return LibraryImportResult(records)

    snapshot = SpotifyLibraryProvider(Client()).import_library(
        scope={"playlist_ids": [], "include_liked": True}
    )

    assert calls == [(True, [])]
    assert [track.provider_id for track in snapshot.tracks] == ["liked-1"]


def test_spotify_provider_preserves_empty_selected_playlist_metadata_and_raw_counts():
    playlist = SpotifyPlaylist(
        "playlist-1",
        "Empty playlist",
        url="https://open.spotify.com/playlist/playlist-1",
        artwork_url="https://image.test/playlist.jpg",
        total_tracks=0,
        access_status="accessible",
        owner_id="owner-1",
        owner_name="Owner",
        is_owned=True,
    )

    class EmptyClient:
        def list_playlists(self):
            return [playlist]

        def fetch_library(self, *, include_liked, playlists, on_progress=None):
            return LibraryImportResult([], counts=LibraryCounts())

    empty = SpotifyLibraryProvider(EmptyClient()).import_library(
        scope={"playlist_ids": ["playlist-1"]}
    )
    assert len(empty.playlists) == 1
    assert empty.playlists[0].provider_id == "playlist-1"
    assert empty.playlists[0].metadata == {
        "url": "https://open.spotify.com/playlist/playlist-1",
        "artwork_url": "https://image.test/playlist.jpg",
        "total_tracks": 0,
        "item_count": 0,
        "access_status": "accessible",
        "access_reason": "",
        "owner_id": "owner-1",
        "owner_name": "Owner",
        "is_owned": True,
    }

    records = [
        SpotifyLibraryRecord(
            "one", "One", ["Artist"], playlists=[playlist.navigation]
        ),
        SpotifyLibraryRecord("two", "Two", ["Artist"], liked=True),
    ]

    class CountedClient:
        def list_playlists(self):
            return [playlist]

        def fetch_library(self, *, include_liked, playlists, on_progress=None):
            return LibraryImportResult(
                records,
                playlists=[playlist],
                counts=LibraryCounts(
                    playlist_count=1,
                    playlist_item_count=3,
                    liked_item_count=1,
                    included_count=2,
                    duplicate_count=1,
                    skipped_null_count=1,
                ),
            )

    counted = SpotifyLibraryProvider(CountedClient()).import_library()
    assert counted.import_report.total_rows == 4
    assert counted.import_report.accepted_rows == 3
    assert counted.import_report.skipped_rows == 1
    assert counted.import_report.duplicate_rows == 1
    assert counted.import_report.unique_tracks == 2
    assert counted.import_report.liked_count == 1

    inaccessible = SpotifyPlaylist(
        "playlist-1", "Empty playlist", access_status="inaccessible",
        access_reason="permission denied",
    )

    class PartialClient:
        def list_playlists(self):
            return [playlist]

        def fetch_library(self, *, include_liked, playlists, on_progress=None):
            return LibraryImportResult(
                [],
                playlists=[inaccessible],
                counts=LibraryCounts(playlist_count=1, inaccessible_count=1),
            )

    partial = SpotifyLibraryProvider(PartialClient()).import_library()
    assert partial.metadata["partial"] is True
    assert partial.import_report.inaccessible_count == 1
    assert partial.playlists[0].metadata["access_status"] == "inaccessible"


def test_soundcloud_json_export_is_strict_and_normalized():
    provider = SoundCloudExportProvider.from_json(
        '{"collection": [{"id": 10, "title": "Track", "user": {"username": "DJ"},'
        ' "permalink_url": "https://soundcloud.com/dj/track"},'
        '{"title": "Track", "artist": "DJ", "playlist_name": "Set"}]}'
    )
    snapshot = provider.import_library()
    assert snapshot.provider == "soundcloud"
    assert snapshot.unique_track_count == 1
    assert snapshot.tracks[0].provider_id == "10"
    assert snapshot.tracks[0].playlist_names == ["Set"]
    assert [(playlist.provider_id, playlist.name) for playlist in snapshot.playlists] == [
        ("name:set", "Set")
    ]
    assert snapshot.normalized_tracks()[0].origin == "soundcloud"


def test_soundcloud_export_accepts_playlist_id_aliases_and_reports_unique_counts():
    provider = SoundCloudExportProvider.from_csv(
        "title,artist,id,playlist name,SoundCloud Playlist ID,liked\n"
        "Song,DJ,1,First,p-1,yes\n"
        "Song,DJ,1,Second,p-2,yes\n"
        ",DJ,2,Skipped,p-3,no\n"
    )
    snapshot = provider.import_library()

    assert {(playlist.provider_id, playlist.name) for playlist in snapshot.playlists} == {
        ("p-1", "First"), ("p-2", "Second")
    }
    assert snapshot.tracks[0].playlist_memberships == [
        ("p-2", "Second"), ("p-1", "First")
    ]
    assert snapshot.import_report.total_rows == 3
    assert snapshot.import_report.accepted_rows == 2
    assert snapshot.import_report.skipped_rows == 1
    assert snapshot.import_report.duplicate_rows == 1
    assert snapshot.import_report.unique_tracks == 1
    assert snapshot.import_report.liked_count == 1


def test_soundcloud_csv_export_skips_incomplete_rows():
    provider = SoundCloudExportProvider.from_csv(
        "title,artist,id,liked\nValid,DJ,1,yes\n,Missing,2,no\n"
    )
    tracks = provider.import_library().tracks
    assert len(tracks) == 1
    assert tracks[0].liked is True


def test_soundcloud_csv_file_export_preserves_csv_newlines(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("title,artist\nSong,DJ\n", encoding="utf-8")

    provider = SoundCloudExportProvider.from_file(path)

    assert provider.import_library().unique_track_count == 1


def test_soundcloud_blank_artist_uses_provisional_ascii_title_prefix():
    provider = SoundCloudExportProvider.from_csv(
        "Track name,Artist name,Type,Soundcloud - id\n"
        '"Sample Artist - Sample Song",,Playlist,1\n'
    )
    track = provider.import_library().tracks[0]
    assert track.artist == "Sample Artist"
    assert track.title == "Sample Song"
    assert track.metadata["artist_source"] == "title_prefix_provisional"
    assert track.metadata["raw_title"] == "Sample Artist - Sample Song"


def test_soundcloud_entity_rows_are_not_promoted_to_songs():
    provider = SoundCloudExportProvider.from_csv(
        "Track name,Artist name,Type,Soundcloud - id\n"
        '"Sample Album - Deluxe",,Album,1\n'
        '"Sample Artist - Profile",,Artist,2\n'
        '"Sample Artist – Sample Song",,Playlist,3\n'
    )
    snapshot = provider.import_library()
    assert snapshot.tracks == []
    assert snapshot.import_report.skipped_rows == 3
    assert snapshot.import_report.missing_artist_count == 1


def test_soundcloud_json_preserves_permalink_url():
    provider = SoundCloudExportProvider.from_json(
        '{"tracks": [{"title": "Sample Song", "artist": "Sample Uploader",'
        '"permalink_url": "https://soundcloud.com/example/sample-song"}]}'
    )
    track = provider.import_library().tracks[0]
    assert track.url == "https://soundcloud.com/example/sample-song"


def test_soundcloud_csv_id_url_is_retained_as_url():
    provider = SoundCloudExportProvider.from_csv(
        "Track name,Artist name,Soundcloud - id\n"
        '"Sample Song","Sample Uploader","https://soundcloud.com/example/sample-song"\n'
    )
    track = provider.import_library().tracks[0]
    assert track.url == "https://soundcloud.com/example/sample-song"


def test_soundcloud_export_does_not_accept_unsupported_file(tmp_path):
    path = tmp_path / "export.txt"
    path.write_text("password=do-not-read", encoding="utf-8")
    try:
        SoundCloudExportProvider.from_file(path)
    except ValueError as exc:
        assert "JSON or CSV" in str(exc)
    else:
        raise AssertionError("unsupported export was accepted")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _HTTP:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        if url.endswith("/me/playlists"):
            return _Response({"collection": [{"id": 4, "title": "Set"}], "next_href": None})
        if url.endswith("/users/me/likes/tracks"):
            return _Response({"collection": [{"track": {"id": 8, "title": "Song", "user": {"username": "Artist"}}}], "next_href": None})
        return _Response({"collection": [], "next_href": None})


class _TokenHTTP:
    def post(self, url, data=None, headers=None):
        self.url, self.data = url, data
        return _Response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "scope": "non-expiring"})


def test_soundcloud_official_client_uses_linked_pagination_and_oauth_headers():
    oauth = object.__new__(SoundCloudOAuth)
    oauth._token = type("Token", (), {"access_token": "access", "expired": lambda self: False})()
    oauth.store = None
    http = _HTTP()
    client = SoundCloudClient(oauth, http_client=http)
    assert client.get_playlists()[0]["id"] == 4
    assert client.get_liked_tracks()[0]["track"]["id"] == 8
    assert all(call[1]["linked_partitioning"] == "true" for call in http.calls)
    assert all(call[2]["Authorization"] == "OAuth access" for call in http.calls)


def test_soundcloud_api_provider_reports_raw_items_and_duplicates():
    class Client:
        def playlists(self):
            return [{"id": 4, "title": "Set"}]

        def playlist_tracks(self, playlist_id):
            assert playlist_id == "4"
            return [{"id": 8, "title": "Song", "user": {"username": "Artist"}}]

        def liked_tracks(self):
            return [{"id": 8, "title": "Song", "user": {"username": "Artist"}}]

        def close(self):
            return None

    snapshot = SoundCloudAPIProvider(Client()).import_library(
        scope={"playlist_ids": ["4"], "include_liked": True}
    )
    assert snapshot.import_report.total_rows == 2
    assert snapshot.import_report.accepted_rows == 2
    assert snapshot.import_report.unique_tracks == 1
    assert snapshot.import_report.duplicate_rows == 1
    assert snapshot.import_report.liked_count == 1


def test_soundcloud_oauth_validates_state_and_exchanges_pkce_code():
    http = _TokenHTTP()
    oauth = SoundCloudOAuth("client", "secret", "http://127.0.0.1/callback", http_client=http)
    verifier = "v" * 43
    request = oauth.begin_authorization(state="state", code_verifier=verifier)
    assert "code_challenge=" in request.url
    try:
        oauth.exchange_code("code", returned_state="wrong", expected_state="state", code_verifier=request.code_verifier)
    except Exception as exc:
        assert "state" in str(exc).lower()
    else:
        raise AssertionError("invalid state was accepted")
    token = oauth.exchange_code("code", returned_state="state", expected_state="state", code_verifier=request.code_verifier)
    assert token.access_token == "access"
    assert http.data["grant_type"] == "authorization_code"
