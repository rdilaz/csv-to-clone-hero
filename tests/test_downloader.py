"""Downloader resume/idempotency tests."""

import hashlib
import io
import json
import os
import zipfile

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.downloader import DownloadManager

from .conftest import FakeSource, make_chart


def _valid_chart_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Song\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
    return payload.getvalue()


def _make_selection(db, candidate) -> int:
    db.upsert_candidate(candidate)
    db.replace_selections([{
        "kind": "exact",
        "track_id": None,
        "artist_norm": "",
        "candidate_id": candidate.db_id,
        "priority_tier": 0,
        "status": "selected",
        "score": 95.0,
        "explanation": "test",
        "planned_bytes": 100,
        "target_relpath": "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng",
    }])
    return db.get_selections()[0]["id"]


def test_download_completes_and_records(config):
    db = Database(config.database_path)
    content = _valid_chart_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, size = manager.download_selection(chart, source, "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    )
    assert success, message
    assert size == len(content)
    target = config.library_root / "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    assert target.exists()
    assert target.read_bytes() == content
    row = db.get_download_for_candidate(chart.db_id)
    assert row["sha256"] == hashlib.sha256(content).hexdigest()
    assert json.loads(row["validation_json"])["playable_note_count"] == 1
    db.close()


def test_second_run_does_not_redownload(config):
    db = Database(config.database_path)
    content = _valid_chart_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    _make_selection(db, chart)
    relpath = "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"

    manager = DownloadManager(config, db)
    assert manager.download_selection(chart, source, relpath)[0]
    assert source.downloads == [md5]

    # Second run: no new download.
    success, message, _ = manager.download_selection(chart, source, relpath)
    assert success
    assert "skipped" in message
    assert source.downloads == [md5]
    db.close()


def test_checksum_mismatch_rejected(config):
    db = Database(config.database_path)
    chart = make_chart("Song", "Artist", chart_id=1, md5="0" * 32)  # wrong md5
    source = FakeSource(files={"0" * 32: _valid_chart_bytes()})
    _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, _ = manager.download_selection(chart, source,
        "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng",
    )
    assert not success
    assert "checksum" in message
    target = config.library_root / "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    assert not target.exists()
    db.close()


@pytest.mark.parametrize(
    "escape_relpath",
    [
        # "/" is a path separator on every OS, so this escapes everywhere.
        "../../evil.sng",
        # The host's own separator: "..\..\evil.sng" on Windows (the case this
        # test was written for) and "../../evil.sng" on POSIX, where "\" is an
        # ordinary filename character rather than a separator.
        os.path.join("..", "..", "evil.sng"),
    ],
    ids=["forward-slash", "native-separator"],
)
def test_path_escape_refused(config, escape_relpath):
    db = Database(config.database_path)
    content = _valid_chart_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, _ = manager.download_selection(chart, source, escape_relpath)
    assert not success
    assert "outside library root" in message
    db.close()


def test_invalid_download_is_not_published(config):
    db = Database(config.database_path)
    chart = make_chart("Song", "Artist", chart_id=1, md5="bad")
    source = FakeSource(files={"bad": b"not a chart"})
    relpath = "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    _make_selection(db, chart)

    success, message, _ = DownloadManager(config, db).download_selection(
        chart, source, relpath
    )
    assert not success
    assert "validation" in message
    assert not (config.library_root / relpath).exists()
    db.close()


def test_completed_file_is_revalidated_before_skip(config):
    db = Database(config.database_path)
    content = _valid_chart_bytes()
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    relpath = "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    _make_selection(db, chart)
    manager = DownloadManager(config, db)
    assert manager.download_selection(chart, source, relpath)[0]
    target = config.library_root / relpath
    target.write_bytes(b"corrupt")
    assert manager.download_selection(chart, source, relpath)[0]
    assert source.downloads == [md5, md5]
    db.close()
