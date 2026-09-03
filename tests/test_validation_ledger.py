from __future__ import annotations

from csv_to_clonehero.catalog import EnchorCatalog
from csv_to_clonehero.catalog_validation import validate_catalog_artifacts
from csv_to_clonehero.database import Database


def test_validation_ledger_preserves_rejected_identity_and_resets_changed_artifact(config):
    db = Database(config.database_path)
    try:
        db.observe_chart_artifact("enchor", 10, "old-ref")
        rejected = db.record_chart_artifact_validation(
            "enchor", 10, artifact_ref="old-ref", state="REJECTED",
            reason_code="ARTIFACT_VALIDATION_FAILED", reason="no playable notes",
        )
        assert rejected["state"] == "REJECTED"
        assert db.get_blocked_chart_identities() == {("enchor", 10)}
        assert db.observe_chart_artifact("enchor", 10, "old-ref")["state"] == "REJECTED"
        changed = db.observe_chart_artifact("enchor", 10, "new-ref")
        assert changed["state"] == "UNVERIFIED"
        assert db.get_chart_artifact_validation("enchor", 10) is not None
        assert db.get_blocked_chart_identities() == set()
    finally:
        db.close()


def test_validation_ledger_blocks_selection_without_erasing_candidate(config):
    from tests.conftest import make_chart, make_track

    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="ledger:selection")
        track = make_track("Song", "Artist")
        db.replace_tracks([track], {"fixture.csv": 1})
        chart = db.upsert_candidate(make_chart("Song", "Artist", chart_id=11))
        db.upsert_workspace_candidate(workspace, track.track_key, chart, eligibility="RECOMMENDED")
        db.record_chart_artifact_validation(
            chart.source, chart.chart_id, artifact_ref=chart.md5 or "",
            state="UNAVAILABLE", reason="HTTP 404", http_status=404,
        )
        try:
            db.select_workspace_candidate(
                workspace, track.track_key, chart, require_workspace_candidate=True
            )
        except ValueError as exc:
            assert "rejected or unavailable" in str(exc)
        else:
            raise AssertionError("blocked artifact must not be selectable")
        assert db.get_candidate(chart.db_id) is not None
        assert db.get_chart_artifact_validation(chart.source, chart.chart_id)["reason"] == "HTTP 404"
    finally:
        db.close()


def test_catalog_index_excludes_known_blocked_identity(tmp_path):
    from tests.test_catalog import candidate, write_snapshot

    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "Good"), candidate(2, "Artist", "Bad")])
    catalog = EnchorCatalog(
        path,
        excluded_identities=frozenset({("enchor", 2)}),
        validation_fingerprint="ledger-v1",
    )
    assert catalog.available
    assert catalog.known_chart_count == 2
    assert catalog.chart_count == 1
    assert catalog.has_known_chart(2)
    assert not catalog.has_chart(2)
    assert [chart.chart_id for _, charts in catalog.artist_songs("Artist") for chart in charts] == [1]


def test_catalog_validation_batch_is_bounded_and_restart_safe(config, tmp_path):
    from tests.test_catalog import candidate, write_snapshot

    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First"), candidate(2, "Artist", "Second")])
    config.paths.catalog = str(path)

    class Source:
        name = "enchor"

        def download_chart(self, chart, destination):
            destination.write_bytes(b"not an sng")
            return destination

    db = Database(config.database_path)
    try:
        first = validate_catalog_artifacts(config, db, [Source()], limit=1)
        assert first["attempted"] == 1
        assert first["states"]["REJECTED"] == 1
        assert first["states"]["UNVERIFIED"] == 1
        second = validate_catalog_artifacts(config, db, [Source()], limit=1)
        assert second["attempted"] == 1
        assert second["states"]["REJECTED"] == 2
        assert second["states"]["UNVERIFIED"] == 0
    finally:
        db.close()


def test_schema_17_database_adds_validation_ledger(config):
    path = config.database_path
    db = Database(path)
    with db._conn:
        db._conn.execute("DROP TABLE chart_artifact_validations")
        db._conn.execute("UPDATE schema_version SET version=17")
    db.close()

    migrated = Database(path)
    try:
        assert migrated._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chart_artifact_validations'"
        ).fetchone() is not None
        assert migrated.get_blocked_chart_identities() == set()
    finally:
        migrated.close()
