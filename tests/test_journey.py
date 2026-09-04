"""Slice 1 journey read-model and canonical song-list contracts."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.webapp import create_app


def track(provider: str, provider_id: str, artist: str, title: str) -> ProviderTrack:
    return ProviderTrack(provider=provider, provider_id=provider_id, artist=artist, title=title)


def seed_sources(config, workspace: int) -> None:
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [
            track("spotify", "s1", "Artist", "Alpha"),
            track("spotify", "s2", "Artist", "Shared"),
        ]))
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            track("soundcloud", "c1", "Artist", "Shared"),
            track("soundcloud", "c2", "Artist", "Zulu"),
        ]))
    finally:
        db.close()


@pytest.fixture
def journey_client(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, config


def wait_for_import(client, run_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/sources/soundcloud/import/status?run_id={run_id}").get_json()
        if not status["running"]:
            return status
        time.sleep(0.01)
    pytest.fail("SoundCloud import did not finish")


def upload(client, text: str, filename: str = "music.csv") -> dict:
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(text.encode()), filename)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    return wait_for_import(client, response.get_json()["run_id"])


def test_journey_no_sources_and_stable_envelope(journey_client):
    client, _ = journey_client
    response = client.get("/api/journey")
    assert response.status_code == 200
    data = response.get_json()
    assert data["schema_version"] == 1
    assert data["screen"] == "add_music"
    assert data["music"] == {
        "canonical_count": 0, "spotify_count": 0,
        "soundcloud_count": 0, "shared_count": 0, "songs": [],
    }
    assert data["sources"]["spotify"]["state"] == "not_added"
    assert data["sources"]["soundcloud"]["state"] == "not_added"
    assert data["sources"]["apple"]["state"] == "coming_soon"
    assert data["allowed_actions"]["find_charts"] is False


def test_soundcloud_import_active_and_ready(journey_client):
    client, _ = journey_client
    result = upload(
        client,
        "artist,title,playlist name\nA,One,First Set\nA,One,Second Set\n",
    )
    assert result["status"] == "complete"
    data = client.get("/api/journey").get_json()
    assert data["screen"] == "music_ready"
    assert data["sources"]["soundcloud"]["state"] == "added"
    assert data["sources"]["soundcloud"]["song_count"] == 1
    assert data["sources"]["soundcloud"]["unique_count"] == 1
    assert data["sources"]["soundcloud"]["playlist_count"] == 2
    assert data["sources"]["soundcloud"]["playlist_memberships"] == 2
    assert data["sources"]["soundcloud"]["raw_item_count"] == 2
    assert data["sources"]["soundcloud"]["duplicate_item_count"] == 1
    assert data["music"]["canonical_count"] == 1


def test_failed_first_soundcloud_import_has_no_usable_snapshot(journey_client):
    client, _ = journey_client
    result = upload(client, "not a soundcloud export", "broken.csv")
    assert result["status"] == "failed"
    data = client.get("/api/journey").get_json()
    assert data["screen"] == "attention"
    assert data["sources"]["soundcloud"]["state"] == "attention"
    assert data["sources"]["soundcloud"]["song_count"] == 0
    assert data["music"]["canonical_count"] == 0


def test_soundcloud_first_import_is_authoritatively_active(journey_client, monkeypatch):
    client, _ = journey_client
    import csv_to_clonehero.library_pipeline as library_pipeline

    entered = threading.Event()
    release = threading.Event()
    original = library_pipeline.run_library_import

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(library_pipeline, "run_library_import", blocked)
    response = client.post(
        "/api/sources/soundcloud/import",
        data={"file": (io.BytesIO(b"artist,title\nA,One\n"), "music.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    assert entered.wait(5)
    active = client.get("/api/journey").get_json()
    assert active["screen"] == "importing"
    assert active["operation"]["state"] == "importing"
    assert active["sources"]["soundcloud"]["state"] == "working"
    release.set()
    assert wait_for_import(client, response.get_json()["run_id"])["status"] == "complete"


def test_failed_soundcloud_replacement_retains_previous_snapshot(journey_client, monkeypatch):
    client, config = journey_client
    upload(client, "artist,title\nA,One\n")
    original = Database.upsert_library_snapshot

    def fail(*args, **kwargs):
        raise RuntimeError("replacement persistence failed")

    monkeypatch.setattr(Database, "upsert_library_snapshot", fail)
    result = upload(client, "artist,title\nA,Two\n", "replacement.csv")
    assert result["status"] == "failed"
    assert "retained_previous" in result["result"], result
    assert result["result"]["retained_previous"] is True
    data = client.get("/api/journey").get_json()
    assert data["sources"]["soundcloud"]["state"] == "attention"
    assert data["sources"]["soundcloud"]["song_count"] == 1
    assert data["sources"]["soundcloud"]["attention_reason"]
    assert data["music"]["songs"][0]["title"] == "One"
    monkeypatch.setattr(Database, "upsert_library_snapshot", original)


def test_spotify_connection_is_separate_from_import(journey_client):
    client, config = journey_client
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    db = Database(config.database_path)
    try:
        db.upsert_spotify_account({"spotify_user_id": "user-1", "display_name": "Test"})
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [track("spotify", "s1", "A", "One")]))
    finally:
        db.close()
    data = client.get("/api/journey").get_json()
    assert data["sources"]["spotify"]["imported"] is True
    assert data["sources"]["spotify"]["connected"] is False
    assert data["sources"]["spotify"]["state"] == "attention"
    assert data["music"]["canonical_count"] == 1
    assert data["music"]["spotify_count"] == 1
    assert data["sources"]["spotify"]["allowed_actions"]["reconnect"] is True


def test_spotify_imported_and_connected_state(journey_client):
    _, config = journey_client
    config.spotify.keyring.service = "journey-tests"
    config.spotify.keyring.user = "user"
    class Store:
        def get_password(self, service, username):
            return "refresh-token"
    app = create_app(config, source_factory=lambda cfg: [], keyring=Store())
    app.config["TESTING"] = True
    with app.test_client() as client:
        workspace = client.get("/api/journey").get_json()["workspace_id"]
        db = Database(config.database_path)
        try:
            db.upsert_spotify_account({"spotify_user_id": "user-1"})
            not_imported = client.get("/api/journey").get_json()
            assert not_imported["sources"]["spotify"]["connected"] is True
            assert not_imported["sources"]["spotify"]["imported"] is False
            assert not_imported["sources"]["spotify"]["state"] == "not_added"
            db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [track("spotify", "s1", "A", "One")]))
        finally:
            db.close()
        data = client.get("/api/journey").get_json()
        assert data["sources"]["spotify"]["connected"] is True
        assert data["sources"]["spotify"]["state"] == "added"
        assert data["sources"]["spotify"]["allowed_actions"]["update_music"] is True


def test_root_is_primary_and_legacy_page_routes_redirect(journey_client):
    client, _ = journey_client
    journey = client.get("/journey")
    assert journey.status_code == 302
    assert journey.headers["Location"] == "/"
    html = client.get("/").get_data(as_text=True)
    assert "journey.css" in html
    assert "journey.js" in html
    assert "prototype-2" not in html
    assert "/advanced" not in html
    assert client.get("/").status_code == 200
    advanced = client.get("/advanced")
    assert advanced.status_code == 302
    assert advanced.headers["Location"] == "/"


def test_journey_preserves_lan_access_token_model(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda cfg: [], lan_access_token="private-token")
    app.config["TESTING"] = True
    with app.test_client() as client:
        assert client.get("/journey", environ_base={"REMOTE_ADDR": "192.168.1.2"}).status_code == 401
        response = client.get("/journey?access_token=private-token", environ_base={"REMOTE_ADDR": "192.168.1.2"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/?access_token=private-token"
        response = client.get("/?access_token=private-token", environ_base={"REMOTE_ADDR": "192.168.1.2"})
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "static/journey.css?v=2.5.3&amp;access_token=private-token" in html
        assert "static/journey.js?v=2.5.3&amp;access_token=private-token" in html
        api = client.get("/api/journey?access_token=private-token", environ_base={"REMOTE_ADDR": "192.168.1.2"})
        assert api.status_code == 200


def test_canonical_counts_overlap_and_deterministic_order(journey_client):
    client, config = journey_client
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    seed_sources(config, workspace)
    data = client.get("/api/journey").get_json()
    assert data["music"]["canonical_count"] == 3
    assert data["music"]["spotify_count"] == 2
    assert data["music"]["soundcloud_count"] == 2
    assert data["music"]["shared_count"] == 1
    assert [song["title"] for song in data["music"]["songs"]] == ["Alpha", "Shared", "Zulu"]
    assert data["music"]["songs"][1]["sources"] == ["spotify", "soundcloud"]
    assert data["music"]["songs"][1].keys() == {"artist", "title", "sources"}


@pytest.mark.parametrize("format_name,content_type", [("txt", "text/plain"), ("csv", "text/csv"), ("json", "application/json")])
def test_song_list_exports_complete_canonical_set(journey_client, format_name, content_type):
    client, config = journey_client
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    seed_sources(config, workspace)
    response = client.get(f"/api/library/export?format={format_name}")
    assert response.status_code == 200
    assert response.content_type.startswith(content_type)
    assert re.search(r'filename="my-music-\d{4}-\d{2}-\d{2}\.' + format_name + '"', response.headers["Content-Disposition"])
    body = response.get_data()
    if format_name == "txt":
        assert body.decode().splitlines() == ["Artist - Alpha", "Artist - Shared", "Artist - Zulu"]
    elif format_name == "csv":
        assert body.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        assert [row["title"] for row in rows] == ["Alpha", "Shared", "Zulu"]
        assert rows[1]["sources"] == "spotify,soundcloud"
    else:
        payload = json.loads(body)
        assert payload["schema_version"] == 1
        assert payload["canonical_count"] == 3
        assert len(payload["songs"]) == 3
        assert "database" not in body.decode().lower()


def test_csv_formula_safety_and_empty_exports(journey_client):
    client, config = journey_client
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [track("soundcloud", "x", "=Artist", "-Formula")]))
    finally:
        db.close()
    csv_body = client.get("/api/library/export?format=csv").get_data(as_text=True)
    assert "'=Artist" in csv_body
    assert "'-Formula" in csv_body

    db = Database(config.database_path)
    try:
        db._conn.execute("DELETE FROM library_track_provenance")
        db._conn.execute("DELETE FROM library_tracks")
        db._conn.commit()
    finally:
        db.close()
    assert client.get("/api/library/export?format=txt").get_data() == b""
    assert client.get("/api/library/export?format=json").get_json()["songs"] == []
