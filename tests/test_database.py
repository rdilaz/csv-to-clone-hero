"""Database layer unit tests."""

from csv_to_clonehero.database import Database

from .conftest import make_chart


def test_disabled_files_roundtrip(tmp_path):
    db = Database(tmp_path / "state.db")
    assert db.get_disabled_files() == set()
    db.set_disabled_files({"a.csv", "b.csv"})
    assert db.get_disabled_files() == {"a.csv", "b.csv"}
    db.set_disabled_files({"a.csv"})
    assert db.get_disabled_files() == {"a.csv"}
    db.close()


def test_disabled_files_invalid_meta_defaults_to_empty(tmp_path):
    db = Database(tmp_path / "state.db")
    db.set_meta("disabled_files", "not-json")
    assert db.get_disabled_files() == set()
    db.close()


def test_playlist_creator_metadata_roundtrip(tmp_path):
    db = Database(tmp_path / "state.db")
    db.upsert_spotify_playlist({
        "id": "playlist-1",
        "name": "Owned playlist",
        "owner": {"id": "user-1", "display_name": "Test User"},
    })
    row = db.get_spotify_playlist("playlist-1")
    assert row is not None
    assert row["owner_id"] == "user-1"
    assert row["owner_name"] == "Test User"
    db.close()


def test_cached_query_freshness_check_skips_candidate_materialization(tmp_path):
    db = Database(tmp_path / "state.db")
    key = db.query_key("song", "artist", "title", source_name="test", result_cap=10)
    charts = [make_chart("Title", "Artist", chart_id=idx, md5=f"{idx:032d}") for idx in range(1, 4)]
    db.record_query(
        key, "song", "Artist - Title", charts,
        source_name="test", result_cap=10, returned_count=3, cap_reached=True,
    )

    # Freshness agrees with the full lookup...
    assert db.cached_query_is_fresh(key, ttl_days=7, result_cap=10, source_name="test")
    assert db.fresh_cached_query(key, ttl_days=7, result_cap=10) is not None
    full = db.get_cached_query(key, ttl_days=7, result_cap=10, source_name="test")
    assert full is not None and len(full[0]) == 3

    # ...but never materializes candidates: it stays a hit even after the
    # candidate links are gone, where the full lookup degrades to an empty list.
    with db._conn:
        db._conn.execute("DELETE FROM search_query_candidates")
    assert db.cached_query_is_fresh(key, ttl_days=7, result_cap=10, source_name="test")
    degraded = db.get_cached_query(key, ttl_days=7, result_cap=10, source_name="test")
    assert degraded is not None and degraded[0] == []

    # TTL and cap semantics match the full lookup on both sides.
    assert not db.cached_query_is_fresh(key, ttl_days=0, result_cap=10)
    assert db.get_cached_query(key, ttl_days=0, result_cap=10) is None
    assert not db.cached_query_is_fresh(key, ttl_days=7, result_cap=11)
    assert db.get_cached_query(key, ttl_days=7, result_cap=11) is None
    db.close()


def test_select_workspace_candidate_force_allows_excluded_but_not_incompatible(tmp_path):
    import pytest

    from csv_to_clonehero.models import ChartCandidate

    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="testws", name="workspace", kind="exact", status="complete", phase="complete")
    db.upsert_spotify_tracks([{
        "track_key": "spotify:0001", "spotify_id": "0001", "title": "Song",
        "title_norm": "song", "artist_full": "Artist", "artist_norm": "artist",
        "primary_artist": "Artist", "primary_artist_norm": "artist",
        "album": "Album", "album_norm": "album",
    }])
    chart = ChartCandidate(
        source="test", chart_id=100, md5="a" * 32, name="Song", artist="Artist",
        name_norm="song", artist_norm="artist", charter="Charter",
        instruments=["guitar"], diff_guitar=3,
    )
    db.upsert_workspace_candidate(ws, track_id="spotify:0001", candidate=chart, score=55, eligibility="EXCLUDED", kind="exact")

    with pytest.raises(ValueError, match="not eligible"):
        db.select_workspace_candidate(ws, "spotify:0001", chart, kind="exact")

    sel_id = db.select_workspace_candidate(ws, "spotify:0001", chart, kind="exact", eligibility="ELIGIBLE_REVIEW", force=True)
    assert sel_id is not None

    bad = ChartCandidate(
        source="test", chart_id=101, md5="b" * 32, name="Bad", artist="Wrong",
        name_norm="bad", artist_norm="wrong", charter="X", instruments=["vocals"],
    )
    db.upsert_workspace_candidate(ws, track_id="spotify:0001", candidate=bad, score=10, eligibility="INCOMPATIBLE", kind="exact")
    with pytest.raises(ValueError, match="not eligible"):
        db.select_workspace_candidate(ws, "spotify:0001", bad, kind="exact", force=True)

    db.close()


def test_record_export_item_tolerates_stale_selection_id(tmp_path):
    from csv_to_clonehero.models import ChartCandidate

    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="testws", name="workspace", kind="exact", status="complete", phase="complete")
    db.upsert_spotify_tracks([{
        "track_key": "spotify:0001", "spotify_id": "0001", "title": "Song",
        "title_norm": "song", "artist_full": "Artist", "artist_norm": "artist",
        "primary_artist": "Artist", "primary_artist_norm": "artist",
        "album": "Album", "album_norm": "album",
    }])
    chart = ChartCandidate(
        source="test", chart_id=100, md5="a" * 32, name="Song", artist="Artist",
        name_norm="song", artist_norm="artist", charter="Charter",
        instruments=["guitar"], diff_guitar=3,
    )
    db.upsert_workspace_candidate(ws, track_id="spotify:0001", candidate=chart, score=55, eligibility="ELIGIBLE_REVIEW", kind="exact")
    sel_id = db.select_workspace_candidate(ws, "spotify:0001", chart, kind="exact", eligibility="ELIGIBLE_REVIEW")

    export_id = db.create_export_record(ws, export_key="fk-tolerance", kind="folder", destination="dest", status="running", managed=False)

    # A selection id that no longer exists must be dropped, not fail the FK.
    db.record_export_item(export_id, {
        "selection_id": 999999,
        "source": "test",
        "chart_id": 100,
        "target_relpath": "Artist/Album/Artist - Song.sng",
        "status": "published",
    })
    rows = db.get_export_items(export_id)
    assert len(rows) == 1
    assert rows[0]["selection_id"] is None

    # A valid selection id is still recorded.
    db.record_export_item(export_id, {
        "selection_id": sel_id,
        "source": "test",
        "chart_id": 200,
        "target_relpath": "Artist/Album/Artist - Other.sng",
        "status": "published",
    })
    assert any(item["selection_id"] == sel_id for item in db.get_export_items(export_id))

    db.close()
