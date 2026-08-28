"""End-to-end pipeline test using a fake chart source (no network)."""

import hashlib
import json
import shutil
import threading
import time

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_build, run_scan, run_search

from .conftest import FIXTURES, FakeSource, make_chart, make_track, valid_sng_bytes


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
            content = valid_sng_bytes(chart.name)
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

    # Hybrid library hierarchy: <Artist>/<Album>/<file>.sng under the root.
    assert (config.library_root / "Foo Fighters" / "The Colour and the Shape").is_dir()
    assert (config.library_root / "The Rolling Stones" / "Aftermath").is_dir()
    assert not (config.library_root / "00 - His Music").exists()

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
    assert not config.library_root.exists() or not any(config.library_root.iterdir())
    # ...but reports and estimates still exist.
    summary = json.loads((config.reports_path / "summary.json").read_text())
    assert summary["plan"]["estimated_bytes"] > 0


def test_exact_search_persists_review_rows_incrementally(config):
    db = Database(config.database_path)
    fast_track = make_track("Fast", "Artist Fast", spotify_id="fast")
    slow_track = make_track("Slow", "Artist Slow", spotify_id="slow")
    db.replace_tracks([fast_track, slow_track], {"test.csv": 2})

    class DelayedSource(FakeSource):
        def search_song(self, artist, title):
            if title == "Slow":
                time.sleep(0.35)
            return super().search_song(artist, title)

    source = DelayedSource(
        song_results={
            ("Artist Fast", "Fast"): [make_chart("Fast", "Artist Fast", chart_id=1)],
            ("Artist Slow", "Slow"): [make_chart("Slow", "Artist Slow", chart_id=2)],
        }
    )
    from csv_to_clonehero.overrides import Overrides

    error = []

    def worker():
        try:
            worker_db = Database(config.database_path)
            try:
                run_search(config, worker_db, [source], Overrides(), include_expansion=False)
            finally:
                worker_db.close()
        except Exception as exc:  # pragma: no cover - assertion below reports it
            error.append(exc)

    db.close()
    thread = threading.Thread(target=worker)
    thread.start()
    observer = Database(config.database_path)
    saw_partial = False
    deadline = time.time() + 2
    while time.time() < deadline and thread.is_alive():
        rows = observer.get_match_scores()
        if rows and len({row["track_id"] for row in rows}) == 1:
            saw_partial = True
            break
        time.sleep(0.02)
    thread.join(timeout=2)
    observer.close()
    assert not error
    assert saw_partial


def test_exact_only_skips_artists_and_expansion_reuses_exact_cache(config):
    db = Database(config.database_path)
    track = make_track("Song", "Artist")
    db.replace_tracks([track], {"test.csv": 1})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                song_results={("Artist", "Song"): [make_chart("Song", "Artist", chart_id=1)]},
                artist_results={("Artist"): [make_chart("Other", "Artist", chart_id=2)]},
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    from csv_to_clonehero.overrides import Overrides

    run_search(config, db, [source], Overrides(), include_expansion=False)
    assert source.song_calls == 1
    assert source.artist_calls == 0
    run_search(config, db, [source], Overrides(), include_expansion=True)
    assert source.song_calls == 1
    assert source.artist_calls == 1
    db.close()


def _no_overrides():
    from csv_to_clonehero.overrides import Overrides

    return Overrides()


# ------------------------------------------------------------- catalog-first

def test_catalog_first_uses_artist_queries_for_qualifying_artists(config):
    db = Database(config.database_path)
    tracks = [
        make_track("A1", "Artist A"),
        make_track("A2", "Artist A"),
        make_track("A3", "Artist A"),
        make_track("B1", "Artist B"),
        make_track("B2", "Artist B"),
        make_track("B3", "Artist B"),
    ]
    db.replace_tracks(tracks, {"test.csv": 6})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                artist_results={
                    "Artist A": [
                        make_chart("A1", "Artist A", chart_id=1),
                        make_chart("A2", "Artist A", chart_id=2),
                        make_chart("A3", "Artist A", chart_id=3),
                    ],
                    "Artist B": [
                        make_chart("B1", "Artist B", chart_id=4),
                        make_chart("B2", "Artist B", chart_id=5),
                        make_chart("B3", "Artist B", chart_id=6),
                    ],
                }
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 2
    assert source.song_calls == 0
    assert result.auto == 6
    db.close()


def test_catalog_first_falls_back_to_song_for_below_threshold_artists(config):
    db = Database(config.database_path)
    tracks = [make_track("S1", "Artist"), make_track("S2", "Artist")]
    db.replace_tracks(tracks, {"test.csv": 2})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                song_results={
                    ("Artist", "S1"): [make_chart("S1", "Artist", chart_id=1)],
                    ("Artist", "S2"): [make_chart("S2", "Artist", chart_id=2)],
                }
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 0
    assert source.song_calls == 2
    assert result.auto == 2
    db.close()


def test_catalog_miss_falls_back_to_song_query(config):
    db = Database(config.database_path)
    tracks = [make_track("S1", "Artist"), make_track("S2", "Artist"), make_track("S3", "Artist")]
    db.replace_tracks(tracks, {"test.csv": 3})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                artist_results={"Artist": [make_chart("Other", "Artist", chart_id=99)]},
                song_results={
                    ("Artist", "S1"): [make_chart("S1", "Artist", chart_id=1)],
                    ("Artist", "S2"): [make_chart("S2", "Artist", chart_id=2)],
                    ("Artist", "S3"): [make_chart("S3", "Artist", chart_id=3)],
                },
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 1
    assert source.song_calls == 3
    assert result.auto == 3
    db.close()


def test_catalog_error_falls_back_to_song_query(config):
    db = Database(config.database_path)
    tracks = [make_track("S1", "Artist"), make_track("S2", "Artist"), make_track("S3", "Artist")]
    db.replace_tracks(tracks, {"test.csv": 3})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                song_results={
                    ("Artist", "S1"): [make_chart("S1", "Artist", chart_id=1)],
                    ("Artist", "S2"): [make_chart("S2", "Artist", chart_id=2)],
                    ("Artist", "S3"): [make_chart("S3", "Artist", chart_id=3)],
                }
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            from csv_to_clonehero.sources.base import SourceError

            raise SourceError("catalog boom")

    source = CountingSource()
    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 1
    assert source.song_calls == 3
    assert result.auto == 3
    db.close()


def test_manual_rejects_block_catalog_fallback(config):
    db = Database(config.database_path)
    tracks = [
        make_track("Alpha One", "Artist"),
        make_track("Beta Two", "Artist"),
        make_track("Gamma Three", "Artist"),
    ]
    db.replace_tracks(tracks, {"test.csv": 3})

    catalog_charts = {
        "Alpha One": make_chart("Alpha One", "Artist", chart_id=1),
        "Beta Two": make_chart("Beta Two", "Artist", chart_id=2),
    }
    fallback_chart = make_chart("Gamma Three", "Artist", chart_id=11)

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                artist_results={"Artist": list(catalog_charts.values())},
                song_results={("Artist", "Gamma Three"): [fallback_chart]},
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    # Reject every catalog candidate for the first two tracks.
    for track in tracks[:2]:
        for chart in catalog_charts.values():
            db.set_manual_decision(track.track_key, "test", chart.chart_id, "reject")

    # Raise the review threshold so the catalog's fuzzy matches for the third
    # track stay in REJECT territory and trigger fallback.
    config.matching.review_threshold = 85.0

    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 1
    # Only the third track may fall back; the first two are blocked by manual decisions.
    assert source.song_calls == 1
    assert result.auto == 1
    db.close()


def test_catalog_first_disabled_uses_legacy_song_queries(config):
    db = Database(config.database_path)
    tracks = [make_track("S1", "Artist"), make_track("S2", "Artist"), make_track("S3", "Artist")]
    db.replace_tracks(tracks, {"test.csv": 3})
    config.search.artist_catalog_first = False

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                artist_results={"Artist": [make_chart("Other", "Artist", chart_id=99)]},
                song_results={
                    ("Artist", "S1"): [make_chart("S1", "Artist", chart_id=1)],
                    ("Artist", "S2"): [make_chart("S2", "Artist", chart_id=2)],
                    ("Artist", "S3"): [make_chart("S3", "Artist", chart_id=3)],
                },
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    result = run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 0
    assert source.song_calls == 3
    assert result.auto == 3
    db.close()


def test_scan_honors_disabled_files(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    (config.input_path / "a.csv").write_text("Song,Artist\nA,B\n", encoding="utf-8")
    (config.input_path / "b.csv").write_text("Song,Artist\nC,D\n", encoding="utf-8")
    db = Database(config.database_path)
    db.set_disabled_files({"a.csv"})

    stats = run_scan(config, db, _no_overrides())
    assert stats.csv_files == 1
    assert stats.total_rows == 1
    tracks = db.get_tracks()
    assert len(tracks) == 1
    assert tracks[0].title == "C"
    db.close()


def test_scan_re_enable_restores_tracks(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    (config.input_path / "a.csv").write_text("Song,Artist\nA,B\n", encoding="utf-8")
    db = Database(config.database_path)
    db.set_disabled_files({"a.csv"})

    run_scan(config, db, _no_overrides())
    assert len(db.get_tracks()) == 0
    db.set_disabled_files(set())
    run_scan(config, db, _no_overrides())
    assert len(db.get_tracks()) == 1
    db.close()


def test_expansion_reuses_exact_catalog_queries(config):
    db = Database(config.database_path)
    tracks = [
        make_track("A1", "Artist A"),
        make_track("A2", "Artist A"),
        make_track("A3", "Artist A"),
        make_track("B1", "Artist B"),
    ]
    db.replace_tracks(tracks, {"test.csv": 4})

    class CountingSource(FakeSource):
        def __init__(self):
            super().__init__(
                artist_results={
                    "Artist A": [make_chart("Extra A", "Artist A", chart_id=10)],
                    "Artist B": [make_chart("Extra B", "Artist B", chart_id=11)],
                },
                song_results={
                    ("Artist A", "A1"): [make_chart("A1", "Artist A", chart_id=1)],
                    ("Artist A", "A2"): [make_chart("A2", "Artist A", chart_id=2)],
                    ("Artist A", "A3"): [make_chart("A3", "Artist A", chart_id=3)],
                    ("Artist B", "B1"): [make_chart("B1", "Artist B", chart_id=4)],
                },
            )
            self.song_calls = 0
            self.artist_calls = 0

        def search_song(self, artist, title):
            self.song_calls += 1
            return super().search_song(artist, title)

        def search_artist(self, artist):
            self.artist_calls += 1
            return super().search_artist(artist)

    source = CountingSource()
    # Exact pass: Artist A qualifies for catalog; Artist B falls back to per-song.
    run_search(config, db, [source], _no_overrides(), include_expansion=False)
    assert source.artist_calls == 1
    assert source.song_calls == 1

    # Expansion: Artist A's catalog is already cached from the exact pass;
    # Artist B is eligible and needs its first artist query.
    run_search(config, db, [source], _no_overrides(), include_expansion=True)
    assert source.artist_calls == 2
    assert source.song_calls == 1
    db.close()
