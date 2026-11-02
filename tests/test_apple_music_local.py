"""Local home-screen Apple Music import (Music/iTunes TXT or XML export)."""

import io

from csv_to_clonehero.webapp import create_app

TXT_EXPORT = (
    "Name\tArtist\tAlbum\n"
    "Everlong\tFoo Fighters\tThe Colour and the Shape\n"
    "Hysteria\tMuse\tAbsolution\n"
).encode("utf-16")


def _client(config):
    app = create_app(config, enforce_single_process=False, recover_interrupted_state=False)
    app.config["TESTING"] = True
    return app.test_client()


def test_apple_tile_is_available_and_import_adds_songs(config):
    client = _client(config)
    before = client.get("/api/journey").get_json()
    sources = before["sources"] if "sources" in before else before["journey"]["sources"]
    apple = sources["apple"] if isinstance(sources, dict) else next(s for s in sources if s["source_key"] == "apple")
    assert apple["availability"] == "available"
    assert apple["allowed_actions"]["import_music"] is True

    response = client.post(
        "/api/sources/apple/import",
        data={"file": (io.BytesIO(TXT_EXPORT), "Library.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["ok"] is True
    assert body["source"]["tracks"] == 2

    after = client.get("/api/journey").get_json()
    sources = after["sources"] if "sources" in after else after["journey"]["sources"]
    apple = sources["apple"] if isinstance(sources, dict) else next(s for s in sources if s["source_key"] == "apple")
    assert apple["imported"] is True
    assert apple["song_count"] == 2


def test_apple_import_rejects_other_formats(config):
    client = _client(config)
    response = client.post(
        "/api/sources/apple/import",
        data={"file": (io.BytesIO(b"a,b\n"), "songs.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
