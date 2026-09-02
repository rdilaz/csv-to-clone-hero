"""Database layer unit tests."""

import pytest
from pydantic import ValidationError

from csv_to_clonehero.config import ExpansionConfig
from csv_to_clonehero.database import Database, SCHEMA_VERSION

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


def test_clear_workspace_candidates_scoped_and_global(tmp_path):
    from csv_to_clonehero.models import ChartCandidate

    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="testws", name="workspace", kind="exact", status="complete", phase="complete")
    db.upsert_spotify_tracks([{
        "track_key": "spotify:0001", "spotify_id": "0001", "title": "Song",
        "title_norm": "song", "artist_full": "Artist", "artist_norm": "artist",
        "primary_artist": "Artist", "primary_artist_norm": "artist",
        "album": "Album", "album_norm": "album",
    }])
    exact_chart = ChartCandidate(
        source="test", chart_id=100, md5="a" * 32, name="Song", artist="Artist",
        name_norm="song", artist_norm="artist", charter="Charter",
        instruments=["guitar"], diff_guitar=3,
    )
    exp_chart = ChartCandidate(
        source="test", chart_id=200, md5="b" * 32, name="Expansion Song", artist="Artist",
        name_norm="expansion song", artist_norm="artist", charter="Charter",
        instruments=["guitar"], diff_guitar=3,
    )

    db.upsert_workspace_candidate(ws, track_id="spotify:0001", candidate=exact_chart, score=90, eligibility="EXACT_CONFIRMED", kind="exact")
    db.select_workspace_candidate(ws, "spotify:0001", exact_chart, kind="exact")

    db.upsert_workspace_candidate(ws, track_id=None, candidate=exp_chart, score=85, eligibility="EXPANSION_CONFIRMED", kind="expansion", artist_norm="artist")
    db.select_workspace_candidate(ws, None, exp_chart, kind="expansion", artist_norm="artist")

    assert len(db.get_workspace_candidates(ws)) == 2
    assert len(db.get_workspace_selections(ws)) == 2

    # Clear only expansion
    db.clear_workspace_candidates(ws, kind="expansion")
    assert len(db.get_workspace_candidates(ws, kind="expansion")) == 0
    assert len(db.get_workspace_candidates(ws, kind="exact")) == 1
    assert len(db.get_workspace_selections(ws, kind="expansion")) == 0
    assert len(db.get_workspace_selections(ws, kind="exact")) == 1

    # Clear all
    db.clear_workspace_candidates(ws)
    assert len(db.get_workspace_candidates(ws)) == 0
    assert len(db.get_workspace_selections(ws)) == 0

    db.close()


def test_select_workspace_candidate_blocks_nested_production_hard_gate(tmp_path):
    import json

    from csv_to_clonehero.models import ChartCandidate

    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="nested-gate")
    db.upsert_spotify_tracks([{
        "track_key": "spotify:nested", "spotify_id": "nested", "title": "Song",
        "title_norm": "song", "artist_full": "Artist", "artist_norm": "artist",
        "primary_artist": "Artist", "primary_artist_norm": "artist",
    }])
    chart = ChartCandidate(
        source="test", chart_id=102, md5="c" * 32, name="Song", artist="Artist",
        name_norm="song", artist_norm="artist", instruments=["guitar"], diff_guitar=3,
    )
    db.upsert_workspace_candidate(
        ws,
        "spotify:nested",
        chart,
        score=99,
        eligibility="EXCLUDED",
        decision="REJECT",
        breakdown_json=json.dumps({
            "factors": {
                "gates": {
                    "hard_gate": True,
                    "hard_gate_reasons": ["artist identity conflict"],
                }
            }
        }),
        kind="exact",
    )

    with pytest.raises(ValueError, match="hard compatibility gate"):
        db.select_workspace_candidate(
            ws, "spotify:nested", chart, kind="exact", force=True
        )
    db.close()


def _workspace_track(db, spotify_id="0001"):
    db.upsert_spotify_tracks([{
        "track_key": f"spotify:{spotify_id}", "spotify_id": spotify_id,
        "title": "Song", "title_norm": "song", "artist_full": "Artist",
        "artist_norm": "artist", "primary_artist": "Artist",
        "primary_artist_norm": "artist", "album": "Album", "album_norm": "album",
    }])


def test_schema_migration_preserves_legacy_expansion_state(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    ws = db.create_workspace(
        workspace_key="legacy", status="running", phase="expansion_preview"
    )
    _workspace_track(db)
    db.set_workspace_scope(ws, playlist_ids=["playlist-1"], include_liked=True)
    db.replace_workspace_library_tracks(ws, db.get_spotify_tracks(True))
    artists = [
        {"artist_name": f"Artist {index}", "artist_norm": f"artist-{index}",
         "selected": True, "metadata": {"preview": ["song"]}}
        for index in range(26)
    ]
    db.replace_workspace_expansion_artists(ws, artists)
    exact = make_chart("Song", "Artist", chart_id=100, md5="a" * 32)
    expansion = make_chart("Other", "Artist", chart_id=200, md5="b" * 32)
    db.upsert_workspace_candidate(
        ws, "spotify:0001", exact, score=95, eligibility="EXACT_CONFIRMED", kind="exact"
    )
    db.select_workspace_candidate(ws, "spotify:0001", exact, kind="exact")
    db.upsert_workspace_candidate(
        ws, None, expansion, score=85, eligibility="EXPANSION_CONFIRMED",
        kind="expansion", artist_norm="artist-0",
    )
    db.select_workspace_candidate(
        ws, None, expansion, kind="expansion", artist_norm="artist-0"
    )
    with db._conn:
        db._conn.execute("UPDATE schema_version SET version = 5")
    db.close()

    migrated = Database(path)
    version = migrated._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert len(migrated.get_workspace_candidates(ws, kind="exact")) == 1
    assert len(migrated.get_workspace_selections(ws, kind="exact")) == 1
    assert len(migrated.get_workspace_candidates(ws, kind="expansion")) == 1
    assert len(migrated.get_workspace_selections(ws, kind="expansion")) == 1
    assert all(row["selected"] == 1 for row in migrated.get_workspace_expansion_artists(ws))
    assert all(row["metadata_json"] != "{}" for row in migrated.get_workspace_expansion_artists(ws))
    assert migrated.get_workspace_scope_data(ws)["playlist_ids"] == ["playlist-1"]
    assert len(migrated.get_workspace_library_track_ids(ws)) == 1
    migrated.close()


def test_expansion_batch_checkpoint_interrupt_and_resume(tmp_path):
    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="batch")
    db.replace_workspace_expansion_artists(ws, [
        {"artist_name": "One", "artist_norm": "one"},
        {"artist_name": "Two", "artist_norm": "two"},
    ])
    batch_id = db.create_expansion_batch(
        ws, ["one", "two"], run_id="run-1", selection_fingerprint="fingerprint",
        status="running",
    )
    db.checkpoint_expansion_batch_artist(
        batch_id, "one", phase="preview", status="complete", request_count=3,
        metadata={"songs": ["A"]},
    )
    db.checkpoint_expansion_batch_artist(
        batch_id, "two", phase="preview", status="running", request_count=1,
    )
    assert db.mark_active_expansion_batches_interrupted() == 1
    assert db.get_expansion_batch(batch_id)["status"] == "interrupted"
    artists = db.get_expansion_batch_artists(batch_id)
    assert [row["preview_status"] for row in artists] == ["complete", "interrupted"]

    resumed = db.resume_expansion_batch(batch_id, run_id="run-2")
    assert resumed["run_id"] == "run-2"
    assert resumed["cancel_requested"] == 0
    artists = db.get_expansion_batch_artists(batch_id)
    assert [row["preview_status"] for row in artists] == ["complete", "pending"]
    assert db.request_expansion_batch_cancel(batch_id)["cancel_requested"] == 1
    db.close()


def test_expansion_batch_rejects_more_than_25_artists(tmp_path):
    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="bounded")
    artists = [
        {"artist_name": str(index), "artist_norm": f"artist-{index}"}
        for index in range(26)
    ]
    db.replace_workspace_expansion_artists(ws, artists)
    with pytest.raises(ValueError, match="at most 25"):
        db.create_expansion_batch(ws, [item["artist_norm"] for item in artists])
    db.close()


def test_replace_expansion_artist_candidates_is_transactional_and_idempotent(tmp_path):
    db = Database(tmp_path / "state.db")
    ws = db.create_workspace(workspace_key="replace")
    db.replace_workspace_expansion_artists(
        ws, [{"artist_name": "Artist", "artist_norm": "artist"}]
    )
    batch_id = db.create_expansion_batch(ws, ["artist"], status="running", phase="search")
    old = make_chart("Old", "Artist", chart_id=300, md5="c" * 32)
    new = make_chart("New", "Artist", chart_id=301, md5="d" * 32)
    db.upsert_workspace_candidate(
        ws, None, old, kind="expansion", artist_norm="artist", score=80
    )

    with pytest.raises(ValueError, match="unknown expansion batch artist"):
        db.replace_expansion_artist_candidates(
            ws, "artist", [{"candidate": new, "score": 90}], batch_id=batch_id + 999
        )
    rows = db.get_workspace_candidates(ws, kind="expansion")
    assert [row["chart_id"] for row in rows] == [300]

    first = db.replace_expansion_artist_candidates(
        ws, "artist", [{"candidate": new, "score": 90}],
        batch_id=batch_id, request_count=2,
    )
    second = db.replace_expansion_artist_candidates(
        ws, "artist", [{"candidate": new, "score": 91}],
        batch_id=batch_id, request_count=2,
    )
    assert len(first) == len(second) == 1
    rows = db.get_workspace_candidates(ws, kind="expansion")
    assert len(rows) == 1 and rows[0]["chart_id"] == 301 and rows[0]["score"] == 91
    artist = db.get_expansion_batch_artists(batch_id)[0]
    assert artist["search_status"] == "complete"
    assert artist["search_requests"] == 2
    db.close()


def test_expansion_config_enforces_hard_batch_limit():
    assert ExpansionConfig().max_artists_per_batch == 25
    assert ExpansionConfig(max_artists_per_batch=1).max_artists_per_batch == 1
    with pytest.raises(ValidationError):
        ExpansionConfig(max_artists_per_batch=26)
