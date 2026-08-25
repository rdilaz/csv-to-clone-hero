"""Storage planner tests: exact matches must outrank expansion."""

import json

from csv_to_clonehero.database import Database
from csv_to_clonehero.planner import build_plan

from .conftest import make_chart, make_track


def _setup_db(config) -> Database:
    db = Database(config.database_path)
    return db


def _record_exact(db, config, track, chart, score=95.0):
    track.db_id = track.db_id or _insert_track(db, track)
    db.upsert_candidate(chart)
    db.record_match_score(track.db_id, chart.db_id, score, "AUTO_ACCEPT",
                          json.dumps({"explanation": "test"}))


def _insert_track(db, track) -> int:
    db.replace_tracks([track], {"test.csv": 1})
    return db.get_tracks()[0].db_id


def _record_expansion(db, artist_name, artist_norm, chart, score, cap=10):
    db.replace_expansion_artists([{
        "artist_name": artist_name, "artist_norm": artist_norm,
        "track_count": 3, "is_featured_only": False, "cap": cap, "disabled": False,
    }])
    db.upsert_candidate(chart)
    artist_id = db.get_expansion_artists()[0]["id"]
    db.record_expansion_candidate(artist_id, chart.db_id, score,
                                  json.dumps({"explanation": "test"}))


def test_exact_outranks_expansion_when_space_limited(config):
    config.storage.max_library_gib = 0.00007  # ~75 KB budget: room for one chart only
    config.storage.estimated_chart_bytes = 50 * 1024

    db = _setup_db(config)
    track = make_track("Song A", "Artist A")
    exact_chart = make_chart("Song A", "Artist A", chart_id=1)
    _record_exact(db, config, track, exact_chart)

    expansion_chart = make_chart("Song B", "Artist A", chart_id=2)
    _record_expansion(db, "Artist A", "artist a", expansion_chart, score=95.0)

    plan = build_plan(config, db)  # no size lookup -> uses estimate
    exact_entry = next(e for e in plan if e["kind"] == "exact")
    expansion_entry = next(e for e in plan if e["kind"] == "expansion")

    assert exact_entry["status"] == "selected"
    assert expansion_entry["status"] == "skipped_for_space"
    db.close()


def test_expansion_uses_real_sizes_when_available(config):
    config.storage.max_library_gib = 1.0
    db = _setup_db(config)
    track = make_track("Song A", "Artist A")
    chart = make_chart("Song A", "Artist A", chart_id=1)
    _record_exact(db, config, track, chart)

    plan = build_plan(config, db, size_lookup=lambda c: 12345)
    assert plan[0]["planned_bytes"] == 12345
    db.close()


def test_duplicate_charts_not_planned_twice(config):
    db = _setup_db(config)
    track1 = make_track("Same Song", "Same Artist", spotify_id="id1")
    track2 = make_track("Same Song", "Same Artist", spotify_id="id2")
    db.replace_tracks([track1, track2], {"a.csv": 2})
    ids = [t.db_id for t in db.get_tracks()]
    # Both tracks matched the same chart.
    chart = make_chart("Same Song", "Same Artist", chart_id=7)
    db.upsert_candidate(chart)
    for tid in ids:
        db.record_match_score(tid, chart.db_id, 95.0, "AUTO_ACCEPT",
                              json.dumps({"explanation": "test"}))

    plan = build_plan(config, db)
    # One md5 -> one selection only.
    assert len(plan) == 1
    db.close()


def test_expansion_cap_respected(config):
    db = _setup_db(config)
    track = make_track("Song A", "Big Artist")
    _insert_track(db, track)
    charts = [make_chart(f"Hit {i}", "Big Artist", chart_id=100 + i) for i in range(6)]
    db.replace_expansion_artists([{
        "artist_name": "Big Artist", "artist_norm": "big artist",
        "track_count": 10, "is_featured_only": False, "cap": 3, "disabled": False,
    }])
    artist_id = db.get_expansion_artists()[0]["id"]
    for i, chart in enumerate(charts):
        db.upsert_candidate(chart)
        db.record_expansion_candidate(artist_id, chart.db_id, 90.0 + i,
                                      json.dumps({"explanation": "test"}))

    config.expansion.min_score = 80.0
    plan = build_plan(config, db)
    expansion = [e for e in plan if e["kind"] == "expansion"]
    assert len(expansion) == 3
    db.close()


def test_exact_only_plan_ignores_persisted_expansion(config):
    db = _setup_db(config)
    track = make_track("Song A", "Artist A")
    _insert_track(db, track)
    exact = make_chart("Song A", "Artist A", chart_id=1)
    expansion = make_chart("Song B", "Artist A", chart_id=2)
    _record_exact(db, config, track, exact)
    _record_expansion(db, "Artist A", "artist a", expansion, score=95.0)

    plan = build_plan(config, db, include_expansion=False)
    assert [entry["kind"] for entry in plan] == ["exact"]
    assert db.get_selections()[0]["kind"] == "exact"
    db.close()


def test_windows_safe_target_paths(config):
    config.library.layout = "legacy"
    db = _setup_db(config)
    track = make_track("Song: Live?", "AC/DC")
    chart = make_chart("Song: Live?", "AC/DC", chart_id=1)
    _record_exact(db, config, track, chart)
    plan = build_plan(config, db)
    relpath = plan[0]["target_relpath"]
    for char in '<>:"|?*':
        assert char not in relpath
    assert "00 - His Music" in relpath
    assert "Exact Playlist Songs" in relpath
    db.close()
