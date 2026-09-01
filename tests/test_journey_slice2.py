"""Slice 2 integration tests for the local journey matching boundary."""

from __future__ import annotations

import io
import threading
import time

import pytest

from csv_to_clonehero.catalog import CatalogLookup
from csv_to_clonehero.combined_pipeline import CombinedDiscoveryResult, discover_combined, matching_scan_fingerprint
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.webapp import create_app

from .conftest import make_chart


class FixtureCatalog:
    available = True
    diagnostic = ""
    snapshot_id = "slice-2-catalog"
    chart_count = 1

    def __init__(self, chart):
        self.chart = chart

    def candidates(self, artist, title):
        return (self.chart,) if (artist, title) == ("Artist", "Song") else ()

    def high_recall_candidates(self, artist, title, **_kwargs):
        candidates = self.candidates(artist, title)
        return CatalogLookup(
            candidates=candidates,
            strict_exact=bool(candidates),
            methods=("exact",) if candidates else (),
            retrieved_song_keys=1 if candidates else 0,
            exact_artist=bool(candidates),
            exact_title=bool(candidates),
        )

    def artist_songs(self, artist):
        return (("Song", (self.chart,)),) if artist == "artist" else ()


@pytest.fixture
def matching_client(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, config, app


def local_workspace(client, config) -> int:
    return int(client.get("/api/journey").get_json()["workspace_id"])


def seed_library(client, config, count: int = 1) -> int:
    workspace = local_workspace(client, config)
    db = Database(config.database_path)
    try:
        values = [ProviderTrack(provider="soundcloud", provider_id=f"sc-{i}", artist="Artist", title="Song" if i == 0 else f"Song {i}") for i in range(count)]
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", values))
    finally:
        db.close()
    return workspace


def seed_completed(client, config, monkeypatch, count: int = 1) -> int:
    workspace = seed_library(client, config, count)
    chart = make_chart("Song", "Artist", chart_id=900)
    catalog = FixtureCatalog(chart)
    manifest = {"snapshot_id": catalog.snapshot_id, "sha256": "slice-2-sha", "chart_count": 1}
    import csv_to_clonehero.catalog as catalog_module
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(catalog_module, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: catalog)
    db = Database(config.database_path)
    try:
        key = db.get_meta("local_workspace_key")
        discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [ProviderTrack(provider="soundcloud", provider_id="sc-0", artist="Artist", title="Song")])],
            workspace_key=key,
        )
    finally:
        db.close()
    return workspace


def wait_for_state(client, state: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = client.get("/api/journey").get_json()
        if latest["matching"]["state"] == state:
            return latest
        time.sleep(0.01)
    pytest.fail(f"expected {state}, got {latest}")


def test_music_ready_allows_find_charts_and_read_does_not_start_matching(matching_client):
    client, config, app = matching_client
    seed_library(client, config)
    before = client.get("/api/journey").get_json()
    assert before["matching"]["state"] == "music_ready"
    assert before["allowed_actions"]["find_charts"] is True
    assert app.extensions["spotify_job"].running is False
    assert client.get("/api/journey").get_json()["matching"]["state"] == "music_ready"


def test_find_charts_uses_local_combined_path_and_restores_without_scoring(matching_client, monkeypatch):
    client, config, _ = matching_client
    workspace = seed_completed(client, config, monkeypatch)
    import csv_to_clonehero.combined_pipeline as pipeline

    entered = threading.Event()
    release = threading.Event()
    original_fast = pipeline.completed_scan_fast_path

    def blocked_fast(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_fast(*args, **kwargs)

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", blocked_fast)
    monkeypatch.setattr(pipeline, "score_candidate", lambda *args, **kwargs: pytest.fail("warm path scored a candidate"))
    response = client.post("/api/discovery/start", json={"source": "combined", "workspace_id": workspace})
    assert response.status_code == 202
    restoring = client.get("/api/journey").get_json()
    assert restoring["matching"]["state"] == "matching_restoring"
    assert restoring["matching"]["results"] is None
    assert entered.wait(5)
    release.set()
    found = wait_for_state(client, "charts_found")
    assert found["matching"]["authoritative"] is True
    assert found["matching"]["details"]["saved_work_reuse"] is True


def test_identical_soundcloud_reimport_preserves_authoritative_matching(matching_client, monkeypatch):
    client, config, _ = matching_client
    seed_completed(client, config, monkeypatch)
    result = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"artist,title\nArtist,Song\n"), "same.csv")},
        content_type="multipart/form-data",
    )
    assert result.status_code == 202
    status = wait_for_state(client, "charts_found")
    assert status["matching"]["authoritative"] is True
    import_status = client.get(f"/api/sources/soundcloud/import/status?run_id={result.get_json()['run_id']}").get_json()
    assert import_status["result"]["preserved_matching"] is True


def test_changed_soundcloud_reimport_invalidates_old_matching(matching_client, monkeypatch):
    client, config, _ = matching_client
    seed_completed(client, config, monkeypatch)
    result = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"artist,title\nArtist,Changed\n"), "changed.csv")},
        content_type="multipart/form-data",
    )
    assert result.status_code == 202
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/sources/soundcloud/import/status?run_id={result.get_json()['run_id']}").get_json()
        if not status["running"]:
            break
        time.sleep(0.01)
    assert status["result"]["preserved_matching"] is False
    matching = client.get("/api/journey").get_json()["matching"]
    assert matching["state"] == "music_ready"
    assert matching["results"] is None


def test_cold_find_charts_calls_discover_combined_and_exposes_real_progress(matching_client, monkeypatch):
    client, config, _ = matching_client
    workspace = seed_library(client, config)
    chart = make_chart("Song", "Artist", chart_id=901)
    catalog = FixtureCatalog(chart)
    manifest = {"snapshot_id": catalog.snapshot_id, "sha256": "cold-sha", "chart_count": 1}
    import csv_to_clonehero.catalog as catalog_module
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(catalog_module, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: catalog)
    discover_calls = []
    original_discover = pipeline.discover_combined

    def spy_discover(*args, **kwargs):
        discover_calls.append((args, kwargs))
        return original_discover(*args, **kwargs)

    monkeypatch.setattr(pipeline, "discover_combined", spy_discover)
    entered = threading.Event()
    release = threading.Event()
    original_score = pipeline.score_candidate

    def blocked_score(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_score(*args, **kwargs)

    monkeypatch.setattr(pipeline, "score_candidate", blocked_score)
    response = client.post("/api/discovery/start", json={"source": "combined", "workspace_id": workspace})
    assert response.status_code == 202
    assert client.get("/api/journey").get_json()["matching"]["state"] == "matching_restoring"
    assert entered.wait(5)
    running = client.get("/api/journey").get_json()
    assert running["matching"]["state"] == "matching_running"
    assert running["matching"]["total"] == 1
    assert running["matching"]["processed"] == 0
    assert running["matching"]["current_song"] == {"artist": "Artist", "title": "Song"}
    release.set()
    assert wait_for_state(client, "charts_found")["matching"]["authoritative"] is True
    assert len(discover_calls) == 1
    assert discover_calls[0][1]["expansion_artists"] == ()


def test_final_result_partition_uses_recommended_and_review_only(matching_client, monkeypatch):
    client, config, _ = matching_client
    workspace = seed_library(client, config, count=4)
    chart = make_chart("Song", "Artist", chart_id=902)
    manifest = {"snapshot_id": "partition", "sha256": "partition-sha", "chart_count": 1}
    import csv_to_clonehero.catalog as catalog_module
    monkeypatch.setattr(catalog_module, "read_catalog_manifest", lambda _path: manifest)
    import csv_to_clonehero.combined_pipeline as pipeline
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    db = Database(config.database_path)
    try:
        key = db.get_meta("local_workspace_key")
        db.initialize_discovery_song_progress(workspace, [
            type("T", (), {"track_key": "artist|song", "artist_full": "Artist", "title": "Song"})(),
            type("T", (), {"track_key": "artist|song 1", "artist_full": "Artist", "title": "Song 1"})(),
            type("T", (), {"track_key": "artist|song 2", "artist_full": "Artist", "title": "Song 2"})(),
            type("T", (), {"track_key": "artist|song 3", "artist_full": "Artist", "title": "Song 3"})(),
        ])
        task_scan = db.create_discovery_scan(workspace, scan_key="partition-generation", kind="exact", status="running", metadata={"workflow": "combined_catalog"})
        task = db.upsert_discovery_task(workspace, scan_id=task_scan, source="combined", kind="generation", query_key="partition", status="running")
        for key_name, classification in (("artist|song", "strong"), ("artist|song 1", "review"), ("artist|song 2", "no_suitable"), ("artist|song 3", "not_playable")):
            db.update_discovery_song_progress(workspace, key_name, status="complete", classification=classification)
        db.upsert_candidate(chart)
        track_id = db._conn.execute("SELECT id FROM spotify_tracks WHERE track_key=?", ("artist|song",)).fetchone()[0]
        db.upsert_workspace_candidate(workspace, track_id, chart, task_id=task, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", score=95)
        db.update_discovery_task(task, status="complete", completed=4, total=4)
        db.create_discovery_scan(workspace, scan_key="partition-generation", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        count, digest, _ = db.workspace_current_candidate_digest(workspace)
        fp = matching_scan_fingerprint((), manifest, config, library_fingerprint=db.get_library_matching_fingerprint(workspace))
        db.save_matching_scan_cache(workspace, fp, 4, {"tracks": 4, "persisted_candidates": count, "candidate_relationship_digest": digest})
        db.update_workspace_progress(workspace, status="complete", phase="complete", total_tracks=4, completed_tracks=4, metadata={"run_id": "partition-run", "scan_key": "catalog"})
        db.update_workspace(workspace, status="complete", phase="complete")
    finally:
        db.close()
    result = client.get("/api/journey").get_json()["matching"]
    assert result["state"] == "charts_found"
    assert result["results"] == {"best_matches": 1, "best_available": 1, "recommendations": 2, "no_suitable": 1, "not_playable": 1, "no_candidate": 0}
    assert sum(result["results"][key] for key in ("best_matches", "best_available", "no_suitable", "not_playable", "no_candidate")) == 4


@pytest.mark.parametrize("terminal_status,expected", [("interrupted", "matching_interrupted"), ("failed", "matching_failed"), ("partial", "matching_interrupted")])
def test_newest_noncomplete_generation_hides_old_completed_results(matching_client, monkeypatch, terminal_status, expected):
    client, config, _ = matching_client
    workspace = seed_completed(client, config, monkeypatch)
    db = Database(config.database_path)
    try:
        db.create_discovery_scan(workspace, scan_key=f"newest-{terminal_status}", kind="exact", status=terminal_status, metadata={"workflow": "combined_catalog"})
        db.update_workspace(workspace, status=terminal_status, phase=terminal_status)
    finally:
        db.close()
    matching = client.get("/api/journey").get_json()["matching"]
    assert matching["state"] == expected
    assert matching["authoritative"] is False
    assert matching["results"] is None


def test_matching_cannot_start_twice_and_blocks_import(matching_client, monkeypatch):
    client, config, _ = matching_client
    workspace = seed_library(client, config)
    import csv_to_clonehero.combined_pipeline as pipeline
    entered = threading.Event()
    release = threading.Event()

    def blocked_fast(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", blocked_fast)
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: (_ for _ in ()).throw(RuntimeError("stop cold path")))
    assert client.post("/api/discovery/start", json={"source": "combined", "workspace_id": workspace}).status_code == 202
    assert client.get("/api/journey").get_json()["matching"]["state"] == "matching_restoring"
    assert entered.wait(5)
    assert client.post("/api/discovery/start", json={"source": "combined", "workspace_id": workspace}).status_code == 409
    upload = client.post("/api/sources/soundcloud/import", data={"file": (io.BytesIO(b"artist,title\nA,B\n"), "music.csv")}, content_type="multipart/form-data")
    assert upload.status_code == 409
    release.set()


def test_failed_newest_generation_stays_failed_after_restart(matching_client, monkeypatch):
    client, config, _ = matching_client
    workspace = seed_library(client, config)
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: (_ for _ in ()).throw(RuntimeError("fixture matching failure")))
    assert client.post("/api/discovery/start", json={"source": "combined", "workspace_id": workspace}).status_code == 202
    failed = wait_for_state(client, "matching_failed")
    assert failed["matching"]["results"] is None
    fresh_app = create_app(config, source_factory=lambda cfg: [])
    fresh_app.config["TESTING"] = True
    with fresh_app.test_client() as fresh:
        assert fresh.get("/api/journey").get_json()["matching"]["state"] == "matching_failed"


def test_process_restart_recovers_running_generation_as_interrupted(matching_client):
    client, config, _ = matching_client
    workspace = seed_library(client, config)
    db = Database(config.database_path)
    try:
        db.create_discovery_scan(workspace, scan_key="restart-running", kind="exact", status="running", metadata={"workflow": "combined_catalog"})
        db.update_workspace(workspace, status="running", phase="finding_charts")
    finally:
        db.close()
    fresh_app = create_app(config, source_factory=lambda cfg: [])
    fresh_app.config["TESTING"] = True
    with fresh_app.test_client() as fresh:
        matching = fresh.get("/api/journey").get_json()["matching"]
        assert matching["state"] == "matching_interrupted"
        assert matching["results"] is None


def test_running_progress_and_elapsed_are_server_driven(matching_client):
    client, config, app = matching_client
    workspace = seed_library(client, config)
    started_at = time.time() - 75
    job = app.extensions["spotify_job"]
    job.begin(workspace, workspace_key="local", scan_key="catalog", run_id="elapsed-run")
    job.started_at = started_at
    job.phase = "finding_charts"
    job.message = "Finding your Clone Hero charts..."

    db = Database(config.database_path)
    try:
        db.initialize_discovery_song_progress(workspace, [
            type("T", (), {"track_key": "artist|song", "artist_full": "Artist", "title": "Song"})(),
        ])
        db.update_discovery_song_progress(workspace, "artist|song", status="checking")
        db.update_workspace_progress(
            workspace,
            status="running",
            phase="finding_charts",
            total_tracks=1,
            completed_tracks=0,
            metadata={"run_id": "elapsed-run", "scan_key": "catalog", "started_at": started_at},
        )
    finally:
        db.close()

    try:
        matching = client.get("/api/journey").get_json()["matching"]
        assert matching["state"] == "matching_running"
        assert matching["processed"] == 0
        assert matching["total"] == 1
        assert matching["current_song"] == {"artist": "Artist", "title": "Song"}
        assert matching["started_at"] == started_at
        assert matching["elapsed_seconds"] >= 70
    finally:
        with job.lock:
            job.running = False
            job.status = "created"
            job.worker = None


def test_in_memory_matching_failure_hides_older_authoritative_results(matching_client, monkeypatch):
    client, config, app = matching_client
    workspace = seed_completed(client, config, monkeypatch)
    job = app.extensions["spotify_job"]
    job.begin(workspace, workspace_key="local", scan_key="catalog", run_id="failed-new-run")
    job.fail("new matching run failed")

    matching = client.get("/api/journey").get_json()["matching"]
    assert matching["state"] == "matching_failed"
    assert matching["authoritative"] is False
    assert matching["results"] is None
