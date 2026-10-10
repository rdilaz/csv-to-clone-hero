"""End-to-end tests for the multi-user web app (sessions, auth, jobs, download)."""

from __future__ import annotations

import time
import io
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from csv_to_clonehero.server.web import create_app


@pytest.fixture(autouse=True)
def spotify_client_id(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client")
    monkeypatch.delenv("GOFUNDME_URL", raising=False)


@pytest.fixture
def config(tmp_path):
    from csv_to_clonehero.config import AppConfig

    cfg = AppConfig()
    cfg.base_dir = tmp_path
    cfg.paths.input_dir = "input_playlists"
    cfg.paths.output_dir = "output_library"
    cfg.paths.reports_dir = "reports"
    cfg.paths.database = "state/csv_to_clonehero.db"
    cfg.server.enforce_secret_key = False
    from csv_to_clonehero.spotify import SpotifyToken

    class FakeOAuth:
        def __init__(self):
            self.req = None

        def begin_authorization(self):
            from csv_to_clonehero.spotify import (
                PKCEPair,
                SpotifyAuthorizationRequest,
            )

            self.req = SpotifyAuthorizationRequest(
                url="https://accounts.spotify.com/authorize?fake=1",
                state="state-value",
                pkce=PKCEPair.create(),
                redirect_uri="http://127.0.0.1:8765/api/auth/spotify/callback",
            )
            return self.req

        def exchange_code(self, code, **kwargs):
            return SpotifyToken(access_token="at", refresh_token="rt", expires_at=time.time() + 3600, scope=("user-library-read",))

    cfg._fake_oauth = FakeOAuth()
    return cfg


@pytest.fixture
def app(config, monkeypatch):
    import csv_to_clonehero.server.web as web

    monkeypatch.setattr(web, "_guest_oauth", lambda cfg: cfg._fake_oauth)
    monkeypatch.setattr(
        web,
        "_fetch_profile",
        lambda cfg, token: {"id": "user-1", "display_name": "Alice", "country": "US", "product": "premium", "href": ""},
    )

    application = create_app(config)
    yield application
    application.extensions["jobs"].shutdown(timeout=2)
    application.extensions["downloads"].close()


@pytest.fixture
def client(app):
    return app.test_client()


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["release"] == "riff-v2-20261008"
    assert r.get_json()["experience"] == "signal-check"


def test_landing_is_public(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Connect Spotify" in r.data
    assert b"cause-bar" not in r.data
    assert b"Get every song" not in r.data
    assert b"score the best match" not in r.data
    assert b"Enchor" in r.data
    assert b"Riff" in r.data
    assert b"Your music" in r.data
    assert b"Community charts" in r.data
    assert b"Clone Hero" in r.data
    assert b"Songs folder" not in r.data


def test_support_links_are_disabled_by_default(client, config, monkeypatch):
    assert config.monetization.effective_gofundme_url == ""
    assert not config.monetization.gofundme_enabled
    assert not config.monetization.gofundme_banner
    monkeypatch.setenv("GOFUNDME_URL", "https://example.test/community-support")
    assert "cause-bar" not in client.get("/").get_data(as_text=True)
    assert "https://example.test/community-support" not in client.get("/about").get_data(as_text=True)


def test_support_banner_can_be_disabled_without_changing_the_shell(client, config):
    config.monetization.gofundme_enabled = True
    config.monetization.gofundme_url = "https://example.test/community-support"
    config.monetization.gofundme_banner = False
    html = client.get("/").get_data(as_text=True)
    assert "cause-bar" not in html
    assert "Music studio" in html


def test_enabled_gofundme_banner_requires_explicit_destination(client, config):
    config.monetization.gofundme_enabled = True
    config.monetization.gofundme_banner = True
    assert "cause-bar" not in client.get("/").get_data(as_text=True)
    config.monetization.gofundme_url = "https://example.test/community-support"
    html = client.get("/").get_data(as_text=True)
    assert 'href="https://example.test/community-support"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "Support the creator of Clone Hero" in html


def test_gofundme_environment_url_overrides_configured_url(client, config, monkeypatch):
    config.monetization.gofundme_enabled = True
    config.monetization.gofundme_banner = True
    config.monetization.gofundme_url = "https://example.test/old-support"
    override = "https://example.test/community-support"
    monkeypatch.setenv("GOFUNDME_URL", override)
    html = client.get("/").get_data(as_text=True)
    assert f'href="{override}"' in html
    assert "https://example.test/old-support" not in html


def test_authenticated_review_shell_prioritizes_human_decisions(client):
    _sign_in(client)
    html = client.get("/").get_data(as_text=True)

    assert '<p class="kicker">YOUR MUSIC STUDIO</p>' in html
    assert "Select recommended" in html
    assert "Select same-recording charts" in html
    assert "Include alternate recordings" in html
    assert '<h1 id="confirmTitle">Build your <em>setlist.</em></h1>' in html
    assert "Advanced review settings" not in html
    assert "Selection cutoff" not in html
    assert "Change folder" not in html
    assert "local destination" not in html.casefold()
    assert 'type="text" name="destination"' not in html


def test_authenticated_shell_contains_bounded_matching_activity(client):
    _sign_in(client)
    html = client.get("/").get_data(as_text=True)
    assert 'id="stageFind"' in html
    assert 'id="activityList"' in html
    assert 'id="stageDone"' in html
    assert "You can close this page and come back" in html
    assert "worker" not in html.casefold()


def test_matching_template_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).parents[1] / "src/csv_to_clonehero/server/static/public.js"
    result = subprocess.run([node, "--check", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_public_shell_uses_csp_compatible_external_assets(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert '<script src="/static/public.js?v=riff-ux-20261010" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/public.css?v=riff-ux-20261010">' in html
    assert '<script src="/static/studio.js?v=riff-ux-20261010" defer></script>' in html
    assert '<script src="/static/riff-scene.js?v=riff-ux-20261010" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/studio.css?v=riff-ux-20261010">' in html
    assert "<script>" not in html
    assert " onclick=" not in html
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-src 'self' https://open.spotify.com" in csp
    assert "'unsafe-inline'" not in csp


@pytest.mark.parametrize(
    ("filename", "mimetype"),
    [
        ("riff-wordmark.svg", "image/svg+xml"),
        ("signal-knot.svg", "image/svg+xml"),
        ("ibm-plex-sans-latin1.woff2", "font/woff2"),
        ("space-grotesk.woff2", "font/woff2"),
        ("OFL-IBM-Plex.txt", "text/plain"),
        ("OFL-Space-Grotesk.txt", "text/plain"),
    ],
)
def test_signal_check_assets_are_served_from_the_public_package(
    client, filename, mimetype
):
    response = client.get(f"/static/{filename}")
    assert response.status_code == 200
    if filename.endswith(".woff2"):
        assert response.mimetype in {mimetype, "application/octet-stream"}
    else:
        assert response.mimetype == mimetype
    assert len(response.data) > 100


def test_anonymous_soundcloud_import_requires_spotify_session(client):
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"Track name,Artist name,Soundcloud - id\nArtist - Song,Artist,sc-1\n"), "export.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 401
    assert client.get("/api/sources").status_code == 401
    landing = client.get("/").get_data(as_text=True)
    assert 'id="guestSoundcloudForm"' not in landing
    assert "private Add Music step" in landing


def test_authenticated_soundcloud_import_uses_owned_workspace(client, config):
    _sign_in(client)
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"title,artist,id\nSong,Artist,sc-1\n"), "export.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["soundcloud_library"]["imported_scope"]["known"] is True
    assert response.get_json()["sources"] == [{
        "accepted_items": 1,
        "duplicate_items": 0,
        "include_liked": False,
        "liked": 0,
        "liked_in_playlists": 0,
        "liked_only": 0,
        "playlist_memberships": 0,
        "playlist_only": 0,
        "playlist_unique_tracks": 0,
        "playlists": 0,
        "provider": "soundcloud",
        "provenance": 1,
        "raw_items": 1,
        "raw_items_lower_bound": False,
        "skipped_items": 0,
        "status": "active",
        "tracks": 1,
        "unique_tracks": 1,
        "updated_at": response.get_json()["sources"][0]["updated_at"],
    }]
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key

    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        assert workspace is not None
        assert db.get_combined_summary(workspace["id"])["tracks"] == 1
    finally:
        db.close()


def test_journey_restores_cached_spotify_library_and_live_refresh_persists(
    client, config, monkeypatch
):
    from types import SimpleNamespace

    import csv_to_clonehero.server.web as web
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import (
        LibrarySnapshot,
        ProviderPlaylist,
        ProviderTrack,
        SourceImportReport,
    )
    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(
            workspace_id,
            LibrarySnapshot(
                "spotify",
                [
                    ProviderTrack(
                        "spotify",
                        "track-1",
                        "Song",
                        "Artist",
                        playlist_ids=["saved-list"],
                        playlist_names=["Saved mix"],
                        liked=True,
                    )
                ],
                [
                    ProviderPlaylist(
                        "spotify",
                        "saved-list",
                        "Saved mix",
                        metadata={"total_tracks": 17, "owner_name": "Alice"},
                    )
                ],
                import_report=SourceImportReport(
                    total_rows=18,
                    accepted_rows=18,
                    unique_tracks=1,
                    duplicate_rows=17,
                    liked_count=1,
                ),
            ),
            scope={"playlist_ids": ["saved-list"], "include_liked": True},
        )
        db.set_workspace_scope(workspace_id, ["saved-list"], True)
    finally:
        db.close()

    provider_calls = []

    class SpotifyClient:
        def list_playlists(self):
            provider_calls.append("playlists")
            return [
                SimpleNamespace(
                    id="fresh-list",
                    name="Fresh mix",
                    total_tracks=23,
                    owner_name="Alice",
                )
            ]

        def get_saved_tracks_count(self):
            provider_calls.append("liked")
            return 11

        def get_liked_tracks(self):
            raise AssertionError("the count endpoint should be used")

        def close(self):
            provider_calls.append("close")

    monkeypatch.setattr(web, "build_oauth", lambda *_args: object())
    monkeypatch.setattr(web, "_client_for", lambda *_args: SpotifyClient())

    cached = client.get("/api/journey").get_json()["spotify_library"]
    assert provider_calls == []
    assert cached["cached"] is True
    assert cached["cache_source"] == "latest_import"
    assert cached["playlists"] == [{
        "id": "saved-list",
        "name": "Saved mix",
        "owner_name": "Alice",
        "track_count": 17,
    }]
    assert cached["liked_count"] is None
    assert cached["known_unique_songs"] == 1
    assert cached["imported_scope"] == {
        "known": True,
        "playlist_ids": ["saved-list"],
        "include_liked": True,
    }

    refreshed = client.get("/api/spotify/scope").get_json()
    assert provider_calls == ["playlists", "liked", "close"]
    assert refreshed["cached"] is False
    assert refreshed["playlists"][0]["id"] == "fresh-list"
    assert refreshed["liked_count"] == 11
    assert refreshed["last_refreshed_at"] is not None

    persisted = client.get("/api/journey").get_json()["spotify_library"]
    assert persisted["cached"] is True
    assert persisted["cache_source"] == "spotify"
    assert persisted["playlists"][0]["id"] == "fresh-list"
    assert persisted["workspace"]["scope"]["playlist_ids"] == ["saved-list"]

    class BrokenSpotifyClient:
        def list_playlists(self):
            raise RuntimeError("provider unavailable")

        def close(self):
            provider_calls.append("broken-close")

    monkeypatch.setattr(web, "_client_for", lambda *_args: BrokenSpotifyClient())
    failed = client.get("/api/spotify/scope")
    assert failed.status_code == 502
    assert "saved choices were not changed" in failed.get_json()["error"]
    after_failure = client.get("/api/journey").get_json()["spotify_library"]
    assert after_failure["playlists"][0]["id"] == "fresh-list"
    assert after_failure["liked_count"] == 11


def test_partial_spotify_import_does_not_claim_exact_imported_scope(client, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import (
        LibrarySnapshot,
        ProviderTrack,
        SourceImportReport,
    )

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(
            workspace_id,
            LibrarySnapshot(
                "spotify",
                [ProviderTrack("spotify", "partial", "Song", "Artist", liked=True)],
                metadata={"partial": True},
                import_report=SourceImportReport(
                    total_rows=2,
                    accepted_rows=1,
                    skipped_rows=1,
                    unique_tracks=1,
                    error_count=1,
                ),
            ),
            scope={"playlist_ids": [], "include_liked": True},
        )
        db.set_workspace_scope(workspace_id, [], True)
    finally:
        db.close()

    spotify = client.get("/api/journey").get_json()["spotify_library"]
    assert spotify["cache_available"] is True
    assert spotify["liked_count"] is None
    assert spotify["imported_scope"]["known"] is False


def test_api_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/workspace").status_code == 401


def test_login_redirects_to_spotify(client, config):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.spotify.com/authorize" in r.headers["Location"]


def test_post_login_return_path_rejects_backslash_network_location(client):
    client.get("/?next=/\\attacker.example")
    with client.session_transaction() as session:
        assert "next" not in session


def test_oauth_callback_signs_in(client, config):
    state = "state-value"
    with client.session_transaction() as sess:
        sess["oauth_state"] = state
        sess["oauth_verifier"] = "verifier"
        sess["oauth_redirect_uri"] = "http://127.0.0.1:8765/api/auth/spotify/callback"
    r = client.get(f"/api/auth/spotify/callback?code=abc&state={state}")
    assert r.status_code == 302
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.get_json()["display_name"] == "Alice"


def test_oauth_callback_denial_returns_safe_error_page(client):
    response = client.get("/api/auth/spotify/callback?error=access_denied")

    assert response.status_code == 400
    assert b"Spotify sign-in failed" in response.data


def test_oauth_callback_rejects_empty_spotify_profile(client, config, monkeypatch):
    import csv_to_clonehero.server.web as web
    from csv_to_clonehero.database import Database

    monkeypatch.setattr(web, "_fetch_profile", lambda _config, _token: {"id": "", "display_name": ""})
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-value"
        sess["oauth_verifier"] = "verifier"
        sess["oauth_redirect_uri"] = "cb"

    response = client.get("/api/auth/spotify/callback?code=abc&state=state-value")

    assert response.status_code == 502
    assert b"please try again" in response.data
    assert client.get("/api/me").status_code == 401
    db = Database(config.database_path)
    try:
        assert db.get_user("") is None
    finally:
        db.close()


def test_soundcloud_import_enforces_public_track_cap(client, config):
    _sign_in(client)
    config.server.max_scope_tracks = 1
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"title,artist,id\nOne,Artist,1\nTwo,Artist,2\n"), "export.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert "too many songs" in response.get_json()["error"]


def test_logout_revokes_session(client, config):
    # sign in
    with client.session_transaction() as sess:
        sess["oauth_state"] = "st"
        sess["oauth_verifier"] = "v"
        sess["oauth_redirect_uri"] = "cb"
    client.get("/api/auth/spotify/callback?code=c&state=st")
    assert client.get("/api/me").status_code == 200
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 401


def test_production_mutations_require_same_origin_riff_request(client, config):
    _sign_in(client)
    config.server.enforce_secret_key = True
    config.server.base_url = "https://riff.ryo-nd.com"

    missing = client.post("/api/spotify/scope", json={"include_liked": True})
    assert missing.status_code == 403
    wrong = client.post(
        "/api/spotify/scope",
        json={"include_liked": True},
        headers={"X-Riff-Request": "1", "Origin": "https://attacker.example"},
    )
    assert wrong.status_code == 403
    accepted = client.post(
        "/api/spotify/scope",
        json={"include_liked": True},
        headers={"X-Riff-Request": "1", "Origin": "https://riff.ryo-nd.com"},
    )
    assert accepted.status_code == 200


def test_create_and_poll_job(client, config, monkeypatch):
    _sign_in(client)
    _prepare_discovery(client, config)
    client.application.extensions["jobs"].source_factory = lambda cfg: []
    from csv_to_clonehero.spotify_pipeline import DiscoveryResult, DiscoveryStats

    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: DiscoveryResult(status="complete", phase="complete", stats=DiscoveryStats()),
    )
    from csv_to_clonehero.database import Database

    r = client.post("/api/jobs", json={"kind": "discovery"})
    assert r.status_code == 202
    job_id = r.get_json()["job"]["id"]
    deadline = time.time() + 8
    while time.time() < deadline:
        row = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if row["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    row = client.get(f"/api/jobs/{job_id}").get_json()["job"]
    assert row["status"] == "complete"


def test_public_spotify_job_uses_current_matching_for_recommendations(
    client, app, config, monkeypatch
):
    from csv_to_clonehero.spotify import LibraryImportResult, SpotifyLibraryRecord
    from tests.conftest import make_chart

    class Client:
        def fetch_library(self, include_liked=True):
            assert include_liked is True
            return LibraryImportResult([
                SpotifyLibraryRecord("exact", "The Only Exception", ["Paramore"]),
                SpotifyLibraryRecord("false-positive", "Drowning", ["Atreyu"]),
            ])

        def close(self):
            pass

    class Source:
        name = "test"
        supports_direct_download = True

        def search_song(self, artist, title):
            chart_title = "Falling Down" if title == "Drowning" else title
            chart = make_chart(chart_title, artist, chart_id=1 if title == "Drowning" else 2)
            chart.source = self.name
            chart.charter = "Harmonix"
            chart.icon = "rb2"
            chart.pack_name = "Official Pack"
            chart.diff_band = 5
            chart.difficulty_fields = {
                "diff_band": 5, "diff_guitar": 3, "diff_bass": 3, "diff_drums": 3,
            }
            return [chart]

        def search_artist(self, artist):
            raise AssertionError("two single-track artists must use exact song searches")

        def close(self):
            pass

    _sign_in(client)
    _prepare_discovery(client, config)
    oauth = object()
    monkeypatch.setattr("csv_to_clonehero.server.jobs.build_oauth", lambda *_args: oauth)
    monkeypatch.setattr(
        "csv_to_clonehero.server.jobs.build_spotify_client",
        lambda _config, value: Client() if value is oauth else pytest.fail("wrong OAuth"),
    )
    app.extensions["jobs"].source_factory = lambda _config: [Source()]

    response = client.post("/api/jobs", json={"kind": "discovery"})
    assert response.status_code == 202
    job_id = response.get_json()["job"]["id"]
    deadline = time.time() + 8
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "complete"

    payload = client.get("/api/workspace").get_json()
    tracks = {track["title"]: track for track in payload["tracks"]}
    assert payload["summary"]["strong"] == 1
    assert tracks["The Only Exception"]["review_state"] == "strong"
    assert tracks["The Only Exception"]["candidates"][0]["title"] == "The Only Exception"
    assert tracks["Drowning"]["review_state"] != "strong"
    assert tracks["Drowning"]["candidates"] == []

    selected = client.post("/api/selection/bulk", json={"scope": "strong"})
    assert selected.status_code == 200
    assert selected.get_json()["changed"] == 1
    tracks = {
        track["title"]: track
        for track in client.get("/api/workspace").get_json()["tracks"]
    }
    assert tracks["The Only Exception"]["selected"] is True
    assert tracks["Drowning"]["selected"] is False


def test_matching_activity_endpoint_is_bounded_and_searchable(client, config):
    _sign_in(client)
    _prepare_discovery(client, config)
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key
    from tests.conftest import make_track

    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = db.upsert_spotify_tracks([make_track("Song", "Artist", "activity-1")])[0]
        db.initialize_discovery_song_progress(workspace["id"], [track])
        job = db.create_job(1, "discovery", workspace_key=workspace["workspace_key"])
        job_id = int(job["id"])
    finally:
        db.close()
    response = client.get(f"/api/jobs/{job_id}/songs?limit=999&search=Artist")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["limit"] == 100
    assert payload["total"] == 1
    assert payload["songs"][0]["status"] == "waiting"


def test_create_job_requires_scope_and_fresh_authoritative_preflight(client, config):
    _sign_in(client)
    missing = client.post("/api/jobs", json={"kind": "discovery"})
    assert missing.status_code == 412
    assert "Choose at least one Spotify playlist" in missing.get_json()["error"]
    assert "Check Enchor" in missing.get_json()["error"]

    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    still_missing = client.post("/api/jobs", json={"kind": "discovery"})
    assert still_missing.status_code == 412
    assert still_missing.get_json()["preflight"]["usable"] is False


def test_preflight_refresh_persists_global_authoritative_status(client, app):
    _sign_in(client)
    from csv_to_clonehero.sources.enchor import EnchorCatalogPreflight

    class FakeEnchor:
        name = "enchor"
        supports_direct_download = True
        exportable = True
        discovery_only = False

        def preflight_catalog(self):
            return EnchorCatalogPreflight(
                checked_at=time.time(),
                success=True,
                supported=True,
                authoritative=True,
                found=321,
                out_of=321,
                returned_count=1,
                page=1,
            )

        def close(self):
            pass

    app.extensions["jobs"].source_factory = lambda cfg: [FakeEnchor()]
    app.extensions["jobs"].shutdown()
    refreshed = client.post("/api/preflight", json={"force": True})
    assert refreshed.status_code == 202
    job_id = refreshed.get_json()["job"]["id"]
    app.extensions["jobs"]._run_job(app.extensions["jobs"].get(job_id))
    assert client.get(f"/api/jobs/{job_id}").get_json()["job"]["result"]["preflight"]["out_of"] == 321
    cached = client.get("/api/preflight").get_json()["preflight"]
    assert cached["usable"] is True
    assert cached["authoritative"] is True


def test_fresh_global_preflight_cannot_be_force_overwritten(client, app, config):
    _sign_in(client)
    _prepare_discovery(client, config)
    calls = []
    app.extensions["jobs"].source_factory = lambda cfg: calls.append(True) or []
    response = client.post("/api/preflight", json={"force": True})
    assert response.status_code == 200
    assert response.get_json()["preflight"]["out_of"] == 100
    assert calls == []


def test_busy_public_preflight_is_observable_and_retryable_without_adapter_calls(client, app):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.throttle import global_gate

    _sign_in(client)
    app.extensions["jobs"].shutdown()
    calls = []
    app.extensions["jobs"].source_factory = lambda _config: calls.append(True) or []
    with global_gate().acquire(tag="existing-job"):
        response = client.post("/api/preflight", json={})
        assert response.status_code == 202
        job_id = response.get_json()["job"]["id"]
        app.extensions["jobs"]._run_job(app.extensions["jobs"].get(job_id))
    preflight = client.get(f"/api/jobs/{job_id}").get_json()["job"]["result"]["preflight"]
    assert preflight["mode"] == "live_fallback"
    assert preflight["status"] == "busy"
    assert preflight["busy"] and preflight["retryable"] and not preflight["usable"]
    assert calls == []
    connection = Database(app.extensions["jobs"].config.database_path)
    try:
        assert connection.get_source_preflight("enchor") is None
    finally:
        connection.close()


def test_track_only_scope_is_rejected(client):
    _sign_in(client)
    response = client.post("/api/spotify/scope", json={"track_ids": ["track-1"]})
    assert response.status_code == 400
    assert "not supported" in response.get_json()["error"]


def test_selection_rejects_candidate_outside_workspace(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        track = make_track("Song", "Artist", spotify_id="track-1")
        db.upsert_spotify_tracks([track])
        chart = make_chart("Song", "Artist", chart_id=77)
        db.upsert_candidate(chart)
        candidate_id = chart.db_id
    finally:
        db.close()
    response = client.post(
        "/api/selection",
        json={"track_key": track.track_key, "candidate_id": candidate_id, "action": "select"},
    )
    assert response.status_code == 400
    assert "does not belong" in response.get_json()["error"]


def test_workspace_score_threshold_is_persisted_and_validated(client, config):
    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    assert client.post("/api/workspace/settings", json={"minimum_score": 0}).status_code == 200
    workspace = client.get("/api/workspace").get_json()
    assert workspace["minimum_score"] == 0
    invalid = client.post("/api/workspace/settings", json={"minimum_score": 101})
    assert invalid.status_code == 400


def test_score_threshold_controls_selection_but_never_overrides_hard_gate(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        from csv_to_clonehero.server.auth import user_workspace_key

        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = make_track("Ain't No Thief", "Viagra Boys", spotify_id="track-1")
        db.upsert_spotify_tracks([track])
        chart = make_chart("Ain't Nice", "Viagra Boys", chart_id=78)
        db.upsert_candidate(chart)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, chart, score=60,
            eligibility="EXCLUDED", decision="REJECT",
            breakdown_json='{"gate_factors": {"hard_gate": false}}', kind="exact",
        )
        hard = make_chart("Ain't No Thief", "Viagra Boys", chart_id=79)
        db.upsert_candidate(hard)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, hard, score=99,
            eligibility="EXCLUDED", decision="REJECT",
            breakdown_json='{"gate_factors": {"hard_gate": true}}', kind="exact",
        )
        chart_id, hard_id = chart.db_id, hard.db_id
    finally:
        db.close()

    assert client.post("/api/workspace/settings", json={"minimum_score": 50}).status_code == 200
    unconfirmed = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": chart_id, "action": "select",
    })
    assert unconfirmed.status_code == 400
    assert "explicit confirmation" in unconfirmed.get_json()["error"]
    selected = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": chart_id, "action": "select",
        "confirm_possible": True,
    })
    assert selected.status_code == 200
    assert client.post("/api/workspace/settings", json={"minimum_score": 100}).status_code == 200
    below = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": chart_id, "action": "select",
    })
    assert below.status_code == 400
    assert client.post("/api/workspace/settings", json={"minimum_score": 0}).status_code == 200
    gated = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": hard_id, "action": "select",
    })
    assert gated.status_code == 400
    assert "hard compatibility gate" in gated.get_json()["error"]


def test_bulk_selection_defaults_to_strong_and_can_explicitly_include_review(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(Database.workspace_key_for_user("user-1"))
        track = db.upsert_spotify_tracks([make_track("Song", "Artist", "bulk-track")])[0]
        db.replace_workspace_library_tracks(workspace["id"], [track])
        strong = make_chart("Song", "Artist", chart_id=811)
        review = make_chart("Song (Live)", "Artist", chart_id=812)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, strong, score=95,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, review, score=82,
            eligibility="ELIGIBLE_REVIEW", decision="REVIEW", kind="exact",
        )
        strong_id, review_id = strong.db_id, review.db_id
    finally:
        db.close()

    default = client.post("/api/selection/bulk", json={})
    assert default.status_code == 200
    assert default.get_json()["scope"] == "best_matches"
    db = Database(config.database_path)
    try:
        selected = db.list_workspace_selections(workspace["id"])
        assert len(selected) == 1
        assert selected[0]["candidate_id"] == strong_id
    finally:
        db.close()

    explicit = client.post(
        "/api/selection/bulk",
        json={"preset": "maximum_playable", "track_ids": [track.track_key]},
    )
    assert explicit.status_code == 200
    assert explicit.get_json()["scope"] == "maximum_playable"
    db = Database(config.database_path)
    try:
        selected = db.list_workspace_selections(workspace["id"])
        assert len(selected) == 2
        assert {row["candidate_id"] for row in selected} == {strong_id, review_id}
    finally:
        db.close()
    cleared = client.post("/api/selection", json={
        "track_key": track.track_key,
        "candidate_id": review_id,
        "action": "clear",
    })
    assert cleared.status_code == 200
    db = Database(config.database_path)
    try:
        selected = db.list_workspace_selections(workspace["id"])
        assert len(selected) == 1
        assert selected[0]["candidate_id"] == strong_id
    finally:
        db.close()


def test_bulk_cohorts_replace_instead_of_accumulate(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(Database.workspace_key_for_user("user-1"))
        tracks = db.upsert_spotify_tracks([
            make_track("Exact Song", "Exact Artist", "cohort-strong"),
            make_track("Ain't No Thief", "Viagra Boys", "cohort-review"),
        ])
        db.replace_workspace_library_tracks(workspace["id"], tracks)
        strong = make_chart("Exact Song", "Exact Artist", chart_id=821)
        review = make_chart("Ain't No Thief (Live)", "Viagra Boys", chart_id=822)
        db.upsert_workspace_candidate(
            workspace["id"], tracks[0].track_key, strong, score=95,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.upsert_workspace_candidate(
            workspace["id"], tracks[1].track_key, review, score=82,
            eligibility="ELIGIBLE_REVIEW", decision="REVIEW", kind="exact",
        )
    finally:
        db.close()

    assert client.post("/api/selection/bulk", json={"preset": "maximum_playable"}).get_json()["changed"] == 2
    assert client.get("/api/workspace").get_json()["summary"]["selected"] == 2
    assert client.post("/api/selection/bulk", json={"scope": "strong"}).get_json()["changed"] == 1
    summary = client.get("/api/workspace").get_json()["summary"]
    assert summary["selected"] == 1
    assert summary["selected_strong"] == 1
    assert summary["selected_review"] == 0


def test_duplicate_start_returns_existing_job(client, app, config):
    _sign_in(client)
    _prepare_discovery(client, config)
    app.extensions["jobs"].shutdown(timeout=2)

    first = client.post("/api/jobs", json={"kind": "discovery"})
    second = client.post("/api/jobs", json={"kind": "discovery"})
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.get_json()["job"]["id"] == first.get_json()["job"]["id"]


def test_cancel_job_requires_owner_and_returns_cancelled(client, app, config):
    _sign_in(client)
    _prepare_discovery(client, config)
    app.extensions["jobs"].shutdown(timeout=2)
    created = client.post("/api/jobs", json={"kind": "discovery"})
    job_id = created.get_json()["job"]["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.get_json()["job"]["status"] == "cancelled"


def test_workspace_management_is_owner_scoped(client, app, config):
    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    listing = client.get("/api/workspaces")
    assert listing.status_code == 200
    workspace_id = listing.get_json()["workspaces"][0]["id"]
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 200
    assert client.post(f"/api/workspaces/{workspace_id}/reset").status_code == 200
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    db = Database(config.database_path)
    try:
        track = make_track("Cached Song", "Cached Artist", spotify_id="cached-track")
        db.upsert_spotify_tracks([track])
        chart = make_chart("Cached Song", "Cached Artist", chart_id=901)
        db.upsert_candidate(chart)
        db.upsert_workspace_candidate(
            workspace_id, track.track_key, chart, score=90,
            eligibility="RECOMMENDED", decision="ACCEPT", kind="exact",
        )
        candidate_id = chart.db_id
        other = db.upsert_user(spotify_user_id="other-owner")
        other_id = db.create_workspace(
            workspace_key=Database.workspace_key_for_user(other["spotify_user_id"])
        )
    finally:
        db.close()
    assert client.get(f"/api/workspaces/{other_id}").status_code == 404
    assert client.post(f"/api/workspaces/{other_id}/reset").status_code == 404
    assert client.delete(f"/api/workspaces/{other_id}").status_code == 404

    downloads = app.extensions["downloads"]
    archive = downloads.root / "workspace-delete.zip"
    archive.write_bytes(b"transient")
    token = downloads.create(1, workspace_id, archive)["token"]
    assert client.delete(f"/api/workspaces/{workspace_id}").get_json()["deleted"] is True
    assert downloads.resolve(token) is None
    assert not archive.exists()
    assert client.delete(f"/api/workspaces/{workspace_id}").get_json()["deleted"] is False
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404
    db = Database(config.database_path)
    try:
        assert db.get_candidate(candidate_id) is not None
    finally:
        db.close()


def test_workspace_reset_delete_require_active_job_cancellation(client, app, config):
    _sign_in(client)
    _prepare_discovery(client, config)
    app.extensions["jobs"].shutdown(timeout=2)
    workspace_id = client.get("/api/workspaces").get_json()["workspaces"][0]["id"]
    job_id = client.post("/api/jobs", json={"kind": "discovery"}).get_json()["job"]["id"]

    assert client.post(f"/api/workspaces/{workspace_id}/reset").status_code == 409
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.post(f"/api/workspaces/{workspace_id}/reset").status_code == 200


def test_workspace_empty_before_run(client, config):
    _sign_in(client)
    data = client.get("/api/workspace").get_json()
    assert data["tracks"] == []


def test_fresh_authenticated_sources_creates_user_scoped_workspace(client, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key

    _sign_in(client)
    response = client.get("/api/sources")

    assert response.status_code == 200
    workspace_id = response.get_json()["workspace_id"]
    assert workspace_id > 0
    db = Database(config.database_path)
    try:
        row = db.get_workspace(workspace_id)
        assert row is not None
        assert row["workspace_key"] == user_workspace_key("user-1")
    finally:
        db.close()


def test_public_journey_restores_owned_workspace_state(client, app, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderPlaylist, ProviderTrack, SourceImportReport
    from csv_to_clonehero.server.auth import user_workspace_key
    from tests.conftest import make_chart

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        snapshot = LibrarySnapshot(
            "soundcloud",
            [ProviderTrack(
                "soundcloud", "sc-1", "Song", "Artist",
                playlist_ids=["set-1"], playlist_names=["Set One"],
            )],
            [ProviderPlaylist("soundcloud", "set-1", "Set One")],
            metadata={"import": "upload"},
            import_report=SourceImportReport(
                total_rows=3, accepted_rows=2, skipped_rows=1,
                duplicate_rows=1, unique_tracks=1,
            ),
        )
        db.upsert_library_snapshot(workspace["id"], snapshot)
        track_id = next(iter(db.get_workspace_library_track_ids(workspace["id"])))
        track = next(track for track in db.get_spotify_tracks(True) if track.db_id == track_id)
        chart = make_chart("Song", "Artist", chart_id=921)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, chart, score=95,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace["id"], track.track_key, chart, kind="exact")
        job = db.create_job(1, "discovery", workspace_key=workspace["workspace_key"])
        db.update_job(int(job["id"]), status="complete", phase="complete")
        workspace_id = int(workspace["id"])
    finally:
        db.close()
    archive = app.extensions["downloads"].root / "journey.zip"
    archive.write_bytes(b"zip")
    token = app.extensions["downloads"].create(1, workspace_id, archive)["token"]

    response = client.get("/api/journey")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["workspace"] == {
        "id": workspace_id,
        "status": payload["workspace_status"],
        "phase": payload["workspace"]["phase"],
        "updated_at": payload["workspace"]["updated_at"],
    }
    assert payload["spotify_scope"]["include_liked"] is True
    assert payload["combined"]["tracks"] == 1
    assert payload["sources"][0]["provider"] == "soundcloud"
    expected_source_counts = {
        "unique_tracks": 1,
        "playlists": 1,
        "playlist_memberships": 1,
        "raw_items": 3,
        "accepted_items": 2,
        "skipped_items": 1,
        "duplicate_items": 1,
    }
    assert {
        key: payload["sources"][0][key] for key in expected_source_counts
    } == expected_source_counts
    assert payload["job"]["status"] == "complete"
    assert "workspace_key" not in payload["job"]
    assert {key: payload["review"][key] for key in {
        "strong", "review", "unavailable", "selected",
        "selected_strong", "selected_review", "total",
    }} == {
        "strong": 1, "review": 0, "unavailable": 0, "selected": 1,
        "selected_strong": 1, "selected_review": 0, "total": 1,
    }
    assert payload["download"]["token"] == token
    assert payload["download"]["url"].endswith(token)
    assert "path" not in payload["download"]


@pytest.mark.parametrize("song_count", [1, 2], ids=["one-song", "two-songs"])
@pytest.mark.parametrize("expansion_mode", ["disabled", "empty", "variants"])
def test_public_journey_restores_song_and_chart_selection_counts(
    client, app, config, song_count, expansion_mode
):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
    from tests.conftest import make_chart

    config.server.enable_expansion = expansion_mode != "disabled"
    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace_id, LibrarySnapshot("spotify", [
            ProviderTrack("spotify", f"count-{index}", title, "Artist")
            for index, title in enumerate(["First Song", "Second Song"][:song_count])
        ]))
        tracks = sorted(
            db.get_library_tracks(workspace_id=workspace_id), key=lambda track: track.title
        )
        generation = db.create_discovery_scan(
            workspace_id, scan_key="selection-counts", kind="exact", status="complete",
            metadata={"workflow": "combined_catalog"},
        )
        task = db.upsert_discovery_task(
            workspace_id, scan_id=generation, source="test",
            query_key="selection-counts", status="complete",
        )
        first_song_candidates = []
        for index, track in enumerate(tracks):
            for variant in range(3 if index == 0 else 1):
                chart = make_chart(
                    track.title, "Artist", chart_id=1000 + index * 10 + variant,
                    charter=f"Charter {variant}",
                )
                db.upsert_workspace_candidate(
                    workspace_id, track.track_key, chart, score=99,
                    eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task,
                )
                db.select_workspace_candidate(workspace_id, track.track_key, chart)
                if index == 0:
                    first_song_candidates.append(chart.db_id)
        expansion_candidates = []
        if expansion_mode == "variants":
            for variant in range(2):
                chart = make_chart(
                    "Additional Song", "Artist", chart_id=1100 + variant,
                    charter=f"Expansion Charter {variant}",
                )
                db.upsert_workspace_candidate(
                    workspace_id, None, chart, kind="expansion", artist_norm="artist",
                    score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT",
                    metadata={"exact_generation_id": generation},
                )
                db.select_workspace_candidate(
                    workspace_id, None, chart, kind="expansion", artist_norm="artist",
                    metadata={"selection_origin": "explicit", "possible_confirmed": True},
                )
                expansion_candidates.append(chart.db_id)
        db.update_workspace(
            workspace_id, status="complete", phase="complete",
            metadata={"workflow": "public_catalog"},
        )
    finally:
        db.close()

    page = client.get("/api/workspace?limit=1&selected=true").get_json()
    assert page["total"] == song_count
    assert len(page["tracks"]) == 1
    assert page["tracks"][0]["selected_count"] == 3
    assert set(page["tracks"][0]["selected_candidate_ids"]) == set(first_song_candidates)
    assert {key: page["summary"][key] for key in (
        "total", "selected", "selected_tracks", "selected_charts", "selection_count",
    )} == {
        "total": song_count, "selected": song_count, "selected_tracks": song_count,
        "selected_charts": song_count + 2, "selection_count": song_count + 2,
    }
    empty_page = client.get("/api/workspace?search=not-in-this-library").get_json()
    assert empty_page["total"] == 0
    assert empty_page["tracks"] == []
    assert empty_page["summary"] == page["summary"]

    restored = app.test_client()
    _sign_in(restored)
    journey = restored.get("/api/journey").get_json()
    assert journey["workspace_id"] == workspace_id
    assert journey["combined"]["tracks"] == song_count
    assert journey["review"] == {
        key: value for key, value in page["summary"].items() if key != "selection_count"
    }
    assert journey["download"] is None
    expansion = restored.get("/api/expansion/results?limit=1")
    if expansion_mode == "disabled":
        assert expansion.status_code == 404
    else:
        assert expansion.status_code == 200
        results = expansion.get_json()
        assert results["total"] == int(expansion_mode == "variants")
        assert results["summary"] == {
            "total": int(expansion_mode == "variants"),
            "selected": len(expansion_candidates),
        }
        if expansion_candidates:
            assert results["songs"][0]["selected_count"] == 2
            assert set(results["songs"][0]["selected_candidate_ids"]) == set(expansion_candidates)
        else:
            assert results["songs"] == []
        later_page = restored.get("/api/expansion/results?limit=1&offset=1").get_json()
        assert later_page["songs"] == []
        assert later_page["summary"] == results["summary"]

    cleared = restored.post("/api/selection", json={
        "track_key": tracks[0].track_key,
        "candidate_id": first_song_candidates[1], "action": "clear",
    })
    assert cleared.status_code == 200
    review = restored.get("/api/journey").get_json()["review"]
    assert review["selected_charts"] == song_count + 1
    assert review["selected_tracks"] == song_count
    assert restored.post("/api/selection/bulk", json={"action": "clear"}).status_code == 200
    review = restored.get("/api/journey").get_json()["review"]
    assert review["selected_charts"] == review["selected_tracks"] == review["selected"] == 0
    assert review["total"] == song_count
    if expansion_mode != "disabled":
        assert restored.get("/api/expansion/results").get_json()["summary"] == results["summary"]


def test_workspace_paging_search_filters_and_summary_are_bounded(client, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.get("/api/sources").status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        tracks = db.upsert_spotify_tracks([
            make_track("Alpha", "Artist A", "page-a"),
            make_track("Beta", "Artist B", "page-b"),
            make_track("Gamma", "Artist C", "page-c"),
        ])
        db.replace_workspace_library_tracks(workspace["id"], tracks)
        strong = make_chart("Alpha", "Artist A", chart_id=931, charter="Strong Charter")
        review = make_chart("Beta Live", "Artist B", chart_id=932, charter="Review Charter")
        db.upsert_workspace_candidate(
            workspace["id"], tracks[0].track_key, strong, score=95,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.upsert_workspace_candidate(
            workspace["id"], tracks[1].track_key, review, score=80,
            eligibility="ELIGIBLE_REVIEW", decision="REVIEW", kind="exact",
        )
        db.select_workspace_candidate(workspace["id"], tracks[0].track_key, strong, kind="exact")
        db.update_workspace(workspace["id"], status="complete", phase="complete")
    finally:
        db.close()

    first = client.get("/api/workspace?limit=1&offset=1").get_json()
    assert first["limit"] == 1
    assert first["offset"] == 1
    assert first["total"] == 3
    assert len(first["tracks"]) == 1
    assert {key: first["summary"][key] for key in {
        "strong", "review", "unavailable", "selected",
        "selected_strong", "selected_review", "selection_count", "total",
    }} == {
        "strong": 1, "review": 1, "unavailable": 1, "selected": 1,
        "selected_strong": 1, "selected_review": 0,
        "selection_count": 1, "total": 3,
    }
    bounded = client.get("/api/workspace?limit=999").get_json()
    assert bounded["limit"] == 100
    assert client.get("/api/workspace?review_state=needs_review").get_json()["total"] == 1
    assert client.get("/api/workspace?state=review").get_json()["total"] == 1
    assert client.get("/api/workspace?state=unavailable").get_json()["total"] == 1
    selected = client.get("/api/workspace?selected=true").get_json()
    assert selected["total"] == 1 and selected["tracks"][0]["title"] == "Alpha"
    searched = client.get("/api/workspace?search=Review%20Charter").get_json()
    assert searched["total"] == 1 and searched["tracks"][0]["title"] == "Beta"


def test_workspace_selectable_tier_filter_excludes_blocked_and_no_result(
    client, monkeypatch
):
    import csv_to_clonehero.server.web as web

    _sign_in(client)

    def card(title, tier=None):
        candidates = [] if tier is None else [{
            "candidate_id": len(title), "result_tier": tier, "selected": False,
            "score": 80, "review": {"state": "needs_review"},
        }]
        return {
            "track_key": title.casefold(), "artist": "Artist", "title": title,
            "review_state": "unavailable" if not candidates else "review",
            "selected": False, "selected_count": 0, "candidates": candidates,
        }

    monkeypatch.setattr(web, "_review_cards", lambda *_args, **_kwargs: [
        card("Recommended", "RECOMMENDED_EXACT"),
        card("Variant", "SAME_RECORDING_CHART_VARIANT"),
        card("Alternate", "ALTERNATE_RECORDING"),
        card("Possible", "POSSIBLE_MATCH"),
        card("Blocked", "BLOCKED"),
        card("No result"),
    ])
    tiers = (
        "RECOMMENDED_EXACT,SAME_RECORDING_CHART_VARIANT,"
        "ALTERNATE_RECORDING,POSSIBLE_MATCH"
    )
    payload = client.get(f"/api/workspace?tier={tiers}").get_json()
    assert payload["total"] == 4
    assert {item["title"] for item in payload["tracks"]} == {
        "Recommended", "Variant", "Alternate", "Possible",
    }


def test_workspace_projection_never_leaks_another_owner(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    db = Database(config.database_path)
    try:
        other = db.upsert_user(spotify_user_id="other-user")
        other_workspace = db.create_workspace(
            workspace_key=Database.workspace_key_for_user(other["spotify_user_id"])
        )
        track = db.upsert_spotify_tracks([make_track("Private", "Other", "private-track")])[0]
        db.replace_workspace_library_tracks(other_workspace, [track])
        chart = make_chart("Private", "Other", chart_id=933)
        db.upsert_workspace_candidate(
            other_workspace, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
    finally:
        db.close()

    payload = client.get("/api/workspace?search=Private").get_json()
    assert payload["total"] == 0
    assert payload["tracks"] == []


def test_public_download_route_is_owner_scoped_and_single_use(
    client, app, config, monkeypatch
):
    import csv_to_clonehero.server.web as web

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    archive = app.extensions["downloads"].root / "owner-only.zip"
    archive.write_bytes(b"PK\x03\x04owner-only")
    token = app.extensions["downloads"].create(1, workspace_id, archive)["token"]

    monkeypatch.setattr(
        web,
        "_fetch_profile",
        lambda _config, _token: {
            "id": "user-2", "display_name": "Bob", "country": "US",
            "product": "free", "href": "",
        },
    )
    other = app.test_client()
    _sign_in(other)
    assert other.get(f"/api/download/{token}").status_code == 404
    assert app.extensions["downloads"].resolve(token) is not None

    owned = client.get(f"/api/download/{token}")
    assert owned.status_code == 200
    assert owned.data == b"PK\x03\x04owner-only"
    assert client.get(f"/api/download/{token}").status_code in {404, 410}


def test_public_download_build_dto_restores_metadata_and_consumes_archive(
    client, app, config, monkeypatch
):
    import zipfile

    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
    from tests.conftest import FakeSource, make_chart, valid_sng_bytes

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    content = valid_sng_bytes("Song")
    charts = [make_chart("Song", "Artist", chart_id=1200 + index) for index in range(2)]
    source = FakeSource(files={chart.md5: content for chart in charts})
    source.download_hash_verified = False
    config.export.download_workers = 1
    monkeypatch.setattr("csv_to_clonehero.sources.build_sources", lambda _config: [source])
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace_id, LibrarySnapshot("spotify", [
            ProviderTrack("spotify", "download-dto", "Song", "Artist")
        ]))
        track = db.get_library_tracks(workspace_id=workspace_id)[0]
        generation = db.create_discovery_scan(
            workspace_id, scan_key="download-dto", kind="exact", status="complete"
        )
        task = db.upsert_discovery_task(
            workspace_id, scan_id=generation, source="test",
            query_key="download-dto", status="complete",
        )
        for chart in charts:
            db.upsert_workspace_candidate(
                workspace_id, track.track_key, chart, score=99,
                eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task,
            )
            db.select_workspace_candidate(workspace_id, track.track_key, chart)
        db.update_workspace(workspace_id, status="complete", phase="complete")
    finally:
        db.close()

    response = client.post("/api/download", json={})
    assert response.status_code == 202, response.get_json()
    payload = response.get_json()
    assert set(payload) == {"job", "created"}
    assert payload["created"] is True
    assert payload["job"]["kind"] == "download"
    job_id = payload["job"]["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        terminal = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if terminal["status"] in {"complete", "partial", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert terminal["status"] == "complete", terminal
    assert "token" not in json.dumps(terminal)
    assert "path" not in terminal["result"]
    summary = terminal["result"]["summary"]

    journey = client.get("/api/journey").get_json()
    download = journey["download"]
    assert set(download) == {"token", "archive_id", "created_at", "expires_at", "ttl_seconds", "size_bytes", "url"}
    assert isinstance(download["token"], str) and download["token"]
    assert download["ttl_seconds"] == app.extensions["downloads"].ttl_seconds
    assert download["size_bytes"] > 0
    assert summary == {
        "success": True, "published": 1, "failed": 0, "invalid": 0,
        "download_failures": 0, "already_present": 0, "ready": 2,
        "selected": 2, "remaining": 0, "status": "success",
        "bytes": download["size_bytes"], "errors": [],
    }
    assert journey["download_job"]["result"]["archive_created_at"] == download["created_at"]
    assert journey["download_job"]["result"]["archive_id"] == download["archive_id"]
    assert source.downloads == [chart.md5 for chart in charts]
    archive_path = Path(app.extensions["downloads"].resolve(download["token"])["path"])

    restored = app.test_client()
    _sign_in(restored)
    journey = restored.get("/api/journey").get_json()
    restored_download = journey["download"]
    assert set(restored_download) == set(download)
    assert {key: restored_download[key] for key in download} == download
    assert restored_download["url"] == f"/api/download/{download['token']}"
    assert restored_download["created_at"] < restored_download["expires_at"]
    assert "summary" not in journey
    assert journey["workspace_id"] == workspace_id
    assert journey["review"]["selected_charts"] == 2
    assert journey["review"]["selected_tracks"] == 1
    assert journey["job"] is None
    assert journey["download_job"]["id"] == job_id
    assert restored.get("/api/jobs").get_json()["jobs"][0]["id"] == job_id

    streamed = restored.get(restored_download["url"], buffered=True)
    assert streamed.status_code == 200
    assert streamed.mimetype == "application/zip"
    assert streamed.headers["Content-Disposition"] == 'attachment; filename="riff-library.zip"'
    assert streamed.headers["Cache-Control"] == "private, no-store"
    assert int(streamed.headers["Content-Length"]) == len(streamed.data) == download["size_bytes"]
    with zipfile.ZipFile(io.BytesIO(streamed.data)) as archive:
        songs = [name for name in archive.namelist() if name.endswith(".sng")]
        assert len(songs) == 1
        assert archive.read(songs[0]) == content
    assert not archive_path.exists()
    assert restored.get(restored_download["url"]).status_code == 404
    after = restored.get("/api/journey").get_json()
    assert after["download"] is None
    assert after["review"] == journey["review"]
    db = Database(config.database_path)
    try:
        assert {row["candidate_id"] for row in db.list_workspace_selections(workspace_id)} == {
            chart.db_id for chart in charts
        }
        assert db.get_workspace_owned_track_ids(workspace_id) == set()
    finally:
        db.close()


def _seed_async_download_choice(config, workspace_id):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    db = Database(config.database_path)
    try:
        track = db.upsert_spotify_tracks([make_track("Queued Song", "Artist", "queued-song")])[0]
        db.replace_workspace_library_tracks(workspace_id, [track])
        db.create_discovery_scan(workspace_id, scan_key="queued", kind="exact", status="complete")
        chart = make_chart("Queued Song", "Artist", chart_id=2451)
        db.upsert_workspace_candidate(
            workspace_id, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace_id, track.track_key, chart)
    finally:
        db.close()


def _wait_download_job(client, job_id, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"complete", "partial", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"download job {job_id} did not finish")


def test_async_download_post_is_prompt_unique_owner_scoped_and_blocks_workspace_reset(
    client, app, config, monkeypatch
):
    import csv_to_clonehero.server.jobs as job_module
    import csv_to_clonehero.server.web as web
    from csv_to_clonehero.database import Database

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    started = threading.Event()
    release = threading.Event()
    builds = []

    def slow_build(_config, _db, workspace, _sources, staging, **kwargs):
        builds.append(workspace)
        started.set()
        assert release.wait(5)
        archive = staging / "slow-build.zip"
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {
            "success": True, "published": 1, "failed": 0, "remaining": 0,
            "selected": 1, "bytes": archive.stat().st_size, "errors": [],
        }

    monkeypatch.setattr(job_module, "build_archive", slow_build)
    try:
        began = time.monotonic()
        first = client.post("/api/download", json={})
        assert first.status_code == 202
        assert time.monotonic() - began < 1.0
        job_id = first.get_json()["job"]["id"]
        assert first.get_json()["created"] is True
        assert started.wait(3)
        duplicate = client.post("/api/download", json={})
        assert duplicate.status_code == 202
        assert duplicate.get_json()["created"] is False
        assert duplicate.get_json()["job"]["id"] == job_id
        journey = client.get("/api/journey").get_json()
        assert journey["download_job"]["id"] == job_id
        assert journey["job"] is None
        assert journey["download"] is None
        assert client.post(f"/api/workspaces/{workspace_id}/reset").status_code == 409
        assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409
        assert client.post("/api/selection", json={"action": "clear", "kind": "exact"}).status_code == 409
        assert client.post("/api/selection", json={"action": "clear", "kind": "expansion"}).status_code == 409
        assert client.post("/api/selection/bulk", json={"action": "clear"}).status_code == 409

        monkeypatch.setattr(web, "_fetch_profile", lambda *_: {"id": "other-user", "display_name": "Other"})
        other = app.test_client()
        _sign_in(other)
        assert other.get(f"/api/jobs/{job_id}").status_code == 404
        assert other.get("/api/journey").get_json()["download_job"] is None
        assert other.post("/api/selection/bulk", json={"action": "clear"}).status_code == 200
    finally:
        release.set()
    job = _wait_download_job(client, job_id)
    assert job["status"] == "complete", job
    assert builds == [workspace_id]
    download = client.get("/api/journey").get_json()["download"]
    assert download["created_at"] == job["result"]["archive_created_at"]
    db = Database(config.database_path)
    try:
        persisted = dict(db.get_job(job_id))
        assert "token" not in persisted["result_json"]
        assert "slow-build.zip" not in persisted["result_json"]
    finally:
        db.close()


def test_queued_download_fails_closed_when_selected_chart_changes(client, app, config, monkeypatch):
    import csv_to_clonehero.server.jobs as job_module
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    manager = app.extensions["jobs"]
    manager.shutdown(timeout=2)
    called = []
    monkeypatch.setattr(job_module, "build_archive", lambda *a, **k: called.append(True))
    response = client.post("/api/download", json={})
    assert response.status_code == 202
    job_id = response.get_json()["job"]["id"]
    db = Database(config.database_path)
    try:
        track = db.get_spotify_tracks(True)[0]
        replacement = make_chart("Queued Song", "Artist", chart_id=2452)
        db.upsert_workspace_candidate(
            workspace_id, track.track_key, replacement, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.clear_workspace_selection(workspace_id, kind="exact")
        db.select_workspace_candidate(workspace_id, track.track_key, replacement)
        assert len(db.list_workspace_selections(workspace_id)) == 1
    finally:
        db.close()
    manager.start()
    job = _wait_download_job(client, job_id)
    assert job["status"] == "failed"
    assert "choices changed" in job["error"].lower()
    assert called == []
    assert client.get("/api/journey").get_json()["download"] is None


def test_download_discards_archive_if_choices_change_during_build(client, app, config, monkeypatch):
    import csv_to_clonehero.server.jobs as job_module
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    started = threading.Event()
    release = threading.Event()
    archive = app.extensions["downloads"].root / "changed-during-build.zip"

    def slow_build(_config, _db, _workspace, _sources, _staging, **_kwargs):
        started.set()
        assert release.wait(5)
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {
            "success": True, "published": 1, "failed": 0, "remaining": 0,
            "selected": 1, "errors": [],
        }

    monkeypatch.setattr(job_module, "build_archive", slow_build)
    queued = client.post("/api/download", json={})
    assert queued.status_code == 202
    job_id = queued.get_json()["job"]["id"]
    try:
        assert started.wait(3)
        # A request already past the HTTP guard can commit its selection write.
        # The same-count swap must still invalidate the archive before handoff.
        db = Database(config.database_path)
        try:
            track = db.get_spotify_tracks(True)[0]
            replacement = make_chart("Queued Song", "Artist", chart_id=2452)
            db.upsert_workspace_candidate(
                workspace_id, track.track_key, replacement, score=99,
                eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
            )
            db.clear_workspace_selection(workspace_id, kind="exact")
            db.select_workspace_candidate(workspace_id, track.track_key, replacement)
            assert len(db.list_workspace_selections(workspace_id)) == 1
        finally:
            db.close()
    finally:
        release.set()
    job = _wait_download_job(client, job_id)
    assert job["status"] == "failed"
    assert "choices changed" in job["error"].lower()
    assert not archive.exists()
    assert client.get("/api/journey").get_json()["download"] is None


def test_completed_download_job_after_restart_has_no_stale_link(client, app, config, monkeypatch):
    import csv_to_clonehero.server.jobs as job_module

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)

    def synthetic_build(_config, _db, _workspace, _sources, staging, **_kwargs):
        archive = staging / "restart-build.zip"
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {"success": True, "published": 1, "failed": 0, "remaining": 0, "selected": 1, "errors": []}

    monkeypatch.setattr(job_module, "build_archive", synthetic_build)
    queued = client.post("/api/download", json={}).get_json()["job"]
    assert _wait_download_job(client, queued["id"])["status"] == "complete"
    assert client.get("/api/journey").get_json()["download"] is not None
    app.extensions["jobs"].shutdown(timeout=2)
    app.extensions["downloads"].close()

    restarted = create_app(config)
    try:
        visitor = restarted.test_client()
        _sign_in(visitor)
        journey = visitor.get("/api/journey").get_json()
        assert journey["download_job"]["status"] == "complete"
        assert journey["download"] is None
    finally:
        restarted.extensions["jobs"].shutdown(timeout=2)
        restarted.extensions["downloads"].close()


def test_async_download_cancellation_and_failed_registration_leave_no_archive(
    client, app, config, monkeypatch
):
    import csv_to_clonehero.server.jobs as job_module
    from csv_to_clonehero.server.downloads import DownloadError

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    started = threading.Event()

    def cancelled_build(_config, _db, _workspace, _sources, _staging, **kwargs):
        started.set()
        assert kwargs["cancellation_event"].wait(5)
        raise DownloadError("Preparation cancelled")

    monkeypatch.setattr(job_module, "build_archive", cancelled_build)
    queued = client.post("/api/download", json={}).get_json()["job"]
    assert started.wait(3)
    assert client.post(f"/api/jobs/{queued['id']}/cancel", json={}).status_code == 200
    assert _wait_download_job(client, queued["id"])["status"] == "cancelled"
    assert client.get("/api/journey").get_json()["download"] is None

    def prepared_build(_config, _db, _workspace, _sources, staging, **_kwargs):
        archive = staging / "registration-failed.zip"
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {"success": True, "published": 1, "failed": 0, "remaining": 0, "selected": 1, "errors": []}

    monkeypatch.setattr(job_module, "build_archive", prepared_build)
    monkeypatch.setattr(app.extensions["downloads"], "create", lambda *a: (_ for _ in ()).throw(OSError("registry failed")))
    retry = client.post("/api/download", json={}).get_json()["job"]
    failed = _wait_download_job(client, retry["id"])
    assert failed["status"] == "failed"
    assert not (app.extensions["downloads"].root / "registration-failed.zip").exists()
    assert client.get("/api/journey").get_json()["download"] is None


def test_download_cancel_at_final_handoff_revokes_archive(client, app, config, monkeypatch):
    import csv_to_clonehero.server.jobs as job_module
    from csv_to_clonehero.database import Database

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    archive = app.extensions["downloads"].root / "cancel-at-handoff.zip"

    def prepared_build(_config, _db, _workspace, _sources, _staging, **_kwargs):
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {
            "success": True, "published": 1, "failed": 0, "remaining": 0,
            "selected": 1, "errors": [],
        }

    real_finish = Database.finish_download_job_if_active

    def cancel_before_finish(db, job_id, **kwargs):
        db.request_job_cancel(job_id)
        return real_finish(db, job_id, **kwargs)

    monkeypatch.setattr(job_module, "build_archive", prepared_build)
    monkeypatch.setattr(Database, "finish_download_job_if_active", cancel_before_finish)
    queued = client.post("/api/download", json={})
    assert queued.status_code == 202
    job = _wait_download_job(client, queued.get_json()["job"]["id"])
    assert job["status"] == "cancelled"
    assert job["result"] == {}
    assert client.get("/api/journey").get_json()["download"] is None
    assert not archive.exists()


def test_async_archive_identity_distinguishes_prior_zip_after_latest_loss(client, app, config, monkeypatch):
    from types import SimpleNamespace
    import csv_to_clonehero.server.downloads as download_module
    import csv_to_clonehero.server.jobs as job_module
    from csv_to_clonehero.database import Database

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)
    fixed_time = time.time()
    monkeypatch.setattr(download_module, "time", SimpleNamespace(time=lambda: fixed_time))
    registry = app.extensions["downloads"]
    db = Database(config.database_path)
    try:
        user_id = db.get_user("user-1")["id"]
    finally:
        db.close()
    prior_path = registry.root / "prior-clock.zip"
    prior_path.write_bytes(b"PK\x03\x04synthetic prior")
    prior = registry.create(user_id, workspace_id, prior_path)

    def prepared_build(_config, _db, _workspace, _sources, staging, **_kwargs):
        archive = staging / "current-clock.zip"
        archive.write_bytes(b"PK\x03\x04synthetic current")
        return archive, {"success": True, "published": 1, "failed": 0, "remaining": 0, "selected": 1, "errors": []}

    monkeypatch.setattr(job_module, "build_archive", prepared_build)
    queued = client.post("/api/download", json={}).get_json()["job"]
    job = _wait_download_job(client, queued["id"])
    assert job["status"] == "complete"
    current = client.get("/api/journey").get_json()["download"]
    assert current["created_at"] == prior["created_at"] == job["result"]["archive_created_at"]
    assert current["archive_id"] == job["result"]["archive_id"] != prior["archive_id"]
    assert current["token"] not in json.dumps(job["result"])
    assert registry.resolve(current["archive_id"]) is None

    registry.revoke(current["token"])

    restored = client.get("/api/journey").get_json()
    assert restored["download"]["archive_id"] == prior["archive_id"]
    assert restored["download_job"]["result"]["archive_id"] != restored["download"]["archive_id"]


def test_async_partial_archive_preserves_failure_report_and_live_link(client, app, config, monkeypatch):
    import csv_to_clonehero.server.jobs as job_module

    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _seed_async_download_choice(config, workspace_id)

    def partial_build(_config, _db, _workspace, _sources, staging, **_kwargs):
        archive = staging / "partial-build.zip"
        archive.write_bytes(b"PK\x03\x04synthetic")
        return archive, {
            "success": True, "published": 1, "failed": 1, "remaining": 1,
            "selected": 2, "errors": ["A chart was unavailable"],
        }

    monkeypatch.setattr(job_module, "build_archive", partial_build)
    queued = client.post("/api/download", json={}).get_json()["job"]
    job = _wait_download_job(client, queued["id"])
    assert job["status"] == "partial"
    assert job["result"]["summary"]["failed"] == 1
    journey = client.get("/api/journey").get_json()
    assert journey["download_job"]["status"] == "partial"
    assert journey["download"]["created_at"] == job["result"]["archive_created_at"]


def test_browser_exporter_has_single_route(app):
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule == "/browser-exporter.js"]
    assert len(rules) == 1


def test_public_server_hides_expansion_apis_when_disabled(client, config):
    _sign_in(client)
    config.server.enable_expansion = False
    assert client.get("/api/catalog/summary").status_code == 404
    assert client.get("/api/expansion/artists").status_code == 404
    assert client.post("/api/expansion/artists/select", json={"artists": []}).status_code == 404
    assert client.post("/api/expansion/score", json={}).status_code == 404


def test_public_expansion_choices_job_review_and_selection_are_separate(
    client, app, config, monkeypatch
):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
    from tests.conftest import make_chart

    _sign_in(client)
    client.get("/api/sources")
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(Database.workspace_key_for_user("user-1"))
        db.upsert_library_snapshot(
            workspace["id"],
            LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "known", "Known", "Artist")
            ]),
        )
        generation = db.create_discovery_scan(
            workspace["id"], scan_key="catalog-exact", kind="exact", status="complete",
            metadata={"workflow": "combined_catalog"},
        )
        db.update_workspace(
            workspace["id"], status="complete", phase="complete",
            metadata={"workflow": "public_catalog"},
        )
    finally:
        db.close()

    known = make_chart("Known", "Artist", chart_id=750)
    more = make_chart("More", "Artist", chart_id=751)

    class Catalog:
        available = True
        diagnostic = ""
        snapshot_id = "web-catalog"
        chart_count = 2

        def artist_songs(self, artist):
            return (("known", (known,)), ("more", (more,))) if artist == "artist" else ()

    monkeypatch.setattr(
        "csv_to_clonehero.catalog.load_enchor_catalog", lambda *args, **kwargs: Catalog()
    )
    app.extensions["jobs"].source_factory = lambda _config: pytest.fail(
        "catalog expansion constructed a ChartSource"
    )

    choices = client.get("/api/expansion/artists")
    assert choices.status_code == 200
    choice = choices.get_json()["artists"][0]
    assert choice["artist_norm"] == "artist"
    assert choice["eligible_songs"] == 1

    started = client.post("/api/expansion", json={"artists": ["artist"]})
    assert started.status_code == 202
    job_id = started.get_json()["job"]["id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "complete"
    assert job["result"]["network_requests"] == 0

    results = client.get("/api/expansion/results").get_json()
    assert results["total"] == 1
    candidate = results["songs"][0]["candidates"][0]
    selected = client.post("/api/selection", json={
        "kind": "expansion",
        "artist_norm": "artist",
        "candidate_id": candidate["candidate_id"],
        "action": "select",
        "confirm_possible": True,
    })
    assert selected.status_code == 200
    assert client.get("/api/expansion/results").get_json()["summary"]["selected"] == 1

    assert client.post("/api/selection/bulk", json={"action": "clear"}).status_code == 200
    db = Database(config.database_path)
    try:
        expansion = db.list_workspace_selections(workspace["id"], kind="expansion")
        assert len(expansion) == 1
        assert int(json.loads(db.get_workspace_candidates(
            workspace["id"], kind="expansion"
        )[0]["metadata_json"])["exact_generation_id"]) == generation
    finally:
        db.close()


def test_workspace_review_payload_uses_human_states_and_current_selection(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track
    from csv_to_clonehero.server.auth import user_workspace_key

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = make_track("Ain't No Thief", "Viagra Boys", spotify_id="review-track", album="Album")
        stored = db.upsert_spotify_tracks([track])[0]
        db.replace_workspace_library_tracks(workspace["id"], [stored])

        strong = make_chart("Ain't No Thief", "Viagra Boys", chart_id=701)
        review = make_chart("Ain't Nice", "Viagra Boys", chart_id=702)
        hard = make_chart("Ain't No Thief", "Viagra Boys", chart_id=703, instruments=[], diff_guitar=None)
        db.upsert_workspace_candidate(
            workspace["id"], stored.track_key, strong, score=94,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT",
            breakdown_json=json.dumps({
                "factors": {
                    "match": {
                        "title": {"similarity": 100, "exact_normalized": True},
                        "artist": {"similarity": 100, "exact_normalized": True},
                    },
                    "quality": {"full_band": 8},
                    "gates": {"hard_gate": False, "playable_content": True, "required_five_fret_guitar": True},
                }
            }),
        )
        db.upsert_workspace_candidate(
            workspace["id"], stored.track_key, review, score=82,
            eligibility="ELIGIBLE_REVIEW", decision="REVIEW",
            breakdown_json=json.dumps({
                "factors": {
                    "match": {
                        "title": {"similarity": 92},
                        "artist": {"similarity": 94},
                        "version_markers": {"track": [], "candidate": ["live"], "common": []},
                    },
                    "quality": {"unrated_guitar": -3},
                    "gates": {"hard_gate": False, "playable_content": True, "required_five_fret_guitar": True},
                }
            }),
        )
        db.upsert_workspace_candidate(
            workspace["id"], stored.track_key, hard, score=99,
            eligibility="INCOMPATIBLE", decision="REJECT",
            breakdown_json=json.dumps({
                "reasons": ["no playable chart content"],
                "factors": {
                    "gates": {
                        "hard_gate": True,
                        "playable_content": False,
                        "hard_gate_reasons": ["no playable chart content"],
                    }
                },
            }),
        )
        db.select_workspace_candidate(
            workspace["id"], stored.track_key, strong,
            kind="exact", eligibility="RECOMMENDED",
        )
    finally:
        db.close()

    payload = client.get("/api/workspace").get_json()
    item = payload["tracks"][0]
    candidates = {candidate["chart_id"]: candidate for candidate in item["candidates"]}
    assert item["review_label"] == "Strong match"
    assert item["best_candidate_id"] == candidates[701]["candidate_id"]
    assert item["selected_candidate_id"] == candidates[701]["candidate_id"]
    assert candidates[701]["selected"] is True
    assert candidates[701]["review"]["summary"]
    assert candidates[702]["review_label"] == "Needs review"
    assert candidates[702]["review"]["summary"]
    assert candidates[702]["review"]["why"]
    assert candidates[703]["review_label"] == "Not playable"
    assert candidates[703]["selectable"] is False
    assert "RECOMMENDED" not in item["review_summary"]


def test_workspace_review_payload_is_honest_when_matching_is_incomplete(client, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key
    from tests.conftest import make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = db.upsert_spotify_tracks([make_track("Unmatched", "Artist", "unmatched")])[0]
        db.replace_workspace_library_tracks(workspace["id"], [track])
        db.update_workspace(workspace["id"], status="complete", phase="complete")
    finally:
        db.close()

    complete = client.get("/api/workspace").get_json()["tracks"][0]
    assert complete["review_label"] == "No suitable chart found"
    assert "completed search" in complete["review_summary"]

    db = Database(config.database_path)
    try:
        db.update_workspace(workspace["id"], status="partial", phase="complete")
    finally:
        db.close()
    partial = client.get("/api/workspace").get_json()["tracks"][0]
    assert partial["review_label"] == "Matching incomplete"
    assert "run the search again" in partial["review_summary"]


@pytest.mark.parametrize(
    ("track_title", "artist", "chart_title"),
    [
        ("Drowning", "Atreyu", "Falling Down"),
        ("You Rock My World", "Michael Jackson", "Rock With You"),
        ("Ain't No Thief", "Viagra Boys", "Ain't Nice"),
    ],
)
def test_stale_false_positive_cannot_be_public_best_match_or_bulk_selected(
    client, config, track_title, artist, chart_title
):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = db.upsert_spotify_tracks([
            make_track(track_title, artist, f"false-{chart_title}")
        ])[0]
        db.replace_workspace_library_tracks(workspace["id"], [track])
        chart = make_chart(chart_title, artist, chart_id=970)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, chart, score=100,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.update_workspace(workspace["id"], status="complete", phase="complete")
    finally:
        db.close()

    payload = client.get("/api/workspace").get_json()
    assert payload["summary"]["strong"] == 0
    assert payload["tracks"][0]["review_state"] != "strong"
    selected = client.post("/api/selection/bulk", json={"scope": "strong"})
    assert selected.status_code == 200
    assert selected.get_json()["changed"] == 0


def test_public_review_and_selection_use_latest_completed_scan(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(Database.workspace_key_for_user("user-1"))
        track = db.upsert_spotify_tracks([make_track("Song", "Artist", "generation")])[0]
        db.replace_workspace_library_tracks(workspace["id"], [track])
        old_scan = db.create_discovery_scan(
            workspace["id"], scan_key="old", kind="exact", status="complete"
        )
        old_task = db.upsert_discovery_task(
            workspace["id"], scan_id=old_scan, source="test", kind="song", query_key="old"
        )
        stale = make_chart("Song", "Artist", chart_id=980)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, stale, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=old_task,
        )
        current_scan = db.create_discovery_scan(
            workspace["id"], scan_key="current", kind="exact", status="complete"
        )
        current_task = db.upsert_discovery_task(
            workspace["id"], scan_id=current_scan, source="test", kind="song", query_key="current"
        )
        current = make_chart("Song", "Artist", chart_id=981)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, current, score=98,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=current_task,
        )
        stale_id = stale.db_id
    finally:
        db.close()

    payload = client.get("/api/workspace").get_json()
    assert [item["chart_id"] for item in payload["tracks"][0]["candidates"]] == [981]
    rejected = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": stale_id, "action": "select",
    })
    assert rejected.status_code == 400
    assert "does not belong" in rejected.get_json()["error"]

    db = Database(config.database_path)
    try:
        db.create_discovery_scan(
            workspace["id"], scan_key="incomplete", kind="exact", status="partial"
        )
    finally:
        db.close()
    incomplete = client.get("/api/workspace").get_json()
    assert incomplete["tracks"][0]["candidates"] == []
    rejected_current = client.post("/api/selection", json={
        "track_key": track.track_key,
        "candidate_id": current.db_id,
        "action": "select",
    })
    assert rejected_current.status_code == 409
    assert rejected_current.get_json()["reload"] is True


def test_download_requires_selection(client, config):
    _sign_in(client)
    r = client.post("/api/download", json={})
    assert r.status_code in (400, 500)


def _sign_in(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "st"
        sess["oauth_verifier"] = "v"
        sess["oauth_redirect_uri"] = "cb"
    client.get("/api/auth/spotify/callback?code=c&state=st")


def _prepare_discovery(client, config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.sources.enchor import PREFLIGHT_VERSION

    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        db.record_source_preflight(
            {
                "source_name": "enchor",
                "endpoint": "/search",
                "request_version": PREFLIGHT_VERSION,
                "success": True,
                "supported": True,
                "authoritative": True,
                "found": 100,
                "out_of": 100,
                "returned_count": 1,
            },
            ttl_seconds=3600,
        )
    finally:
        db.close()
