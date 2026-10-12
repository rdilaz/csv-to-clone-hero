"""Large ZIP selections use one candidate membership read per cohort."""

import json

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.server.downloads import DownloadError, _validated_selections
from tests.conftest import make_chart, make_track


@pytest.fixture
def selected_library(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(workspace_key="batch-download")
    tracks = db.upsert_spotify_tracks([
        make_track(f"Known {index}", "Artist", f"known-{index}") for index in range(3)
    ])
    db.replace_workspace_library_tracks(workspace, tracks)
    generation = db.create_discovery_scan(
        workspace, scan_key="batch-current", kind="exact", status="complete"
    )
    task = db.upsert_discovery_task(
        workspace, scan_id=generation, source="test", query_key="known", status="complete"
    )
    exact = [make_chart(track.title, "Artist", chart_id=100 + index) for index, track in enumerate(tracks)]
    expansion = [make_chart(f"More {index}", "Artist", chart_id=200 + index) for index in range(40)]
    for track, chart in zip(tracks, exact):
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=99, eligibility="RECOMMENDED", task_id=task
        )
        db.select_workspace_candidate(workspace, track.track_key, chart)
    for chart in expansion:
        db.upsert_workspace_candidate(
            workspace, None, chart, kind="expansion", artist_norm="artist",
            score=90, eligibility="ELIGIBLE_REVIEW", metadata={"exact_generation_id": generation}
        )
        db.select_workspace_candidate(workspace, None, chart, kind="expansion", artist_norm="artist")
    try:
        yield db, workspace, generation, tracks, exact, expansion
    finally:
        db.close()


@pytest.mark.parametrize("expansion_count", [3, 40])
def test_membership_queries_are_constant_and_selection_order_is_preserved(
    config, selected_library, monkeypatch, expansion_count
):
    db, workspace, generation, _tracks, exact, expansion = selected_library
    with db._conn:
        db._conn.execute(
            "DELETE FROM workspace_selections WHERE workspace_id=? AND kind='expansion' AND candidate_id>?",
            (workspace, expansion[expansion_count - 1].db_id),
        )
    before = [dict(row) for row in db.list_workspace_selections(workspace)]
    candidate_reads = []
    original = db.get_workspace_candidates

    def observe(*args, **kwargs):
        candidate_reads.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "get_workspace_candidates", observe)
    validated = _validated_selections(db, workspace, config=config)

    assert [row["candidate_id"] for row in validated] == [
        chart.db_id for chart in exact + expansion[:expansion_count]
    ]
    assert candidate_reads == [
        {"kind": "expansion", "include_rejected": True, "include_candidate_data": False},
        {"kind": "exact", "include_rejected": True, "include_candidate_data": False,
         "generation_id": generation},
    ]
    assert [dict(row) for row in db.list_workspace_selections(workspace)] == before


@pytest.mark.parametrize("gate", [
    "wrong_track_membership", "wrong_artist_membership", "missing_expansion_membership",
    "stale_expansion_generation", "stale_exact_generation", "outside_current_library",
    "imported_song", "wrong_exact_identity", "wrong_expansion_artist", "unplayable",
    "rejected_artifact", "unavailable_artifact", "disabled_expansion",
])
def test_batched_membership_keeps_current_generation_and_eligibility_gates(
    config, selected_library, gate
):
    db, workspace, generation, tracks, exact, expansion = selected_library
    blocked = expansion[0]
    if gate in {"wrong_track_membership", "stale_exact_generation", "outside_current_library",
                "wrong_exact_identity"}:
        blocked = exact[0]
    with db._conn:
        if gate == "wrong_track_membership":
            db._conn.execute("UPDATE workspace_selections SET track_id=? WHERE candidate_id=?",
                             (tracks[1].db_id, blocked.db_id))
        elif gate == "wrong_artist_membership":
            db._conn.execute("UPDATE workspace_selections SET artist_norm='other' WHERE candidate_id=?",
                             (blocked.db_id,))
        elif gate == "missing_expansion_membership":
            db._conn.execute("DELETE FROM workspace_candidates WHERE candidate_id=?", (blocked.db_id,))
        elif gate == "stale_expansion_generation":
            db._conn.execute("UPDATE workspace_candidates SET metadata_json=? WHERE candidate_id=?",
                             (json.dumps({"exact_generation_id": generation + 1}), blocked.db_id))
        elif gate == "stale_exact_generation":
            db._conn.execute("UPDATE workspace_candidates SET task_id=NULL WHERE candidate_id=?",
                             (blocked.db_id,))
    if gate == "outside_current_library":
        db.replace_workspace_library_tracks(workspace, tracks[1:])
    elif gate == "imported_song":
        blocked.name = tracks[0].title
        blocked.name_norm = tracks[0].title_norm
        db.upsert_candidate(blocked)
    elif gate in {"wrong_exact_identity", "wrong_expansion_artist"}:
        blocked.artist = "Completely Unrelated"
        blocked.artist_norm = "completely unrelated"
        db.upsert_candidate(blocked)
    elif gate == "unplayable":
        blocked.instruments = []
        blocked.diff_guitar = None
        blocked.difficulty_fields = {}
        db.upsert_candidate(blocked)
    elif gate in {"rejected_artifact", "unavailable_artifact"}:
        db.record_chart_artifact_validation(
            blocked.source, blocked.chart_id, artifact_ref=blocked.md5,
            state="REJECTED" if gate == "rejected_artifact" else "UNAVAILABLE"
        )
    elif gate == "disabled_expansion":
        config.server.enable_expansion = False
    before = [dict(row) for row in db.list_workspace_selections(workspace)]

    validated = _validated_selections(db, workspace, config=config)

    expected = exact if gate == "disabled_expansion" else [
        chart for chart in exact + expansion if chart.db_id != blocked.db_id
    ]
    assert [row["candidate_id"] for row in validated] == [chart.db_id for chart in expected]
    assert [dict(row) for row in db.list_workspace_selections(workspace)] == before


def test_incomplete_latest_generation_blocks_exact_and_expansion(config, selected_library):
    db, workspace, _generation, _tracks, _exact, _expansion = selected_library
    db.create_discovery_scan(workspace, scan_key="new-pending", kind="exact", status="pending")
    before = [dict(row) for row in db.list_workspace_selections(workspace)]

    with pytest.raises(DownloadError, match="No downloadable charts"):
        _validated_selections(db, workspace, config=config)

    assert [dict(row) for row in db.list_workspace_selections(workspace)] == before


def test_possible_exact_match_still_requires_selection_confirmation(config, selected_library, monkeypatch):
    import csv_to_clonehero.matching as matching

    db, workspace, _generation, _tracks, exact, expansion = selected_library
    classify = matching.classify_result_tier

    def possible_exact(breakdown, *, kind="exact"):
        return (matching.POSSIBLE_MATCH, []) if kind == "exact" else classify(breakdown, kind=kind)

    monkeypatch.setattr(matching, "classify_result_tier", possible_exact)
    with db._conn:
        db._conn.execute("UPDATE workspace_selections SET metadata_json=? WHERE candidate_id=?",
                         (json.dumps({"possible_confirmed": True}), exact[0].db_id))

    validated = _validated_selections(db, workspace, config=config)

    assert [row["candidate_id"] for row in validated] == [
        chart.db_id for chart in exact[:1] + expansion
    ]


def test_expansion_all_coverage_still_retains_compatible_review_choices(config, selected_library):
    db, workspace, generation, _tracks, exact, expansion = selected_library
    for index, chart in enumerate(expansion[:2]):
        chart.instruments = ["guitar"]
        chart.diff_guitar = None
        chart.difficulty_fields = {}
        db.upsert_candidate(chart)
        with db._conn:
            db._conn.execute("UPDATE workspace_candidates SET metadata_json=? WHERE candidate_id=?", (
                json.dumps({"exact_generation_id": generation, "coverage_mode": "all" if index == 0 else "recommended"}),
                chart.db_id,
            ))

    validated = _validated_selections(db, workspace, config=config)

    assert [row["candidate_id"] for row in validated] == [
        chart.db_id for chart in exact + expansion if chart.db_id != expansion[1].db_id
    ]
    retained = next(row for row in validated if row["candidate_id"] == expansion[0].db_id)
    assert retained["eligibility"] == "ELIGIBLE_REVIEW"
