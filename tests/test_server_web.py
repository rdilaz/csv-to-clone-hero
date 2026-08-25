"""End-to-end tests for the multi-user web app (sessions, auth, jobs, download)."""

from __future__ import annotations

import time

import pytest

from csv_to_clonehero.server.web import create_app


@pytest.fixture(autouse=True)
def spotify_client_id(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client")


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
    return create_app(config)


@pytest.fixture
def client(app):
    return app.test_client()


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_landing_is_public(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Continue with Spotify" in r.data


def test_api_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/workspace").status_code == 401


def test_login_redirects_to_spotify(client, config):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.spotify.com/authorize" in r.headers["Location"]


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


def test_create_and_poll_job(client, config, monkeypatch):
    _sign_in(client)
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


def test_workspace_empty_before_run(client, config):
    _sign_in(client)
    data = client.get("/api/workspace").get_json()
    assert data["tracks"] == []


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