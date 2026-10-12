"""Delivery recovery stays bounded independently of full review hydration."""

import json
import time

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.server import web
from tests.test_server_web import app, client, config, spotify_client_id, _sign_in
from tests.test_studio_api import _seed


def test_delivery_status_requires_live_session_and_never_creates_workspace(client, app, config):
    assert client.get("/api/delivery/status").status_code == 401
    _sign_in(client)
    response = client.get("/api/delivery/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["workspace_id"] is None
    assert payload["selection_count"] == 0
    assert payload["download"] is None and payload["download_job"] is None
    db = Database(config.database_path)
    assert db.get_workspace(Database.workspace_key_for_user("user-1")) is None
    with client.session_transaction() as session:
        db.revoke_session(session["sid"])
    db.close()
    assert client.get("/api/delivery/status").status_code == 401


def test_5909_saved_choices_count_without_review_or_candidate_hydration(client, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    db = Database(config.database_path)
    template = json.loads(db._conn.execute("SELECT data_json FROM candidate_charts WHERE id=?", (ids[0],)).fetchone()[0])
    # Bulk fixture setup retains real candidate identities and exact variants
    # while avoiding thousands of unrelated per-row setup transactions.
    charts = []
    for index in range(5903):
        chart_id = 90000 + index
        value = {**template, "chart_id": chart_id, "md5": f"{chart_id:032d}"}
        charts.append(("test", chart_id, value["md5"], json.dumps(value)))
    with db._conn:
        db._conn.executemany("INSERT INTO candidate_charts(source,chart_id,md5,data_json) VALUES(?,?,?,?)", charts)
    more_ids = [int(row[0]) for row in db._conn.execute("SELECT id FROM candidate_charts WHERE source='test' AND chart_id>=90000 ORDER BY chart_id")]
    now = time.time()
    selected = [(workspace, "exact", tracks[index // 2].db_id, candidate, "", now, now) for index, candidate in enumerate(ids)]
    selected += [(workspace, "exact", tracks[0].db_id, candidate, "", now, now) for candidate in more_ids]
    selected += [(workspace, "expansion", None, extra, "artist", now, now),
                 (workspace, "catalog", None, wrong, "haste the day", now, now)]
    with db.atomic_workspace_selection():
        with db.record_public_selection_change(workspace, "Fixture saved choices", generation):
            db._conn.executemany("""INSERT INTO workspace_selections
                (workspace_id,kind,track_id,candidate_id,artist_norm,selected_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", selected)
    before = db._public_selection_rows(workspace)
    expected_receipt = db.public_selection_receipt(workspace)
    assert len(before) == 5909
    db.close()

    def expensive(*args, **kwargs):
        pytest.fail("ZIP status must not hydrate or rescore review candidates")

    for name in ("_review_cards", "_expansion_review_cards", "_spotify_library_payload", "_provider_library_payload"):
        monkeypatch.setattr(web, name, expensive)
    for name in ("get_workspace_candidates", "get_spotify_tracks", "list_workspace_selections", "candidate_from_workspace_row"):
        monkeypatch.setattr(Database, name, expensive)

    response = client.get("/api/delivery/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["workspace_id"] == workspace
    assert payload["exact_generation_id"] == generation
    assert payload["selection_summary"] == {
        "library": {"songs": 2, "charts": 5907},
        "expansion": {"songs": 1, "charts": 1},
        "catalog": {"songs": 1, "charts": 1},
    }
    assert payload["selection_count"] == 5909
    assert payload["selection_counts_are_saved"] is True
    assert {name: payload[name] for name in expected_receipt} == expected_receipt
    assert payload["undo"]["available"]
    assert "review" not in payload and "selected" not in payload
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()


def test_delivery_status_receipts_are_owner_scoped_and_do_not_consume_zip(client, app, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    app.extensions["jobs"].shutdown(timeout=2)
    registry = app.extensions["downloads"]
    archive = registry.root / "status-owner.zip"
    archive.write_bytes(b"PK\x03\x04status-only")
    meta = registry.create(1, workspace, archive)
    db = Database(config.database_path)
    key = Database.workspace_key_for_user("user-1")
    matching = db.create_job(1, "discovery", workspace_key=key)
    db.update_job(int(matching["id"]), status="complete", phase="complete")
    expansion = db.create_job(1, "expansion", workspace_key=key)
    db.update_job(int(expansion["id"]), status="complete", phase="complete")
    download_job = db.create_job(1, "download", workspace_key=key)
    db.update_job(int(download_job["id"]), status="complete", phase="complete", result={"archive_id": meta["archive_id"]})
    preflight = db.create_job(1, "preflight", workspace_key=key)
    db.update_job(int(preflight["id"]), status="complete", phase="complete")
    # A newer job for the same user but another workspace is not its receipt.
    unrelated = db.create_job(1, "download", workspace_key="local:other")
    db.update_job(int(unrelated["id"]), status="complete", phase="complete")
    db.close()
    for _ in range(2):
        payload = client.get("/api/delivery/status").get_json()
        assert payload["download"]["token"] == meta["token"]
        assert payload["download"]["archive_id"] == meta["archive_id"]
        assert payload["download"]["url"] == f"/api/download/{meta['token']}"
        assert "path" not in payload["download"]
        assert payload["job"]["id"] == expansion["id"]
        assert payload["download_job"]["id"] == download_job["id"]
        assert payload["preflight_job"]["id"] == preflight["id"]
        assert "workspace_key" not in payload["download_job"]
        assert registry.resolve(meta["token"]) is not None
        assert archive.exists()

    monkeypatch.setattr(web, "_fetch_profile", lambda *_: {"id": "user-2", "display_name": "Other"})
    other = app.test_client()
    _sign_in(other)
    other_status = other.get("/api/delivery/status").get_json()
    assert other_status["download"] is None and other_status["download_job"] is None
    assert other.get(payload["download"]["url"]).status_code == 404
    assert registry.resolve(meta["token"]) is not None
    streamed = client.get(payload["download"]["url"], buffered=True)
    assert streamed.status_code == 200
    assert streamed.data == b"PK\x03\x04status-only"
    assert client.get("/api/delivery/status").get_json()["download"] is None
    assert client.get(payload["download"]["url"]).status_code == 404

