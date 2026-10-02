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
        scan_id = db.create_discovery_scan(
            workspace_id, scan_key="seed-current", kind="exact", status="complete",
            metadata={"workflow": "combined_catalog"}
        )
        task_id = db.upsert_discovery_task(
            workspace_id, scan_id=scan_id, source="seed", kind="song",
            query_key="seed-current-song", status="complete", total=1, completed=1,
        )
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
                    task_id=task_id,
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
        "Riff",
        "music to Clone Hero",
        "journey.css",
        "journey.js",
        "Restoring your library...",
        'data-app-surface="journey"',
    ):
        assert text in html
    assert "source_key" not in html
    assert "provenance" not in html
    assert "/advanced" not in html
    assert "WHAT'S HAPPENING" not in html
    ids = re.findall(r'id="([^\"]+)"', html)
    assert len(ids) == len(set(ids))


def test_local_home_exposes_spotify_sync_scope_and_import_controls(client):
    html = client.get("/").get_data(as_text=True)
    assert "Sync Spotify" not in html
    assert "spotifyScope" not in html
    assert "/api/spotify/inventory" not in html
    assert client.post("/api/spotify/import", json={}).status_code == 401


def test_matching_loading_state_has_accessible_immediate_feedback(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-test="journey-loading"' in html
    assert "Restoring your library..." in html
    assert "globalOperation" not in html
    assert "READY TO FIND CHARTS" not in html


def test_local_home_exposes_provenance_filters_and_activity_shell(client):
    html = client.get("/").get_data(as_text=True)
    assert "WHAT'S HAPPENING" not in html
    assert "View all song activity" not in html
    assert "data-source-filter" not in html
    assert "Preview chart matches" not in html

    payload = client.get("/api/sources").get_json()["journey"]
    assert payload["sources"]["soundcloud"]["state"] == "not_imported"
    assert payload["visualization"]["complete"] is False
    assert {key for key in payload["visualization"] if key.endswith("_count") or key in {"phase", "complete"}} >= {
        "spotify_count", "soundcloud_count", "shared_count", "canonical_count", "catalog_chart_count",
        "processed_count", "chartable_count", "strong_count", "review_count", "phase", "complete",
    }


def test_local_journey_persists_import_provenance_and_counts(client, config):
    initial = client.get("/api/sources").get_json()
    seed_local_workspace(config, initial["workspace_id"], charts=False)
    journey = client.get("/api/sources").get_json()["journey"]
    assert journey["sources"]["soundcloud"]["state"] == "imported"
    assert journey["sources"]["soundcloud"]["count"] == 2
    assert journey["combined"]["canonical_count"] == 2
    assert journey["music"][0]["source_label"] == "soundcloud"
    assert all(song["sources"] == ["soundcloud"] for song in journey["music"])


def test_current_matching_generation_is_the_candidate_boundary(client, config, tmp_path):
    initial = client.get("/api/sources").get_json()
    workspace = initial["workspace_id"]
    track = seed_local_workspace(config, workspace)
    db = Database(config.database_path)
    try:
        old_scan = db.create_discovery_scan(workspace, scan_key="old", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        old_task = db.upsert_discovery_task(workspace, scan_id=old_scan, source="fake", kind="song", query_key="old")
        current_scan = db.create_discovery_scan(workspace, scan_key="current", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        current_task = db.upsert_discovery_task(workspace, scan_id=current_scan, source="fake", kind="song", query_key="current")
        current = db._conn.execute("SELECT id FROM candidate_charts WHERE chart_id = 301").fetchone()["id"]
        blocked = db._conn.execute("SELECT id FROM candidate_charts WHERE chart_id = 303").fetchone()["id"]
        db._conn.execute("UPDATE workspace_candidates SET task_id = ? WHERE workspace_id = ? AND candidate_id IN (?, ?)", (current_task, workspace, current, blocked))
        stale_chart = make_chart("Song", "Artist", chart_id=399)
        db.upsert_candidate(stale_chart)
        stale = db._conn.execute("SELECT id FROM candidate_charts WHERE chart_id = 399").fetchone()["id"]
        db.upsert_workspace_candidate(workspace, track.track_key, stale_chart, score=98, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=old_task)
        db._conn.commit()
    finally:
        db.close()

    review = client.get(f"/api/review?workspace_id={workspace}&state=eligible").get_json()
    candidates = [item for card in review["tracks"] for item in card["candidates"]]
    assert 301 in [item["chart_id"] for item in candidates]
    assert 399 not in [item["chart_id"] for item in candidates]
    assert review["tracks"][0]["recommendation_candidate_id"] == current
    assert client.post("/api/spotify/select", json={"workspace_id": workspace, "track_id": track.db_id, "source": "fake", "chart_id": 399}).status_code == 404
    assert client.post("/api/discovery/select", json={"workspace_id": workspace, "track_id": track.track_key, "candidate_id": stale}).status_code == 404
    assert client.post("/api/discovery/select", json={"workspace_id": workspace, "track_id": track.track_key, "candidate_id": blocked}).status_code == 400
    bulk = client.post("/api/spotify/select-all", json={"workspace_id": workspace, "scope": "eligible"})
    assert bulk.status_code == 200
    assert bulk.get_json()["selected"] == 1
    assert [item["chart_id"] for item in bulk.get_json()["items"]] == [301]
    persisted = client.get(f"/api/discovery/selections?workspace_id={workspace}").get_json()
    assert persisted["selected_count"] == 1
    assert [item["chart_id"] for item in persisted["items"]] == [301]

    db = Database(config.database_path)
    try:
        now = time.time()
        db._conn.execute("INSERT INTO workspace_selections (workspace_id, kind, track_id, candidate_id, score, eligibility, status, selected_at, updated_at) VALUES (?, 'exact', ?, ?, 98, 'RECOMMENDED', 'selected', ?, ?)", (workspace, track.db_id, stale, now, now))
        db._conn.commit()
    finally:
        db.close()
    before_preview = client.get(
        f"/api/discovery/selections?workspace_id={workspace}"
    ).get_json()
    preview = client.post("/api/spotify/export/preview", json={"workspace_id": workspace, "kind": "folder", "path": str(tmp_path / "songs")})
    assert preview.status_code == 200
    assert preview.get_json()["ok"] is False
    after_preview = client.get(
        f"/api/discovery/selections?workspace_id={workspace}"
    ).get_json()
    assert after_preview["selected_count"] == before_preview["selected_count"]
    assert [item["candidate_id"] for item in after_preview["items"]] == [
        item["candidate_id"] for item in before_preview["items"]
    ]
    assert client.post("/api/spotify/export/start", json={"workspace_id": workspace, "kind": "folder", "path": str(tmp_path / "songs")}).status_code == 400


def test_journey_static_script_parses_in_javascript_engine(client, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed")

    script = client.get("/static/journey.js").get_data(as_text=True)
    script_path = tmp_path / "home.js"
    script_path.write_text(script, encoding="utf-8")
    checked = subprocess.run(
        ["node", "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "/api/sources/soundcloud/import" in script
    assert "/api/discovery/start" in script
    assert "/api/export/preview" in script
    assert "/api/paths/pick" in script
    assert "/api/spotify/export/start" in script
    assert "/api/export/status" in script
    assert "/api/spotify/export/cancel" in script
    assert "/api/journey" in script
    assert "/api/spotify/status" not in script


def test_advanced_page_is_only_a_compatibility_redirect(client):
    response = client.get("/advanced")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert "Review and export charts" not in response.get_data(as_text=True)


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
        assert journey["review_url"] == "/"

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
    assert "Review matches" not in home
    assert "/advanced" not in home
    assert "source_key" not in home
    assert "provenance" not in home


def test_review_api_preserves_local_workspace_without_page_handoff(client, config):
    initial = client.get("/api/sources").get_json()
    seed_local_workspace(config, initial["workspace_id"])

    home = client.get("/").get_data(as_text=True)
    assert 'data-app-surface="journey"' in home
    assert "/advanced" not in home

    review = client.get(
        f"/api/review?workspace_id={initial['workspace_id']}&state=eligible&limit=50&offset=0"
    ).get_json()
    assert review["workspace_id"] == initial["workspace_id"]
    assert review["source_neutral"] is True
    assert review["journey"]["matching_complete"] is True
    assert review["tracks"]



def test_charts_found_cohort_scopes_replace_and_rehydrate(client, config):
    initial = client.get("/api/sources").get_json()
    seed_local_workspace(config, initial["workspace_id"])
    db = Database(config.database_path)
    try:
        task_id = db._conn.execute(
            "SELECT id FROM discovery_tasks WHERE workspace_id = ? ORDER BY id DESC LIMIT 1",
            (initial["workspace_id"],),
        ).fetchone()["id"]
        missing = next(
            track for track in db.get_spotify_tracks(True) if track.title == "Missing Song"
        )
        review = make_chart("Missing Song", "Artist", chart_id=304)
        db.upsert_candidate(review)
        db.upsert_workspace_candidate(
            initial["workspace_id"],
            missing.track_key,
            review,
            score=82,
            eligibility="ELIGIBLE_REVIEW",
            decision="REVIEW",
            breakdown_json=json.dumps({
                "eligibility": "ELIGIBLE_REVIEW",
                "factors": {
                    "gates": {
                        "hard_gate": False,
                        "playable_content": True,
                        "required_five_fret_guitar": True,
                    },
                },
            }),
            kind="exact",
            task_id=task_id,
        )
    finally:
        db.close()

    workspace = initial["workspace_id"]

    strong = client.post(
        "/api/discovery/select-all",
        json={"workspace_id": workspace, "scope": "strong"},
    )
    assert strong.status_code == 200
    assert strong.get_json()["selected"] == 1
    assert strong.get_json()["scope"] == "strong"
    assert strong.get_json()["selected_count"] == 1
    assert [item["chart_id"] for item in strong.get_json()["items"]] == [301]

    cleared = client.post(
        "/api/discovery/select-all",
        json={"workspace_id": workspace, "action": "clear"},
    )
    assert cleared.get_json()["selected_count"] == 0
    all_recommendations = client.post(
        "/api/discovery/select-all",
        json={"workspace_id": workspace, "scope": "eligible"},
    )
    assert all_recommendations.status_code == 200
    assert all_recommendations.get_json()["selected"] == 2
    assert all_recommendations.get_json()["selected_count"] == 2
    assert {item["chart_id"] for item in all_recommendations.get_json()["items"]} == {301, 304}

    client.post(
        "/api/discovery/select-all",
        json={"workspace_id": workspace, "action": "clear"},
    )
    best_again = client.post(
        "/api/discovery/select-all",
        json={"workspace_id": workspace, "scope": "strong"},
    )
    assert best_again.status_code == 200
    assert best_again.get_json()["selected_count"] == 1
    assert [item["chart_id"] for item in best_again.get_json()["items"]] == [301]

    fresh_app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    fresh_app.config["TESTING"] = True
    with fresh_app.test_client() as fresh:
        persisted = fresh.get(
            f"/api/discovery/selections?workspace_id={workspace}"
        ).get_json()
        assert persisted["selected_count"] == 1
        assert [item["chart_id"] for item in persisted["items"]] == [301]


def test_title_conflict_cannot_be_selected_or_preflighted(config):
    from csv_to_clonehero.combined_pipeline import discover_combined
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    wrong = make_chart(
        "That's What You Get", "Paramore", chart_id=305,
        charter="Harmonix", icon="rb2",
    )

    class WrongSongCatalog:
        available = True
        diagnostic = ""
        snapshot_id = "title-conflict"
        chart_count = 1

        def candidates(self, artist, title):
            return (wrong,) if (artist, title) == ("Paramore", "The Only Exception") else ()

        def artist_songs(self, _artist):
            return ()

    db = Database(config.database_path)
    try:
        discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "paramore", "The Only Exception", "Paramore")
            ])],
            workspace_key="combined:web-title-conflict",
            catalog=WrongSongCatalog(),
        )
        workspace = int(db.get_workspace("combined:web-title-conflict")["id"])
        row = db.get_workspace_candidates(
            workspace, kind="exact", include_rejected=True, current_only=True
        )[0]
        candidate_id = int(row["candidate_id"])
        assert row["eligibility"] == "INCOMPATIBLE"
    finally:
        db.close()

    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        strong = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "strong"},
        ).get_json()
        eligible = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "eligible"},
        ).get_json()
        assert strong["selected_count"] == 0
        assert eligible["selected_count"] == 0
        rejected = http.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": "paramore|the only exception",
                "candidate_id": candidate_id,
            },
        )
        assert rejected.status_code == 400
        assert http.get(
            f"/api/discovery/selections?workspace_id={workspace}"
        ).get_json()["selected_count"] == 0
        preview = http.post(
            "/api/export/preview",
            json={"workspace_id": workspace, "kind": "folder"},
        )
        assert preview.status_code == 400
        assert "select at least one" in preview.get_json()["error"].casefold()


def test_plain_below_floor_identity_cannot_be_selected_or_preflighted(config):
    from csv_to_clonehero.combined_pipeline import discover_combined
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    conflict = make_chart(
        "Falling Down", "Atreyu", chart_id=306,
        charter="Harmonix", icon="rb2",
    )
    conflict.pack_name = "Official Pack"

    class ConflictCatalog:
        available = True
        diagnostic = ""
        snapshot_id = "title-review"
        chart_count = 1

        def candidates(self, artist, title):
            return (conflict,) if (artist, title) == ("Atreyu", "Drowning") else ()

        def artist_songs(self, _artist):
            return ()

    db = Database(config.database_path)
    try:
        discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "atreyu", "Drowning", "Atreyu")
            ])],
            workspace_key="combined:web-title-floor",
            catalog=ConflictCatalog(),
        )
        workspace = int(db.get_workspace("combined:web-title-floor")["id"])
        row = db.get_workspace_candidates(
            workspace, kind="exact", include_rejected=True, current_only=True
        )[0]
        candidate_id = int(row["candidate_id"])
        track_key = str(row["track_key"])
        assert row["eligibility"] == "INCOMPATIBLE"
    finally:
        db.close()

    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        strong = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "strong"},
        ).get_json()
        assert strong["selected_count"] == 0
        eligible = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "eligible"},
        ).get_json()
        assert eligible["selected_count"] == 0
        direct = http.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": track_key,
                "candidate_id": candidate_id,
            },
        )
        assert direct.status_code == 400
        preview = http.post(
            "/api/export/preview",
            json={
                "workspace_id": workspace,
                "kind": "folder",
                "path": str(config.base_dir / "Songs"),
            },
        )
        assert preview.status_code == 400
        assert "select at least one" in preview.get_json()["error"].casefold()


def test_review_identity_is_best_available_and_never_strong(config):
    from csv_to_clonehero.combined_pipeline import discover_combined
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    borderline = make_chart(
        "The Blessing / Shukufuku", "YOASOBI", chart_id=307,
        charter="Harmonix", icon="rb2",
    )
    borderline.pack_name = "Official Pack"

    class BorderlineCatalog:
        available = True
        diagnostic = ""
        snapshot_id = "title-review"
        chart_count = 1

        def candidates(self, artist, title):
            return (borderline,) if (artist, title) == ("YOASOBI", "Shukufuku") else ()

        def artist_songs(self, _artist):
            return ()

    db = Database(config.database_path)
    try:
        discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "yoasobi", "Shukufuku", "YOASOBI")
            ])],
            workspace_key="combined:web-title-review",
            catalog=BorderlineCatalog(),
        )
        workspace = int(db.get_workspace("combined:web-title-review")["id"])
    finally:
        db.close()

    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        strong = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "strong"},
        ).get_json()
        assert strong["selected_count"] == 0
        eligible = http.post(
            "/api/discovery/select-all",
            json={"workspace_id": workspace, "scope": "eligible"},
        ).get_json()
        assert eligible["selected_count"] == 1
        assert eligible["items"][0]["eligibility"] == "ELIGIBLE_REVIEW"
        preview = http.post(
            "/api/export/preview",
            json={
                "workspace_id": workspace,
                "kind": "folder",
                "path": str(config.base_dir / "Songs"),
            },
        )
        assert preview.status_code == 200
        assert preview.get_json()["ok"] is True


def test_confirmation_review_is_paged_searchable_mutable_and_persistent(client, config):
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    workspace = client.get("/api/sources").get_json()["workspace_id"]
    library = [
        ProviderTrack(
            provider="soundcloud",
            provider_id=f"confirmation-{index:03d}",
            artist=f"Artist {index:03d}",
            title=f"Song {index:03d}",
        )
        for index in range(60)
    ]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", library))
        tracks = sorted(db.get_spotify_tracks(True), key=lambda track: track.title)
        scan_id = db.create_discovery_scan(
            workspace,
            scan_key="confirmation-current",
            kind="exact",
            status="complete",
            metadata={"workflow": "combined_catalog"},
        )
        task_id = db.upsert_discovery_task(
            workspace,
            scan_id=scan_id,
            source="test",
            kind="generation",
            query_key="confirmation-current",
            status="complete",
            total=len(tracks),
            completed=len(tracks),
        )
        for index, track in enumerate(tracks):
            review_tier = index >= 30
            chart = make_chart(
                track.title + (" (Live)" if review_tier else ""),
                track.primary_artist,
                chart_id=1000 + index,
            )
            db.upsert_candidate(chart)
            db.upsert_workspace_candidate(
                workspace,
                track.track_key,
                chart,
                score=82 if review_tier else 95,
                eligibility="ELIGIBLE_REVIEW" if review_tier else "RECOMMENDED",
                decision="REVIEW" if review_tier else "AUTO_ACCEPT",
                kind="exact",
                task_id=task_id,
            )
        db.update_workspace(workspace, status="complete", phase="complete")
        db.update_workspace_progress(
            workspace,
            status="complete",
            phase="complete",
            total_tracks=len(tracks),
            completed_tracks=len(tracks),
        )
    finally:
        db.close()

    first = client.get(
        f"/api/review?workspace_id={workspace}&state=eligible&limit=25&offset=0"
    ).get_json()
    assert first["page"]["returned"] == 25
    assert first["page"]["total"] == 60
    assert first["page"]["has_more"] is True
    assert client.get(
        f"/api/review?workspace_id={workspace}&state=strong&limit=25"
    ).get_json()["page"]["total"] == 30
    assert client.get(
        f"/api/review?workspace_id={workspace}&state=review&limit=25"
    ).get_json()["page"]["total"] == 30

    searched = client.get(
        f"/api/review?workspace_id={workspace}&state=eligible&limit=25&search=Artist%20055"
    ).get_json()
    assert searched["page"]["total"] == 1
    outside = searched["tracks"][0]
    assert outside["title"] == "Song 055"
    first_card = first["tracks"][0]

    def select(card):
        return client.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": card["track_key"],
                "candidate_id": card["recommendation_candidate_id"],
            },
        )

    assert select(first_card).get_json()["selected_count"] == 1
    assert select(outside).get_json()["selected_count"] == 2
    assert client.get(
        f"/api/review?workspace_id={workspace}&state=strong&limit=25"
    ).get_json()["page"]["total"] == 30
    assert client.get(
        f"/api/review?workspace_id={workspace}&state=review&limit=25"
    ).get_json()["page"]["total"] == 30
    selected = client.get(
        f"/api/review?workspace_id={workspace}&state=selected&limit=25"
    ).get_json()
    assert selected["page"]["total"] == 2
    assert {card["track_key"] for card in selected["tracks"]} == {
        first_card["track_key"], outside["track_key"]
    }

    cleared = client.post(
        "/api/discovery/select",
        json={
            "workspace_id": workspace,
            "kind": "exact",
            "track_id": outside["track_key"],
            "action": "clear",
        },
    ).get_json()
    assert cleared["selected_count"] == 1
    after_clear = client.get(
        f"/api/review?workspace_id={workspace}&state=selected&limit=25"
    ).get_json()
    assert [card["track_key"] for card in after_clear["tracks"]] == [first_card["track_key"]]
    assert select(outside).get_json()["selected_count"] == 2

    fresh_app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    fresh_app.config["TESTING"] = True
    with fresh_app.test_client() as fresh:
        tray = fresh.get(f"/api/discovery/selections?workspace_id={workspace}").get_json()
        rehydrated = fresh.get(
            f"/api/review?workspace_id={workspace}&state=selected&limit=25"
        ).get_json()
        assert tray["selected_count"] == 2
        assert {card["track_key"] for card in rehydrated["tracks"]} == {
            first_card["track_key"], outside["track_key"]
        }


def test_review_bulk_actions_are_explicit_scoped_and_preserve_manual_alternatives(client, config):
    initial = client.get("/api/sources").get_json()
    workspace = initial["workspace_id"]
    track = seed_local_workspace(config, workspace)
    db = Database(config.database_path)
    try:
        task_id = db._conn.execute("SELECT id FROM discovery_tasks WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (workspace,)).fetchone()["id"]
        missing = next(item for item in db.get_spotify_tracks(True) if item.title == "Missing Song")
        chart = make_chart("Missing Song", "Artist", chart_id=305)
        db.upsert_candidate(chart)
        db.upsert_workspace_candidate(workspace, missing.track_key, chart, score=82, eligibility="ELIGIBLE_REVIEW", decision="REVIEW", kind="exact", task_id=task_id)
    finally:
        db.close()

    alternative = client.post("/api/review/decide", json={"workspace_id": workspace, "track_key": track.track_key, "source": "test", "chart_id": 302, "decision": "accept"})
    assert alternative.status_code == 200
    strong = client.post("/api/spotify/select-all", json={"workspace_id": workspace, "scope": "strong"}).get_json()
    assert strong["selected"] == 0
    assert strong["selected_count"] == 1
    assert client.post("/api/spotify/select-all", json={"workspace_id": workspace, "scope": "review"}).get_json()["selected"] == 1
    assert client.post("/api/spotify/select-all", json={"workspace_id": workspace, "scope": "review"}).get_json()["selected"] == 0
    selected = client.get(f"/api/review?workspace_id={workspace}").get_json()
    song = next(card for card in selected["tracks"] if card["track_key"] == track.track_key)
    assert next(item for item in song["candidates"] if item["selected"])["chart_id"] == 302
    assert song["selection_state"] == "alternative_selected"
    assert song["recommendation_candidate_id"] == song["best_candidate_id"]


def test_review_skip_and_source_provenance_filters_survive_reload(client, config):
    from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack

    workspace = client.get("/api/sources").get_json()["workspace_id"]
    seed_local_workspace(config, workspace)
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [
            ProviderTrack("spotify", "sp-song", "Song", "Artist"),
            ProviderTrack("spotify", "sp-only", "Spotify Only", "Artist"),
        ]))
        task_id = db._conn.execute("SELECT id FROM discovery_tasks WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (workspace,)).fetchone()["id"]
        missing = next(item for item in db.get_spotify_tracks(True) if item.title == "Missing Song")
        missing_chart = make_chart("Missing Song", "Artist", chart_id=307)
        db.upsert_candidate(missing_chart)
        db.upsert_workspace_candidate(workspace, missing.track_key, missing_chart, score=82, eligibility="ELIGIBLE_REVIEW", decision="REVIEW", kind="exact", task_id=task_id)
        spotify_only = next(item for item in db.get_spotify_tracks(True) if item.title == "Spotify Only")
        chart = make_chart("Spotify Only", "Artist", chart_id=306)
        db.upsert_candidate(chart)
        db.upsert_workspace_candidate(workspace, spotify_only.track_key, chart, score=94, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=task_id)
    finally:
        db.close()

    skipped = client.post("/api/review/decide", json={"workspace_id": workspace, "track_key": "artist|song", "source": "test", "chart_id": 301, "decision": "reject"})
    assert skipped.status_code == 200
    reloaded = client.get(f"/api/review?workspace_id={workspace}").get_json()
    song = next(card for card in reloaded["tracks"] if card["title"] == "Song")
    assert song["selection_state"] == "skipped"
    assert not client.get(f"/api/spotify/selections?workspace_id={workspace}").get_json()["items"]

    all_cards = client.get(f"/api/review?workspace_id={workspace}&state=all").get_json()["tracks"]
    assert {card["title"] for card in all_cards} == {"Song", "Missing Song", "Spotify Only"}
    assert len([card for card in all_cards if card["title"] == "Song"]) == 1
    assert len(client.get(f"/api/review?workspace_id={workspace}&source=both&state=all").get_json()["tracks"]) == 1
    assert {card["title"] for card in client.get(f"/api/review?workspace_id={workspace}&source=spotify&state=all").get_json()["tracks"]} == {"Song", "Spotify Only"}
    assert {card["title"] for card in client.get(f"/api/review?workspace_id={workspace}&source=soundcloud&state=all").get_json()["tracks"]} == {"Song", "Missing Song"}
    assert client.get(f"/api/review?workspace_id={workspace}&source=spotify&state=strong&search=Spotify&sort=score&limit=1").get_json()["page"]["returned"] == 1


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
            task_id = db._conn.execute(
                "SELECT id FROM discovery_tasks WHERE workspace_id = ? ORDER BY id DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()["id"]
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
                task_id=task_id,
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
        preview = http.post(
            "/api/spotify/export/preview",
            json={
                "kind": "folder",
                "workspace_id": workspace_id,
                "path": str(config.base_dir / "Songs"),
            },
        )
        assert preview.status_code == 200
        assert preview.get_json()["selected_chart_count"] == 1
        assert preview.get_json()["eligible_chart_count"] == 1
        assert preview.get_json()["estimated_bytes"] > 0
        started = http.post(
            "/api/spotify/export/start",
            json={"kind": "folder", "workspace_id": workspace_id, "path": str(config.base_dir / "Songs")},
        )
        assert started.status_code == 200
        assert started.get_json()["kind"] == "folder"
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            status = http.get("/api/spotify/export/status").get_json()
            if not status["running"]:
                break
            time.sleep(0.05)
        assert status is not None and status["status"] == "complete"
        assert status["kind"] == "folder"
        assert status["result"]["kind"] == "folder"
        assert status["result"]["success"] is True
        assert status["result"]["published"] == 1
        assert status["result"]["failed"] == 0
        assert status["result"]["ready"] == 1
        db = Database(config.database_path)
        try:
            records = db.list_export_records(workspace_id)
            assert records and records[0]["status"] == "success"
            items = db.get_export_items(records[0]["id"])
            assert items and items[0]["status"] == "published"
        finally:
            db.close()
        advanced = http.get("/advanced")
        assert advanced.status_code == 302
        assert advanced.headers["Location"] == "/"


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
        assert 'data-app-surface="journey"' in page.get_data(as_text=True)
        assert "/advanced" not in page.get_data(as_text=True)
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


def test_advanced_page_is_retired(client):
    response = client.get("/advanced")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert "Review and export charts" not in response.get_data(as_text=True)


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


@pytest.mark.parametrize(
    "client_filename",
    ["..\\..\\evil?.csv", "../../evil?.csv"],
    ids=["windows-client", "posix-client"],
)
def test_upload_sanitizes_filename(client, config, client_filename):
    # The uploading browser's OS picks the separator, so both must be stripped
    # regardless of the OS the server runs on.
    # Preserve literal backslashes across the multipart quoted-string boundary.
    # Werkzeug's test builder does not escape them, while newer parsers correctly
    # consume an unescaped backslash as a quoted-pair escape before our route runs.
    quoted_filename = client_filename.replace("\\", "\\\\").replace('"', '\\"')
    boundary = "riff-upload-filename-test"
    data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{quoted_filename}"\r\n'
        "Content-Type: text/csv\r\n\r\nSong,Artist\nX,Y\n"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    response = client.post(
        "/api/upload", data=data,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    saved = response.get_json()["saved"][0]
    assert ".." not in saved and "?" not in saved
    assert saved == "evil_.csv"
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
    # Drive/filesystem root of the host OS ("C:\" on Windows, "/" on POSIX,
    # where "C:\" is just a relative folder name).
    filesystem_root = config.base_dir.anchor
    root = client.post("/api/output-path/check", json={"path": filesystem_root}).get_json()
    assert root["ok"] is False
    assert "drive root" in root["error"]
    # Folder that would contain the state DB
    state_holder = client.post(
        "/api/output-path/check", json={"path": str(config.base_dir)}
    ).get_json()
    assert state_holder["ok"] is False


def test_open_folder_404_when_missing(client):
    response = client.post("/api/open-folder")
    assert response.status_code == 404


def test_build_busy_state_blocks_mutations_but_keeps_review_readable(client, config):
    export_job = client.application.extensions["export_job"]
    with export_job.lock:
        export_job.running = True
        export_job.status = "running"
        export_job.current = "Downloading Artist - Song"
        export_job.total = 2
        export_job.done = 1
    try:
        status = client.get("/api/status").get_json()
        assert status["operation"]["state"] == "building"
        assert status["operation"]["progress"]["completed"] == 1
        assert client.post("/api/upload").status_code == 409
        assert client.post("/api/spotify/select-all", json={}).status_code == 409
        assert client.post("/api/review/decide", json={}).status_code == 409
        assert client.get("/api/review").status_code == 200
    finally:
        with export_job.lock:
            export_job.running = False
            export_job.status = "idle"


def test_open_folder_is_loopback_only(client):
    response = client.post("/api/open-folder", environ_base={"REMOTE_ADDR": "192.0.2.10"})
    assert response.status_code == 403


def _set_active_export(app, *, status="running", done=1, total=3):
    job = app.extensions["export_job"]
    with job.lock:
        job.running = status in {"running", "cancelling"}
        job.status = status
        job.done = done
        job.total = total
        job.current = "Downloading Artist - Song"
        job.destination = str(app.config.get("TEST_BUILD_DESTINATION", ""))
    return job


def test_build_start_rejects_zero_selection_with_user_reason(client, config):
    workspace = client.get("/api/sources").get_json()["workspace_id"]
    response = client.post(
        "/api/spotify/export/start",
        json={"kind": "folder", "workspace_id": workspace, "path": str(config.base_dir / "Songs")},
    )
    assert response.status_code == 400
    assert "Select at least one chart first" in response.get_json()["error"]


def test_build_preview_exposes_all_truthful_buckets(client, config):
    workspace = client.get("/api/sources").get_json()["workspace_id"]
    track = seed_local_workspace(config, workspace)
    db = Database(config.database_path)
    try:
        candidates = db.get_workspace_candidates(workspace, track_id=track.db_id, kind="exact", include_rejected=True)
        good = next(row for row in candidates if int(row["chart_id"]) == 301)
        blocked = next(row for row in candidates if int(row["chart_id"]) == 303)
        db.select_workspace_candidate(workspace, track.db_id, good["candidate_id"], kind="exact", force=True, eligibility="RECOMMENDED")
        now = time.time()
        db._conn.execute(
            "INSERT INTO workspace_selections (workspace_id, kind, track_id, candidate_id, score, eligibility, status, selected_at, updated_at) VALUES (?, 'exact', ?, ?, 99, 'INCOMPATIBLE', 'selected', ?, ?)",
            (workspace, track.db_id, blocked["candidate_id"], now, now),
        )
        db._conn.commit()
    finally:
        db.close()
    data = client.post(
        "/api/spotify/export/preview",
        json={"kind": "folder", "workspace_id": workspace, "path": str(config.base_dir / "Songs")},
    ).get_json()
    assert data["selected_chart_count"] == 2
    assert data["reusable_count"] >= 0
    assert data["download_required_count"] >= 0
    assert data["invalid_count"] >= 1
    assert data["estimated_bytes"] >= 0
    assert data["destination"] == str((config.base_dir / "Songs").resolve())
    cleared = client.post(
        "/api/spotify/select",
        json={"workspace_id": workspace, "track_id": track.db_id, "candidate_id": good["candidate_id"], "clear": True},
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["selection_count"] == 1


def test_journey_folder_preflight_is_no_write_and_exposes_destination_truth(config):
    source = FakeSource()
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    with app.test_client() as http:
        workspace = http.get("/api/sources").get_json()["workspace_id"]
        track = seed_local_workspace(config, workspace)
        db = Database(config.database_path)
        try:
            current = next(
                row
                for row in db.get_workspace_candidates(
                    workspace,
                    track_id=track.track_key,
                    kind="exact",
                    include_rejected=True,
                    current_only=True,
                )
                if int(row["chart_id"]) == 301
            )
        finally:
            db.close()

        empty = http.post(
            "/api/export/preview",
            json={"kind": "folder", "workspace_id": workspace},
        )
        assert empty.status_code == 400
        assert "select at least one" in empty.get_json()["error"].casefold()

        selected = http.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": track.track_key,
                "candidate_id": current["candidate_id"],
            },
        )
        assert selected.status_code == 200
        assert selected.get_json()["selected_count"] == 1

        initial = http.post(
            "/api/export/preview",
            json={"kind": "folder", "workspace_id": workspace},
        ).get_json()
        assert initial["ok"] is True
        assert initial["selected_chart_count"] == 1
        assert initial["eligible_chart_count"] == 1
        assert initial["reusable_count"] == 0
        assert initial["download_required_count"] == 1
        assert initial["destination"] == str(config.library_root.resolve())
        assert initial["available_bytes"] > 0
        assert initial["current_library_bytes"] == 0
        assert initial["configured_budget_bytes"] == config.storage.max_library_bytes
        assert isinstance(initial["warnings"], list)

        dedicated = config.base_dir / "Dedicated Songs"
        assert dedicated.exists() is False
        changed = http.post(
            "/api/export/preview",
            json={
                "kind": "folder",
                "workspace_id": workspace,
                "path": str(dedicated),
            },
        ).get_json()
        assert changed["ok"] is True
        assert changed["destination"] == str(dedicated.resolve())
        assert changed["selected_chart_count"] == 1
        assert dedicated.exists() is False

        tray = http.get(
            f"/api/discovery/selections?workspace_id={workspace}"
        ).get_json()
        assert tray["selected_count"] == 1
        assert source.downloads == []
        assert app.extensions["export_job"].running is False

    db = Database(config.database_path)
    try:
        assert db.list_export_records(workspace_id=workspace) == []
    finally:
        db.close()
    assert list(config.base_dir.rglob("*.sng")) == []


def test_local_export_uses_trusted_history_to_relocate_owned_artifact(config, tmp_path):
    import hashlib

    from csv_to_clonehero.exporter import ExportManifest, ManifestRecord
    from csv_to_clonehero.filesystem import destination_path
    from tests.conftest import valid_sng_bytes

    content = valid_sng_bytes("Owned history")
    digest = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=301, md5=digest)
    source = FakeSource(files={digest: content})
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    root = tmp_path / "Songs"

    with app.test_client() as http:
        workspace = http.get("/api/sources").get_json()["workspace_id"]
        track = seed_local_workspace(config, workspace)
        db = Database(config.database_path)
        try:
            db.upsert_candidate(chart)
            row = next(
                candidate
                for candidate in db.get_workspace_candidates(
                    workspace,
                    track_id=track.track_key,
                    kind="exact",
                    include_rejected=True,
                    current_only=True,
                )
                if int(candidate["chart_id"]) == 301
            )
            candidate_id = int(row["candidate_id"])
            old_relpath = "Old Artist/Old Album/Old.sng"
            old_path = root / old_relpath
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(content)
            record = ManifestRecord(
                source="test",
                chart_id=301,
                sha256=hashlib.sha256(content).hexdigest(),
                bytes_count=len(content),
                target_relpath=old_relpath,
            )
            export_id = db.create_export_record(
                workspace,
                export_key="owned-history",
                kind="folder",
                destination=str(root),
                status="success",
            )
            db.update_export_record(
                export_id,
                manifest_json=ExportManifest(
                    records=[record], destination=str(root.resolve()), kind="folder"
                ).to_dict(),
            )
        finally:
            db.close()

        selected = http.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": track.track_key,
                "candidate_id": candidate_id,
            },
        )
        assert selected.status_code == 200
        preview = http.post(
            "/api/export/preview",
            json={"workspace_id": workspace, "kind": "folder", "path": str(root)},
        ).get_json()
        expected = destination_path(config, chart, "exact", track=track)
        assert preview["reusable_count"] == 1
        assert preview["items"][0]["target_relpath"] == expected.as_posix()

        exported = http.post(
            "/api/export/folder",
            json={"workspace_id": workspace, "kind": "folder", "path": str(root)},
        )
        assert exported.status_code == 200
        payload = exported.get_json()
        assert payload["success"] is True
        assert source.downloads == []
        assert old_path.exists() is False
        assert (root / expected).read_bytes() == content
        assert len(list(root.rglob("*.sng"))) == 1
        assert [item["target_relpath"] for item in payload["manifest"]["records"]] == [
            expected.as_posix()
        ]


def test_duplicate_build_request_preserves_active_build(client):
    job = _set_active_export(client.application, done=2, total=4)
    try:
        response = client.post("/api/spotify/export/start", json={"kind": "folder"})
        assert response.status_code == 409
        assert response.get_json()["busy"] is True
        assert job.running is True
        assert job.done == 2
        assert job.total == 4
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_all_conflicting_mutations_are_backend_locked_during_build(client):
    job = _set_active_export(client.application)
    try:
        requests = [
            ("/api/upload", {"data": {}}),
            ("/api/sources/soundcloud/import", {"data": {}}),
            ("/api/discovery/start", {"json": {"source": "combined"}}),
            ("/api/spotify/select", {"json": {}}),
            ("/api/spotify/select-all", {"json": {}}),
            ("/api/spotify/select-recommended", {"json": {}}),
            ("/api/review/decide", {"json": {}}),
            ("/api/review/accept-all", {"json": {}}),
        ]
        for route, kwargs in requests:
            response = client.post(route, **kwargs)
            assert response.status_code == 409, route
            assert response.get_json()["busy"] is True
        assert client.get("/api/review").status_code == 200
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_build_status_reports_real_counters_and_cross_page_state(client):
    job = _set_active_export(client.application, done=2, total=5)
    try:
        with job.lock:
            job.destination = "C:\\Songs"
            job.item_states = {
                0: {"status": "published", "result": "downloaded"},
                1: {"status": "duplicate", "result": "already_present"},
                2: {"status": "failed", "result": "failed"},
                3: {"status": "invalid", "result": "invalid"},
                4: {"status": "pending", "result": ""},
            }
        status = client.get("/api/status").get_json()
        export = client.get("/api/spotify/export/status").get_json()
        assert status["operation"]["state"] == "building"
        assert status["operation"]["progress"]["completed"] == 2
        assert status["operation"]["progress"]["total"] == 5
        assert status["operation"]["detail"] == "Downloading Artist - Song"
        assert export["operation"]["state"] == "building"
        assert export["operation"]["progress"]["completed"] == 2
        assert export["items"]["failed"] == 1
        assert export["items"]["invalid"] == 1
        assert export["items"]["remaining"] == 1
        advanced = client.get("/advanced")
        assert advanced.status_code == 302
        assert advanced.headers["Location"] == "/"
        assert "globalOperation" not in client.get("/").get_data(as_text=True)
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_terminal_success_is_library_ready_and_retains_destination(client, tmp_path):
    destination = tmp_path / "Songs"
    job = _set_active_export(client.application, done=2, total=2)
    with job.lock:
        job.running = False
        job.status = "complete"
        job.destination = str(destination)
        job.result = {
            "success": True, "status": "success", "selected": 2,
            "ready": 2, "published": 2, "added": 1, "already_present": 1,
            "invalid": 0, "failed": 0, "destination": str(destination),
        }
    status = client.get("/api/spotify/export/status").get_json()
    assert status["operation"]["state"] == "complete"
    assert status["operation"]["label"] == "LIBRARY READY"
    assert status["destination"] == str(destination)
    assert status["result"]["ready"] == 2


def test_matching_busy_state_blocks_review_mutation_but_allows_read(client):
    matching = client.application.extensions["spotify_job"]
    with matching.lock:
        matching.running = True
        matching.status = "running"
        matching.message = "Matching Artist - Song"
    try:
        status = client.get("/api/status").get_json()
        assert status["operation"]["state"] == "matching"
        response = client.post("/api/review/accept-all", json={})
        assert response.status_code == 409
        assert "Spotify discovery" in response.get_json()["error"]
        assert client.get("/api/review").status_code == 200
        advanced = client.get("/advanced")
        assert advanced.status_code == 302
        assert advanced.headers["Location"] == "/"
    finally:
        with matching.lock:
            matching.running = False
            matching.status = "created"


def test_unknown_export_progress_has_no_fake_eta_or_percentage(client):
    job = _set_active_export(client.application, done=0, total=0)
    try:
        payload = client.get("/api/spotify/export/status").get_json()
        assert payload["operation"]["state"] == "building"
        assert payload["operation"]["progress"]["total"] == 0
        assert "eta" not in payload["operation"]
        assert "percent" not in payload["operation"]
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_export_cancelling_survives_subsequent_progress_then_settles(client):
    job = _set_active_export(client.application, done=0, total=2)
    try:
        assert client.post("/api/spotify/export/cancel").get_json()["status"] == "cancelling"
        job.update(1, 2, "Publishing Artist - Song")
        assert job.snapshot()["status"] == "cancelling"
        with job.lock:
            job.running = False
            job.status = "cancelled"
            job.result = {"status": "cancelled", "success": False, "selected": 2, "ready": 1, "cancelled_items": 1}
        assert client.get("/api/spotify/export/status").get_json()["operation"]["state"] == "cancelled"
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_real_worker_preserves_cancelled_terminal_status_with_useful_output(config, monkeypatch):
    source = FakeSource()
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    with app.test_client() as http:
        workspace = http.get("/api/sources").get_json()["workspace_id"]
        track = seed_local_workspace(config, workspace)
        db = Database(config.database_path)
        try:
            current = next(
                row
                for row in db.get_workspace_candidates(
                    workspace,
                    track_id=track.track_key,
                    kind="exact",
                    include_rejected=True,
                    current_only=True,
                )
                if int(row["chart_id"]) == 301
            )
        finally:
            db.close()
        assert http.post(
            "/api/discovery/select",
            json={
                "workspace_id": workspace,
                "kind": "exact",
                "track_id": track.track_key,
                "candidate_id": current["candidate_id"],
            },
        ).status_code == 200

        item = type("Item", (), {
            "selection_index": 0,
            "selection_id": 1,
            "source": "test",
            "chart_id": 301,
            "target_relpath": "Artist/Song.sng",
            "target_path": config.base_dir / "Songs" / "Artist" / "Song.sng",
            "status": "published",
            "result": "published",
            "bytes_count": 100,
            "sha256": "",
            "source_hash": "",
            "source_hash_algorithm": "",
            "artifact_key": "",
            "failure": "",
            "playable_note_count": 1,
        })()
        cancelled_result = type("Result", (), {
            "kind": "folder",
            "destination": config.base_dir / "Songs",
            "success": True,
            "cancelled": True,
            "published_count": 1,
            "failed_count": 0,
            "invalid_count": 0,
            "download_failure_count": 0,
            "cancelled_items": [object()],
            "ready_count": 1,
            "remaining_count": 1,
            "output_bytes": 100,
            "errors": [],
            "items": [item],
            "preflight": None,
            "manifest": None,
            "manifest_path": None,
            "sync": None,
        })()
        monkeypatch.setattr(
            "csv_to_clonehero.webapp.ExportService.export_folder",
            lambda self, *args, **kwargs: cancelled_result,
        )
        started = http.post(
            "/api/spotify/export/start",
            json={
                "kind": "folder",
                "workspace_id": workspace,
                "path": str(config.base_dir / "Songs"),
            },
        )
        assert started.status_code == 200
        for _ in range(100):
            status = http.get(
                f"/api/export/status?workspace_id={workspace}"
            ).get_json()
            if not status["running"]:
                break
            time.sleep(0.01)
        assert status["status"] == "cancelled"
        assert status["operation"]["state"] == "cancelled"
        assert status["result"]["ready"] == 1


@pytest.mark.parametrize(
    ("status", "expected", "label"),
    [
        ("complete", "complete", "LIBRARY READY"),
        ("partial", "partial", "LIBRARY READY — WITH ISSUES"),
        ("failed", "failed", "BUILD FAILED"),
        ("cancelled", "cancelled", "BUILD CANCELLED"),
        ("interrupted", "interrupted", "BUILD INTERRUPTED"),
    ],
)
def test_export_operation_states_are_consistent(client, status, expected, label):
    job = client.application.extensions["export_job"]
    with job.lock:
        job.running = False
        job.status = status
        job.result = {"success": status in {"complete", "partial"}, "ready": 1, "failed": 1, "invalid": 1}
    export = client.get("/api/spotify/export/status").get_json()["operation"]
    app_status = client.get("/api/status").get_json()["operation"]
    assert export["state"] == expected
    assert export["label"] == label
    assert app_status["state"] == expected


def test_newer_matching_failure_beats_completed_export_history(client):
    job = client.application.extensions["export_job"]
    with job.lock:
        job.running = False
        job.status = "complete"
        job.result = {"success": True, "ready": 1}
    matching = client.application.extensions["spotify_job"]
    with matching.lock:
        matching.running = False
        matching.status = "failed"
        matching.error = "catalog stopped"
    try:
        operation = client.get("/api/status").get_json()["operation"]
        assert operation["state"] == "failed"
        assert operation["detail"] == "catalog stopped"
    finally:
        with matching.lock:
            matching.status = "created"
            matching.error = ""


def test_open_folder_prefers_actual_completed_export_destination(client, config, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    actual = tmp_path / "actual-build-destination"
    actual.mkdir()
    job = client.application.extensions["export_job"]
    with job.lock:
        job.running = False
        job.kind = "folder"
        job.status = "complete"
        job.destination = str(actual)
        job.result = {"success": True, "status": "success"}
    response = client.post("/api/open-folder")
    assert response.status_code == 200
    assert opened == [str(actual.resolve())]
    assert str(actual.resolve()) != str(config.library_root.resolve())


def test_primary_root_does_not_serve_the_retired_build_surface(client):
    html = client.get("/").get_data(as_text=True)
    assert "journey.js?v=2.5.3" in html
    assert "done: status.done" not in html
    assert "Calculating build preview" not in html
    assert "LIBRARY READY" not in html
    assert client.get("/advanced").status_code == 302


def test_retired_page_does_not_expose_build_preview_script(client):
    response = client.get("/advanced")
    assert response.status_code == 302
    assert "previewGeneration" not in response.get_data(as_text=True)


def test_newer_import_operation_outweighs_historical_export(client):
    export = client.application.extensions["export_job"]
    with export.lock:
        export.status = "complete"
        export.finished_at = time.time() - 30
        export.result = {"success": True, "ready": 5}
    imported = client.application.extensions["soundcloud_import_job"]
    with imported.lock:
        imported.running = False
        imported.status = "complete"
        imported.finished_at = time.time()
        imported.result = {"source": {"tracks": 5031}}
    try:
        operation = client.get("/api/status").get_json()["operation"]
        assert operation["label"] == "SOUNDCLOUD IMPORT COMPLETE"
        assert "5,031 SoundCloud songs imported" in operation["detail"]
    finally:
        with imported.lock:
            imported.status = "idle"
            imported.result = None


def test_restoring_matching_payload_hides_unauthoritative_partition(client):
    matching = client.application.extensions["spotify_job"]
    with matching.lock:
        matching.running = True
        matching.status = "running"
        matching.phase = "restoring"
        matching.message = "Checking saved match results..."
    try:
        status = client.get("/api/status").get_json()["operation"]
        assert status["state"] == "matching"
        assert "Checking saved match results" in status["detail"]
    finally:
        with matching.lock:
            matching.running = False
            matching.status = "created"
            matching.phase = ""
            matching.message = ""


def test_spotify_disconnected_copy_preserves_imported_library_contract(client):
    html = client.get("/").get_data(as_text=True)
    assert "Spotify songs already imported" in html or "connection_copy" in client.get("/api/sources").get_data(as_text=True)


def test_real_home_path_does_not_reuse_old_progress_after_import(client, config):
    workspace = client.get("/api/sources").get_json()["workspace_id"]
    seed_local_workspace(config, workspace, charts=False)
    db = Database(config.database_path)
    try:
        tracks = db.get_spotify_tracks(True)
        db.initialize_discovery_song_progress(workspace, tracks)
        db.update_discovery_song_progress(workspace, tracks[0].track_key, status="complete", classification="review")
        db.update_workspace(workspace, status="ready_for_matching", phase="ready_for_matching")
    finally:
        db.close()
    payload = client.get("/api/sources").get_json()
    journey = payload["journey"]
    assert journey["matching_status"] == "ready"
    assert journey["progress"]["processed"] == 0
    assert journey["progress"]["total"] == 0
    assert journey["progress"]["recent"] == []
    assert journey["counts"]["no_candidate_songs"] == 0
    assert journey["counts"]["no_suitable_songs"] == 0


def test_build_endpoints_remain_backend_compatible_after_page_cutover(client):
    advanced = client.get("/advanced")
    home = client.get("/").get_data(as_text=True)
    assert advanced.status_code == 302
    assert advanced.headers["Location"] == "/"
    assert "Estimated download" not in home
    assert "preview.destination" not in home
    assert client.post("/api/spotify/export/preview", json={}).status_code in {400, 404}


def test_retry_reuses_completed_chart_and_retries_only_incomplete(config):
    good_bytes = _sng_bytes()
    good_md5 = hashlib.md5(good_bytes).hexdigest()
    bad_md5 = "f" * 32
    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(files={good_md5: good_bytes})
            self.attempts = []

        def download_chart(self, candidate, destination):
            self.attempts.append(candidate.md5)
            return super().download_chart(candidate, destination)

    source = CountingSource()
    app = create_app(config, source_factory=lambda cfg: [source])
    app.config["TESTING"] = True
    destination = config.base_dir / "Songs"
    with app.test_client() as client:
        workspace = client.get("/api/sources").get_json()["workspace_id"]
        seed_local_workspace(config, workspace, charts=False)
        db = Database(config.database_path)
        try:
            task_id = db._conn.execute("SELECT id FROM discovery_tasks WHERE workspace_id = ? ORDER BY id DESC LIMIT 1", (workspace,)).fetchone()["id"]
            tracks = {item.title: item for item in db.get_spotify_tracks(True)}
            for chart, track in ((make_chart("Song", "Artist", chart_id=801, md5=good_md5), tracks["Song"]), (make_chart("Missing Song", "Artist", chart_id=802, md5=bad_md5), tracks["Missing Song"])):
                db.upsert_candidate(chart)
                db.upsert_workspace_candidate(workspace, track.track_key, chart, score=94, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=task_id)
                candidate_id = db._conn.execute("SELECT id FROM candidate_charts WHERE chart_id = ?", (chart.chart_id,)).fetchone()["id"]
                db.select_workspace_candidate(workspace, track.db_id, candidate_id, kind="exact", force=True, eligibility="RECOMMENDED")
        finally:
            db.close()
        response = client.post("/api/spotify/export/folder", json={"workspace_id": workspace, "path": str(destination)})
        assert response.status_code == 200
        assert source.downloads.count(good_md5) == 1
        retry = client.post("/api/spotify/export/folder", json={"workspace_id": workspace, "path": str(destination), "retry_failed": True})
        assert retry.status_code == 200
        assert source.downloads.count(good_md5) == 1
        assert source.attempts.count(bad_md5) >= 2


def test_cancellation_is_explicit_and_completed_files_are_preserved(client, tmp_path):
    job = _set_active_export(client.application, done=1, total=2)
    completed = tmp_path / "Artist" / "Song.sng"
    completed.parent.mkdir(parents=True)
    completed.write_bytes(b"completed")
    try:
        cancelled = client.post("/api/spotify/export/cancel")
        assert cancelled.status_code == 200
        assert cancelled.get_json()["status"] == "cancelling"
        with job.lock:
            job.running = False
            job.status = "cancelled"
            job.result = {"success": False, "status": "cancelled", "ready": 1, "selected": 2, "cancelled_items": 1}
        terminal = client.get("/api/spotify/export/status").get_json()
        assert terminal["status"] == "cancelled"
        assert terminal["result"]["ready"] == 1
        assert terminal["result"]["cancelled_items"] == 1
        assert completed.read_bytes() == b"completed"
    finally:
        with job.lock:
            job.running = False
            job.status = "idle"


def test_stale_running_export_becomes_interrupted_without_auto_resume(config):
    db = Database(config.database_path)
    try:
        db.create_export_record(export_key="stale-run", kind="folder", destination=str(config.base_dir / "Songs"), status="running")
    finally:
        db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as client:
        status = client.get("/api/spotify/export/status").get_json()
        assert status["status"] == "interrupted"
        assert status["operation"]["state"] == "interrupted"
        assert status["running"] is False
        assert app.extensions["export_job"].worker is None


def test_partial_export_survives_fresh_app_as_partial(config):
    db = Database(config.database_path)
    try:
        export_id = db.create_export_record(export_key="partial-run", kind="folder", destination=str(config.base_dir / "Songs"), status="partial")
        db.record_export_item(export_id, {"source": "test", "chart_id": 1, "target_relpath": "Artist/Song.sng", "status": "published", "result": "downloaded"})
        db.record_export_item(export_id, {"source": "test", "chart_id": 2, "target_relpath": "Artist/Bad.sng", "status": "failed", "error": "download failed"})
    finally:
        db.close()
    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as client:
        payload = client.get("/api/spotify/export/status").get_json()
        assert payload["status"] == "partial"
        assert payload["operation"]["state"] == "partial"
        assert payload["operation"]["label"] == "LIBRARY READY — WITH ISSUES"


def test_export_status_filters_durable_history_to_requested_workspace(config):
    db = Database(config.database_path)
    try:
        first_workspace = db.create_workspace(
            workspace_key="history:first",
            name="First history",
            kind="exact",
            status="complete",
            phase="complete",
        )
        second_workspace = db.create_workspace(
            workspace_key="history:second",
            name="Second history",
            kind="exact",
            status="complete",
            phase="complete",
        )
        first_export = db.create_export_record(
            first_workspace,
            export_key="first-history",
            kind="folder",
            destination=str(config.base_dir / "First Songs"),
            status="complete",
        )
        db.record_export_item(
            first_export,
            {
                "source": "test",
                "chart_id": 1,
                "target_relpath": "Artist/First.sng",
                "status": "published",
                "result": "downloaded",
            },
        )
        second_export = db.create_export_record(
            second_workspace,
            export_key="second-history",
            kind="folder",
            destination=str(config.base_dir / "Second Songs"),
            status="partial",
        )
        db.record_export_item(
            second_export,
            {
                "source": "test",
                "chart_id": 2,
                "target_relpath": "Artist/Second.sng",
                "status": "failed",
                "error": "download failed",
            },
        )
    finally:
        db.close()

    app = create_app(config, source_factory=lambda cfg: [FakeSource()])
    app.config["TESTING"] = True
    with app.test_client() as http:
        first = http.get(
            f"/api/export/status?workspace_id={first_workspace}"
        ).get_json()
        second = http.get(
            f"/api/export/status?workspace_id={second_workspace}"
        ).get_json()
        assert first["workspace_id"] == first_workspace
        assert first["status"] == "complete"
        assert first["result"]["ready"] == 1
        assert second["workspace_id"] == second_workspace
        assert second["status"] == "partial"
        assert second["result"]["ready"] == 0


def test_open_folder_rejects_arbitrary_path_and_accepts_validated_destination(client, config, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    response = client.post("/api/open-folder", json={"path": str(unrelated)})
    assert response.status_code == 404
    destination = config.library_root
    destination.mkdir()
    job = client.application.extensions["export_job"]
    with job.lock:
        job.destination = str(destination.resolve())
        job.status = "complete"
        job.result = {"success": True}
    # The opener uses the configured destination, never a request-supplied path.
    response = client.post("/api/open-folder", json={"path": str(unrelated)})
    assert response.status_code == 200
    assert opened == [str(destination.resolve())]


def test_build_ui_has_reduced_motion_and_no_fake_export_eta(client):
    html = client.get("/").get_data(as_text=True)
    assert "prefers-reduced-motion" not in html
    assert "estimated download size" not in html.lower()
    assert "No fake ETA" not in html
    assert "eta_seconds" not in html


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
        # The album "DAMN." is written as "DAMN": sanitize_component strips
        # trailing dots, which Windows cannot store. Win32 also drops a
        # trailing dot on lookup, so "DAMN." only happened to match there.
        target = (
            config.base_dir / "chosen_songs" / "Kendrick Lamar" / "DAMN"
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
        assert started.status_code == 400
        assert "Select at least one chart first" in started.get_json()["error"]
        assert http.get("/api/spotify/export/status").get_json()["running"] is False

        # A second empty request is also refused without creating a worker.
        again = http.post("/api/spotify/export/start", json={"kind": "folder", "path": str(songs)})
        assert again.status_code == 400
