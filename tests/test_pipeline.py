"""End-to-end pipeline test using a fake chart source (no network)."""

import hashlib
import json
import shutil

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_build, run_scan, run_search

from .conftest import FIXTURES, FakeSource, make_chart


def _prepare_input(config):
    input_dir = config.input_path
    input_dir.mkdir(parents=True, exist_ok=True)
    for fixture in FIXTURES.glob("*.csv"):
        shutil.copy(fixture, input_dir / fixture.name)


def _fake_source():
    charts = {}
    # Exact matches for the fixture playlists.
    exact = [
        ("Foo Fighters", "Everlong", make_chart("Everlong", "Foo Fighters", chart_id=1)),
        ("The Rolling Stones", "Paint It, Black - 2009 Remaster",
         make_chart("Paint It, Black", "The Rolling Stones", chart_id=2, icon="rb1")),
        ("Flo Rida", "Low", make_chart("Low", "Flo Rida", chart_id=3)),
        ("Blue Öyster Cult", "(Don't Fear) The Reaper",
         make_chart("(Don't Fear) The Reaper", "Blue Öyster Cult", chart_id=4)),
        ("The Rolling Stones", "Gimme Shelter",
         make_chart("Gimme Shelter", "The Rolling Stones", chart_id=5)),
        ("Kendrick Lamar", "Humble", make_chart("HUMBLE.", "Kendrick Lamar", chart_id=6)),
        ("Gorillaz", "Feel Good Inc.",
         make_chart("Feel Good Inc.", "Gorillaz", chart_id=7)),
    ]
    for artist, title, chart in exact:
        charts.setdefault((artist, title), []).append(chart)
    files = {}
    for chart_list in charts.values():
        for chart in chart_list:
            content = (chart.name * 1000).encode()
            chart.md5 = hashlib.md5(content).hexdigest()
            files[chart.md5] = content
    return FakeSource(song_results=charts, files=files)


def test_full_build_pipeline(config):
    _prepare_input(config)
    db = Database(config.database_path)
    source = _fake_source()

    result = run_build(config, db, [source], _no_overrides(), dry_run=False)
    db.close()

    assert result.scan.csv_files == 2
    assert result.scan.unique_tracks == 8
    assert result.search.auto >= 7  # all except "Song Without Id"
    assert result.search.no_match >= 1
    assert result.downloaded == result.plan.exact_selected
    assert result.download_errors == 0

    # Reports exist.
    reports = config.reports_path
    for name in ("summary.json", "summary.txt", "exact_matches.csv", "review_needed.csv",
                 "no_matches.csv", "downloaded.csv", "skipped_for_space.csv"):
        assert (reports / name).exists(), name

    summary = json.loads((reports / "summary.json").read_text())
    assert summary["unique_tracks"] == 8
    assert summary["exact"]["auto_matches"] >= 7

    # Library hierarchy created.
    exact_dir = config.library_root / "00 - His Music" / "Exact Playlist Songs"
    assert (exact_dir / "Foo Fighters").is_dir()
    assert (exact_dir / "The Rolling Stones").is_dir()

    # Idempotent rebuild: nothing downloaded twice.
    db = Database(config.database_path)
    source2 = _fake_source()
    result2 = run_build(config, db, [source2], _no_overrides(), dry_run=False)
    db.close()
    assert source2.downloads == []


def test_dry_run_downloads_nothing(config):
    _prepare_input(config)
    db = Database(config.database_path)
    source = _fake_source()

    result = run_build(config, db, [source], _no_overrides(), dry_run=True)
    db.close()

    assert result.downloaded == 0
    assert source.downloads == []
    assert not (config.library_root / "00 - His Music").exists()
    # ...but reports and estimates still exist.
    summary = json.loads((config.reports_path / "summary.json").read_text())
    assert summary["plan"]["estimated_bytes"] > 0


def _no_overrides():
    from csv_to_clonehero.overrides import Overrides

    return Overrides()
