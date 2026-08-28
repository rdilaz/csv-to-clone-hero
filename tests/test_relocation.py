"""Auto-relocation: existing downloads move to the active library layout."""

import hashlib

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import (
    cleanup_empty_folders,
    run_downloads,
    run_plan,
    run_relocation,
    run_search,
)

from .conftest import FakeSource, make_chart, make_track


def _seed_and_build_legacy(config):
    """Build one chart under the legacy layout; return (db, source, content)."""
    config.library.layout = "legacy"
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Song\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
    content = payload.getvalue()
    md5 = hashlib.md5(content).hexdigest()
    track = make_track("Song", "Artist", album="Album")
    chart = make_chart("Song", "Artist", chart_id=1, md5=md5)
    source = FakeSource(
        song_results={("Artist", "Song"): [chart]},
        files={md5: content},
    )
    db = Database(config.database_path)
    db.replace_tracks([track], {"test.csv": 1})

    from csv_to_clonehero.overrides import Overrides

    run_search(config, db, [source], Overrides(), include_expansion=False)
    run_plan(config, db, [source], include_expansion=False)
    downloaded, errors, _ = run_downloads(config, db, [source])
    assert (downloaded, errors) == (1, 0)
    return db, source, md5


def test_relocation_moves_completed_downloads_to_new_layout(config):
    db, _source, md5 = _seed_and_build_legacy(config)
    legacy_path = (
        config.library_root
        / "00 - His Music" / "Exact Playlist Songs" / "Artist" / "Artist - Song [TestCharter].sng"
    )
    assert legacy_path.exists()

    # Switch layouts and re-plan (selections get the hybrid target path).
    config.library.layout = "hybrid"
    run_plan(config, db, [_source], include_expansion=False)
    stats = run_relocation(config, db)
    assert stats.checked == 1
    assert stats.moved == 1
    assert stats.errors == 0

    new_path = (
        config.library_root / "Artist" / "Album" / "Artist - Song [TestCharter].sng"
    )
    assert new_path.exists()
    assert not legacy_path.exists()

    candidate_id = db.get_selections()[0]["candidate_id"]
    row = db.get_download_for_candidate(candidate_id)
    assert row["path"] == str(new_path.resolve())

    removed = cleanup_empty_folders(config)
    assert removed >= 1
    assert not (config.library_root / "00 - His Music").exists()
    db.close()


def test_relocation_is_idempotent(config):
    db, source, _md5 = _seed_and_build_legacy(config)
    config.library.layout = "hybrid"
    run_plan(config, db, [source], include_expansion=False)
    first = run_relocation(config, db)
    assert first.moved == 1
    again = run_relocation(config, db)
    assert again.checked == 1
    assert again.moved == 0
    assert again.errors == 0
    db.close()


def test_relocation_recovers_interrupted_move(config):
    db, source, _md5 = _seed_and_build_legacy(config)
    config.library.layout = "hybrid"
    run_plan(config, db, [source], include_expansion=False)

    # Simulate a crash between the file move and the DB update: the file sits
    # at the target while the DB still points at the legacy path.
    row = db.get_selections(status="selected")[0]
    target = (config.library_root / row["target_relpath"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    old_path = db.get_download_for_candidate(row["candidate_id"])["path"]
    target.write_bytes(open(old_path, "rb").read())
    import os

    os.unlink(old_path)

    stats = run_relocation(config, db)
    assert stats.moved == 0  # nothing to move; DB repointed only
    assert stats.errors == 0
    assert db.get_download_for_candidate(row["candidate_id"])["path"] == str(target)
    db.close()


def test_relocation_records_errors_and_continues(config, monkeypatch):
    db, source, _md5 = _seed_and_build_legacy(config)
    config.library.layout = "hybrid"
    run_plan(config, db, [source], include_expansion=False)

    import csv_to_clonehero.pipeline as pipeline

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.os, "replace", boom)
    monkeypatch.setattr(pipeline.shutil, "move", boom)
    stats = run_relocation(config, db)
    assert stats.moved == 0
    assert stats.errors == 1
    stages = [row["stage"] for row in db.get_errors()]
    assert "relocate" in stages
    db.close()
