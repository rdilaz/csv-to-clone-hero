from __future__ import annotations

import httpx

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
    SpotifyPlaylist,
    SpotifyToken,
    parse_library,
)
from csv_to_clonehero.spotify_pipeline import (
    build_spotify_estimate,
    estimate_spotify_duration,
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
