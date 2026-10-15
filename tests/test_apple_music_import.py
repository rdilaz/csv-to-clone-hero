"""Metadata-only Apple Music export imports, isolation and discovery handoff."""

from __future__ import annotations

import io
import json
import plistlib
import time
from pathlib import Path

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.apple_music import AppleMusicExportProvider, MAX_EXPORT_BYTES
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.server.web import create_app


TXT = b"Name\tArtist\tAlbum\tLocation\nSong\tArtist\tAlbum\tfile:///private/song.m4a\nSong\tArtist\tAlbum\tignore\nMissing artist\t\tAlbum\tignore\n"


@pytest.fixture(autouse=True)
def private_test_temp(tmp_path, monkeypatch):
    # Keep upload scratch files inside the test sandbox on Windows as well.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "cp1252"])
def test_txt_metadata_only_dedup_and_encoding(encoding, tmp_path, monkeypatch):
    import csv_to_clonehero.providers.apple_music as module
    from tempfile import TemporaryDirectory

    created = []

    def temporary(**kwargs):
        directory = TemporaryDirectory(dir=tmp_path, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(module, "TemporaryDirectory", temporary)
    provider = AppleMusicExportProvider.from_bytes(
        TXT.decode().encode(encoding), extension=".TXT", label="Selected playlist"
    )
    snapshot = provider.import_library()
    assert len(snapshot.tracks) == 1
    song = snapshot.tracks[0]
    assert song.provider == "apple_music"
    assert song.provider_id.startswith("export:")
    assert snapshot.normalized_tracks()[0].spotify_id.startswith("apple_music:export:")
    assert song.playlist_names == ["Selected playlist"]
    assert song.url == ""
    assert "private" not in json.dumps(vars(song))
    assert snapshot.import_report.as_dict() == {
        "total_rows": 3, "accepted_rows": 2, "skipped_rows": 1,
        "skipped_reasons": [], "unique_tracks": 1, "duplicate_rows": 1,
        "playlists": ["Selected playlist"], "liked_count": 0,
        "missing_artist_count": 0, "missing_provider_id_count": 0,
        "inaccessible_count": 0, "error_count": 0,
    }
    assert provider.capabilities() == {"official_api": False, "oauth": False, "user_export": True}
    assert all(not path.exists() for path in created)
    assert list(tmp_path.iterdir()) == []


def test_xml_discards_unneeded_fields_and_uses_selected_export_label():
    raw = plistlib.dumps({
        "Library Persistent ID": "private-library-identity",
        "Tracks": {"1": {"Name": "Song", "Artist": "Artist", "Album": "Album",
                         "Location": "file:///private/song.m4a", "Persistent ID": "not-an-apple-catalog-id"},
                   "2": {"Name": "Incomplete"}},
        "Playlists": [{"Name": "Other group"}],
    })
    snapshot = AppleMusicExportProvider.from_bytes(raw, extension="xml", label="My selected export").import_library()
    assert len(snapshot.tracks) == 1
    assert snapshot.import_report.skipped_rows == 1
    assert snapshot.playlists[0].name == "My selected export"
    serialized = json.dumps({"metadata": snapshot.metadata, "tracks": [vars(song) for song in snapshot.tracks]})
    assert "private" not in serialized
    assert "not-an-apple-catalog-id" not in serialized
    assert "Other group" not in serialized


@pytest.mark.parametrize("raw,extension", [
    (b"", "txt"), (b"anything", "json"), (b"not a plist", "xml"),
    (b'<?xml version="1.0"?><plist><dict><key>Tracks</key><dict></plist>', "xml"),
    (plistlib.dumps({"Tracks": []}), "xml"),
    (b"Name\tArtist\nNo artist\t\n", "txt"),
])
def test_invalid_exports_raise_friendly_errors(raw, extension):
    with pytest.raises(ValueError):
        AppleMusicExportProvider.from_bytes(raw, extension=extension)


def test_export_limits_are_enforced_before_persistence(monkeypatch):
    import csv_to_clonehero.providers.apple_music as module

    with pytest.raises(ValueError, match="10 MB"):
        AppleMusicExportProvider.from_bytes(b"x" * (MAX_EXPORT_BYTES + 1), extension="txt")
    with pytest.raises(ValueError, match="too many unique"):
        AppleMusicExportProvider.from_bytes(b"Name\tArtist\nOne\tArtist\nTwo\tArtist\n", extension="txt", max_tracks=1)
    monkeypatch.setattr(module, "MAX_EXPORT_ROWS", 1)
    with pytest.raises(ValueError, match="too many rows"):
        AppleMusicExportProvider.from_bytes(TXT, extension="txt")


@pytest.fixture
def application(config, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "offline-test")
    config.server.enforce_secret_key = False
    config.sessions.cookie_secure = False
    app = create_app(config, source_factory=lambda _config: [])
    app.extensions["jobs"].shutdown(timeout=2)
    yield app
    app.extensions["jobs"].shutdown(timeout=2)
    app.extensions["downloads"].close()


def _sign_in(app, config, identity="export-owner"):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id=identity)
        stored = db.create_session(int(user["id"]), expires_at=time.time() + 3600)
    finally:
        db.close()
    client = app.test_client()
    with client.session_transaction() as session:
        session["sid"] = stored["session_id"]
    return client, int(user["id"])


def _upload(client, raw=TXT, filename="playlist.txt", **kwargs):
    return client.post("/api/sources/apple-music/import", data={
        "file": (io.BytesIO(raw), filename), "label": "Selected playlist",
    }, headers={"X-Riff-Request": "1", **kwargs})


def test_import_requires_owner_and_same_origin(application, config):
    anonymous = application.test_client()
    assert anonymous.get("/api/sources/apple-music/status").status_code == 401
    assert _upload(anonymous).status_code == 401
    owner, _ = _sign_in(application, config)
    response = owner.post("/api/sources/apple-music/import", data={"file": (io.BytesIO(TXT), "playlist.txt")})
    assert response.status_code == 403
    assert _upload(owner, Origin="https://untrusted.example").status_code == 403
    status = owner.get("/api/sources/apple-music/status").get_json()
    assert status["account_sync_available"] is False
    assert status["account_sync"] == "not_configured"


def test_import_is_private_replaces_only_apple_and_does_not_start_matching(application, config):
    owner, _ = _sign_in(application, config)
    other, _ = _sign_in(application, config, "another-owner")
    response = _upload(owner, filename="../../private-name.txt")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["metadata_only"] is True
    assert payload["combined"]["tracks"] == 1
    assert payload["source"]["report"]["skipped_rows"] == 1
    assert owner.get("/api/sources/apple-music/status").get_json()["imported_unique_tracks"] == 1
    assert other.get("/api/sources/apple-music/status").get_json()["imported_unique_tracks"] == 0
    db = Database(config.database_path)
    try:
        workspace = payload["workspace_id"]
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            ProviderTrack("soundcloud", "sc-one", "Shared", "Artist")
        ], metadata={"import": "export"}))
        assert db.get_running_jobs() == []
        assert db.get_workspace_candidates(workspace, kind="exact") == []
        assert db.list_user_jobs(1) == []
    finally:
        db.close()
    second = _upload(owner, b"Name\tArtist\nShared\tArtist\nNew\tArtist\n").get_json()
    assert second["combined"]["tracks"] == 2
    assert {source["provider"] for source in second["sources"]} == {"apple_music", "soundcloud"}
    assert "private-name" not in json.dumps(second)
    db = Database(config.database_path)
    try:
        assert {row["title"] for row in db.get_neutral_library_tracks(workspace)} == {"Shared", "New"}
        assert all(row["provider_id"].startswith("export:")
                   for row in db.get_track_provenance(workspace) if row["provider"] == "apple_music")
    finally:
        db.close()


def test_import_rejects_malformed_and_over_limit_without_erasing_saved_music(application, config):
    owner, _ = _sign_in(application, config)
    assert _upload(owner).status_code == 200
    assert _upload(owner, b'<?xml version="1.0"?><plist><dict></plist>', "bad.xml").status_code == 422
    assert _upload(owner, TXT, "bad.json").status_code == 400
    config.server.max_library_tracks = 1
    assert _upload(owner, b"Name\tArtist\nOne\tArtist\nTwo\tArtist\n").status_code == 422
    assert owner.get("/api/sources/apple-music/status").get_json()["imported_unique_tracks"] == 1
    config.server.max_content_length = 40
    assert _upload(owner).status_code == 413


def test_active_job_blocks_export_replacement(application, config):
    owner, user_id = _sign_in(application, config)
    payload = _upload(owner).get_json()
    db = Database(config.database_path)
    try:
        workspace = db.get_workspace(payload["workspace_id"])
        # A queued download must also protect the input its selection is based on.
        db.create_job(user_id, "download", workspace_key=workspace["workspace_key"])
        assert _upload(owner).status_code == 409
    finally:
        db.close()


def test_apple_export_hands_metadata_to_existing_live_fallback_without_provider_calls(config, monkeypatch):
    from csv_to_clonehero.server.jobs import JobManager
    from csv_to_clonehero.server.security import TokenCipher, generate_fernet_key
    from csv_to_clonehero.sources.enchor import PREFLIGHT_VERSION
    from csv_to_clonehero.spotify_pipeline import DiscoveryResult, DiscoveryStats

    config.server.enforce_secret_key = False
    db = Database(config.database_path)
    user = db.upsert_user(spotify_user_id="apple-only-owner")
    key = Database.workspace_key_for_user("apple-only-owner")
    workspace = db.create_workspace(workspace_key=key, name="apple-only")
    db.upsert_library_snapshot(workspace, AppleMusicExportProvider.from_bytes(TXT, extension="txt").import_library())
    db.record_source_preflight({
        "source_name": "enchor", "endpoint": "/search", "request_version": PREFLIGHT_VERSION,
        "success": True, "supported": True, "authoritative": True, "found": 1, "out_of": 1,
    }, ttl_seconds=3600)
    captured = {}

    def discovery(_config, _db, _sources, client, **kwargs):
        assert client is None
        captured["records"] = kwargs["prefetched_library"].records
        return DiscoveryResult(status="complete", phase="complete", stats=DiscoveryStats())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A user export must not request provider account access")

    monkeypatch.setattr("csv_to_clonehero.spotify_pipeline.run_spotify_discovery", discovery)
    monkeypatch.setattr("csv_to_clonehero.server.jobs.build_oauth", forbidden)
    monkeypatch.setattr("csv_to_clonehero.server.jobs.build_spotify_client", forbidden)
    manager = JobManager(config, TokenCipher(generate_fernet_key()), source_factory=lambda _config: [], auto_start=False)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=key)
        manager._run_job(job)
        assert manager.get(job.id).status == "complete"
        assert [(row["title"], row["artists"]) for row in captured["records"]] == [("Song", ["Artist"])]
        assert captured["records"][0]["spotify_id"].startswith("apple_music:")
    finally:
        manager.shutdown(timeout=2)
        db.close()
