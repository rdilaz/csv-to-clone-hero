"""Offline tests for the neutral music-library provider boundary."""

from __future__ import annotations

from csv_to_clonehero.providers.base import ProviderTrack, merge_provider_tracks
from csv_to_clonehero.providers.soundcloud import SoundCloudExportProvider
from csv_to_clonehero.providers.soundcloud_api import SoundCloudClient, SoundCloudOAuth


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
    assert snapshot.normalized_tracks()[0].origin == "soundcloud"


def test_soundcloud_csv_export_skips_incomplete_rows():
    provider = SoundCloudExportProvider.from_csv(
        "title,artist,id,liked\nValid,DJ,1,yes\n,Missing,2,no\n"
    )
    tracks = provider.import_library().tracks
    assert len(tracks) == 1
    assert tracks[0].liked is True


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
