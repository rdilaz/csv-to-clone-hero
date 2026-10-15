"""Offline release-wrapper integration using real guarded APIs and ZIP bytes."""
from __future__ import annotations

import hashlib
import io
import threading
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.sources.base import SourceError
from csv_to_clonehero.unified_preview import CachedSource, LOCAL_OWNER, create_app
from tests.catalog_audio_fixtures import sng_audio, wav_audio
from tests.conftest import make_chart
from tests.test_catalog import write_snapshot

HEADERS = {"X-Riff-Request": "1", "Origin": "http://localhost"}


@pytest.fixture
def preview(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("The local release preview must never construct a live source or send HTTP")

    monkeypatch.setattr("csv_to_clonehero.sources.build_sources", forbidden)
    monkeypatch.setattr("httpx.Client.request", forbidden)
    # Ambient production settings must not redirect this opt-in wrapper.
    for name in ("DATABASE_URL", "CATALOG_PATH", "SECRET_KEY", "TOKEN_ENCRYPTION_KEY"):
        monkeypatch.setenv(name, "ignored-invalid-preview-environment")
    cache = tmp_path / "read-only-cache"
    library = tmp_path / "real-Songs"
    runtime = tmp_path / "runtime"
    cache.mkdir()
    library.mkdir()
    package = sng_audio(metadata={"name": "Cached Song", "artist": "Local Artist"}, files=[
        ("notes.chart", b"[Song]\n{\nResolution = 192\n}\n[ExpertSingle]\n{\n0 = N 0 0\n}\n"),
        ("song.wav", wav_audio()),
    ])
    charts = []
    for index, title in enumerate(("Cached Song", "Unavailable Song", "Cached Alias"), 1):
        chart = make_chart(title, "Local Artist", chart_id=index)
        chart.source = "enchor"
        chart.year = "1995"
        charts.append(chart)
    charts[2].md5 = charts[0].md5
    key = hashlib.sha256(f"enchor\0source-id:{charts[0].md5}".encode()).hexdigest()
    artifact = cache / f"{key}.sng"
    artifact.write_bytes(package)
    (library / "owned.sng").write_bytes(package)
    catalog = tmp_path / "catalog.jsonl"
    write_snapshot(catalog, charts)
    app = create_app(catalog_path=catalog, runtime_root=runtime, artifact_roots=(cache,),
                     library_roots=(library,), background_inventory=False)
    app.testing = True
    try:
        yield app, app.test_client(), runtime, library, artifact
    finally:
        app.extensions["jobs"].shutdown(timeout=2)
        app.extensions["downloads"].close()


def pick(client, key="enchor:1", **overrides):
    history = client.get("/api/selection/history").get_json()
    catalog = client.get("/api/catalog").get_json()
    payload = {"catalog_key": key, "snapshot_token": catalog["snapshot_token"],
               "selection_revision": history["selection_revision"],
               "exact_generation_id": int(history["exact_generation_id"] or 0), "action": "select"}
    payload.update(overrides)
    return client.post("/api/catalog/selection", json=payload, headers=HEADERS)


def wait_job(client, job_id):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"complete", "partial", "failed", "cancelled"}:
            return job
        time.sleep(0.03)
    pytest.fail("Local ZIP preparation did not complete promptly")


def test_real_catalog_cached_media_and_inventory_are_honest(preview):
    app, client, runtime, library, artifact = preview
    before = artifact.read_bytes(), (library / "owned.sng").read_bytes()
    catalog = client.get("/api/catalog").get_json()
    assert catalog["total_charts"] == 3 and catalog["total_songs"] == 3
    cached, unavailable = catalog["items"][:2]
    assert cached["preview"]["available"] is True
    assert cached["preview"]["local_url"].startswith("/api/companion/audio/")
    assert cached["preview"]["clip_limit_ms"] == 45000
    assert cached["installed"] is True and cached["selectable"] is True
    assert unavailable["selectable"] is False
    assert unavailable["preview"]["url"] is None and unavailable["preview"]["available"] is False
    assert "cached" in unavailable["blocked_reason"]
    media = client.get(cached["preview"]["local_url"], headers={"Range": "bytes=0-41"})
    assert media.status_code == 206 and media.data == artifact.read_bytes()[:42]
    assert client.get(cached["preview"]["local_url"]).status_code == 416
    status = client.get("/api/local-preview").get_json()
    assert status["cached_catalog_candidates"] == 2 and status["cached_packages"] == 1
    assert status["network_downloads"] is status["native_packaging"] is False
    assert app.extensions["config"].database_path.is_relative_to(runtime)
    assert before == (artifact.read_bytes(), (library / "owned.sng").read_bytes())
    assert client.get("/api/catalog?q=unavailable").get_json()["total_filtered_charts"] == 1


def test_preview_source_card_does_not_claim_an_external_account(preview):
    _app, client, _runtime, _library, _artifact = preview
    html = client.get('/').get_data(as_text=True)
    assert 'data-local-preview="true"' in html
    assert 'Account access is unavailable in this preview' in html
    assert '>Not connected</span>' in html
    assert 'id="refreshSpotifyButton" type="button" disabled' in html
    assert '>Connected</span>' not in html


@pytest.mark.parametrize("changes", [{}, {"action": None}])
def test_missing_or_invalid_cached_selection_fails_closed(preview, changes):
    _app, client, *_ = preview
    assert pick(client, "enchor:2", **changes).status_code in {400, 409}
    history = client.get("/api/selection/history").get_json()
    assert history["catalog_summary"]["charts"] == 0
    catalog = client.get("/api/catalog").get_json()
    default_action = {"catalog_key": "enchor:2", "snapshot_token": catalog["snapshot_token"],
                      "selection_revision": history["selection_revision"], "exact_generation_id": 0}
    assert client.post("/api/catalog/selection", json=default_action, headers=HEADERS).status_code == 409


def test_original_revision_snapshot_alias_guards_remain(preview):
    _app, client, *_ = preview
    assert pick(client, snapshot_token="stale").status_code == 409
    first = pick(client)
    assert first.status_code == 200 and first.get_json()["changed"] == 1
    assert pick(client, "enchor:3").get_json()["changed"] == 0
    assert pick(client, selection_revision=0).status_code == 409
    assert client.get("/api/catalog/selections").get_json()["total"] == 1


def test_actual_cached_zip_restores_receipt_in_fresh_client_and_consumes_once(preview):
    app, client, runtime, library, artifact = preview
    before = artifact.read_bytes(), (library / "owned.sng").read_bytes()
    assert pick(client).status_code == 200
    response = client.post("/api/download", json={}, headers=HEADERS)
    assert response.status_code == 202
    job = wait_job(client, response.get_json()["job"]["id"])
    assert job["status"] == "complete", job
    fresh = app.test_client()
    restored = fresh.get("/api/delivery/status").get_json()
    assert restored["selection_count"] == 1 and restored["download_job"]["id"] == job["id"]
    assert restored["download"]["archive_id"] == job["result"]["archive_id"]
    url = restored["download"]["url"]
    downloaded = fresh.get(url)
    assert downloaded.status_code == 200
    with ZipFile(io.BytesIO(downloaded.data)) as archive:
        songs = [name for name in archive.namelist() if name.endswith(".sng")]
        assert len(songs) == 1 and archive.read(songs[0]) == artifact.read_bytes()
        assert any("manifest" in name for name in archive.namelist())
    assert fresh.get(url).status_code in {404, 410}
    assert fresh.get("/api/delivery/status").get_json()["download"] is None
    assert not list(library.glob("*.part"))
    assert before == (artifact.read_bytes(), (library / "owned.sng").read_bytes())
    db = Database(app.extensions["config"].database_path)
    try:
        owner = db.get_user_by_spotify_id(LOCAL_OWNER)
        assert db.get_user_token(int(owner["id"])) is None
    finally:
        db.close()


def test_repeated_preparation_reconnects_to_one_pending_job(preview):
    app, client, *_ = preview
    assert pick(client).status_code == 200
    entered, release = threading.Event(), threading.Event()
    original = app.extensions["jobs"].archive_builder

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    app.extensions["jobs"].archive_builder = held
    try:
        first = client.post("/api/download", json={}, headers=HEADERS).get_json()
        assert entered.wait(5)
        again = client.post("/api/download", json={}, headers=HEADERS).get_json()
        assert first["job"]["id"] == again["job"]["id"] and again["created"] is False
    finally:
        release.set()
    assert wait_job(client, first["job"]["id"])["status"] == "complete"


@pytest.mark.parametrize("path,options", [
    ("/api/local-preview", {"environ_overrides": {"REMOTE_ADDR": "192.0.2.9"}}),
    ("/", {"base_url": "http://evil.example"}),
    ("/static/catalog.js", {"environ_overrides": {"REMOTE_ADDR": "192.0.2.9"}}),
])
def test_loopback_and_host_boundary_covers_every_route(preview, path, options):
    assert preview[1].get(path, **options).status_code == 403


def test_mutation_origin_external_provider_and_real_library_boundaries(preview):
    _app, client, runtime, library, *_ = preview
    assert client.post("/api/download", json={}).status_code == 403
    assert client.post("/api/download", json={}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    for path in ("/login", "/api/auth/spotify/callback", "/api/spotify/scope", "/api/soundcloud/scope"):
        assert client.get(path).status_code == 409
    assert client.post("/api/companion/folder", json={"path": str(library)}, headers=HEADERS).status_code == 400
    assert client.post("/api/companion/folder", json={"path": str(runtime / "Songs")}, headers=HEADERS).status_code == 200
    install = {"catalog_key": "enchor:1", "destination": str(runtime / "Songs"), "confirm": True}
    assert client.post("/api/companion/install", json=install, headers=HEADERS).status_code == 201
    assert client.post("/api/companion/install", json=install, headers=HEADERS).status_code == 409
    assert len(list(library.iterdir())) == 1


def test_cache_source_refuses_stale_identity_and_external_destination(preview, tmp_path):
    app, _client, runtime, *_ = preview
    state = app.extensions["companion"]
    source = CachedSource(state, runtime)
    candidate = state.chart("enchor:1")
    with pytest.raises(SourceError, match="isolated runtime"):
        source.download_chart(candidate, tmp_path / "outside.sng")
    missing = state.chart("enchor:2")
    with pytest.raises(SourceError, match="cached"):
        source.get_download_info(missing)


def test_runtime_may_not_overlap_any_read_only_input(tmp_path):
    for kind in ("library_roots", "artifact_roots"):
        with pytest.raises(ValueError, match="overlap"):
            create_app(catalog_path=tmp_path / "catalog.jsonl", runtime_root=tmp_path / "runtime",
                       **{kind: (tmp_path / "runtime" / "readonly",)})
