"""Offline companion behavior, including the filesystem write boundary."""
from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from csv_to_clonehero.companion import create_app, parsed_year
from tests.conftest import make_chart
from tests.test_catalog import write_snapshot


def package_bytes(artist="Artist One", title="First Song"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("song.ini", f"[song]\nartist = {artist}\nname = {title}\nalbum = Debut\n")
        archive.writestr("notes.chart", "[Song]\n{\nResolution = 192\n}\n[ExpertSingle]\n{\n0 = N 0 0\n}\n")
    return stream.getvalue()


@pytest.fixture
def companion(tmp_path):
    sandbox = tmp_path / "sandbox"
    songs = sandbox / "Songs"
    songs.mkdir(parents=True)
    library = tmp_path / "real-library"
    library.mkdir()
    (library / "owned.sng").write_bytes(package_bytes())
    cache = tmp_path / "state" / "export-artifacts"
    cache.mkdir(parents=True)
    charts = []
    for chart_id, title, artist, album, year in (
        (1, "First Song", "Artist One", "Debut", "1995"),
        (2, "Second Song", "Artist Two", "Album Needle", "2005-01-02"),
        (3, "Unknown Year", "Artist Three", "Other", "unknown"),
        (4, "First Song", "Artist One", "Debut", "1995"),
    ):
        chart = make_chart(title, artist, chart_id=chart_id)
        chart.source = "enchor"
        chart.album = album
        chart.year = year
        charts.append(chart)
    cache_key = hashlib.sha256(f"enchor\0source-id:{charts[0].md5}".encode()).hexdigest()
    artifact = cache / f"{cache_key}.sng"
    artifact.write_bytes(package_bytes())
    catalog = tmp_path / "catalog.jsonl"
    write_snapshot(catalog, charts)
    app = create_app(catalog_path=catalog, sandbox_root=sandbox,
                     library_roots=(library,), artifact_roots=(cache,))
    app.testing = True
    return app, app.test_client(), songs, library, artifact


def test_catalog_counts_search_decade_and_installed_badges(companion):
    _, client, _, library, _ = companion
    before = {path.name: path.read_bytes() for path in library.iterdir()}
    result = client.get("/api/companion/catalog").get_json()
    assert result["total_charts"] == 4
    assert result["total_songs"] == 3
    assert result["filtered"] == 4
    assert result["items"][0]["installed"] is True
    assert result["items"][0]["installed_match"] == "song-metadata"
    assert result["items"][0]["install_available"] is True
    assert result["items"][0]["preview"]["available"] is False  # Valid ZIP, not SNGPKG audio format.
    assert client.get("/api/companion/catalog?q=album%20needle").get_json()["filtered"] == 1
    assert client.get("/api/companion/catalog?q=artist%20two&decade=2000s").get_json()["filtered"] == 1
    assert client.get("/api/companion/catalog?decade=1990").get_json()["filtered"] == 2
    assert client.get("/api/companion/catalog?decade=2010").get_json()["filtered"] == 0
    assert before == {path.name: path.read_bytes() for path in library.iterdir()}


def test_catalog_pages_and_repeatable_shuffle(companion):
    _, client, *_ = companion
    first = client.get("/api/companion/catalog?limit=2").get_json()
    second = client.get("/api/companion/catalog?offset=2&limit=2").get_json()
    assert first["has_more"] and not second["has_more"]
    assert {item["catalog_key"] for item in first["items"] + second["items"]} == {"enchor:1", "enchor:2", "enchor:3", "enchor:4"}
    shuffled = client.get("/api/companion/catalog?shuffle_seed=testing").get_json()
    assert shuffled == client.get("/api/companion/catalog?shuffle_seed=testing").get_json()
    cached = client.get("/api/companion/catalog?cached_only=1").get_json()
    assert cached["total_charts"] == 4 and cached["filtered"] == 1
    assert cached["items"][0]["catalog_key"] == "enchor:1"


@pytest.mark.parametrize("query", ["offset=-1", "limit=101", "limit=0", "decade=1995", "decade=banana", "offset=no"])
def test_bad_catalog_parameters(companion, query):
    assert companion[1].get("/api/companion/catalog?" + query).status_code == 400


def test_status_and_library_are_read_only_and_honest(companion):
    app, client, _, library, _ = companion
    status = client.get("/api/companion/status").get_json()
    assert status["mode"] == "sandbox-preview"
    assert status["catalog_count"] == 4
    assert status["folder"] is None
    assert status["network_downloads"] is False
    assert status["native_packaging"] is False
    assert status["cached_artifacts"] == 1
    info = client.get("/api/companion/library").get_json()
    assert info["path"] == str(library)
    assert info["installed_count"] == 1
    assert info["read_only"] is True
    assert "unpacked" in info["scope"]
    assert set(app.extensions) == {"companion"}


def test_folder_choice_does_not_write_and_rejects_real_library_or_traversal(companion):
    _, client, songs, library, _ = companion
    assert not list(songs.iterdir())
    result = client.post("/api/companion/folder", json={"path": str(songs)})
    assert result.status_code == 200
    assert result.get_json()["write_performed"] is False
    for invalid in (str(library), str(songs / "missing"), str(songs / ".." / "Songs"), "Songs"):
        assert client.post("/api/companion/folder", json={"path": invalid}).status_code == 400
    assert not list(songs.iterdir())


def test_install_requires_chosen_folder_and_specific_confirmation(companion):
    _, client, songs, _, _ = companion
    payload = {"catalog_key": "enchor:1", "destination": str(songs), "confirm": True}
    assert client.post("/api/companion/install", json=payload).status_code == 409
    assert client.post("/api/companion/folder", json={"path": str(songs)}).status_code == 200
    for confirmation in (False, "true", 1, None):
        assert client.post("/api/companion/install", json={**payload, "confirm": confirmation}).status_code == 400
    assert not list(songs.iterdir())
    assert client.post("/api/companion/install", json={**payload, "catalog_key": "enchor:2"}).status_code == 409
    assert client.post("/api/companion/install", json={**payload, "catalog_key": "enchor:../1"}).status_code == 404


def test_install_is_atomic_add_only_and_preserves_source_and_real_library(companion):
    _, client, songs, library, artifact = companion
    source_before = artifact.read_bytes()
    library_before = (library / "owned.sng").read_bytes()
    client.post("/api/companion/folder", json={"path": str(songs)})
    payload = {"catalog_key": "enchor:1", "destination": str(songs), "confirm": True}
    result = client.post("/api/companion/install", json=payload)
    assert result.status_code == 201
    body = result.get_json()
    target = Path(body["path"])
    assert target.parent == songs and target.suffix == ".sng"
    assert target.read_bytes() == source_before
    assert body["overwritten"] is False and body["sandbox"] is True
    assert client.post("/api/companion/install", json=payload).status_code == 409
    assert list(songs.iterdir()) == [target]
    assert target.read_bytes() == source_before == artifact.read_bytes()
    assert library_before == (library / "owned.sng").read_bytes()
    assert client.get("/api/companion/catalog/enchor/1").get_json()["item"]["sandbox_installed"] is True


def test_concurrent_collision_does_not_overwrite_or_leave_staging(companion, monkeypatch):
    _, client, songs, _, _ = companion
    client.post("/api/companion/folder", json={"path": str(songs)})
    def concurrent_file(_source, target, **_kwargs):
        target.write_bytes(b"existing concurrent user file")
        raise FileExistsError
    monkeypatch.setattr(os, "rename" if os.name == "nt" else "link", concurrent_file)
    result = client.post("/api/companion/install", json={"catalog_key": "enchor:1", "destination": str(songs), "confirm": True})
    assert result.status_code == 409
    files = list(songs.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"existing concurrent user file"


def test_invalid_cached_bytes_cannot_be_installed(companion):
    _, client, songs, _, artifact = companion
    artifact.write_bytes(b"invalid")
    client.post("/api/companion/folder", json={"path": str(songs)})
    result = client.post("/api/companion/install", json={"catalog_key": "enchor:1", "destination": str(songs), "confirm": True})
    assert result.status_code == 409 and not list(songs.iterdir())


def test_audio_is_local_bounded_single_range_and_never_downloads(companion):
    _, client, _, _, artifact = companion
    route = "/api/companion/audio/00000000000000000000000000000001.sng"
    result = client.get(route, headers={"Range": "bytes=0-41"})
    assert result.status_code == 206
    assert result.data == artifact.read_bytes()[:42]
    assert result.headers["Content-Range"] == f"bytes 0-41/{artifact.stat().st_size}"
    conditional = client.get(route, headers={"Range": "bytes=0-41", "If-Range": "Thu, 01 Jan 1970 00:00:00 GMT"})
    assert conditional.status_code == 206 and len(conditional.data) == 42
    for value in (None, "bytes=0-", "bytes=0-1,4-6", "bytes=0-1048576", "bytes=0-999999999"):
        assert client.get(route, headers={"Range": value} if value else {}).status_code == 416
    assert client.get("/api/companion/audio/not-a-hash.sng", headers={"Range": "bytes=0-41"}).status_code == 404


def test_loopback_and_origin_guards(companion):
    _, client, songs, *_ = companion
    assert client.get("/api/companion/status", environ_overrides={"REMOTE_ADDR": "192.0.2.4"}).status_code == 403
    assert client.get("/api/companion/status", base_url="http://evil.example").status_code == 403
    assert client.post("/api/companion/folder", json={"path": str(songs)}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/companion/folder", data="path=ignored").status_code == 415
    assert client.post("/api/companion/folder", json={"path": str(songs)}, headers={"Origin": "http://localhost"}).status_code == 200


def test_unavailable_catalog_fails_closed(tmp_path):
    app = create_app(catalog_path=tmp_path / "missing.jsonl", sandbox_root=tmp_path)
    client = app.test_client()
    assert client.get("/api/companion/catalog").status_code == 503
    assert client.get("/api/companion/status").get_json()["catalog"]["available"] is False


def test_unreadable_inventory_does_not_claim_songs_absent(companion, tmp_path):
    original = companion[0].extensions["companion"]
    app = create_app(catalog_path=original.catalog_path, sandbox_root=original.sandbox_root,
                     library_roots=(tmp_path / "missing-library",))
    client = app.test_client()
    library = client.get("/api/companion/library").get_json()
    assert library["status"] == "partial" and library["installed_count"] is None
    item = client.get("/api/companion/catalog/enchor/1").get_json()["item"]
    assert item["installed"] is None and item["installed_known"] is False


@pytest.mark.parametrize("value,expected", [("1994", 1994), ("2005-03-01", 2005), ("c. 1988", 1988), ("unknown", None), ("1990s", 1990), ("11994", None)])
def test_explicit_year_parsing(value, expected):
    assert parsed_year(value) == expected
