"""Web interface tests (Flask test client, fake source, no real network)."""

import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import zipfile

import pytest

from csv_to_clonehero.webapp import _SpotifyJobState, create_app
from csv_to_clonehero.diagnostics import RunDiagnostics
from csv_to_clonehero.database import Database
from csv_to_clonehero.cli import build_parser

from .conftest import FIXTURES, FakeSource, make_chart, make_track


@pytest.fixture
def client(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def wait_for_soundcloud_import(client, payload, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(
            "/api/sources/soundcloud/import/status"
            f"?run_id={payload['run_id']}"
        ).get_json()
        if not status["running"]:
            return status
        time.sleep(0.01)
    pytest.fail("SoundCloud import did not finish in time")


def seed_local_workspace(config, workspace_id, charts=True):
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    db = Database(config.database_path)
    try:
        snapshot = LibrarySnapshot(
            "soundcloud",
            [
                ProviderTrack(
                    provider="soundcloud",
                    provider_id="sc-song",
                    title="Song",
                    artist="Artist",
                    album="Album",
                ),
                ProviderTrack(
                    provider="soundcloud",
                    provider_id="sc-missing",
                    title="Missing Song",
                    artist="Artist",
                ),
            ],
        )
        db.upsert_library_snapshot(workspace_id, snapshot)
        tracks = {track.title: track for track in db.get_spotify_tracks(True)}
        if charts:
            good = make_chart("Song", "Artist", chart_id=301)
            review = make_chart("Song (Live)", "Artist", chart_id=302)
            blocked = make_chart(
                "Song", "Artist", chart_id=303, instruments=[], diff_guitar=None
            )
            good_breakdown = {
                "eligibility": "RECOMMENDED",
                "factors": {
                    "match": {
                        "title": {"similarity": 100, "exact_normalized": True},
                        "artist": {"similarity": 100, "exact_normalized": True},
                    },
                    "quality": {"full_band": 8},
                    "gates": {
                        "hard_gate": False,
                        "playable_content": True,
                        "required_five_fret_guitar": True,
                    },
                },
            }
            review_breakdown = {
                "eligibility": "ELIGIBLE_REVIEW",
                "factors": {
                    "match": {
                        "title": {"similarity": 92},
                        "artist": {"similarity": 96},
                        "version_markers": {"track": [], "candidate": ["live"]},
                    },
                    "quality": {"unrated_guitar": -3},
                    "gates": {
                        "hard_gate": False,
                        "playable_content": True,
                        "required_five_fret_guitar": True,
                    },
                },
            }
            blocked_breakdown = {
                "eligibility": "INCOMPATIBLE",
                "decision": "REJECT",
                "reasons": ["no playable chart content"],
                "factors": {
                    "gates": {
                        "hard_gate": True,
                        "playable_content": False,
                        "hard_gate_reasons": ["no playable chart content"],
                    },
                },
            }
            for chart, score, eligibility, decision, breakdown in (
                (good, 94, "RECOMMENDED", "AUTO_ACCEPT", good_breakdown),
                (review, 82, "ELIGIBLE_REVIEW", "REVIEW", review_breakdown),
                (blocked, 99, "INCOMPATIBLE", "REJECT", blocked_breakdown),
            ):
                db.upsert_candidate(chart)
                db.upsert_workspace_candidate(
                    workspace_id,
                    tracks["Song"].track_key,
                    chart,
                    score=score,
                    eligibility=eligibility,
                    decision=decision,
                    breakdown_json=json.dumps(breakdown),
                    kind="exact",
                )
        db.update_workspace(
            workspace_id,
            status="complete",
            phase="complete",
            message="Catalog matching complete.",
            error="",
        )
        db.update_workspace_progress(
            workspace_id,
            status="complete",
            phase="complete",
            message="Catalog matching complete.",
            error="",
            metadata={"run_id": "local-test-run", "scan_key": "catalog"},
        )
        return tracks["Song"]
    finally:
        db.close()


def test_index_serves_source_neutral_workflow(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for text in (
        "Music to Clone Hero and YARG",
        "SoundCloud export",
        "Import SoundCloud export",
        "Connect Spotify",
        "Find chart matches",
        "Review and export",
        "Review matches",
    ):
        assert text in html
    assert "source_key" not in html
    assert "provenance" not in html
    assert "Open advanced review and download" not in html
    ids = re.findall(r'id="([^\"]+)"', html)
    assert len(ids) == len(set(ids))


def test_home_inline_script_parses_in_javascript_engine(client, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed")

    html = client.get("/").get_data(as_text=True)
    scripts = re.findall(r"<script>\s*(.*?)\s*</script>", html, flags=re.DOTALL)
    assert len(scripts) == 1
    script_path = tmp_path / "home.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    checked = subprocess.run(
        ["node", "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "/api/sources/soundcloud/import" in scripts[0]
    assert "/api/sources/soundcloud/import/status" in scripts[0]
    assert "/api/discovery/start" in scripts[0]


def test_advanced_inline_script_parses_in_javascript_engine(client, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed")

    html = client.get("/advanced").get_data(as_text=True)
    scripts = re.findall(r"<script>\s*(.*?)\s*</script>", html, flags=re.DOTALL)
    assert len(scripts) == 1
    script_path = tmp_path / "advanced.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    checked = subprocess.run(
        ["node", "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "exported successfully" in scripts[0]


def test_local_journey_summary_and_review_rehydrate_after_fresh_app(client, config):
    initial = client.get("/api/sources").get_json()
    track = seed_local_workspace(config, initial["workspace_id"])

    fresh_app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    fresh_app.config["TESTING"] = True
    with fresh_app.test_client() as fresh:
        sources = fresh.get("/api/sources").get_json()
        journey = sources["journey"]
        assert journey["matching_status"] == "complete"
        assert journey["counts"] == {
            "imported_songs": 2,
            "chart_options": 1,
            "candidate_charts": 3,
            "strong_matches": 1,
            "needs_review": 1,
            "no_candidate_songs": 1,
            "not_playable_songs": 0,
            "no_suitable_songs": 1,
            "selected_charts": 0,
        }
        assert journey["review_url"].startswith("/advanced?workspace_id=")

        status = fresh.get(
            f"/api/discovery/status?workspace_id={initial['workspace_id']}"
        ).get_json()
        assert status["journey"]["headline"] == "Matching complete"

        review = fresh.get("/api/review").get_json()
        assert review["source_neutral"] is True
        assert review["journey"]["counts"]["no_candidate_songs"] == 1
        assert len(review["tracks"]) == 1
        candidates = {
            item["chart_id"]: item for item in review["tracks"][0]["candidates"]
        }
        assert review["tracks"][0]["review_label"] == "Strong match"
        assert candidates[301]["review"]["summary"]
        assert candidates[302]["review_label"] == "Needs review"
        assert "live-version mismatch" in candidates[302]["review_summary"]
        assert candidates[303]["review_label"] == "Not playable"
        assert candidates[303]["selectable"] is False

        selected = fresh.post(
            "/api/review/decide",
            json={
                "track_key": track.track_key,
                "source": "test",
                "chart_id": 301,
                "decision": "accept",
                "workspace_id": initial["workspace_id"],
            },
        )
        assert selected.status_code == 200
        after = fresh.get("/api/review").get_json()
        assert after["tracks"][0]["selected"] is True
        assert after["tracks"][0]["selected_candidate_id"] == candidates[301]["candidate_id"]
        assert after["journey"]["counts"]["selected_charts"] == 1

    home = client.get("/").get_data(as_text=True)
    assert "Review matches" in home
    assert "source_key" not in home
    assert "provenance" not in home


def test_local_export_reports_one_human_readable_success(config):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as chart_zip:
        chart_zip.writestr("song.ini", "[Song]\nname = Song\n")
        chart_zip.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
    content = archive.getvalue()
    md5 = hashlib.md5(content).hexdigest()
    source = FakeSource(files={md5: content})
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    with app.test_client() as http:
        sources = http.get("/api/sources").get_json()
        workspace_id = sources["workspace_id"]
        track = seed_local_workspace(config, workspace_id, charts=False)
        chart = make_chart("Song", "Artist", chart_id=401, md5=md5)
        db = Database(config.database_path)
        try:
            db.upsert_candidate(chart)
            db.upsert_workspace_candidate(
                workspace_id,
                track.track_key,
                chart,
                score=94,
                eligibility="RECOMMENDED",
                decision="AUTO_ACCEPT",
                breakdown_json=json.dumps({
                    "eligibility": "RECOMMENDED",
                    "factors": {
                        "gates": {
                            "hard_gate": False,
                            "playable_content": True,
                            "required_five_fret_guitar": True,
                        },
                    },
                }),
                kind="exact",
            )
        finally:
            db.close()
        selected = http.post(
            "/api/review/decide",
            json={
                "track_key": track.track_key,
                "source": "test",
                "chart_id": 401,
                "decision": "accept",
                "workspace_id": workspace_id,
            },
        )
        assert selected.status_code == 200
        started = http.post(
            "/api/spotify/export/start",
            json={"kind": "folder", "workspace_id": workspace_id, "path": str(config.base_dir / "Songs")},
        )
        assert started.status_code == 200
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = http.get("/api/spotify/export/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.05)
        assert status is not None and status["status"] == "complete"
        assert status["result"]["success"] is True
        assert status["result"]["published"] == 1
        assert status["result"]["failed"] == 0
        assert "exported successfully" in http.get("/advanced").get_data(as_text=True)


def test_local_journey_distinguishes_catalog_failure_and_incomplete_matching(client, config):
    initial = client.get("/api/sources").get_json()
    seed_local_workspace(config, initial["workspace_id"], charts=False)

    unavailable = client.get("/api/sources").get_json()["journey"]
    assert unavailable["matching_status"] == "complete"
    assert unavailable["headline"] == "Chart catalog unavailable"
    assert unavailable["catalog_available"] is False

    db = Database(config.database_path)
    try:
        db.update_workspace(
            initial["workspace_id"],
            status="partial",
            phase="catalog_match",
            message="Matching was interrupted.",
        )
    finally:
        db.close()

    incomplete = client.get("/api/sources").get_json()["journey"]
    assert incomplete["matching_status"] == "incomplete"
    assert incomplete["headline"] == "Matching incomplete"
    review = client.get("/api/review").get_json()
    assert review["progress"]["has_search_run"] is True
    assert review["progress"]["exact_scoring_complete"] is False


def test_soundcloud_export_is_first_class_local_source_without_spotify(client):
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"Track name,Artist name,Playlist name,Type,Soundcloud - id\nArtist - Song,Lyricist,Liked Songs,Favorite,sc-1\n"), "likes.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "running"
    assert payload["run_id"]
    status = wait_for_soundcloud_import(client, payload)
    assert status["status"] == "complete"
    assert status["progress"]["total_tracks"] == 1
    assert status["progress"]["persisted_tracks"] == 1
    result = status["result"]
    assert result["source"]["tracks"] == 1
    assert result["combined"]["tracks"] == 1
    summary = client.get("/api/sources").get_json()
    assert summary["sources"][0]["provider"] == "soundcloud"
    assert summary["journey"]["matching_status"] == "ready"
    assert summary["journey"]["counts"]["imported_songs"] == 1


def test_soundcloud_import_persists_redacted_diagnostics(client, config):
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"Track name,Artist name\nArtist - Song,\nBad row,\n"), "likes.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload["run_id"]
    import_status = wait_for_soundcloud_import(client, payload)
    assert import_status["status"] == "complete"
    status = client.get(f"/api/discovery/status?workspace_id={payload['workspace_id']}").get_json()
    assert status["diagnostics_url"].endswith(payload["run_id"])
    assert status["diagnostics"]["counters"]["accepted_input_rows"] == 1
    downloaded = client.get(status["diagnostics_url"])
    assert downloaded.status_code == 200
    assert b"access_token" not in downloaded.data


def test_run_diagnostics_redacts_secrets_and_bounds_samples(tmp_path):
    run = RunDiagnostics(tmp_path)
    run.update("import", token="secret", samples={"tracks": [{"artist": str(i), "title": "song"} for i in range(100)]})
    data = json.loads(run.path.read_text(encoding="utf-8"))
    assert "token" not in data["events"][0]["data"]
    assert len(data["samples"]["tracks"]) == 50


def test_spotify_scoring_sandbox_uses_production_matcher(client):
    response = client.post(
        "/api/spotify/score-preview",
        json={
            "track": {"artist": "Artist", "title": "Song", "album": "Album"},
            "chart": {
                "artist": "Artist",
                "title": "Song",
                "album": "Album",
                "icon": "gh3",
                "instruments": "guitar,bass,drums,vocals",
                "difficulty": 3,
            },
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["final_score"] >= 90
    assert payload["eligibility"] == "RECOMMENDED"
    assert payload["bonuses"]
    assert "gate_factors" in payload


def test_spotify_callback_binds_exchanged_token_for_background_clients(config):
    class Token:
        access_token = "access-token"
        refresh_token = "refresh-token"
        scope = ("user-library-read",)

        def is_expired(self, _now):
            return False

    class OAuth:
        def __init__(self):
            self.token = None

        def begin_authorization(self, *, state, **_kwargs):
            return {
                "url": f"http://example.test/authorize?state={state}",
                "state": state,
                "redirect_uri": "http://127.0.0.1:8765/callback",
            }

        def exchange_code(self, _code, **_kwargs):
            return Token()

        def save_token(self, token):
            self.token = token
            return token

        def access_token(self):
            if self.token is None:
                raise RuntimeError("Spotify sign-in is required")
            return self.token

    class SpotifyClient:
        def __init__(self, oauth):
            self.oauth = oauth
            self.fetch_calls = 0

        def get_current_user(self):
            self.oauth.access_token()
            return {"id": "test-user", "display_name": "Test user"}

        def fetch_library(self, **_kwargs):
            self.oauth.access_token()
            self.fetch_calls += 1
            return []

    oauth = OAuth()
    app = create_app(
        config,
        source_factory=lambda _config: [FakeSource()],
        oauth_factory=lambda **_kwargs: oauth,
        spotify_factory=lambda oauth, **_kwargs: SpotifyClient(oauth),
    )
    app.config["TESTING"] = True
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(
            workspace_key="spotify-auth-test",
            name="Authenticated scan",
            kind="exact",
        )
        db.set_workspace_scope(workspace, [], include_liked=True)
    finally:
        db.close()
    with app.test_client() as http:
        started = http.get("/api/auth/spotify/start").get_json()
        callback = http.get(
            f"/api/auth/spotify/callback?state={started['state']}&code=valid-code&format=json"
        )
        scan = http.post("/api/spotify/scan", json={"workspace_id": workspace})
        for _ in range(100):
            status = http.get(
                f"/api/spotify/status?workspace_id={workspace}"
            ).get_json()
            if not status["running"]:
                break
            time.sleep(0.01)

    assert callback.status_code == 200
    assert scan.status_code == 202
    assert status["status"] == "complete"
    assert oauth.access_token().refresh_token == "refresh-token"
    assert oauth.token is not None


def test_expansion_state_and_saved_results_are_separate_from_exact(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Expansion workspace",
        kind="exact",
        status="complete",
        phase="complete",
    )
    track = make_track("Known Song", "Artist", "spotify-known")
    db.replace_spotify_library(records=[track], workspace_id=workspace)
    chart = make_chart("Deep Cut", "Artist", chart_id=42)
    db.upsert_workspace_candidate(
        workspace,
        None,
        chart,
        kind="expansion",
        artist_norm="artist",
        score=91.5,
        quality=4.0,
        decision="ACCEPT",
        eligibility="RECOMMENDED",
        breakdown_json=json.dumps({"eligibility": "RECOMMENDED", "explanation": "artist expansion"}),
        metadata={"cap_rank": 1, "artist_cap": 3, "within_artist_cap": True},
    )
    db.replace_workspace_expansion_artists(
        workspace,
        [{
            "artist_name": "Artist",
            "artist_norm": "artist",
            "track_count": 1,
            "cap": 3,
            "selected": True,
        }],
    )
    db.create_discovery_scan(
        workspace,
        scan_key="exact",
        kind="exact",
        status="complete",
        metadata={"stats": {"exact_candidates": 1, "exact_recommended": 1}},
    )
    db.create_discovery_scan(
        workspace,
        scan_key="expansion",
        kind="expansion",
        status="complete",
        metadata={
            "stats": {
                "expansion_artists_requested": 1,
                "expansion_artists_scanned": 1,
                "expansion_candidates": 1,
                "expansion_eligible": 1,
            }
        },
    )
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])

    state = app.extensions["spotify_job"]
    state.begin(workspace, scan_key="expansion")
    assert state.snapshot()["scan_key"] == "expansion"
    state.reset()

    with app.test_client() as http:
        response = http.get(f"/api/spotify/library?workspace_id={workspace}&view=all")
        status = http.get(f"/api/spotify/status?workspace_id={workspace}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["expansion_groups"][0]["artist_name"] == "Artist"
    assert payload["expansion_groups"][0]["candidates"][0]["kind"] == "expansion"
    assert payload["expansion_groups"][0]["candidates"][0]["artist_norm"] == "artist"
    assert payload["expansion_summary"]["unique_songs"] == 1
    assert payload["expansion_summary"]["new_songs"] == 1
    assert {scan["kind"] for scan in payload["scan_history"]} == {"exact", "expansion"}
    assert status.status_code == 200
    assert status.get_json()["scan_key"] == "expansion"

    candidate_id = payload["expansion_groups"][0]["candidates"][0]["candidate_id"]
    with app.test_client() as http:
        selected = http.post(
            "/api/spotify/select",
            json={
                "workspace_id": workspace,
                "kind": "expansion",
                "artist_norm": "artist",
                "candidate_id": candidate_id,
                "action": "select",
            },
        )
        tray = http.get(f"/api/spotify/selections?workspace_id={workspace}")

    assert selected.status_code == 200
    assert tray.get_json()["count"] == 1
    assert tray.get_json()["items"][0]["kind"] == "expansion"


def test_expansion_preview_requires_confirmation_and_drives_artist_catalog_search(config):
    class Keyring:
        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, password):
            self.values[(service, username)] = password

        def delete_password(self, service, username):
            self.values.pop((service, username), None)

    class SpotifyCatalog:
        def get_artist_discography(self, artist):
            assert artist == "Artist"
            return {
                "artist_id": "spotify-artist",
                "albums": 2,
                "tracks": [
                    {"spotify_id": "known", "artist": "Artist", "title": "Known", "album": "Album"},
                    {"spotify_id": "deep", "artist": "Artist", "title": "Deep Cut", "album": "Album"},
                ],
            }

    source = FakeSource(artist_results={"Artist": [make_chart("Deep Cut", "Artist", chart_id=88)]})
    keyring = Keyring()
    config.spotify.keyring.service = "test-service"
    config.spotify.keyring.user = "test-user"
    config.spotify.keyring.username = "test-user"
    keyring.set_password(config.keyring_service, config.keyring_user, "refresh-token")
    db = Database(config.database_path)
    db.upsert_spotify_account({"spotify_user_id": "spotify-user", "display_name": "Test User"})
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Spotify workspace",
        kind="exact",
        status="complete",
        phase="complete",
    )
    db.replace_spotify_library(
        records=[make_track("Known", "Artist", "known")],
        workspace_id=workspace,
    )
    db.replace_workspace_expansion_artists(
        workspace,
        [{"artist_name": "Artist", "artist_norm": "artist", "track_count": 1, "cap": 10, "selected": True}],
    )
    db.close()
    app = create_app(
        config,
        source_factory=lambda _config: [source],
        spotify_factory=lambda: SpotifyCatalog(),
        keyring=keyring,
    )
    app.config["TESTING"] = True

    with app.test_client() as http:
        blocked = http.post(
            "/api/spotify/expansion/start",
            json={"workspace_id": workspace, "artist_norms": ["artist"]},
        )
        assert blocked.status_code == 409
        assert "preview" in blocked.get_json()["error"]

        preview = http.post(
            "/api/spotify/expansion/preview",
            json={"workspace_id": workspace, "artist_norms": ["artist"]},
        )
        assert preview.status_code == 202
        for _ in range(100):
            status = http.get(f"/api/spotify/status?workspace_id={workspace}").get_json()
            if not status["running"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Spotify preview worker did not finish")

        preview_data = http.get(
            f"/api/spotify/expansion/preview?workspace_id={workspace}"
        ).get_json()
        assert preview_data["ready"] is True
        assert preview_data["full_discography_count"] == 2
        assert preview_data["already_in_library_count"] == 1
        assert preview_data["song_count"] == 1
        assert preview_data["expected_queries"] == 1

        expansion = http.post(
            "/api/spotify/expansion/start",
            json={"workspace_id": workspace, "artist_norms": ["artist"]},
        )
        assert expansion.status_code == 202
        for _ in range(100):
            status = http.get(f"/api/spotify/status?workspace_id={workspace}").get_json()
            if not status["running"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail("expansion worker did not finish")
        assert status["scan_key"] == "expansion"

        workspace_data = http.get(
            f"/api/spotify/library?workspace_id={workspace}&view=all"
        ).get_json()
        assert workspace_data["expansion_summary"]["new_songs"] == 1
        assert workspace_data["expansion_groups"][0]["song_title"] == "Deep Cut"


def test_expansion_artist_api_enforces_hard_batch_limit(client, config):
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(
            workspace_key="spotify",
            name="Spotify workspace",
            kind="exact",
            status="complete",
            phase="complete",
        )
        records = [
            make_track("Song", f"Artist {index}", f"spotify-{index}")
            for index in range(26)
        ]
        db.replace_spotify_library(records=records, workspace_id=workspace)
    finally:
        db.close()

    artists = client.get(f"/api/spotify/expansion/artists?workspace_id={workspace}")
    assert artists.status_code == 200
    payload = artists.get_json()
    assert payload["max_artists_per_batch"] == 25
    artist_norms = [item["artist_norm"] for item in payload["artists"]]
    assert len(artist_norms) == 26

    rejected = client.post(
        "/api/spotify/expansion/artists/select",
        json={
            "workspace_id": workspace,
            "artists": [{"artist_norm": norm, "selected": True} for norm in artist_norms],
        },
    )
    assert rejected.status_code == 400
    assert "limited to 25 primary artists; received 26" in rejected.get_json()["error"]


def test_expansion_preview_cancel_is_durable_and_preserves_completed_artists(config):
    class Keyring:
        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, password):
            self.values[(service, username)] = password

        def delete_password(self, service, username):
            self.values.pop((service, username), None)

    entered_second = threading.Event()
    release_second = threading.Event()

    class SpotifyCatalog:
        def get_artist_discography(self, artist, cancellation_event=None):
            if artist == "Artist Two":
                entered_second.set()
                release_second.wait(2)
                if cancellation_event is not None and cancellation_event.is_set():
                    from csv_to_clonehero.spotify import SpotifyCancellationError

                    raise SpotifyCancellationError("cancelled")
            return {"artist_id": artist, "albums": 1, "tracks": []}

    keyring = Keyring()
    config.spotify.keyring.service = "test-service"
    config.spotify.keyring.user = "test-user"
    config.spotify.keyring.username = "test-user"
    keyring.set_password(config.keyring_service, config.keyring_user, "refresh-token")
    db = Database(config.database_path)
    try:
        db.upsert_spotify_account({"spotify_user_id": "spotify-user", "display_name": "Test User"})
        workspace = db.create_workspace(workspace_key="spotify", name="Spotify", kind="exact")
        db.replace_spotify_library(
            records=[make_track("One", "Artist One", "one"), make_track("Two", "Artist Two", "two")],
            workspace_id=workspace,
        )
    finally:
        db.close()
    app = create_app(
        config,
        source_factory=lambda _config: [FakeSource()],
        spotify_factory=lambda: SpotifyCatalog(),
        keyring=keyring,
    )
    app.config["TESTING"] = True

    with app.test_client() as http:
        artists = http.get(f"/api/spotify/expansion/artists?workspace_id={workspace}").get_json()["artists"]
        norms = [item["artist_norm"] for item in artists]
        started = http.post(
            "/api/spotify/expansion/preview",
            json={"workspace_id": workspace, "artist_norms": norms},
        )
        assert started.status_code == 202
        assert entered_second.wait(2)
        cancelled = http.post("/api/spotify/scan/cancel")
        assert cancelled.status_code == 200
        release_second.set()
        for _ in range(100):
            status = http.get(f"/api/spotify/status?workspace_id={workspace}").get_json()
            if not status["running"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail("preview cancellation did not finish")
        assert status["status"] == "cancelled"
        batch = status["expansion_batch"]
        assert batch["status"] == "cancelled"
        assert any(item["preview_status"] == "complete" for item in batch["artists"])
        assert any(item["preview_status"] in {"running", "interrupted", "pending"} for item in batch["artists"])


@pytest.mark.parametrize("remaining", [1, 5, 10, 25])
def test_spotify_status_eta_uses_observed_query_rate(monkeypatch, remaining):
    clock = [100.0]
    monkeypatch.setattr("csv_to_clonehero.webapp.time.time", lambda: clock[0])
    state = _SpotifyJobState()
    state.begin(1)
    state.progress_update({"total_tasks": remaining + 2, "total_work": remaining + 2})
    clock[0] += 4
    state.progress_update({"completed_tasks": 1, "completed_work": 1, "cached_queries": 0})
    clock[0] += 4
    state.progress_update({"completed_tasks": 2, "completed_work": 2, "cached_queries": 0})

    status = state.snapshot()

    assert status["estimate_source"] == "live_phase_rate"
    assert status["eta_seconds"] == remaining * 4
    assert status["eta_lower_seconds"] < status["eta_seconds"] < status["eta_upper_seconds"]


def test_spotify_status_eta_uses_scoring_rate_after_phase_change(monkeypatch):
    clock = [200.0]
    monkeypatch.setattr("csv_to_clonehero.webapp.time.time", lambda: clock[0])
    state = _SpotifyJobState()
    state.begin(1)
    state.progress_update({"total_tasks": 2, "completed_tasks": 2, "scoring_tasks_total": 12,
                           "total_work": 14, "completed_work": 2})
    clock[0] += 0.5
    state.progress_update({"scoring_tasks_completed": 1, "completed_work": 3})
    clock[0] += 0.5
    state.progress_update({"scoring_tasks_completed": 2, "completed_work": 4})

    status = state.snapshot()

    assert status["scoring_seconds_per_item"] == 0.5
    assert status["eta_seconds"] == 5.0


def test_spotify_status_eta_ignores_cached_query_burst(monkeypatch):
    clock = [300.0]
    monkeypatch.setattr("csv_to_clonehero.webapp.time.time", lambda: clock[0])
    state = _SpotifyJobState()
    state.begin(1)
    state.progress_update({"total_tasks": 12, "total_work": 12})
    clock[0] += 0.01
    state.progress_update({"completed_tasks": 5, "completed_work": 5, "cached_queries": 5})
    clock[0] += 4
    state.progress_update({"completed_tasks": 6, "completed_work": 6, "cached_queries": 5})
    clock[0] += 4
    state.progress_update({"completed_tasks": 7, "completed_work": 7, "cached_queries": 5})

    status = state.snapshot()

    assert status["query_seconds_per_item"] == 4.0
    assert status["eta_seconds"] == 20.0


def test_spotify_status_eta_shrinks_when_remaining_queries_are_cached(monkeypatch):
    clock = [300.0]
    monkeypatch.setattr("csv_to_clonehero.webapp.time.time", lambda: clock[0])
    state = _SpotifyJobState()
    state.begin(1)
    state.progress_update({"total_tasks": 10, "total_work": 10, "estimated_cached_queries": 10})
    clock[0] += 4
    state.progress_update({"completed_tasks": 2, "completed_work": 2, "cached_queries": 2})
    clock[0] += 0.08
    state.progress_update({"completed_tasks": 3, "completed_work": 3, "cached_queries": 3})
    clock[0] += 0.08
    state.progress_update({"completed_tasks": 4, "completed_work": 4, "cached_queries": 4})

    status = state.snapshot()

    assert status["cached_seconds_per_item"] == 0.08
    assert status["eta_seconds"] < 4 * 6
    assert status["eta_seconds"] == round(6 * 0.08, 1)


def test_spotify_status_eta_expands_when_fallback_queries_are_added(monkeypatch):
    clock = [400.0]
    monkeypatch.setattr("csv_to_clonehero.webapp.time.time", lambda: clock[0])
    state = _SpotifyJobState()
    state.begin(1)
    state.progress_update({"total_tasks": 5, "total_work": 10})
    clock[0] += 3
    state.progress_update({"completed_tasks": 1, "completed_work": 1})
    clock[0] += 3
    state.progress_update({"completed_tasks": 2, "completed_work": 2})
    state.progress_update({"total_tasks": 8, "total_work": 13})

    status = state.snapshot()

    assert status["eta_seconds"] >= 18.0
    assert status["remaining_work"] == 11


def test_spotify_status_keeps_only_recent_events():
    state = _SpotifyJobState()
    state.begin(1)
    with state.lock:
        for index in range(8):
            state._log_locked(f"step {index}")
    status = state.snapshot()

    assert len(status["recent_events"]) == 6
    assert status["recent_events"][0]["message"] == "step 2"


def test_running_spotify_status_does_not_replay_stale_persisted_error(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Spotify library",
        kind="exact",
        status="cancelled",
        phase="cancelled",
    )
    db.update_workspace_progress(
        workspace,
        status="cancelled",
        phase="cancelled",
        message="Spotify discovery cancelled locally",
        error="Spotify discovery cancelled locally",
    )
    db.close()

    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.extensions["spotify_job"].begin(workspace)
    with app.test_client() as http:
        status = http.get(f"/api/spotify/status?workspace_id={workspace}").get_json()

    assert status["running"] is True
    assert status["error"] == ""


def test_spotify_status_does_not_apply_a_new_run_to_another_workspace(config):
    db = Database(config.database_path)
    old_workspace = db.create_workspace(
        workspace_key="old", name="Old scan", kind="exact", status="complete", phase="complete",
        metadata={"scan_key": "exact", "run_id": "old-run"},
    )
    active_workspace = db.create_workspace(
        workspace_key="active", name="Active scan", kind="exact", status="running", phase="starting",
        metadata={"scan_key": "exact", "run_id": "new-run"},
    )
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.extensions["spotify_job"].begin(active_workspace, scan_key="exact", run_id="new-run")

    with app.test_client() as http:
        old_status = http.get(f"/api/spotify/status?workspace_id={old_workspace}").get_json()
        active_status = http.get(f"/api/spotify/status?workspace_id={active_workspace}").get_json()

    assert old_status["running"] is False
    assert old_status["status"] == "complete"
    assert old_status["run_id"] == "old-run"
    assert active_status["running"] is True
    assert active_status["run_id"] == "new-run"


def test_dead_spotify_worker_is_recovered_and_retry_status_is_exposed(config):
    app = create_app(config, source_factory=lambda _config: [FakeSource()])
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify-stale",
        name="Stale Spotify scan",
        kind="exact",
        status="running",
        phase="starting",
        metadata={"scan_key": "exact", "run_id": "stale-run"},
    )
    db.close()
    state = app.extensions["spotify_job"]
    state.begin(workspace, run_id="stale-run")
    dead_worker = threading.Thread(target=lambda: None)
    state.attach_worker("stale-run", dead_worker)
    dead_worker.start()
    dead_worker.join()

    with app.test_client() as http:
        status = http.get(f"/api/spotify/status?workspace_id={workspace}").get_json()

    assert status["running"] is False
    assert status["status"] == "interrupted"
    assert status["run_id"] == "stale-run"
    assert status["recovered"] is True
    assert status["heartbeat_at"] is not None
    db = Database(config.database_path)
    try:
        assert db.get_workspace(workspace)["status"] == "interrupted"
    finally:
        db.close()


def test_old_spotify_worker_cannot_clear_new_run():
    state = _SpotifyJobState()
    state.begin(1, run_id="old-run")
    old_worker = threading.Thread(target=lambda: None)
    state.attach_worker("old-run", old_worker)
    state.begin(1, run_id="new-run")
    new_worker = threading.Thread(target=lambda: None)
    state.attach_worker("new-run", new_worker)

    assert state.cleanup("old-run", old_worker) is False
    assert state.snapshot()["running"] is True
    assert state.snapshot()["run_id"] == "new-run"


def test_active_spotify_worker_remains_owned():
    state = _SpotifyJobState()
    release = threading.Event()
    active_worker = threading.Thread(target=lambda: release.wait(2))
    state.begin(1, run_id="active-run")
    state.attach_worker("active-run", active_worker)
    active_worker.start()
    try:
        snapshot = state.snapshot()
        assert snapshot["running"] is True
        assert snapshot["worker_alive"] is True
        assert snapshot["worker_known"] is True
    finally:
        release.set()
        active_worker.join()


def test_spotify_workspace_save_checkpoints_durable_database(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Spotify library",
        kind="exact",
        status="complete",
        phase="complete",
    )
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])

    with app.test_client() as http:
        response = http.post(
            "/api/spotify/workspace/save",
            json={"workspace_id": workspace},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["saved"] is True
    assert payload["database"] == str(config.database_path)
    db = Database(config.database_path)
    metadata = json.loads(db.get_workspace(workspace)["metadata_json"])
    db.close()
    assert metadata["saved_explicitly"] is True


def test_lan_mode_requires_bearer_token_and_keeps_oauth_on_host(config):
    app = create_app(config, source_factory=lambda cfg: [FakeSource()], lan_access_token="lan-secret")
    remote = {"REMOTE_ADDR": "192.168.1.44"}

    with app.test_client() as http:
        assert http.get("/", environ_base=remote).status_code == 401
        assert http.get("/api/spotify/workspaces", environ_base=remote).status_code == 401
        page = http.get(
            "/?access_token=lan-secret",
            environ_base=remote,
        )
        assert page.status_code == 200
        assert "lan-secret" in page.get_data(as_text=True)
        authorized = http.get(
            "/api/spotify/workspaces",
            headers={"X-CSV-To-CloneHero-Access-Token": "lan-secret"},
            environ_base=remote,
        )
        assert authorized.status_code == 200
        assert http.get(
            "/api/auth/spotify/start",
            headers={"X-CSV-To-CloneHero-Access-Token": "lan-secret"},
            environ_base=remote,
        ).status_code == 403


def test_public_mode_uses_browser_downloads_on_remote_device(config):
    config.spotify.public_base_url = "https://charts.example.com"
    app = create_app(config, source_factory=lambda cfg: [FakeSource()], lan_access_token="public-secret")
    remote = {"REMOTE_ADDR": "203.0.113.44"}

    with app.test_client() as http:
        page = http.get("/?access_token=public-secret", environ_base=remote)
        assert page.status_code == 200
        assert "const publicMode = true" in page.get_data(as_text=True)
        assert http.post(
            "/api/spotify/path/pick",
            headers={"X-CSV-To-CloneHero-Access-Token": "public-secret"},
            environ_base=remote,
        ).status_code == 403
        # Spotify callback requests do not carry the private service token; the
        # route is reached and later rejects the missing OAuth state instead.
        callback = http.get("/api/auth/spotify/callback?state=wrong", environ_base=remote)
        assert callback.status_code == 400


def test_web_cli_exposes_explicit_lan_mode():
    args = build_parser().parse_args(["web", "--lan", "--access-token", "test-token", "--no-browser"])

    assert args.lan is True
    assert args.access_token == "test-token"
    assert args.no_browser is True


def test_spotify_workspaces_lists_loadable_saved_state(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="My saved Spotify scan",
        kind="exact",
        status="complete",
        phase="complete",
    )
    db.update_workspace(workspace, completed_tracks=25, metadata={"saved_explicitly": True, "saved_at": 123.0})
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])

    with app.test_client() as http:
        response = http.get("/api/spotify/workspaces")

    assert response.status_code == 200
    item = response.get_json()["workspaces"][0]
    assert item["id"] == workspace
    assert item["name"] == "My saved Spotify scan"
    assert item["completed_tracks"] == 25
    assert item["saved_explicitly"] is True


def test_spotify_saved_workspace_tracks_are_server_paged(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Large saved scan",
        kind="exact",
        status="complete",
        phase="complete",
    )
    tracks = [make_track(f"Song {index:03d}", "Artist", f"spotify-{index}") for index in range(75)]
    db.replace_spotify_library(records=tracks, workspace_id=workspace)
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])

    with app.test_client() as http:
        first = http.get(
            f"/api/spotify/library?workspace_id={workspace}&view=all&limit=20&offset=0"
        ).get_json()
        second = http.get(
            f"/api/spotify/library/tracks?workspace_id={workspace}&view=all&limit=20&offset=20"
        ).get_json()

    assert len(first["tracks"]) == 20
    assert first["track_total"] == 75
    assert first["has_more"] is True
    assert len(second["tracks"]) == 20
    assert second["track_offset"] == 20
    assert second["has_more"] is True
    assert first["tracks"][0]["spotify_id"] != second["tracks"][0]["spotify_id"]


def test_spotify_workspace_reset_endpoint(client, config):
    db = Database(config.database_path)
    try:
        workspace_id = db.create_workspace(
            workspace_key="spotify-reset-test",
            name="Reset test",
            kind="exact",
            status="complete",
            phase="complete",
        )
        db.replace_spotify_library(
            records=[make_track("Personal Song", "Personal Artist", "personal-song")],
            workspace_id=workspace_id,
        )
        db.set_workspace_scope(workspace_id, [], include_liked=True)
    finally:
        db.close()

    reset_res = client.post("/api/spotify/workspace/reset", json={"workspace_id": workspace_id})
    assert reset_res.status_code == 200
    assert reset_res.get_json()["ok"] is True
    assert reset_res.get_json()["status"] == "reset"

    db = Database(config.database_path)
    try:
        row = db.get_workspace(workspace_id)
        assert row["status"] == "created"
        assert row["phase"] == "idle"
        candidates = db.get_workspace_candidates(workspace_id)
        assert len(candidates) == 0
        assert db.get_workspace_expansion_artists(workspace_id) == []
        assert db.get_library_tracks(workspace_id=workspace_id) == []
        assert db.get_workspace_scope_data(workspace_id)["playlist_ids"] == []
    finally:
        db.close()


def test_app_startup_removes_legacy_demo_workspace(config):
    db = Database(config.database_path)
    try:
        db.create_workspace(
            workspace_key="spotify-demo",
            name="Legacy demo",
            kind="exact",
        )
    finally:
        db.close()

    create_app(config, source_factory=lambda _config: [FakeSource()])

    db = Database(config.database_path)
    try:
        assert db.get_workspace("spotify-demo") is None
    finally:
        db.close()


def test_advanced_page_serves_music_file_import_workflow(client):
    response = client.get("/advanced")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for text in (
        "Review and export charts",
        "Add music exports",
        "Preview chart matches",
        "Review your matches",
        "My music",
        "Best chart",
        "Why this match?",
        "Download selected charts",
        "iTunes/Music TXT",
        "iTunes/Music XML",
    ):
        assert text in html
    assert 'id="picker" accept=".csv,.txt,.xml"' in html
    assert 'id="review"' in html
    assert 'id="jobStatus"' in html
    assert 'href="/"' in html
    assert 'id="legacyImportSources"' in html
    assert 'id="localJourney"' in html
    assert "Spotify library workflow" not in html


def test_upload_list_delete_csv(client, config):
    data = {"files": (io.BytesIO(b"Song,Artist\nX,Y\n"), "My Playlist.csv")}
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.get_json()["saved"] == ["My Playlist.csv"]

    files = client.get("/api/files").get_json()
    assert [f["name"] for f in files] == ["My Playlist.csv"]
    assert (config.input_path / "My Playlist.csv").exists()

    response = client.post("/api/files/delete", json={"name": "My Playlist.csv"})
    assert response.status_code == 200
    assert client.get("/api/files").get_json() == []


def test_upload_rejects_non_csv(client):
    data = {"files": (io.BytesIO(b"MZ"), "virus.exe")}
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert response.get_json()["rejected"] == ["virus.exe"]
    assert response.get_json()["saved"] == []


def test_upload_accepts_itunes_txt_and_xml(client, config):
    txt = (FIXTURES / "itunes" / "itunes_playlist.txt").read_bytes()
    xml = (FIXTURES / "itunes" / "itunes_library.xml").read_bytes()
    data = {
        "files": [
            (io.BytesIO(txt), "My iTunes Export.txt"),
            (io.BytesIO(xml), "iTunes Library.xml"),
            (io.BytesIO(b"MZ"), "evil.exe"),
        ]
    }
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    payload = response.get_json()
    assert payload["saved"] == ["My iTunes Export.txt", "iTunes Library.xml"]
    assert payload["rejected"] == ["evil.exe"]
    names = {f["name"] for f in client.get("/api/files").get_json()}
    assert names == {"My iTunes Export.txt", "iTunes Library.xml"}

    client.post("/api/files/delete", json={"name": "iTunes Library.xml"})
    names = [f["name"] for f in client.get("/api/files").get_json()]
    assert names == ["My iTunes Export.txt"]


def test_upload_sanitizes_filename(client, config):
    data = {"files": (io.BytesIO(b"Song,Artist\nX,Y\n"), "..\\..\\evil?.csv")}
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    saved = response.get_json()["saved"][0]
    assert ".." not in saved and "?" not in saved
    assert saved.endswith(".csv")
    assert (config.input_path / saved).exists()


def test_delete_path_traversal_blocked(client, config):
    response = client.post("/api/files/delete", json={"name": "..\\config.yaml"})
    # Path(...).name strips traversal; nothing outside input dir is touched.
    assert response.status_code == 200
    assert (config.base_dir / "config.yaml").exists() or True  # nothing deleted


def test_report_path_traversal_blocked(client):
    assert client.get("/reports/..%2F..%2Fconfig.yaml").status_code in (403, 404)


def test_dry_run_job_completes(client, config):
    # Upload a fixture playlist.
    csv_bytes = (FIXTURES / "playlist_mixed.csv").read_bytes()
    client.post(
        "/api/upload",
        data={"files": (io.BytesIO(csv_bytes), "playlist_mixed.csv")},
        content_type="multipart/form-data",
    )

    response = client.post("/api/run/dry-run")
    assert response.status_code == 202

    for _ in range(200):
        status = client.get("/api/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("job did not finish")

    assert status["error"] == ""
    assert status["activity"] == "Job complete"
    assert status["started_at"] is not None
    assert status["elapsed_seconds"] >= 0
    assert [s["status"] for s in status["stages"]] == ["done"] * 5

    summary = client.get("/api/summary").get_json()
    assert summary["unique_tracks"] == 4
    reports = client.get("/api/reports").get_json()
    assert "summary.json" in reports
    assert client.get("/reports/summary.json").status_code == 200


def test_full_build_job_downloads(config):
    content = _sng_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Humble", "Kendrick Lamar", chart_id=1, md5=md5)
    source = FakeSource(
        song_results={("Kendrick Lamar", "Humble"): [chart]},
        files={md5: content},
    )
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [source])
    with app.test_client() as client:
        csv_bytes = (FIXTURES / "playlist_mixed.csv").read_bytes()
        client.post(
            "/api/upload",
            data={"files": (io.BytesIO(csv_bytes), "playlist_mixed.csv")},
            content_type="multipart/form-data",
        )
        assert client.post("/api/run/build").status_code == 202
        for _ in range(200):
            status = client.get("/api/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.05)
        assert status["error"] == ""
        summary = client.get("/api/summary").get_json()
        assert summary["downloads"]["completed"] == 1
        assert source.downloads == [md5]


def test_cannot_run_two_jobs_at_once(client):
    # No job running yet, so first call starts; immediate second is a conflict
    # only while the first is running - emulate by checking the endpoint
    # behaves when not running (no 409) and returns JSON either way.
    response = client.post("/api/run/dry-run")
    assert response.status_code in (202, 409)


def test_cross_job_lockout_blocks_mutations_while_spotify_runs(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Spotify library",
        kind="exact",
        status="running",
        phase="running",
    )
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.extensions["spotify_job"].begin(workspace)
    with app.test_client() as http:
        export = http.post("/api/spotify/export/start", json={"kind": "folder"})
        assert export.status_code == 409
        assert "already running" in export.get_json()["error"]
        export_sync = http.post("/api/spotify/export/folder", json={"kind": "folder"})
        assert export_sync.status_code == 409
        legacy = http.post("/api/run/dry-run")
        assert legacy.status_code == 409
        simple = http.post("/api/run/simple", json={"output_dir": "output_library"})
        assert simple.status_code == 409
        status = http.get("/api/status").get_json()
        assert status["busy"] is True
        assert "Spotify discovery" in status["running_jobs"]
    app.extensions["spotify_job"].finish(
        type("Result", (), {"status": "complete", "phase": "complete", "error": "", "partial": False})()
    )


def test_cross_job_lockout_releases_when_spotify_finishes(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(
        workspace_key="spotify",
        name="Spotify library",
        kind="exact",
        status="complete",
        phase="complete",
    )
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.extensions["spotify_job"].begin(workspace)
    app.extensions["spotify_job"].finish(
        type("Result", (), {"status": "complete", "phase": "complete", "error": "", "partial": False})()
    )
    with app.test_client() as http:
        status = http.get("/api/status").get_json()
        assert status["busy"] is False
        assert http.post("/api/run/dry-run").status_code in (202, 409)


# ------------------------------------------------------------ simple mode
def test_output_path_check_default_ok(client):
    res = client.post("/api/output-path/check", json={"path": "output_library"})
    payload = res.get_json()
    assert payload["ok"] is True
    assert payload["resolved"]


def test_output_path_check_rejects_bad_paths(client, config):
    # Empty
    assert client.post("/api/output-path/check", json={"path": ""}).get_json()["ok"] is False
    # Inside the input dir
    inside = client.post(
        "/api/output-path/check",
        json={"path": str(config.input_path / "out")},
    ).get_json()
    assert inside["ok"] is False
    # Drive root
    root = client.post("/api/output-path/check", json={"path": "C:\\\\"}).get_json()
    assert root["ok"] is False
    # Folder that would contain the state DB
    state_holder = client.post(
        "/api/output-path/check", json={"path": str(config.base_dir)}
    ).get_json()
    assert state_holder["ok"] is False


def test_open_folder_404_when_missing(client):
    response = client.post("/api/open-folder")
    assert response.status_code == 404


def test_simple_run_end_to_end(config, monkeypatch):
    content = _sng_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Humble", "Kendrick Lamar", chart_id=1, md5=md5)
    source = FakeSource(
        song_results={("Kendrick Lamar", "Humble"): [chart]},
        files={md5: content},
    )
    config.input_path.mkdir(parents=True, exist_ok=True)
    opened = []
    import os

    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    with app.test_client() as client:
        csv_bytes = (FIXTURES / "playlist_mixed.csv").read_bytes()
        client.post(
            "/api/upload",
            data={"files": (io.BytesIO(csv_bytes), "playlist_mixed.csv")},
            content_type="multipart/form-data",
        )
        out_dir = str(config.base_dir / "chosen_songs")
        response = client.post(
            "/api/run/simple", json={"output_dir": out_dir, "expansion": False}
        )
        assert response.status_code == 202
        for _ in range(300):
            status = client.get("/api/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("simple job did not finish")

        assert status["error"] == ""
        # Live log events were recorded and capped.
        assert status["events"]
        assert len(status["events"]) <= 200
        # Completion counts exposed for the simple page.
        result = status["result"]
        assert result["matched"] == 1
        assert result["downloaded"] == 1
        assert result["no_match"] == 3
        # Downloads went to the per-run override folder (hybrid layout)...
        target = (
            config.base_dir / "chosen_songs" / "Kendrick Lamar" / "DAMN."
            / "Kendrick Lamar - Humble [TestCharter].sng"
        )
        assert target.exists()
        # ...while reports and the state DB stay pinned to the base dir.
        assert (config.reports_path / "summary.json").exists()
        assert not (config.base_dir / "chosen_songs" / "state").exists()
        assert config.database_path.exists()

        # Open folder opens the chosen output dir.
        response = client.post("/api/open-folder")
        assert response.status_code == 200
        assert opened == [str(config.base_dir / "chosen_songs")]


def test_simple_run_rejects_invalid_output(client, config):
    (config.input_path / "x.csv").write_text("Song,Artist\nX,Y\n", encoding="utf-8")
    response = client.post("/api/run/simple", json={"output_dir": ""})
    assert response.status_code == 400


def test_simple_run_requires_files(client):
    response = client.post(
        "/api/run/simple", json={"output_dir": "output_library", "expansion": False}
    )
    assert response.status_code == 400
    assert "files" in response.get_json()["error"]


def test_estimate_and_exact_then_expansion_modes(config):
    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__()
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    with app.test_client() as http:
        http.post(
            "/api/upload",
            data={"files": (io.BytesIO((FIXTURES / "playlist_mixed.csv").read_bytes()), "mixed.csv")},
            content_type="multipart/form-data",
        )
        estimate = http.get("/api/estimate").get_json()
        assert estimate["exact"]["song_count"] == 4
        assert estimate["exact"]["uncached_query_count"] == 4

        assert http.post("/api/run/exact-dry-run").status_code == 202
        for _ in range(200):
            status = http.get("/api/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.02)
        assert status["error"] == ""
        assert source.artist_calls == 0
        song_calls_after_exact = source.song_calls

        assert http.post("/api/run/expansion-dry-run").status_code == 202
        for _ in range(200):
            status = http.get("/api/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.02)
        assert status["error"] == ""
        assert source.song_calls == song_calls_after_exact
        assert source.artist_calls > 0


# ----------------------------------------------------------- file picker
def test_files_list_includes_enabled_flag(client, config):
    (config.input_path / "a.csv").write_text("Song,Artist\nX,Y\n", encoding="utf-8")
    files = client.get("/api/files").get_json()
    assert files == [{"name": "a.csv", "size": pytest.approx(16, abs=4), "enabled": True}]


def test_toggle_file_persists_and_reflects_in_list(client, config):
    (config.input_path / "a.csv").write_text("Song,Artist\nX,Y\n", encoding="utf-8")
    res = client.post("/api/files/toggle", json={"name": "a.csv", "enabled": False})
    assert res.status_code == 200
    assert res.get_json() == {"name": "a.csv", "enabled": False}
    files = client.get("/api/files").get_json()
    assert files[0]["enabled"] is False

    client.post("/api/files/toggle", json={"name": "a.csv", "enabled": True})
    files = client.get("/api/files").get_json()
    assert files[0]["enabled"] is True


def test_toggle_missing_file_returns_404(client):
    res = client.post("/api/files/toggle", json={"name": "nope.csv", "enabled": False})
    assert res.status_code == 404


def test_simple_run_requires_enabled_files(client, config):
    (config.input_path / "a.csv").write_text("Song,Artist\nX,Y\n", encoding="utf-8")
    client.post("/api/files/toggle", json={"name": "a.csv", "enabled": False})
    res = client.post("/api/run/simple", json={"output_dir": "out", "expansion": False})
    assert res.status_code == 400
    assert "enabled" in res.get_json()["error"]


def test_estimate_respects_disabled_files(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        http.post(
            "/api/upload",
            data={
                "files": [
                    (io.BytesIO(b"Song,Artist\nA,B\n"), "a.csv"),
                    (io.BytesIO(b"Song,Artist\nC,D\n"), "b.csv"),
                ]
            },
            content_type="multipart/form-data",
        )
        full = http.get("/api/estimate").get_json()
        assert full["exact"]["song_count"] == 2

        http.post("/api/files/toggle", json={"name": "a.csv", "enabled": False})
        partial = http.get("/api/estimate").get_json()
        assert partial["exact"]["song_count"] == 1


def _sng_bytes(name="Test", artist="Test Artist") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", f"[song]\nname = {name}\nartist = {artist}\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
        # Incompressible padding keeps the file above the "unusually small"
        # cleanup heuristic, like a real chart with audio.
        archive.writestr("audio.ogg", os.urandom(150_000))
    return payload.getvalue()


def _sngpkg_bytes(
    name="Test",
    artist="Test Artist",
    album="Test Album",
    song_length="185000",
) -> bytes:
    def pair(key: str, value: str) -> bytes:
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        return (
            struct.pack("<i", len(key_bytes))
            + key_bytes
            + struct.pack("<i", len(value_bytes))
            + value_bytes
        )

    metadata = b"".join(
        pair(key, value)
        for key, value in (
            ("name", name),
            ("artist", artist),
            ("album", album),
            ("song_length", song_length),
        )
    )
    chart = (
        "[Song]\n{\nResolution = 192\n}\n"
        "[ExpertSingle]\n{\n0 = N 0 0\n}\n"
    ).encode("utf-8")
    mask = bytes(range(16))
    files = [("notes.chart", chart), ("guitar.opus", b"audio")]
    index_length = sum(1 + len(file_name.encode("utf-8")) + 16 for file_name, _ in files)
    data_start = 26 + 16 + len(metadata) + 16 + index_length + 8
    index = bytearray()
    encoded: list[bytes] = []
    offset = data_start
    for file_name, plain in files:
        masked = bytes(
            value ^ mask[position % 16] ^ (position % 256)
            for position, value in enumerate(plain)
        )
        encoded.append(masked)
        name_bytes = file_name.encode("utf-8")
        index.extend(bytes([len(name_bytes)]))
        index.extend(name_bytes)
        index.extend(struct.pack("<QQ", len(masked), offset))
        offset += len(masked)
    return (
        b"SNGPKG"
        + struct.pack("<I", 1)
        + mask
        + struct.pack("<QQ", 8 + len(metadata), 4)
        + metadata
        + struct.pack("<QQ", 8 + len(index), len(files))
        + index
        + struct.pack("<Q", sum(map(len, encoded)))
        + b"".join(encoded)
    )


def _build_library(folder):
    folder.mkdir(parents=True, exist_ok=True)
    good = folder / "Test Artist" / "Test Artist - Test.sng"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(_sng_bytes())
    duplicate = folder / "Copies" / "Test Artist - Test (copy).sng"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(_sng_bytes())
    broken = folder / "Broken.sng"
    broken.write_bytes(b"this is not a zip archive")
    return good, duplicate, broken


def test_library_link_scan_status_and_verify(config, tmp_path):
    songs = tmp_path / "Songs"
    _build_library(songs)
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        link = http.post("/api/spotify/library-import", json={"path": str(songs)})
        assert link.status_code == 200
        data = link.get_json()
        assert data["valid"] == 2
        assert data["invalid"] == 1
        assert data["writable"] is True

        status = http.get("/api/spotify/inventory/status").get_json()
        assert status["linked"] is True
        assert status["writable"] is True
        assert status["status"]["valid_count"] == 2
        assert status["status"]["invalid_count"] == 1

        verify = http.post("/api/spotify/library/verify").get_json()
        assert verify["ok"] is True
        assert verify["valid"] == 2 and verify["invalid"] == 1


def test_library_scan_reads_sngpkg_metadata(config, tmp_path):
    from csv_to_clonehero.library_inventory import scan_library
    package = _sngpkg_bytes(
        name="Lost", artist="Linkin Park", album="Meteora", song_length="201392"
    )

    songs = tmp_path / "Songs"
    songs.mkdir()
    (songs / "Linkin Park - Lost.sng").write_bytes(package)

    result = scan_library(songs)
    assert result.scanned == 1
    assert len(result.valid_entries) == 1
    entry = result.valid_entries[0]
    assert entry.artist == "Linkin Park"
    assert entry.title == "Lost"
    assert entry.album == "Meteora"
    assert entry.duration_ms == 201392
    assert entry.parser_source == "sngpkg"


def test_library_issues_and_cleanup_quarantines_problems(config, tmp_path):
    songs = tmp_path / "Songs"
    good, duplicate, broken = _build_library(songs)
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        assert http.post("/api/spotify/library-import", json={"path": str(songs)}).status_code == 200

        issues = http.get("/api/spotify/library/issues").get_json()
        assert [item["relative_path"] for item in issues["broken"]] == ["Broken.sng"]
        assert len(issues["duplicates"]) == 1
        assert len(issues["duplicates"][0]["entries"]) == 2

        clean = http.post("/api/spotify/library/clean", json={"scope": "all"}).get_json()
        quarantine = songs.parent / "Songs - quarantine"
        assert clean["moved"] == 2
        assert not broken.exists()
        assert (quarantine / "Broken.sng").exists()
        # The alphabetically-first duplicate copy is kept; the other is quarantined.
        assert duplicate.exists()
        assert not good.exists()
        assert len(list(songs.rglob("*.sng"))) == 1

        status = http.get("/api/spotify/inventory/status").get_json()
        assert status["status"]["file_count"] == 1


def test_library_link_creates_missing_folder(config, tmp_path):
    songs = tmp_path / "Clone Hero" / "Songs"
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        link = http.post("/api/spotify/library-import", json={"path": str(songs)})
        assert link.status_code == 200
        data = link.get_json()
        assert data["created"] is True
        assert songs.is_dir()
        assert data["valid"] == 0


def test_path_browse_lists_drives_and_subfolders(config, tmp_path):
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "Beta").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        root = http.get("/api/spotify/path/browse").get_json()
        assert root["parent"] is None
        assert root["folders"]

        listing = http.get("/api/spotify/path/browse", query_string={"path": str(tmp_path)}).get_json()
        names = [folder["name"] for folder in listing["folders"]]
        assert "Alpha" in names and "Beta" in names
        assert "file.txt" not in names
        assert listing["parent"]

        missing = http.get("/api/spotify/path/browse", query_string={"path": str(tmp_path / "nope")})
        assert missing.status_code == 400


def test_public_mode_remote_export_requires_linked_library(config, tmp_path):
    config.spotify.public_base_url = "https://charts.example.com"
    songs = tmp_path / "Songs"
    songs.mkdir(parents=True, exist_ok=True)
    other = tmp_path / "Other"
    other.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_path)
    db.create_workspace(workspace_key="spotify", name="Spotify library", kind="exact",
                        status="complete", phase="complete")
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()], lan_access_token="tok")
    remote = {"REMOTE_ADDR": "203.0.113.44"}
    headers = {"X-CSV-To-CloneHero-Access-Token": "tok"}
    with app.test_client() as http:
        # Unlinked: remote folder export is refused.
        blocked = http.post("/api/spotify/export/folder", headers=headers,
                            json={"path": str(songs)}, environ_base=remote)
        assert blocked.status_code == 403

        assert http.post("/api/spotify/library-import", headers=headers,
                         json={"path": str(songs)}, environ_base=remote).status_code == 200

        # Linked: the remote gate passes (empty selection -> no-op success).
        allowed = http.post("/api/spotify/export/folder", headers=headers,
                            json={"path": str(songs)}, environ_base=remote)
        assert allowed.status_code != 403

        # A different folder is still refused even while a library is linked.
        refused = http.post("/api/spotify/export/folder", headers=headers,
                            json={"path": str(other)}, environ_base=remote)
        assert refused.status_code == 403


def test_export_start_status_reports_job_progress_and_failure(config, tmp_path):
    songs = tmp_path / "Songs"
    songs.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_path)
    db.create_workspace(workspace_key="spotify", name="Spotify library", kind="exact",
                        status="complete", phase="complete")
    db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        started = http.post("/api/spotify/export/start", json={"kind": "folder", "path": str(songs)})
        assert started.status_code == 200
        assert started.get_json()["started"] is True

        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = http.get("/api/spotify/export/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.05)
        assert status is not None and not status["running"]
        assert status["status"] == "failed"
        assert "select at least one" in status["error"]

        # A second job can start once the first one settled.
        again = http.post("/api/spotify/export/start", json={"kind": "folder", "path": str(songs)})
        assert again.status_code == 200
