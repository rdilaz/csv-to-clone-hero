"""Downloader resume/idempotency tests."""

import hashlib

from csv_to_clonehero.database import Database
from csv_to_clonehero.downloader import DownloadManager

from .conftest import FakeSource, make_chart


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
    content = b"fake sng data"
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    selection_id = _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, size = manager.download_selection(chart, source, "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    )
    assert success, message
    assert size == len(content)
    target = config.library_root / "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    assert target.exists()
    assert target.read_bytes() == content
    db.close()


def test_second_run_does_not_redownload(config):
    db = Database(config.database_path)
    content = b"fake sng data"
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    selection_id = _make_selection(db, chart)
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
    source = FakeSource(files={"0" * 32: b"tampered"})
    selection_id = _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, _ = manager.download_selection(chart, source,
        "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng",
    )
    assert not success
    assert "checksum" in message
    target = config.library_root / "00 - His Music/Exact Playlist Songs/Artist/Artist - Song.sng"
    assert not target.exists()
    db.close()


def test_path_escape_refused(config):
    db = Database(config.database_path)
    content = b"x"
    md5 = hashlib.md5(content).hexdigest()
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(files={md5: content})
    selection_id = _make_selection(db, chart)

    manager = DownloadManager(config, db)
    success, message, _ = manager.download_selection(chart, source, "..\\..\\evil.sng"
    )
    assert not success
    assert "outside library root" in message
    db.close()
