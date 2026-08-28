"""End-to-end tests for the multi-user web app (sessions, auth, jobs, download)."""

from __future__ import annotations

import time
import io
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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

    application = create_app(config)
    yield application
    application.extensions["jobs"].shutdown(timeout=2)


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
    assert b"Get every song" not in r.data
    assert b"score the best match" not in r.data
    assert b"Enchor" in r.data


def test_authenticated_review_shell_prioritizes_human_decisions(client):
    _sign_in(client)
    html = client.get("/").get_data(as_text=True)

    assert "Review your matches" in html
    assert "Advanced review settings" in html
    assert '<details class="advanced-review">' in html
    assert 'role="status" aria-live="polite"' in html
    assert "Minimum match score" not in html
    assert "eligibility" not in html


def test_authenticated_shell_contains_bounded_matching_activity(client):
    _sign_in(client)
    html = client.get("/").get_data(as_text=True)
    assert 'id="matchingProgress"' in html
    assert 'id="activityList"' in html
    assert 'id="reviewMatchesBtn"' in html
    assert 'class="review-card"' not in html


def test_matching_template_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    template = Path(__file__).parents[1] / "src/csv_to_clonehero/server/templates/index.html"
    source = template.read_text(encoding="utf-8")
    match = re.search(r"\{% else %\}.*?\{% endif %\}{% endblock %}", source, re.DOTALL)
    assert match is not None
    script = re.search(r"<script>(.*?)</script>", match.group(0), re.DOTALL)
    assert script is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script.group(1))
        path = handle.name
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_anonymous_soundcloud_export_import_is_available_without_spotify_login(client):
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"Track name,Artist name,Soundcloud - id\nArtist - Song,Artist,sc-1\n"), "export.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["combined"]["tracks"] == 1
    assert client.get("/api/sources").status_code == 200


def test_anonymous_import_is_adopted_after_spotify_login(client, config):
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"title,artist,id\nSong,Artist,sc-1\n"), "export.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    _sign_in(client)
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.server.auth import user_workspace_key

    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        assert workspace is not None
        assert db.get_combined_summary(workspace["id"])["tracks"] == 1
    finally:
        db.close()


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
    refreshed = client.post("/api/preflight", json={"force": True})
    assert refreshed.status_code == 200
    assert refreshed.get_json()["preflight"]["out_of"] == 321
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
        track = make_track("Song", "Artist", spotify_id="track-1")
        db.upsert_spotify_tracks([track])
        chart = make_chart("Song", "Artist", chart_id=78)
        db.upsert_candidate(chart)
        db.upsert_workspace_candidate(
            workspace["id"], track.track_key, chart, score=60,
            eligibility="EXCLUDED", decision="REJECT",
            breakdown_json='{"gate_factors": {"hard_gate": false}}', kind="exact",
        )
        hard = make_chart("Song", "Artist", chart_id=79)
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
    selected = client.post("/api/selection", json={
        "track_key": track.track_key, "candidate_id": chart_id, "action": "select",
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
    assert default.get_json()["scope"] == "strong"
    db = Database(config.database_path)
    try:
        selected = db.list_workspace_selections(workspace["id"])
        assert len(selected) == 1
        assert selected[0]["candidate_id"] == strong_id
    finally:
        db.close()

    explicit = client.post(
        "/api/selection/bulk",
        json={"scope": "eligible", "track_ids": [track.track_key]},
    )
    assert explicit.status_code == 200
    assert explicit.get_json()["scope"] == "eligible"
    db = Database(config.database_path)
    try:
        selected = db.list_workspace_selections(workspace["id"])
        assert len(selected) == 1
        assert selected[0]["candidate_id"] == strong_id
        assert selected[0]["candidate_id"] != review_id
    finally:
        db.close()


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


def test_workspace_review_payload_uses_human_states_and_current_selection(client, config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart, make_track
    from csv_to_clonehero.server.auth import user_workspace_key

    _sign_in(client)
    assert client.post("/api/spotify/scope", json={"include_liked": True}).status_code == 200
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(user_workspace_key("user-1"))
        track = make_track("Song", "Artist", spotify_id="review-track", album="Album")
        stored = db.upsert_spotify_tracks([track])[0]
        db.replace_workspace_library_tracks(workspace["id"], [stored])

        strong = make_chart("Song", "Artist", chart_id=701)
        review = make_chart("Song (Live)", "Artist", chart_id=702)
        hard = make_chart("Song", "Artist", chart_id=703, instruments=[], diff_guitar=None)
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
    assert "live-version mismatch" in candidates[702]["review"]["summary"]
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
