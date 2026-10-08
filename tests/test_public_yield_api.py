"""Coverage expansion remains explicit, owner-scoped and note-validated."""
import json
import time

import pytest

from csv_to_clonehero.combined_pipeline import expand_combined
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from tests.conftest import make_chart
from tests.test_server_web import app, client, config, spotify_client_id, _sign_in


def _seed_catalog(client, config, monkeypatch, *, artists=1, songs=13):
    _sign_in(client)
    client.get("/api/sources")
    names = [f"Artist {index}" for index in range(artists)]
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(Database.workspace_key_for_user("user-1"))["id"]
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            ProviderTrack("soundcloud", str(index), "Known", name)
            for index, name in enumerate(names)
        ]))
        generation = db.create_discovery_scan(
            workspace, scan_key="yield-exact", kind="exact", status="complete",
            metadata={"workflow": "combined_catalog"},
        )
        db.update_workspace(workspace, status="complete", phase="complete",
                            metadata={"workflow": "public_catalog"})
    finally:
        db.close()
    charts = {}
    for index, name in enumerate(names):
        values = [make_chart(f"More {number:02}", name, chart_id=1000 + index * 100 + number,
                             instruments=["guitar"]) for number in range(songs)]
        values.append(make_chart("Known", name, chart_id=50000 + index))
        values.append(make_chart("No notes", name, chart_id=51000 + index,
                                 instruments=[], diff_guitar=None))
        charts[name.casefold()] = values

    class Catalog:
        available = True
        diagnostic = ""
        snapshot_id = "yield-fixture"
        chart_count = sum(len(values) for values in charts.values())
        def artist_songs(self, artist):
            return tuple((chart.name_norm, (chart,)) for chart in charts.get(artist, ()))

    catalog = Catalog()
    monkeypatch.setattr("csv_to_clonehero.catalog.load_enchor_catalog", lambda *args, **kwargs: catalog)
    return workspace, generation, charts, catalog


def _wait_job(client, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"complete", "failed", "cancelled"}:
            assert job["status"] == "complete", job
            return job
        time.sleep(0.03)
    pytest.fail("fixture expansion did not complete")


def test_all_expansion_keeps_guitar_only_songs_beyond_recommendation_cap(client, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch)
    config.expansion.cap_one_song = 2
    db = Database(config.database_path)
    try:
        default = expand_combined(config, db, workspace, ["artist 0"], catalog=catalog,
                                  exact_generation_id=generation)
        assert default.candidates == 0  # quality is still a gate in the legacy recommendation mode
        result = expand_combined(config, db, workspace, ["artist 0"], catalog=catalog,
                                 exact_generation_id=generation, coverage_mode="all")
        rows = db.get_workspace_candidates(workspace, kind="expansion")
        assert result.candidates == len(rows) == 13
        assert all(row["eligibility"] == "ELIGIBLE_REVIEW" for row in rows)
        assert all(json.loads(row["metadata_json"])["coverage_mode"] == "all" for row in rows)
        assert all(row["score"] < 70 for row in rows)
        assert all(row["chart_id"] < 50000 for row in rows)
    finally:
        db.close()


def test_all_artists_resolves_complete_owner_list_in_one_job_and_bulk_selection(client, app, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, artists=30, songs=3)
    app.extensions["jobs"].source_factory = lambda _: pytest.fail("artist expansion must not query a provider")
    choices = client.get("/api/expansion/artists").get_json()
    assert choices["supports_all_artists"] and choices["supports_bulk_selection"]
    assert choices["eligible_artist_count"] == len(choices["artists"]) == 30
    assert choices["max_artists"] == 25
    assert choices["coverage_modes"] == ["all", "recommended"]
    ordinary = client.post("/api/expansion", json={"artists": list(charts), "coverage_mode": "all"})
    assert ordinary.status_code == 400
    start = client.post("/api/expansion", json={"artists": [], "all_artists": True, "coverage_mode": "all"})
    assert start.status_code == 202
    job = _wait_job(client, start.get_json()["job"]["id"])
    assert job["result"]["all_artists"] is True
    assert job["result"]["coverage_mode"] == "all"
    assert job["result"]["network_requests"] == 0
    assert len(job["result"]["artists"]) == 30
    results = client.get("/api/expansion/results").get_json()
    assert results["total"] == 90 and len(results["songs"]) == 25
    assert client.post("/api/expansion/selection/bulk", json={"preset": "one_per_song"}).status_code == 400
    selected = client.post("/api/expansion/selection/bulk", json={"preset": "one_per_song", "confirm_possible": True})
    assert selected.status_code == 200, selected.get_json()
    assert selected.get_json()["songs"] == selected.get_json()["charts"] == 90
    assert client.get("/api/expansion/results").get_json()["summary"]["selected"] == 90
    # A second click replaces the expansion set rather than duplicating songs.
    assert client.post("/api/expansion/selection/bulk", json={"confirm_possible": True}).get_json()["charts"] == 90
    anonymous = app.test_client()
    assert anonymous.get("/api/expansion/results").status_code == 401
    assert anonymous.post("/api/expansion/selection/bulk", json={"confirm_possible": True}).status_code == 401


@pytest.mark.parametrize("payload", [
    {"artists": ["artist 0"], "coverage_mode": "everything"},
    {"artists": ["artist 0"], "coverage_mode": []},
    {"artists": [], "all_artists": "true", "coverage_mode": "all"},
])
def test_expansion_rejects_unsupported_or_ambiguous_coverage(client, config, monkeypatch, payload):
    _seed_catalog(client, config, monkeypatch)
    assert client.post("/api/expansion", json=payload).status_code == 400


def test_bulk_expansion_excludes_stale_generation_and_blocks_active_jobs(client, app, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, songs=1)
    db = Database(config.database_path)
    try:
        expand_combined(config, db, workspace, ["artist 0"], catalog=catalog,
                        exact_generation_id=generation - 1, coverage_mode="all")
    finally:
        db.close()
    assert client.post("/api/expansion/selection/bulk", json={"confirm_possible": True}).status_code == 400
    monkeypatch.setattr(Database, "get_active_job", lambda *args, **kwargs: {"id": 99})
    assert client.post("/api/expansion/selection/bulk", json={"confirm_possible": True}).status_code == 409


def test_exact_recommendation_search_by_charter_preserves_candidate_object(client, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, songs=1)
    db = Database(config.database_path)
    try:
        track = db.get_library_tracks(workspace_id=workspace)[0]
        chart = make_chart("Known", "Artist 0", chart_id=92001, charter="OnlyFixtureCharter")
        task = db.upsert_discovery_task(workspace, scan_id=generation, source="test",
                                       query_key="known", status="complete")
        db.upsert_workspace_candidate(workspace, track.track_key, chart, score=99,
                                      eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task)
    finally:
        db.close()
    selected = client.post("/api/selection/bulk", json={
        "preset": "best_matches", "search": "OnlyFixtureCharter",
    })
    assert selected.status_code == 200, selected.get_json()
    assert selected.get_json()["changed"] == 1
