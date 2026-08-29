import json

from csv_to_clonehero.combined_pipeline import completed_scan_fast_path, discover_combined, matching_cache_fingerprint, matching_scan_fingerprint, rank_expansion_artists
from csv_to_clonehero.catalog import CatalogLookup
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack


class FakeCatalog:
    available = True
    diagnostic = ""

    def __init__(self, chart):
        self.chart = chart

    def candidates(self, artist, title):
        return (self.chart,) if (artist, title) == ("Artist", "Song") else ()

    def artist_songs(self, artist):
        return (("Song", (self.chart,)),) if artist == "artist" else ()


def test_combined_discovery_deduplicates_provenance_and_scores(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=12)
    snapshots = [
        LibrarySnapshot("spotify", [ProviderTrack("spotify", "sp-1", "Song", "Artist", playlist_names=["Mix"])]),
        LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist", liked=True)]),
    ]
    db = Database(config.database_path)
    try:
        result = discover_combined(config, db, snapshots, workspace_key="combined:test", catalog=FakeCatalog(chart))
        assert result.tracks == 1
        assert result.exact_hits == 1
        assert result.candidates == 1
        track = db.get_spotify_tracks(True)[0]
        current = db.get_workspace_candidates(
            "combined:test", track_id=track.db_id, kind="exact", current_only=True
        )
        assert [row["chart_id"] for row in current] == [12]
        assert current[0]["task_id"] is not None
        providers = track.origin_metadata["provenance"]
        assert {entry["provider"] for entry in providers} == {"spotify", "soundcloud"}
    finally:
        db.close()


def test_combined_progress_reports_full_retrieval_and_final_song_state(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=13)
    events = []
    db = Database(config.database_path)
    try:
        result = discover_combined(
            config, db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "sc-1", "Song", "Artist"),
                ProviderTrack("soundcloud", "sc-2", "Missing", "Nobody"),
            ])],
            workspace_key="combined:progress", catalog=FakeCatalog(chart),
            on_progress=events.append,
        )
        assert result.tracks == 2
        assert events[0]["phase"] == "catalog_prepare"
        assert events[-1]["status"] == "complete"
        assert events[-1]["processed_tracks"] == 2
        rows = db.get_discovery_song_progress(1, limit=10)
        assert {row["classification"] for row in rows} == {"strong", "no_catalog_candidate"}
        assert max(len(json.dumps(event)) for event in events) < 2000
    finally:
        db.close()


def test_identical_discovery_reuses_cached_scored_relationships(config, monkeypatch):
    from tests.conftest import make_chart, make_track
    import csv_to_clonehero.combined_pipeline as pipeline

    chart = make_chart("Song", "Artist", chart_id=14)
    snapshot = [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])]
    calls = 0
    original = pipeline.score_candidate

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "score_candidate", counted)
    db = Database(config.database_path)
    try:
        first = discover_combined(config, db, snapshot, workspace_key="combined:cache", catalog=FakeCatalog(chart))
        first_calls = calls
        second = discover_combined(config, db, snapshot, workspace_key="combined:cache", catalog=FakeCatalog(chart))
        assert first_calls == 1
        assert calls == first_calls
        assert second.strong_match_tracks == first.strong_match_tracks
        assert second.review_match_tracks == first.review_match_tracks
        assert db._conn.execute("SELECT COUNT(*) FROM matching_song_cache").fetchone()[0] == 1
    finally:
        db.close()


def test_completed_scan_fast_path_skips_per_song_iteration(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline

    chart = make_chart("Song", "Artist", chart_id=19)
    snapshot = [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])]
    catalog = FakeCatalog(chart)
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1})
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: catalog)
    calls = 0
    original = pipeline.score_candidate
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline, "score_candidate", counted)
    events = []
    db = Database(config.database_path)
    try:
        discover_combined(config, db, snapshot, workspace_key="combined:fast", on_progress=events.append)
        scan_count = db._conn.execute(
            "SELECT COUNT(*) FROM discovery_scans WHERE workspace_id = "
            "(SELECT id FROM discovery_workspaces WHERE workspace_key = 'combined:fast')"
        ).fetchone()[0]
        second = discover_combined(config, db, snapshot, workspace_key="combined:fast", on_progress=events.append)
        assert calls == 1
        assert second.cached_tracks == 1
        assert second.recomputed_tracks == 0
        assert db._conn.execute(
            "SELECT COUNT(*) FROM discovery_scans WHERE workspace_id = "
            "(SELECT id FROM discovery_workspaces WHERE workspace_key = 'combined:fast')"
        ).fetchone()[0] == scan_count
        assert db.get_workspace_candidates(
            "combined:fast", kind="exact", current_only=True
        )
        assert any(event.get("message") == "Reusing 1 existing matches" for event in events)
    finally:
        db.close()


def test_completed_fast_path_ignores_stale_candidate_rows(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline
    chart = make_chart("Song", "Artist", chart_id=20)
    snapshot = [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])]
    catalog = FakeCatalog(chart)
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    db = Database(config.database_path)
    try:
        discover_combined(config, db, snapshot, workspace_key="combined:stale", catalog=catalog)
        library_fp = db.refresh_library_matching_fingerprint(db.get_workspace("combined:stale")["id"])
        old_key = db._conn.execute("SELECT fingerprint FROM matching_scan_cache").fetchone()[0]
        new_key = matching_scan_fingerprint((), manifest, config, library_fingerprint=library_fp)
        db._conn.execute("UPDATE matching_scan_cache SET fingerprint=? WHERE fingerprint=?", (new_key, old_key))
        stale = make_chart("Stale", "Artist", chart_id=21)
        db.upsert_spotify_tracks([ProviderTrack("soundcloud", "stale", "Stale", "Artist")])
        db.upsert_candidate(stale)
        db.upsert_workspace_candidate(db.get_workspace("combined:stale")["id"], "artist|stale", stale, kind="exact")
        assert completed_scan_fast_path(config, db, db.get_workspace("combined:stale")["id"], library_fp) is not None
    finally:
        db.close()


def test_completed_fast_path_materializes_legacy_combined_generation(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline

    chart = make_chart("Song", "Artist", chart_id=22)
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    db = Database(config.database_path)
    try:
        workspace_key = "combined:migration"
        discover_combined(
            config, db,
            [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])],
            workspace_key=workspace_key, catalog=FakeCatalog(chart),
        )
        workspace_id = db.get_workspace(workspace_key)["id"]
        library_fp = db.refresh_library_matching_fingerprint(workspace_id)
        old_key = db._conn.execute("SELECT fingerprint FROM matching_scan_cache").fetchone()[0]
        new_key = matching_scan_fingerprint((), manifest, config, library_fingerprint=library_fp)
        db._conn.execute("UPDATE matching_scan_cache SET fingerprint=? WHERE fingerprint=?", (new_key, old_key))
        from csv_to_clonehero.providers.base import provider_track_to_track
        db.upsert_spotify_tracks([provider_track_to_track(ProviderTrack("soundcloud", "sc-removed", "Removed", "Artist"))])
        removed = next(track for track in db.get_spotify_tracks(True) if track.title == "Removed")
        removed_chart = make_chart("Removed", "Artist", chart_id=24)
        db.upsert_candidate(removed_chart)
        db.upsert_workspace_candidate(workspace_id, removed.track_key, removed_chart, kind="exact")
        db._conn.execute("DELETE FROM discovery_scans WHERE workspace_id=?", (workspace_id,))
        db._conn.execute("DELETE FROM discovery_tasks WHERE workspace_id=?", (workspace_id,))
        db._conn.commit()

        first = completed_scan_fast_path(config, db, workspace_id, library_fp)
        assert first is not None and first.cached_tracks == 1 and first.recomputed_tracks == 0
        generation = db.get_current_combined_generation(workspace_id)
        assert generation is not None
        generation_id = generation["id"]
        assert db.get_workspace_candidates(workspace_id, kind="exact", current_only=True)
        assert db.get_workspace_candidates(workspace_id, track_id=removed.db_id, kind="exact")
        assert db.get_workspace_candidates(workspace_id, track_id=removed.db_id, kind="exact", current_only=True) == []
        second = completed_scan_fast_path(config, db, workspace_id, library_fp)
        assert second is not None and db.get_current_combined_generation(workspace_id)["id"] == generation_id
        assert db._conn.execute("SELECT COUNT(*) FROM discovery_scans WHERE workspace_id=?", (workspace_id,)).fetchone()[0] == 1
    finally:
        db.close()


def test_current_generation_ignores_other_exact_scans_and_newer_incomplete_combined(config):
    from tests.conftest import make_chart, make_track

    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="combined:authority", name="Authority", kind="exact")
        track = make_track("Song", "Artist")
        db.replace_tracks([track], {"test.csv": 1})
        chart = make_chart("Song", "Artist", chart_id=23)
        db.upsert_candidate(chart)
        exact_scan = db.create_discovery_scan(workspace, scan_key="legacy", kind="exact", status="complete")
        task = db.upsert_discovery_task(workspace, scan_id=exact_scan, source="legacy", query_key="legacy", status="complete")
        db.upsert_workspace_candidate(workspace, track.track_key, chart, task_id=task, kind="exact")
        assert db.get_workspace_candidates(workspace, kind="exact", current_only=True) == []
        combined = db.create_discovery_scan(workspace, scan_key="combined", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        task = db.upsert_discovery_task(workspace, scan_id=combined, source="combined", query_key="combined", status="complete")
        db.assign_workspace_track_candidates_to_task(workspace, track.track_key, task)
        assert db.get_workspace_candidates(workspace, kind="exact", current_only=True)
        running = db.create_discovery_scan(workspace, scan_key="combined-running", kind="exact", status="running", metadata={"workflow": "combined_catalog"})
        db.upsert_discovery_task(workspace, scan_id=running, source="combined", query_key="running", status="complete")
        assert db.get_current_combined_generation(workspace) is None
        db.create_discovery_scan(workspace, scan_key="combined-new", kind="exact", status="partial", metadata={"workflow": "combined_catalog"})
        assert db.get_current_combined_generation(workspace) is None
    finally:
        db.close()


def test_partial_library_change_reuses_unchanged_song_only(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline

    first_chart = make_chart("Song", "Artist", chart_id=17)
    second_chart = make_chart("New", "Artist", chart_id=18)
    calls = 0
    original = pipeline.score_candidate
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline, "score_candidate", counted)
    db = Database(config.database_path)
    try:
        class PartialCatalog(FakeCatalog):
            def candidates(self, artist, title):
                return (first_chart,) if title == "Song" else (second_chart,)
        catalog = PartialCatalog(first_chart)
        discover_combined(config, db, [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "one", "Song", "Artist")])], workspace_key="combined:partial", catalog=catalog)
        catalog.chart = second_chart
        discover_combined(config, db, [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "one", "Song", "Artist"), ProviderTrack("soundcloud", "two", "New", "Artist")])], workspace_key="combined:partial", catalog=catalog)
        assert calls == 2
    finally:
        db.close()


def test_matching_cache_fingerprint_changes_with_track_catalog_and_config(config):
    from tests.conftest import make_chart, make_track
    track = make_track("Song", "Artist")
    catalog = FakeCatalog(make_chart("Song", "Artist", chart_id=15))
    baseline = matching_cache_fingerprint(track, catalog, config)
    changed_track = make_track("Changed", "Artist")
    assert matching_cache_fingerprint(changed_track, catalog, config) != baseline
    catalog.snapshot_id = "different"
    assert matching_cache_fingerprint(track, catalog, config) != baseline
    config.search.catalog_artist_fuzzy_limit += 1
    assert matching_cache_fingerprint(track, catalog, config) != baseline
    config.instruments.require_five_fret_guitar = not config.instruments.require_five_fret_guitar
    assert matching_cache_fingerprint(track, catalog, config) != baseline


def test_corrupt_matching_cache_fails_safe_and_recomputes(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline

    chart = make_chart("Song", "Artist", chart_id=16)
    snapshot = [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])]
    db = Database(config.database_path)
    try:
        discover_combined(config, db, snapshot, workspace_key="combined:corrupt", catalog=FakeCatalog(chart))
        db._conn.execute("UPDATE matching_song_cache SET result_json = '{bad'")
        calls = 0
        original = pipeline.score_candidate
        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(pipeline, "score_candidate", counted)
        discover_combined(config, db, snapshot, workspace_key="combined:corrupt", catalog=FakeCatalog(chart))
        assert calls == 1
    finally:
        db.close()


def test_expansion_artist_ranking_only_uses_imported_artists():
    class Catalog:
        available = True

        def artist_songs(self, artist):
            return (("Song", (object(),)),) * (3 if artist == "artist" else 1)

    ranked = rank_expansion_artists(Catalog(), [type("Track", (), {"primary_artist_norm": "artist"})()])
    assert ranked[0]["artist"] == "artist"
    assert ranked[0]["catalog_songs"] == 3


def test_combined_discovery_scores_non_exact_local_candidates_and_keeps_versions(config):
    from tests.conftest import make_chart, make_track

    charts = [
        make_chart("Song", "Artist", chart_id=20),
        make_chart("Song (Live)", "Artist", chart_id=21),
    ]

    class BroadCatalog(FakeCatalog):
        def candidates(self, artist, title):
            return ()

        def high_recall_candidates(self, artist, title, **_kwargs):
            return CatalogLookup(
                candidates=tuple(charts),
                strict_exact=False,
                methods=("artist_fuzzy",),
                retrieved_song_keys=2,
                exact_artist=False,
                exact_title=False,
                candidate_methods={
                    ("test", 20): ("artist_fuzzy", "title_token"),
                    ("test", 21): ("title_token",),
                },
            )

    db = Database(config.database_path)
    try:
        track = make_track("Song", "Artist feat. Guest")
        db.replace_tracks([track], {"test.csv": 1})
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", track.title, track.artist_full)])],
            workspace_key="combined:broad",
            catalog=BroadCatalog(charts[0]),
        )
        assert result.exact_hits == 0
        assert result.broader_retrieval_tracks == 1
        assert result.candidate_charts_evaluated == 2
        assert result.persisted_candidates == 2
        rows = db.get_workspace_candidates(1, kind="exact", include_rejected=True)
        assert {row["chart_id"] for row in rows} == {20, 21}
        provenance = json.loads(rows[0]["metadata_json"])
        assert provenance["catalog_retrieval"]["candidate_methods"]
    finally:
        db.close()
