from __future__ import annotations

import json
import threading

import httpx
import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title
from csv_to_clonehero.spotify import (
    InMemoryCredentialStore,
    LibraryItemStatus,
    LibraryImportResult,
    SpotifyLibraryRecord,
    SpotifyOAuth,
    SpotifyClient,
    SpotifyCancellationError,
    SpotifyPlaylist,
    SpotifyToken,
    parse_library,
)
from csv_to_clonehero.spotify_pipeline import (
    build_spotify_estimate,
    estimate_spotify_duration,
    run_expansion_discovery,
    run_spotify_discovery,
)


def test_pkce_exchange_only_persists_refresh_token():
    store = InMemoryCredentialStore()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
        )
    )
    http = httpx.Client(transport=transport)
    oauth = SpotifyOAuth(
        client_id="client-id",
        credential_store=store,
        http_client=http,
        clock=lambda: 100.0,
        sleeper=lambda _seconds: None,
    )
    request = oauth.begin_authorization(state="known-state")
    token = oauth.exchange_code("authorization-code", returned_state="known-state")

    assert "127.0.0.1" in request.redirect_uri
    assert "code_challenge_method=S256" in request.url
    assert token.access_token == "access-secret"
    assert store.get_password("csv-to-clonehero.spotify", "client-id") == "refresh-secret"
    assert "access-secret" not in store.values.values()
    oauth.close()
    http.close()


def test_pkce_supports_registered_https_callback():
    oauth = SpotifyOAuth(
        client_id="client-id",
        redirect_uri="https://charts.example.com/api/auth/spotify/callback",
    )
    request = oauth.begin_authorization(state="known-state", port=9999)

    assert request.redirect_uri == "https://charts.example.com/api/auth/spotify/callback"
    assert "redirect_uri=https%3A%2F%2Fcharts.example.com" in request.url
    oauth.close()


def test_library_parser_retains_terminal_item_statuses():
    good = {
        "track": {
            "id": "track-1",
            "name": "Song",
            "type": "track",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
        }
    }
    local = {
        "track": {
            "id": "local-1",
            "name": "Local",
            "type": "track",
            "uri": "spotify:local:artist:album:song",
            "artists": [{"name": "Artist"}],
        }
    }
    result = parse_library({"playlist": [None, good, good, local]})

    assert [record.spotify_id for record in result.records] == ["track-1"]
    assert result.status_counts[LibraryItemStatus.SKIPPED_NULL.value] == 1
    assert result.status_counts[LibraryItemStatus.DUPLICATE.value] == 1
    assert result.status_counts[LibraryItemStatus.SKIPPED_LOCAL.value] == 1


def test_playlist_parser_retains_creator_metadata():
    playlist = SpotifyPlaylist.from_payload({
        "id": "playlist-1",
        "name": "Made by me",
        "owner": {"id": "user-1", "display_name": "Test User"},
    })

    assert playlist is not None
    assert playlist.owner_id == "user-1"
    assert playlist.owner_name == "Test User"


def test_spotify_api_refreshes_once_and_honors_rate_limit():
    calls = {"api": 0, "token": 0}
    sleeps: list[float] = []

    def handler(request):
        if request.url.path.endswith("/api/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": "new", "refresh_token": "refresh", "expires_in": 3600},
            )
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"error": "expired"})
        if calls["api"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json={"items": [], "total": 0, "next": None})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = SpotifyOAuth(
        client_id="client-id",
        credential_store=InMemoryCredentialStore(),
        http_client=http,
        sleeper=sleeps.append,
        clock=lambda: 100.0,
    )
    oauth.save_token(SpotifyToken("old", "refresh", expires_at=10000))
    playlists = SpotifyClient(oauth=oauth).list_playlists()

    assert playlists == []
    assert calls == {"api": 3, "token": 1}
    assert sleeps == [0.0]
    oauth.close()
    http.close()


def test_saved_tracks_count_uses_single_item_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"items": [], "total": 1420, "next": None},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = SpotifyOAuth(
        client_id="client-id",
        credential_store=InMemoryCredentialStore(),
        http_client=http,
        clock=lambda: 100.0,
    )
    oauth.save_token(SpotifyToken("access", "refresh", expires_at=10000))
    client = SpotifyClient(oauth=oauth)

    assert client.get_saved_tracks_count() == 1420
    assert len(requests) == 1
    assert requests[0].url.params["limit"] == "1"
    oauth.close()
    http.close()


def test_artist_discography_uses_primary_artist_and_deduplicates_reissues():
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(
                200,
                request=request,
                json={"artists": {"items": [{"id": "artist-1", "name": "Artist"}]}},
            )
        if path.endswith("/artists/artist-1/albums"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "items": [
                        {"id": "album-1", "name": "Album One"},
                        {"id": "album-2", "name": "Reissue"},
                    ],
                    "total": 2,
                    "next": None,
                },
            )
        if path.endswith("/albums/album-1/tracks"):
            items = [
                {
                    "id": "song-1",
                    "name": "Song",
                    "artists": [{"id": "artist-1", "name": "Artist"}],
                },
                {
                    "id": "feature-1",
                    "name": "Featured",
                    "artists": [{"id": "other", "name": "Other"}, {"id": "artist-1", "name": "Artist"}],
                },
            ]
        elif path.endswith("/albums/album-2/tracks"):
            items = [
                {
                    "id": "song-1-remaster",
                    "name": "Song (2009 Remaster)",
                    "artists": [{"id": "artist-1", "name": "Artist"}],
                },
                {
                    "id": "unique-1",
                    "name": "Unique",
                    "artists": [{"id": "artist-1", "name": "Artist"}],
                },
            ]
        else:
            raise AssertionError(f"unexpected Spotify endpoint: {path}")
        return httpx.Response(200, request=request, json={"items": items, "total": len(items), "next": None})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = SpotifyOAuth(
        client_id="client-id",
        credential_store=InMemoryCredentialStore(),
        http_client=http,
        clock=lambda: 100.0,
    )
    oauth.save_token(SpotifyToken("access", "refresh", expires_at=10000))
    client = SpotifyClient(oauth=oauth)

    result = client.get_artist_discography("Artist")

    assert result["artist_id"] == "artist-1"
    assert result["albums"] == 2
    assert [item["title"] for item in result["tracks"]] == ["Song", "Unique"]
    assert result["tracks"][0]["album"] == "Album One"
    assert all("include_groups" not in request.url.params for request in requests if "/albums/" in request.url.path)
    client.close()
    http.close()


def _spotify_client_for_handler(handler, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = SpotifyOAuth(
        client_id="client-id",
        credential_store=InMemoryCredentialStore(),
        http_client=http,
        clock=lambda: 100.0,
        **kwargs,
    )
    oauth.save_token(SpotifyToken("access", "refresh", expires_at=10000))
    return SpotifyClient(oauth=oauth), http


def test_spotify_default_request_timeout_remains_bounded():
    oauth = SpotifyOAuth(client_id="client-id")

    assert isinstance(oauth.http_client, httpx.Client)
    assert oauth.http_client.timeout.connect == 20.0
    assert oauth.http_client.timeout.read == 20.0
    assert oauth.http_client.timeout.write == 20.0
    assert oauth.http_client.timeout.pool == 20.0
    oauth.close()


def test_artist_discography_cancels_before_request():
    event = threading.Event()
    event.set()
    requests = []
    client, http = _spotify_client_for_handler(lambda request: requests.append(request))

    with pytest.raises(SpotifyCancellationError):
        client.get_artist_discography("Artist", cancellation_event=event)

    assert requests == []
    client.close()
    http.close()


def test_artist_discography_cancels_before_next_page():
    event = threading.Event()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, request=request, json={"artists": {"items": [{"id": "artist-1", "name": "Artist"}]}})
        event.set()
        return httpx.Response(
            200,
            request=request,
            json={"items": [{"id": "album-1", "name": "Album"}], "total": 2, "next": "https://api.spotify.com/v1/artists/artist-1/albums?offset=1"},
        )

    client, http = _spotify_client_for_handler(handler)
    with pytest.raises(SpotifyCancellationError):
        client.get_artist_discography("Artist", cancellation_event=event)

    assert paths == ["/v1/search", "/v1/artists/artist-1/albums"]
    client.close()
    http.close()


def test_artist_discography_cancels_before_next_album():
    event = threading.Event()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/search"):
            payload = {"artists": {"items": [{"id": "artist-1", "name": "Artist"}]}}
        elif request.url.path.endswith("/artists/artist-1/albums"):
            payload = {"items": [{"id": "album-1"}, {"id": "album-2"}], "total": 2, "next": None}
        else:
            event.set()
            payload = {"items": [], "total": 0, "next": None}
        return httpx.Response(200, request=request, json=payload)

    client, http = _spotify_client_for_handler(handler)
    with pytest.raises(SpotifyCancellationError):
        client.get_artist_discography("Artist", cancellation_event=event)

    assert "/v1/albums/album-1/tracks" in paths
    assert "/v1/albums/album-2/tracks" not in paths
    client.close()
    http.close()


def test_artist_search_cancels_before_auth_retry():
    event = threading.Event()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        event.set()
        return httpx.Response(401, request=request, json={"error": "expired"})

    client, http = _spotify_client_for_handler(handler)
    with pytest.raises(SpotifyCancellationError):
        client.search_artist("Artist", cancellation_event=event)

    assert calls == 1
    client.close()
    http.close()


def test_artist_search_cancels_during_rate_limit_wait():
    class CancellingEvent:
        def __init__(self):
            self.set = False
            self.waits = []

        def is_set(self):
            return self.set

        def wait(self, timeout):
            self.waits.append(timeout)
            self.set = True
            return True

    event = CancellingEvent()
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request, headers={"Retry-After": "30"})

    client, http = _spotify_client_for_handler(handler, sleeper=sleeps.append)
    with pytest.raises(SpotifyCancellationError):
        client.search_artist("Artist", cancellation_event=event)

    assert calls == 1
    assert event.waits == [30.0]
    assert sleeps == []
    client.close()
    http.close()


def test_spotify_discovery_persists_workspace_candidates(config):
    class Source:
        name = "fake-spotify-source"
        supports_direct_download = True
        download_hash_verified = False

        def search_song(self, artist, title):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=7,
                    md5="7" * 32,
                    name=title,
                    artist=artist,
                    name_norm=normalize_title(title),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
            ]

        def search_artist(self, artist):
            return self.search_song(artist, "Song")

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [SpotifyLibraryRecord("spotify-1", "Song", ["Artist"])],
            )

    db = Database(config.database_path)
    result = run_spotify_discovery(config, db, [Source()], Client())

    assert result.status == "complete"
    assert result.workspace_id is not None
    candidates = db.get_workspace_candidates(result.workspace_id)
    assert len(candidates) == 1
    assert candidates[0]["eligibility"] in {"RECOMMENDED", "ELIGIBLE_REVIEW", "EXCLUDED", "INCOMPATIBLE"}
    assert db.list_workspace_selections(result.workspace_id) == []
    db.close()


def test_complete_local_catalog_filters_before_enchor_queries(config):
    calls = []

    class Source:
        name = "enchor"
        supports_direct_download = True
        download_hash_verified = False

        def catalog_available(self, artist, title=""):
            return not title or title == "Available Song"

        def search_song(self, artist, title):
            calls.append((artist, title))
            return [ChartCandidate(
                source=self.name,
                chart_id=10,
                md5="a" * 32,
                name=title,
                artist=artist,
                name_norm=normalize_title(title),
                artist_norm=normalize_artist(artist),
                instruments=["guitar"],
                diff_guitar=3,
            )]

        def search_artist(self, artist):
            raise AssertionError("a complete local catalog must produce song-only queries")

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult([
                SpotifyLibraryRecord("available", "Available Song", ["Artist"]),
                SpotifyLibraryRecord("missing", "Missing Song", ["Artist"]),
            ])

    db = Database(config.database_path)
    result = run_spotify_discovery(config, db, [Source()], Client())

    assert calls == [("Artist", "Available Song")]
    assert result.stats.library_tracks == 2
    assert result.stats.catalog_eligible_tracks == 1
    assert result.stats.catalog_unavailable_tracks == 1
    assert result.stats.song_queries == 1
    assert result.stats.catalog_queries == 0
    assert len(db.get_library_tracks(workspace_id=result.workspace_id)) == 2
    db.close()


def test_two_artist_exact_expansion_and_manual_export_flow(config):
    """Exercise the guided workflow without Spotify or Enchor network access."""

    class Source:
        name = "guided-flow-source"
        supports_direct_download = True
        download_hash_verified = True

        def __init__(self):
            self.downloaded: list[str] = []

        def _chart(self, title, artist, chart_id):
            return ChartCandidate(
                source=self.name,
                chart_id=chart_id,
                md5=f"{chart_id:032d}",
                name=title,
                artist=artist,
                name_norm=normalize_title(title),
                artist_norm=normalize_artist(artist),
                instruments=["guitar", "bass", "drums", "vocals"],
                diff_guitar=3,
                icon="gh3",
            )

        def search_song(self, artist, title):
            return [self._chart(title, artist, 1 if artist == "Artist One" else 2)]

        def search_artist(self, artist):
            return [self._chart(f"{artist} Deep Cut", artist, 11 if artist == "Artist One" else 12)]

        def get_download_info(self, candidate):
            from csv_to_clonehero.models import DownloadInfo
            return DownloadInfo(url=f"fake://{candidate.md5}", md5=candidate.md5, size_bytes=4)

        def download_chart(self, candidate, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"sng!")
            self.downloaded.append(candidate.md5)
            return destination

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True, playlist_ids=None):
            return LibraryImportResult([
                SpotifyLibraryRecord("spotify-one", "Song One", ["Artist One"]),
                SpotifyLibraryRecord("spotify-two", "Song Two", ["Artist Two"]),
            ])

    source = Source()
    db = Database(config.database_path)
    exact = run_spotify_discovery(config, db, [source], Client())
    workspace = exact.workspace_id
    assert exact.status == "complete"
    assert workspace is not None

    # Exact recommendations are selected automatically by the web workflow.
    exact_selected = db.select_recommended(workspace, kind="exact", minimum_score=90)
    assert len(exact_selected) == 2

    progress: list[dict] = []
    expansion = run_expansion_discovery(
        config,
        db,
        workspace,
        [source],
        selected_artist_norms=["artist one", "artist two"],
        on_progress=lambda payload: progress.append(dict(payload)),
    )
    assert expansion.status == "complete"
    assert expansion.stats.expansion_artists_total == 2
    assert expansion.stats.expansion_artists_completed == 2
    assert progress

    expansion_rows = db.get_workspace_candidates(workspace, kind="expansion")
    assert len(expansion_rows) == 2
    assert [item for item in db.list_workspace_selections(workspace) if item["kind"] == "expansion"] == []

    first = expansion_rows[0]
    db.select_workspace_candidate(
        workspace,
        None,
        candidate_id=first["candidate_id"],
        kind="expansion",
        artist_norm=first["artist_norm"],
    )
    selections = db.list_workspace_selections(workspace)
    assert {item["kind"] for item in selections} == {"exact", "expansion"}

    # The export API consumes these persisted workspace selections; exact and
    # manually accepted expansion charts reach the same tray without rescanning.
    assert len(selections) == 3
    db.close()


def test_preview_backed_expansion_queries_artist_once_and_filters_catalog(config):
    artist_calls: list[str] = []
    song_calls: list[tuple[str, str]] = []

    class Source:
        name = "preview-source"
        supports_direct_download = True
        download_hash_verified = True

        def search_song(self, artist, title):
            song_calls.append((artist, title))
            return []

        def search_artist(self, artist):
            artist_calls.append(artist)
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=31,
                    md5="3" * 32,
                    name="Deep Song",
                    artist=artist,
                    name_norm=normalize_title("Deep Song"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar", "bass"],
                    diff_guitar=3,
                ),
                ChartCandidate(
                    source=self.name,
                    chart_id=32,
                    md5="4" * 32,
                    name="Missing Songs",
                    artist=artist,
                    name_norm=normalize_title("Missing Songs"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar", "bass"],
                    diff_guitar=3,
                ),
                ChartCandidate(
                    source=self.name,
                    chart_id=33,
                    md5="5" * 32,
                    name="Completely Unrelated",
                    artist=artist,
                    name_norm=normalize_title("Completely Unrelated"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                ),
            ]

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [SpotifyLibraryRecord("preview-library", "Library Song", ["Artist"])]
            )

    db = Database(config.database_path)
    exact = run_spotify_discovery(config, db, [Source()], Client())
    assert exact.workspace_id is not None
    artist_calls.clear()
    song_calls.clear()
    db.replace_workspace_expansion_artists(
        exact.workspace_id,
        [{
            "artist_name": "Artist",
            "artist_norm": "artist",
            "track_count": 1,
            "cap": 10,
            "selected": True,
            "metadata": {
                "discography_preview": {
                    "discovered_at": 1.0,
                    "albums": 2,
                    "tracks": [
                        {"artist": "Artist", "title": "Deep Song", "album": "Album"},
                        {"artist": "Artist", "title": "Deep Song (2009 Remaster)", "album": "Reissue"},
                        {"artist": "Artist", "title": "Missing Song", "album": "Album"},
                    ],
                }
            },
        }],
    )
    progress: list[dict] = []
    result = run_expansion_discovery(
        config,
        db,
        exact.workspace_id,
        [Source()],
        selected_artist_norms=["artist"],
        on_progress=lambda payload: progress.append(dict(payload)),
    )

    assert result.status == "complete"
    assert artist_calls == ["Artist"]
    assert song_calls == []
    assert result.stats.expansion_songs_total == 2
    assert result.stats.expansion_songs_with_charts == 2
    assert result.stats.expansion_songs_without_charts == 0
    assert progress
    rows = db.get_workspace_candidates(exact.workspace_id, kind="expansion")
    assert len(rows) == 2
    metadata = [json.loads(row["metadata_json"]) for row in rows]
    assert {item["matched_spotify_title"] for item in metadata} == {"Deep Song", "Missing Song"}
    assert {item["preview_match_method"] for item in metadata} == {"exact", "fuzzy"}
    assert all(item["query_kind"] == "artist_catalog" for item in metadata)
    db.close()


def test_preview_backed_expansion_enforces_artist_candidate_caps(config):
    class Source:
        name = "cap-source"
        supports_direct_download = True
        download_hash_verified = True

        def search_song(self, artist, title):
            return []

        def search_artist(self, artist):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=index + {"One": 1000, "Some": 2000, "Many": 3000}[artist],
                    md5=f"{index + 1:032x}",
                    name=f"Song {index}",
                    artist=artist,
                    name_norm=normalize_title(f"Song {index}"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
                for index in range(60)
            ]

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult([
                SpotifyLibraryRecord(f"{artist}-{index}", f"Owned {index}", [artist])
                for artist, count in (("One", 1), ("Some", 2), ("Many", 5))
                for index in range(count)
            ])

    db = Database(config.database_path)
    exact = run_spotify_discovery(config, db, [Source()], Client())
    assert exact.workspace_id is not None
    db.replace_workspace_expansion_artists(exact.workspace_id, [
        {
            "artist_name": artist,
            "artist_norm": normalize_artist(artist),
            "track_count": count,
            "cap": cap,
            "selected": True,
            "metadata": {"discography_preview": {"tracks": [
                {"artist": artist, "title": f"Song {index}", "spotify_id": f"{artist}-{index}"}
                for index in range(60)
            ]}},
        }
        for artist, count, cap in (("One", 1, 10), ("Some", 2, 25), ("Many", 5, 50))
    ])

    result = run_expansion_discovery(
        config,
        db,
        exact.workspace_id,
        [Source()],
        selected_artist_norms=["one", "some", "many"],
    )

    assert result.status == "complete"
    rows = db.get_workspace_candidates(exact.workspace_id, kind="expansion")
    counts = {
        artist: sum(row["artist_norm"] == artist for row in rows)
        for artist in ("one", "some", "many")
    }
    assert counts == {"one": 10, "some": 25, "many": 50}
    db.close()


def test_expansion_cancellation_preserves_completed_artist(config):
    cancel_event = threading.Event()

    class Source:
        name = "expansion-cancel-source"
        supports_direct_download = True
        download_hash_verified = True

        def search_song(self, artist, title):
            return []

        def search_artist(self, artist):
            if artist == "Artist Two":
                cancel_event.set()
            chart_id = 1 if artist == "Artist One" else 2
            return [ChartCandidate(
                source=self.name,
                chart_id=chart_id,
                md5=f"{chart_id:032d}",
                name="Preview Song",
                artist=artist,
                name_norm=normalize_title("Preview Song"),
                artist_norm=normalize_artist(artist),
                instruments=["guitar"],
                diff_guitar=3,
            )]

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult([
                SpotifyLibraryRecord("one", "Owned One", ["Artist One"]),
                SpotifyLibraryRecord("two", "Owned Two", ["Artist Two"]),
            ])

    db = Database(config.database_path)
    exact = run_spotify_discovery(config, db, [Source()], Client())
    assert exact.workspace_id is not None
    db.replace_workspace_expansion_artists(exact.workspace_id, [
        {
            "artist_name": artist,
            "artist_norm": normalize_artist(artist),
            "track_count": 1,
            "cap": 10,
            "selected": True,
            "metadata": {"discography_preview": {"tracks": [
                {"artist": artist, "title": "Preview Song", "spotify_id": spotify_id}
            ]}},
        }
        for artist, spotify_id in (("Artist One", "preview-one"), ("Artist Two", "preview-two"))
    ])

    result = run_expansion_discovery(
        config,
        db,
        exact.workspace_id,
        [Source()],
        selected_artist_norms=["artist one", "artist two"],
        cancellation_event=cancel_event,
    )

    assert result.status == "cancelled"
    rows = db.get_workspace_candidates(exact.workspace_id, kind="expansion")
    assert [row["artist_norm"] for row in rows] == ["artist one"]
    db.close()


def test_spotify_estimate_reuses_cache_without_materializing_candidates(config):
    class Source:
        name = "estimate-source"
        supports_direct_download = True
        download_hash_verified = False

        def search_song(self, artist, title):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=9,
                    md5="9" * 32,
                    name=title,
                    artist=artist,
                    name_norm=normalize_title(title),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
            ]

        def search_artist(self, artist):
            return self.search_song(artist, "Song")

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [SpotifyLibraryRecord("spotify-1", "Song", ["Artist"])],
            )

    db = Database(config.database_path)
    result = run_spotify_discovery(config, db, [Source()], Client())
    assert result.status == "complete"

    # The cache is now populated.  The estimate must count it as reusable and,
    # critically, must not rebuild candidate objects to do so.
    estimate = build_spotify_estimate(config, db, sources=[Source()], workspace_id=result.workspace_id)
    assert estimate["logical_query_count"] >= 1
    assert estimate["reusable_cached_queries"] == estimate["logical_query_count"]
    assert estimate["fresh_queries"] == 0

    # Dropping candidate links must not change the cached-query count, proving
    # the estimate no longer materializes candidates.
    with db._conn:
        db._conn.execute("DELETE FROM search_query_candidates")
    degraded = build_spotify_estimate(config, db, sources=[Source()], workspace_id=result.workspace_id)
    assert degraded["reusable_cached_queries"] == estimate["reusable_cached_queries"]
    db.close()


def test_spotify_duration_estimate_scales_at_calibration_sizes(config):
    estimates = [
        estimate_spotify_duration(config, query_count=count, track_count=count)["seconds"]
        for count in (1, 5, 10, 25)
    ]

    assert estimates == sorted(estimates)
    assert all(value > 0 for value in estimates)


def test_spotify_progress_includes_fallback_and_scoring_work(config):
    class Source:
        name = "fallback-source"
        supports_direct_download = True
        download_hash_verified = False

        def search_song(self, artist, title):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=20,
                    md5="2" * 32,
                    name=title,
                    artist=artist,
                    name_norm=normalize_title(title),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
            ]

        def search_artist(self, artist):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=21,
                    md5="3" * 32,
                    name="Not one of the songs",
                    artist=artist,
                    name_norm=normalize_title("Not one of the songs"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
            ]

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [
                    SpotifyLibraryRecord(f"spotify-{index}", f"Song {index}", ["Artist"])
                    for index in range(3)
                ],
            )

    progress = []
    db = Database(config.database_path)
    result = run_spotify_discovery(
        config,
        db,
        [Source()],
        Client(),
        on_progress=lambda payload: progress.append(dict(payload)),
    )

    assert result.status == "complete"
    final = progress[-1]
    assert final["total_work"] == final["total_tasks"] + final["scoring_tasks_total"]
    assert final["completed_work"] == final["total_work"]
    assert final["scoring_tasks_total"] > final["total_tracks"]
    db.close()


def test_interrupted_resume_reuses_persisted_query_errors(config):
    class Source:
        name = "resume-source"
        supports_direct_download = True
        download_hash_verified = False

        def __init__(self):
            self.calls = 0

        def search_song(self, artist, title):
            self.calls += 1
            raise RuntimeError("temporary source failure")

        def search_artist(self, artist):
            raise AssertionError("artist search was not expected")

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [SpotifyLibraryRecord("resume-track", "Song", ["Artist"])],
            )

    source = Source()
    db = Database(config.database_path)
    first = run_spotify_discovery(config, db, [source], Client())
    assert first.status == "complete"
    assert source.calls == 1
    db.update_workspace(first.workspace_id, status="interrupted", phase="interrupted")

    second = run_spotify_discovery(config, db, [source], Client(), workspace_id=first.workspace_id)

    assert second.status == "complete"
    assert source.calls == 1
    db.close()


def test_discovery_shortlists_broad_source_results(config):
    class Source:
        name = "broad-source"
        supports_direct_download = True
        download_hash_verified = False

        def search_song(self, artist, title):
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id=index,
                    md5=f"{index:032x}",
                    name=title if index == 1 else f"Unrelated {index}",
                    artist=artist,
                    name_norm=normalize_title(title if index == 1 else f"Unrelated {index}"),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
                for index in range(1, 101)
            ]

        def search_artist(self, artist):
            return []

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [SpotifyLibraryRecord("broad-track", "Target Song", ["Artist"])],
            )

    config.search.candidate_shortlist_size = 12
    db = Database(config.database_path)
    result = run_spotify_discovery(config, db, [Source()], Client())

    assert len(db.get_workspace_candidates(result.workspace_id)) == 12
    db.close()


def test_discovery_cancellation_clears_partial_candidates(config):
    import threading

    cancel_event = threading.Event()

    class CancellableSource:
        name = "cancellable-source"
        supports_direct_download = True
        download_hash_verified = False

        def search_song(self, artist, title):
            cancel_event.set()
            return [
                ChartCandidate(
                    source=self.name,
                    chart_id="song-1",
                    md5="0" * 32,
                    name=title,
                    artist=artist,
                    name_norm=normalize_title(title),
                    artist_norm=normalize_artist(artist),
                    instruments=["guitar"],
                    diff_guitar=3,
                )
            ]

        def search_artist(self, artist):
            return []

        def close(self):
            pass

    class Client:
        def fetch_library(self, include_liked=True):
            return LibraryImportResult(
                [
                    SpotifyLibraryRecord("track-1", "First Song", ["Artist One"]),
                    SpotifyLibraryRecord("track-2", "Second Song", ["Artist Two"]),
                ],
            )

    db = Database(config.database_path)
    result = run_spotify_discovery(
        config,
        db,
        [CancellableSource()],
        Client(),
        cancellation_event=cancel_event,
    )

    assert result.status == "cancelled"
    assert db.get_workspace_candidates(result.workspace_id) == []
    ws = db.get_workspace(result.workspace_id)
    assert ws["status"] == "cancelled"
    db.close()


def test_discovery_cancels_during_provider_import_before_persisting_tracks(config):
    cancel_event = threading.Event()

    class Client:
        def fetch_library(self, include_liked=True):
            cancel_event.set()
            return LibraryImportResult([
                SpotifyLibraryRecord("cancelled-import", "Song", ["Artist"]),
            ])

    db = Database(config.database_path)
    result = run_spotify_discovery(
        config,
        db,
        [],
        Client(),
        cancellation_event=cancel_event,
    )

    assert result.status == "cancelled"
    assert db.get_workspace_library_track_ids(result.workspace_id) == set()
    db.close()


def test_discovery_cancels_from_progress_callback_before_provider_import(config):
    cancel_event = threading.Event()
    calls = []

    class Client:
        def fetch_library(self, include_liked=True):
            calls.append("fetch")
            return LibraryImportResult([])

    def on_progress(payload):
        if payload["phase"] == "spotify_sync":
            cancel_event.set()

    db = Database(config.database_path)
    result = run_spotify_discovery(
        config,
        db,
        [],
        Client(),
        on_progress=on_progress,
        cancellation_event=cancel_event,
    )

    assert result.status == "cancelled"
    assert calls == []
    db.close()
