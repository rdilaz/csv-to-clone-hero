import json

import pytest

from csv_to_clonehero.combined_pipeline import _primary_rejection_reason, _rejection_exemplar, completed_scan_fast_path, discover_combined, enforce_library_limit, estimate_remaining_range, expand_combined, matching_cache_fingerprint, matching_scan_fingerprint, rank_expansion_artists, workspace_snapshots
from csv_to_clonehero.catalog import CatalogLookup
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderPlaylist, ProviderTrack, provider_track_to_track


class FakeCatalog:
    available = True
    diagnostic = ""
    snapshot_id = "fixture"
    sha256 = "fixture-sha"
    validation_fingerprint = ""
    chart_count = 1
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}

    def __init__(self, chart):
        self.chart = chart

    def candidates(self, artist, title):
        return (self.chart,) if (artist, title) == ("Artist", "Song") else ()

    def artist_songs(self, artist):
        return (("Song", (self.chart,)),) if artist == "artist" else ()


@pytest.mark.parametrize("title,artist,chart_title,chart_artist", [
    ("65th & Ingleside", "Chance the Rapper", "10 Miles Wide", "Escape the Fate"),
    ("Acid Rain", "Chance the Rapper", "Mad Man", "Haste the Day"),
])
def test_blocked_identity_does_not_inflate_catalog_yield_even_with_quality_bonus(
    config, title, artist, chart_title, chart_artist
):
    from tests.conftest import make_chart
    from csv_to_clonehero.matching import score_candidate, BLOCKED

    wrong = make_chart(chart_title, chart_artist, chart_id=98001, icon="rb2", charter="Harmonix")
    snapshot = LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "wrong-pair", title, artist)])

    class WrongCatalog(FakeCatalog):
        def candidates(self, _artist, _title):
            return (wrong,)

    db = Database(config.database_path)
    try:
        scored = score_candidate(provider_track_to_track(snapshot.tracks[0]), wrong, config)
        assert scored.final_score >= 70 and scored.result_tier == BLOCKED
        for attempt in range(2):
            result = discover_combined(config, db, [snapshot], workspace_key="combined:blocked-yield", catalog=WrongCatalog(wrong))
            assert result.strong_match_tracks == result.review_match_tracks == 0
            assert result.plausible_match_tracks == result.eligible_candidates == 0
            assert result.rejected_candidates == result.hard_rejected_only_tracks == 1
            assert result.songs_with_selectable_charts == result.selectable_chart_artifacts == 0
            assert result.cached_tracks == attempt
    finally:
        db.close()


def test_blocked_quality_candidate_does_not_suppress_catalog_fallback(config):
    from tests.conftest import make_chart
    wrong = make_chart("Mad Man", "Haste the Day", chart_id=98011, icon="rb2", charter="Harmonix")
    good = make_chart("Acid Rain", "Chance the Rapper", chart_id=98012)
    calls = []

    class FallbackCatalog(FakeCatalog):
        def high_recall_candidates(self, _artist, _title, **kwargs):
            fallback = kwargs.get("include_fuzzy_fallback", False)
            calls.append(fallback)
            return CatalogLookup(candidates=(wrong, good) if fallback else (wrong,),
                                 methods=("artist_fuzzy",), fuzzy_fallback_attempted=fallback)

    db = Database(config.database_path)
    try:
        result = discover_combined(config, db, [LibrarySnapshot("soundcloud", [
            ProviderTrack("soundcloud", "fallback", "Acid Rain", "Chance the Rapper")
        ])], workspace_key="combined:blocked-fallback", catalog=FallbackCatalog(wrong))
        assert calls == [False, True]
        assert result.strong_match_tracks == result.songs_with_selectable_charts == 1
        assert result.eligible_candidates == result.rejected_candidates == 1
    finally:
        db.close()


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
        assert current[0]["track_id"] == track.db_id
        assert current[0]["track_id"] is not None
        assert current[0]["task_id"] is not None
        assert track.track_key == "artist|song"
        providers = track.origin_metadata["provenance"]
        assert {entry["provider"] for entry in providers} == {"spotify", "soundcloud"}
    finally:
        db.close()


def test_combined_discovery_persists_wrong_same_artist_song_as_noneligible(config):
    from tests.conftest import make_chart

    wrong = make_chart("That's What You Get", "Paramore", chart_id=25, charter="Harmonix", icon="rb2")

    class WrongSongCatalog(FakeCatalog):
        def candidates(self, artist, title):
            return (wrong,) if (artist, title) == ("Paramore", "The Only Exception") else ()

    db = Database(config.database_path)
    try:
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "paramore", "The Only Exception", "Paramore")
            ])],
            workspace_key="combined:title-conflict",
            catalog=WrongSongCatalog(wrong),
        )
        rows = db.get_workspace_candidates(
            "combined:title-conflict", kind="exact", include_rejected=True, current_only=True
        )
        assert len(rows) == 1
        assert rows[0]["decision"] == "REJECT"
        assert rows[0]["eligibility"] == "INCOMPATIBLE"
        assert result.strong_match_tracks == 0
        assert result.review_match_tracks == 0
        assert result.hard_rejected_only_tracks == 1
    finally:
        db.close()


def test_combined_discovery_keeps_review_identity_out_of_best_matches(config):
    from tests.conftest import make_chart

    borderline = make_chart(
        "The Blessing / Shukufuku", "YOASOBI", chart_id=27,
        charter="Harmonix", icon="rb2",
    )
    borderline.pack_name = "Official Pack"

    class BorderlineCatalog(FakeCatalog):
        def candidates(self, artist, title):
            return (borderline,) if (artist, title) == ("YOASOBI", "Shukufuku") else ()

    db = Database(config.database_path)
    try:
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "yoasobi", "Shukufuku", "YOASOBI")
            ])],
            workspace_key="combined:title-review",
            catalog=BorderlineCatalog(borderline),
        )
        rows = db.get_workspace_candidates(
            "combined:title-review", kind="exact", include_rejected=True, current_only=True
        )
        assert len(rows) == 1
        assert rows[0]["decision"] == "REVIEW"
        assert rows[0]["eligibility"] == "ELIGIBLE_REVIEW"
        assert result.strong_match_tracks == 0
        assert result.review_match_tracks == 1
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


def test_yield_histogram_partitions_unique_songs_with_specific_reasons(config):
    from tests.conftest import make_chart

    strong = make_chart("Ready", "Artist", chart_id=130)
    no_guitar = make_chart("Blocked", "Artist", chart_id=131)
    no_guitar.instruments = ["drums"]
    no_guitar.diff_guitar = -1

    class YieldCatalog(FakeCatalog):
        chart_count = 2

        def candidates(self, artist, title):
            return {"Ready": (strong,), "Blocked": (no_guitar,)}.get(title, ())

    snapshots = [LibrarySnapshot("soundcloud", [
        ProviderTrack("soundcloud", "1", "Ready", "Artist"),
        ProviderTrack("soundcloud", "2", "Blocked", "Artist"),
        ProviderTrack("soundcloud", "3", "Missing", "Artist"),
    ])]
    db = Database(config.database_path)
    try:
        result = discover_combined(
            config, db, snapshots, workspace_key="combined:yield",
            catalog=YieldCatalog(strong),
        )
        assert sum(result.yield_histogram.values()) == result.tracks == 3
        assert (
            result.strong_match_tracks
            + result.review_match_tracks
            + sum(result.rejection_song_reasons.values())
        ) == result.tracks
        assert result.yield_histogram == {
            "recommended_strong": 1,
            "recommended_review": 0,
            "no_plausible_catalog_candidate": 1,
            "hard_rejected": 1,
            "soft_rejected": 0,
            "incomplete_evidence": 0,
        }
        assert result.rejection_song_reasons["no required 5-fret guitar"] == 1
        assert "INCOMPATIBLE" not in result.rejection_song_reasons
        assert "EXCLUDED" not in result.rejection_song_reasons
    finally:
        db.close()


def test_17000_class_canonical_library_is_within_hard_bound():
    tracks = [
        ProviderTrack("spotify", str(index), f"Song {index}", "Artist")
        for index in range(17_501)
    ]
    assert enforce_library_limit([LibrarySnapshot("spotify", tracks)], 50_000) == 17_501


def test_observed_eta_is_finite_nonnegative_and_waits_for_evidence():
    assert estimate_remaining_range(2, 100, 3.0) == {}
    assert estimate_remaining_range(100, 100, 10.0) == {}
    estimate = estimate_remaining_range(25, 100, 10.0)
    assert estimate["estimated_lower_seconds"] >= 0
    assert estimate["estimated_upper_seconds"] >= estimate["estimated_lower_seconds"]
    assert all(value < float("inf") for value in estimate.values())


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
        assert second.retrieval_seconds == 0
        assert second.scoring_seconds == 0
        assert second.persistence_seconds == 0
        final_scan_count = db._conn.execute(
            "SELECT COUNT(*) FROM discovery_scans WHERE workspace_id = "
            "(SELECT id FROM discovery_workspaces WHERE workspace_key = 'combined:fast')"
        ).fetchone()[0]
        assert final_scan_count in {scan_count, scan_count + 1}
        assert db.get_current_combined_generation("combined:fast") is not None
        assert db.get_workspace_candidates(
            "combined:fast", kind="exact", current_only=True
        )
        assert second.cached_tracks == 1
    finally:
        db.close()


def test_completed_fast_path_ignores_stale_candidate_rows(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline
    chart = make_chart("Song", "Artist", chart_id=20)
    snapshot = [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist")])]
    catalog = FakeCatalog(chart)
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}
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
        assert completed_scan_fast_path(
            config, db, db.get_workspace("combined:stale")["id"], library_fp,
            catalog=catalog,
        ) is not None
    finally:
        db.close()


def test_completed_fast_path_rebuilds_unbound_current_generation(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=201)
    snapshot = LibrarySnapshot(
        "spotify", [ProviderTrack("spotify", "legacy-fast", "Song", "Artist")]
    )
    catalog = FakeCatalog(chart)
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(
            workspace_key="combined:fast-unbound", name="Fast unbound", kind="exact"
        )
        db.upsert_library_snapshot(workspace, snapshot)
        snapshots = workspace_snapshots(db, workspace)
        discover_combined(
            config,
            db,
            snapshots,
            workspace_key="combined:fast-unbound",
            catalog=catalog,
        )
        first_generation = int(db.get_current_combined_generation(workspace)["id"])
        candidate = db.get_workspace_candidates(
            workspace, kind="exact", current_only=True
        )[0]
        db.select_workspace_candidate(
            workspace,
            candidate["track_id"],
            candidate["candidate_id"],
            require_workspace_candidate=True,
        )
        cached_revision = int(
            db._conn.execute(
                "SELECT candidate_revision FROM matching_scan_cache WHERE workspace_id=?",
                (workspace,),
            ).fetchone()[0]
        )
        db._conn.execute(
            "UPDATE workspace_candidates SET track_id=NULL WHERE id=?",
            (candidate["id"],),
        )
        db._conn.commit()
        assert db.get_workspace_candidate_revision(workspace) == cached_revision

        result = discover_combined(
            config,
            db,
            snapshots,
            workspace_key="combined:fast-unbound",
            catalog=catalog,
        )

        assert result.cached_tracks == 1
        assert int(db.get_current_combined_generation(workspace)["id"]) > first_generation
        current = db.get_workspace_candidates(
            workspace, kind="exact", current_only=True
        )
        assert len(current) == 1
        assert current[0]["track_id"] is not None
        assert db.list_workspace_selections(workspace, kind="exact") == []
    finally:
        db.close()


def test_completed_fast_path_materializes_legacy_combined_generation(config, monkeypatch):
    from tests.conftest import make_chart
    import csv_to_clonehero.combined_pipeline as pipeline

    chart = make_chart("Song", "Artist", chart_id=22)
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: FakeCatalog(chart))
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
        workspace = db.get_workspace("combined:partial")
        current = db.get_workspace_candidates(workspace["id"], kind="exact", current_only=True)
        db.select_workspace_candidate(
            workspace["id"], current[0]["track_id"], current[0]["candidate_id"],
            require_workspace_candidate=True,
        )
        catalog.chart = second_chart
        second = discover_combined(config, db, [LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "one", "Song", "Artist"), ProviderTrack("soundcloud", "two", "New", "Artist")])], workspace_key="combined:partial", catalog=catalog)
        assert calls == 2
        assert second.cached_tracks == 1
        assert second.recomputed_tracks == 1
        assert second.candidate_charts_evaluated == 2
        assert second.unique_charts_evaluated == 2
        assert second.exact_hits == 2
        assert db.list_workspace_selections(workspace["id"], kind="exact") == []
    finally:
        db.close()


def test_combined_discovery_binds_legacy_spotify_key_by_database_id(config):
    from csv_to_clonehero.server.web import _review_cards, _review_summary
    from tests.conftest import make_chart, make_track

    spotify_id = "legacy-track-id"
    legacy_key = f"spotify:{spotify_id}"
    neutral_key = "artist|song"
    old_chart = make_chart("Song", "Artist", chart_id=170)
    primary = make_chart("Song", "Artist", chart_id=171)
    variant = make_chart("Song", "Artist", chart_id=172)

    class Catalog(FakeCatalog):
        chart_count = 2

        def candidates(self, artist, title):
            return (primary, variant) if (artist, title) == ("Artist", "Song") else ()

    db = Database(config.database_path)
    try:
        legacy = db.upsert_spotify_track(make_track("Song", "Artist", spotify_id))
        assert legacy.track_key == legacy_key
        workspace = db.create_workspace(
            workspace_key="combined:legacy-binding", name="Legacy binding", kind="exact"
        )
        db.replace_workspace_library_tracks(workspace, [legacy])
        db.upsert_workspace_candidate(
            workspace, legacy.db_id, old_chart, score=95,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace, legacy.db_id, old_chart, kind="exact")
        db._conn.execute(
            "UPDATE workspace_candidates SET track_id=NULL "
            "WHERE workspace_id=? AND candidate_id=?",
            (workspace, old_chart.db_id),
        )
        db._conn.commit()

        normalized = provider_track_to_track(
            ProviderTrack("spotify", spotify_id, "Song", "Artist")
        )
        assert normalized.track_key == neutral_key
        resolved = db.upsert_spotify_track(normalized)
        assert resolved.db_id == normalized.db_id == legacy.db_id

        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("spotify", [
                ProviderTrack("spotify", spotify_id, "Song", "Artist")
            ])],
            workspace_key="combined:legacy-binding",
            catalog=Catalog(primary),
        )

        stored = db.get_spotify_tracks(True)
        assert len(stored) == 1
        assert stored[0].db_id == legacy.db_id
        assert stored[0].track_key == legacy_key
        current = db.get_workspace_candidates(
            workspace, kind="exact", include_rejected=True, current_only=True
        )
        assert {row["chart_id"] for row in current} == {171, 172}
        assert {row["track_id"] for row in current} == {legacy.db_id}
        assert db._conn.execute(
            "SELECT COUNT(*) FROM workspace_candidates "
            "WHERE workspace_id=? AND kind='exact' AND track_id IS NULL",
            (workspace,),
        ).fetchone()[0] == 0
        assert db.list_workspace_selections(workspace, kind="exact") == []
        progress = db.get_discovery_song_progress(workspace)
        assert [row["track_key"] for row in progress] == [neutral_key]

        first_candidate = current[0]
        db.select_workspace_candidate(
            workspace,
            legacy.db_id,
            first_candidate["candidate_id"],
            kind="exact",
            require_workspace_candidate=True,
        )
        cached = discover_combined(
            config,
            db,
            [LibrarySnapshot("spotify", [
                ProviderTrack("spotify", spotify_id, "Song", "Artist")
            ])],
            workspace_key="combined:legacy-binding",
            catalog=Catalog(primary),
        )
        assert cached.cached_tracks == 1
        assert cached.recomputed_tracks == 0
        assert db.list_workspace_selections(workspace, kind="exact") == []
        current = db.get_workspace_candidates(
            workspace, kind="exact", include_rejected=True, current_only=True
        )
        assert {row["track_id"] for row in current} == {legacy.db_id}

        cards = _review_cards(config, db, workspace)
        summary = _review_summary(cards)
        assert len(cards) == 1
        assert cards[0]["track_key"] == legacy_key
        assert {item["chart_id"] for item in cards[0]["candidates"]} == {171, 172}
        assert summary["recommended_songs"] == result.result_tier_histogram["RECOMMENDED_EXACT"] == 1
        assert summary["selectable_charts"] == result.selectable_chart_artifacts == 2
        assert result.songs_with_selectable_charts == 1

        for row in current:
            db.select_workspace_candidate(
                workspace,
                legacy.db_id,
                row["candidate_id"],
                kind="exact",
                require_workspace_candidate=True,
            )
        assert len(db.list_workspace_selections(workspace, kind="exact")) == 2
    finally:
        db.close()


def test_combined_discovery_fails_when_upsert_does_not_resolve_track_id(
    config, monkeypatch
):
    db = Database(config.database_path)
    try:
        monkeypatch.setattr(db, "upsert_spotify_tracks", lambda tracks: list(tracks))
        with pytest.raises(RuntimeError, match="could not resolve 1 database track IDs"):
            discover_combined(
                config,
                db,
                [LibrarySnapshot("soundcloud", [
                    ProviderTrack("soundcloud", "missing", "Song", "Artist")
                ])],
                workspace_key="combined:unresolved",
                catalog=FakeCatalog(None),
            )
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
    catalog.snapshot_id = "fixture"
    catalog.sha256 = "different-body"
    assert matching_cache_fingerprint(track, catalog, config) != baseline
    config.search.catalog_artist_fuzzy_limit += 1
    assert matching_cache_fingerprint(track, catalog, config) != baseline
    config.instruments.require_five_fret_guitar = not config.instruments.require_five_fret_guitar
    assert matching_cache_fingerprint(track, catalog, config) != baseline


def test_matching_semantic_v9_cache_keys_cannot_satisfy_v10(config, monkeypatch):
    from tests.conftest import make_chart, make_track
    import csv_to_clonehero.combined_pipeline as pipeline

    track = make_track("The Only Exception", "Paramore")
    catalog = FakeCatalog(make_chart("The Only Exception", "Paramore", chart_id=26))
    manifest = {"snapshot_id": "fixture", "sha256": "fixture-sha", "chart_count": 1}

    assert pipeline.MATCH_CACHE_VERSION == "combined-match-v10"
    monkeypatch.setattr(pipeline, "MATCH_CACHE_VERSION", "combined-match-v9")
    stale_song = pipeline.matching_cache_fingerprint(track, catalog, config)
    stale_scan = pipeline.matching_scan_fingerprint([track], manifest, config)
    monkeypatch.setattr(pipeline, "MATCH_CACHE_VERSION", "combined-match-v10")

    assert pipeline.matching_cache_fingerprint(track, catalog, config) != stale_song
    assert pipeline.matching_scan_fingerprint([track], manifest, config) != stale_scan


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


def test_catalog_expansion_enforces_gates_song_caps_and_import_exclusion(config):
    from tests.conftest import make_chart

    imported = make_chart("Known", "Artist", chart_id=100)
    first = make_chart("First", "Artist", chart_id=101)
    first_alt = make_chart("First", "Artist", chart_id=102, charter="Other")
    second = make_chart("Second", "Artist", chart_id=103)
    beyond_cap = make_chart("Third", "Artist", chart_id=104)
    gated = make_chart("Broken", "Artist", chart_id=105, instruments=[], diff_guitar=None)

    class ExpansionCatalog(FakeCatalog):
        chart_count = 6

        def artist_songs(self, artist):
            if artist != "artist":
                return ()
            return (
                ("known", (imported,)),
                ("first", (first, first_alt)),
                ("second", (second,)),
                ("third", (beyond_cap,)),
                ("broken", (gated,)),
            )

    config.expansion.cap_one_song = 2
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="combined:expansion")
        db.upsert_library_snapshot(
            workspace,
            LibrarySnapshot("soundcloud", [
                ProviderTrack("soundcloud", "known", "Known", "Artist")
            ]),
        )
        catalog = ExpansionCatalog(first)

        result = expand_combined(
            config, db, workspace, ["artist"], catalog=catalog, exact_generation_id=9
        )

        rows = db.get_workspace_candidates(workspace, kind="expansion")
        charts = [db.get_candidate(int(row["candidate_id"])) for row in rows]
        identities = {(chart.artist_norm, chart.name_norm) for chart in charts}
        assert identities == {("artist", "first"), ("artist", "second")}
        assert len(rows) == 3  # both chart versions for the first capped song
        assert all(row["eligibility"] in {"RECOMMENDED", "ELIGIBLE_REVIEW"} for row in rows)
        assert result.candidates == 3
        assert result.expansion_artists[0]["eligible_songs"] == 3
        assert result.expansion_artists[0]["cap"] == 2
        assert all(json.loads(row["metadata_json"])["exact_generation_id"] == 9 for row in rows)
    finally:
        db.close()


def test_global_song_cache_excludes_workspace_playlist_provenance(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=140)
    snapshot = [LibrarySnapshot("spotify", [ProviderTrack(
        "spotify", "spotify-track", "Song", "Artist",
        playlist_ids=["private-playlist-id"],
        playlist_names=["Private playlist name"],
    )])]
    db = Database(config.database_path)
    try:
        discover_combined(
            config, db, snapshot, workspace_key="combined:cache-privacy",
            catalog=FakeCatalog(chart),
        )
        cached = db._conn.execute("SELECT result_json FROM matching_song_cache").fetchone()[0]
        assert "private-playlist-id" not in cached
        assert "Private playlist name" not in cached
    finally:
        db.close()


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


def test_rejection_reason_and_persisted_exemplar_use_same_candidate(config):
    from tests.conftest import make_chart

    same_artist_wrong_title = make_chart(
        "Good Ass Intro", "Chance the Rapper", chart_id=201
    )
    unrelated_exact_title = make_chart(
        "Chain Smoker", "Unrelated Artist", chart_id=202
    )

    class RejectionCatalog(FakeCatalog):
        chart_count = 2

        def candidates(self, artist, title):
            return (same_artist_wrong_title, unrelated_exact_title)

    db = Database(config.database_path)
    try:
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [ProviderTrack(
                "soundcloud", "chain", "Chain Smoker", "Chance The Rapper"
            )])],
            workspace_key="combined:rejection-exemplar",
            catalog=RejectionCatalog(same_artist_wrong_title),
        )
        rows = db.get_workspace_candidates(
            "combined:rejection-exemplar",
            kind="exact",
            include_rejected=True,
            current_only=True,
        )
        assert [row["chart_id"] for row in rows] == [201]
        assert result.rejection_song_reasons == {"title identity/version conflict": 1}
        explanation = json.loads(rows[0]["breakdown_json"])
        assert "title conflict" in explanation["explanation"]
        progress = db.get_discovery_song_progress_summary("combined:rejection-exemplar")
        assert progress["not_playable"] == 1
    finally:
        db.close()


def test_real_artist_conflict_reason_remains_artist_conflict(config):
    from tests.conftest import make_chart

    chart = make_chart("Chain Smoker", "Unrelated Artist", chart_id=203)

    class ArtistConflictCatalog(FakeCatalog):
        def candidates(self, artist, title):
            return (chart,)

    db = Database(config.database_path)
    try:
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [ProviderTrack(
                "soundcloud", "chain", "Chain Smoker", "Chance The Rapper"
            )])],
            workspace_key="combined:artist-conflict",
            catalog=ArtistConflictCatalog(chart),
        )
        assert result.rejection_song_reasons == {"artist identity conflict": 1}
    finally:
        db.close()


def test_rejection_exemplar_reasons_cover_playability_version_and_threshold(config):
    from csv_to_clonehero.matching import SpotifyScoreBreakdown, score_candidate
    from tests.conftest import make_chart, make_track

    no_five = make_chart("Song", "Artist", chart_id=210, instruments=["drums"])
    no_five.diff_guitar = -1
    no_five_score = score_candidate(make_track("Song", "Artist"), no_five, config)
    assert _primary_rejection_reason(
        "not_playable", (no_five, no_five_score)
    ) == "no required 5-fret guitar"

    version_chart = make_chart("Song (Live)", "Artist", chart_id=211)
    version_score = SpotifyScoreBreakdown(
        final_score=68,
        penalties=[("version mismatch (chart is live)", 25)],
        gate_factors={"hard_gate": False},
        eligibility="EXCLUDED",
        eligibility_reasons=["score below export eligibility threshold"],
        match_factors={
            "version_markers": {
                "track": [], "candidate": ["live"], "matched": False
            }
        },
    )
    below_chart = make_chart("Song", "Artist Band", chart_id=212)
    below_score = SpotifyScoreBreakdown(
        final_score=67,
        gate_factors={"hard_gate": False},
        eligibility="EXCLUDED",
        eligibility_reasons=["score below export eligibility threshold"],
    )
    exemplar = _rejection_exemplar([
        (below_chart, below_score),
        (version_chart, version_score),
        (no_five, no_five_score),
    ])
    assert exemplar == (version_chart, version_score)
    assert _primary_rejection_reason("no_suitable", exemplar) == "cover/live/remix/version conflict"
    assert _primary_rejection_reason(
        "no_suitable", (below_chart, below_score)
    ) == "below safe score threshold"


def test_rejection_exemplar_ties_are_stable_by_source_and_chart_id():
    from csv_to_clonehero.matching import SpotifyScoreBreakdown
    from tests.conftest import make_chart

    first = make_chart("Song", "Artist", chart_id=5)
    first.source = "alpha"
    second = make_chart("Song", "Artist", chart_id=2)
    second.source = "beta"
    score = SpotifyScoreBreakdown(
        final_score=0,
        gate_factors={"hard_gate": True, "hard_gate_reasons": ["hard gate"]},
    )
    assert _rejection_exemplar([(second, score), (first, score)])[0] is first


def test_workspace_snapshots_restore_playlist_scope_without_duplicate_songs(config):
    from csv_to_clonehero.providers.base import SourceImportReport

    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="combined:scope-roundtrip")
        db.upsert_library_snapshot(
            workspace,
            LibrarySnapshot(
                "spotify",
                [ProviderTrack(
                    "spotify", "sp-1", "Song", "Artist",
                    playlist_ids=["one", "two"],
                    playlist_names=["One", "Two"],
                    liked=True,
                    duration_ms=123000,
                )],
                [
                    ProviderPlaylist("spotify", "one", "One"),
                    ProviderPlaylist("spotify", "two", "Two"),
                    ProviderPlaylist("spotify", "empty", "Empty"),
                ],
                metadata={"import": "api"},
                import_report=SourceImportReport(
                    total_rows=4,
                    accepted_rows=3,
                    duplicate_rows=2,
                    unique_tracks=1,
                    liked_count=1,
                ),
            ),
            scope={"playlist_ids": ["one", "two", "empty"], "include_liked": True},
        )
        snapshots = workspace_snapshots(db, workspace)
        assert len(snapshots) == 1
        assert len(snapshots[0].tracks) == 1
        assert {item.provider_id for item in snapshots[0].playlists} == {"one", "two", "empty"}
        assert snapshots[0].tracks[0].playlist_ids == ["one", "two"]
        assert snapshots[0].tracks[0].duration_ms == 123000
        assert snapshots[0].metadata["include_liked"] is True
        assert snapshots[0].import_report.total_rows == 4
    finally:
        db.close()


def test_workspace_snapshot_duration_keeps_match_evidence_identical(config):
    from csv_to_clonehero.matching import score_candidate
    from csv_to_clonehero.providers.base import provider_track_to_track
    from tests.conftest import make_chart

    provider_track = ProviderTrack(
        "spotify", "sp-duration", "Song", "Artist", duration_ms=180000
    )
    chart = make_chart("Song", "Artist", chart_id=219)
    chart.song_length_ms = 181250
    expected = score_candidate(provider_track_to_track(provider_track), chart, config)
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="combined:duration-roundtrip")
        db.upsert_library_snapshot(
            workspace,
            LibrarySnapshot("spotify", [provider_track], metadata={"import": "api"}),
        )
        restored = workspace_snapshots(db, workspace)[0].tracks[0]
        actual = score_candidate(provider_track_to_track(restored), chart, config)
        assert restored.duration_ms == 180000
        assert actual.match_factors["duration"] == expected.match_factors["duration"]
        assert actual.result_tier == expected.result_tier
    finally:
        db.close()


def test_workspace_snapshot_without_duration_remains_valid(config):
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="combined:no-duration")
        db.upsert_library_snapshot(
            workspace,
            LibrarySnapshot("soundcloud", [ProviderTrack(
                "soundcloud", "sc-none", "Song", "Artist"
            )], metadata={"import": "export"}),
        )
        assert workspace_snapshots(db, workspace)[0].tracks[0].duration_ms is None
    finally:
        db.close()


def test_combined_discovery_runs_bounded_fuzzy_stage_only_after_strong_stages_reject(config):
    from tests.conftest import make_chart

    wrong = make_chart("Completely Else", "Artist", chart_id=220)
    recovered = make_chart("Wanted Song", "Artist Alias", chart_id=221)
    calls = []

    class StagedCatalog(FakeCatalog):
        chart_count = 2

        def high_recall_candidates(self, artist, title, **kwargs):
            include_fuzzy = kwargs.get("include_fuzzy_fallback")
            calls.append(include_fuzzy)
            candidates = (wrong, recovered) if include_fuzzy else (wrong,)
            return CatalogLookup(
                candidates=candidates,
                strict_exact=False,
                methods=("artist", "artist_fuzzy") if include_fuzzy else ("artist",),
                retrieved_song_keys=len(candidates),
                exact_artist=True,
                exact_title=False,
                candidate_methods={
                    (chart.source, chart.chart_id): (
                        "artist_fuzzy" if chart is recovered else "artist",
                    )
                    for chart in candidates
                },
                fuzzy_fallback_attempted=bool(include_fuzzy),
            )

    db = Database(config.database_path)
    try:
        result = discover_combined(
            config,
            db,
            [LibrarySnapshot("soundcloud", [ProviderTrack(
                "soundcloud", "wanted", "Wanted Song", "Artist Alias"
            )])],
            workspace_key="combined:staged-fuzzy",
            catalog=StagedCatalog(wrong),
        )
        assert calls == [False, True]
        assert result.strong_match_tracks == 1
        assert result.candidate_charts_evaluated == 2
        assert result.broader_retrieval_tracks == 1
    finally:
        db.close()
