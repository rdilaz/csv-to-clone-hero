from __future__ import annotations

import sqlite3

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
        history = db.get_chart_artifact_validation_history("enchor", 10)
        old_rejection = next(row for row in history if row["state"] == "REJECTED")
        assert old_rejection["artifact_ref"] == "old-ref"
        assert old_rejection["reason"] == "no playable notes"
        assert db.get_chart_artifact_validation("enchor", 10) is not None
        assert db.get_blocked_chart_identities() == set()
        history_count = len(history)
        db.observe_chart_artifact("enchor", 10, "new-ref")
        assert len(db.get_chart_artifact_validation_history("enchor", 10)) == history_count
    finally:
        db.close()


def test_validation_ledger_preserves_verified_evidence_on_artifact_change(config):
    db = Database(config.database_path)
    try:
        db.record_chart_artifact_validation(
            "enchor", 12, artifact_ref="verified-ref", state="VERIFIED",
            validator_version="validator-v1", artifact_sha256="abc123",
            size_bytes=1234, format="sngpkg", playable_note_count=99,
            validation={"valid": True},
        )
        db.record_chart_artifact_validation(
            "enchor", 12, artifact_ref="verified-ref", state="VERIFIED",
            validator_version="validator-v1", artifact_sha256="abc123",
            size_bytes=1234, format="sngpkg", playable_note_count=99,
            validation={"valid": True},
        )
        assert db.get_chart_artifact_validation_history("enchor", 12) == []
        current = db.observe_chart_artifact("enchor", 12, "replacement-ref")
        assert current["state"] == "UNVERIFIED"
        assert current["artifact_ref"] == "replacement-ref"
        history = db.get_chart_artifact_validation_history("enchor", 12)
        assert len(history) == 1
        assert history[0]["state"] == "VERIFIED"
        assert history[0]["artifact_ref"] == "verified-ref"
        assert history[0]["validator_version"] == "validator-v1"
        assert history[0]["artifact_sha256"] == "abc123"
        assert history[0]["size_bytes"] == 1234
        assert history[0]["format"] == "sngpkg"
        assert history[0]["playable_note_count"] == 99
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


def test_direct_snapshot_reassignment_preserves_prior_observation(config):
    db = Database(config.database_path)
    try:
        db.record_chart_artifact_validation(
            "enchor", 14, artifact_ref="same-ref", state="VERIFIED",
            artifact_sha256="same-bytes", catalog_snapshot_id="snapshot-one",
        )
        current = db.observe_chart_artifact(
            "enchor", 14, "same-ref", catalog_snapshot_id="snapshot-two"
        )
        assert current["catalog_snapshot_id"] == "snapshot-two"
        history = db.get_chart_artifact_validation_history("enchor", 14)
        assert len(history) == 1
        assert history[0]["catalog_snapshot_id"] == "snapshot-one"
        assert history[0]["state"] == "VERIFIED"
    finally:
        db.close()


def test_direct_snapshot_observations_keep_cached_counts_consistent(config):
    from tests.conftest import make_chart

    first = make_chart("First", "Artist", chart_id=15)
    second = make_chart("Second", "Artist", chart_id=16)
    db = Database(config.database_path)
    try:
        db.sync_catalog_artifacts("snapshot-counts", [first])
        db.observe_chart_artifact(
            second.source, second.chart_id, second.md5 or "",
            catalog_snapshot_id="snapshot-counts",
        )
        assert db.get_chart_artifact_validation_counts(
            catalog_snapshot_id="snapshot-counts"
        )["UNVERIFIED"] == 2

        db.record_chart_artifact_validation(
            first.source, first.chart_id, artifact_ref=first.md5 or "",
            state="REJECTED", catalog_snapshot_id="snapshot-counts",
        )
        db.observe_chart_artifact(
            first.source, first.chart_id, "changed-ref",
            catalog_snapshot_id="snapshot-counts",
        )
        counts = db.get_chart_artifact_validation_counts(
            catalog_snapshot_id="snapshot-counts"
        )
        assert counts["UNVERIFIED"] == 2
        assert counts["REJECTED"] == 0
    finally:
        db.close()


def test_catalog_snapshot_change_revisions_validation_attribution(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=13)
    db = Database(config.database_path)
    try:
        db.sync_catalog_artifacts("snapshot-one", [chart])
        db.record_chart_artifact_validation(
            chart.source, chart.chart_id, artifact_ref=chart.md5 or "",
            state="VERIFIED", artifact_sha256="verified-bytes",
        )
        db.upsert_candidate(chart)
        db.sync_catalog_artifacts("snapshot-two", [chart])

        current = db.get_chart_artifact_validation(chart.source, chart.chart_id)
        assert current["catalog_snapshot_id"] == "snapshot-two"
        assert current["state"] == "VERIFIED"
        history = db.get_chart_artifact_validation_history(chart.source, chart.chart_id)
        prior = next(row for row in history if row["state"] == "VERIFIED")
        assert prior["catalog_snapshot_id"] == "snapshot-one"
        assert prior["artifact_sha256"] == "verified-bytes"

        changed_metadata = make_chart("Song", "Artist", chart_id=13, charter="Replacement")
        changed_metadata.md5 = chart.md5
        db.sync_catalog_artifacts("snapshot-three", [changed_metadata])
        current = db.get_chart_artifact_validation(chart.source, chart.chart_id)
        assert current["catalog_snapshot_id"] == "snapshot-three"
        assert current["state"] == "UNVERIFIED"
        history = db.get_chart_artifact_validation_history(chart.source, chart.chart_id)
        assert any(
            row["catalog_snapshot_id"] == "snapshot-two" and row["state"] == "VERIFIED"
            for row in history
        )
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
    write_snapshot(path, [
        candidate(1, "Artist", "First"),
        candidate(2, "Artist", "Second"),
        candidate(3, "Artist", "Third"),
    ])
    config.paths.catalog = str(path)

    class Source:
        name = "enchor"

        def __init__(self):
            self.downloaded = []

        def download_chart(self, chart, destination):
            self.downloaded.append(chart.chart_id)
            destination.write_bytes(b"not an sng")
            return destination

    source = Source()
    db = Database(config.database_path)
    first = validate_catalog_artifacts(config, db, [source], limit=1)
    assert first["attempted"] == 1
    assert first["synchronization"] == {
        "synchronized": True, "inserted": 3, "changed": 0,
    }
    assert first["states"]["REJECTED"] == 1
    assert first["states"]["UNVERIFIED"] == 2
    assert source.downloaded == [1]
    untouched = db.get_chart_artifact_validation("enchor", 3)["updated_at"]
    db.close()

    resumed = Database(config.database_path)
    try:
        second = validate_catalog_artifacts(config, resumed, [source], limit=1)
        assert second["attempted"] == 1
        assert second["synchronization"] == {
            "synchronized": False, "inserted": 0, "changed": 0,
        }
        assert second["states"]["REJECTED"] == 2
        assert second["states"]["UNVERIFIED"] == 1
        assert source.downloaded == [1, 2]
        assert resumed.get_chart_artifact_validation("enchor", 3)["updated_at"] == untouched
    finally:
        resumed.close()


def test_transient_unverified_row_does_not_starve_next_batch(config, tmp_path):
    from csv_to_clonehero.sources.base import SourceError
    from tests.test_catalog import candidate, write_snapshot

    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First"), candidate(2, "Artist", "Second")])
    config.paths.catalog = str(path)

    class Source:
        name = "enchor"

        def __init__(self):
            self.downloaded = []

        def download_chart(self, chart, destination):
            self.downloaded.append(chart.chart_id)
            if chart.chart_id == 1:
                raise SourceError("temporary timeout")
            destination.write_bytes(b"not an sng")
            return destination

    source = Source()
    db = Database(config.database_path)
    try:
        first = validate_catalog_artifacts(config, db, [source], limit=1)
        second = validate_catalog_artifacts(config, db, [source], limit=1)
        assert first["attempted"] == second["attempted"] == 1
        assert source.downloaded == [1, 2]
        assert db.get_chart_artifact_validation("enchor", 1)["state"] == "UNVERIFIED"
        assert db.get_chart_artifact_validation("enchor", 2)["state"] == "REJECTED"
    finally:
        db.close()


def test_bounded_batch_query_uses_covering_order_index(config):
    from tests.conftest import make_chart

    db = Database(config.database_path)
    try:
        db.sync_catalog_artifacts(
            "snapshot-query-plan",
            [make_chart(f"Song {index}", "Artist", chart_id=index) for index in range(1, 20)],
        )
        plan = list(db._conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT * FROM chart_artifact_validations
               WHERE state=? AND source=? AND catalog_snapshot_id=?
               ORDER BY checked_at,chart_id LIMIT ?""",
            ("UNVERIFIED", "test", "snapshot-query-plan", 1),
        ))
        details = " ".join(str(row["detail"]) for row in plan)
        assert "idx_chart_artifact_validations_batch" in details
        assert "TEMP B-TREE" not in details
    finally:
        db.close()


def test_multi_state_batch_selection_rotates_by_oldest_attempt(config):
    from tests.conftest import make_chart

    first = make_chart("First", "Artist", chart_id=21)
    second = make_chart("Second", "Artist", chart_id=22)
    db = Database(config.database_path)
    try:
        db.sync_catalog_artifacts("snapshot-multi-state", [first, second])
        db.record_chart_artifact_validation(
            first.source, first.chart_id, artifact_ref=first.md5 or "", state="REJECTED"
        )
        db.record_chart_artifact_validation(
            second.source, second.chart_id, artifact_ref=second.md5 or "", state="UNAVAILABLE"
        )
        with db._conn:
            db._conn.execute(
                "UPDATE chart_artifact_validations SET checked_at=1 WHERE chart_id=?",
                (first.chart_id,),
            )
            db._conn.execute(
                "UPDATE chart_artifact_validations SET checked_at=2 WHERE chart_id=?",
                (second.chart_id,),
            )
        selected = db.list_chart_artifact_validations(
            states=("REJECTED", "UNAVAILABLE"),
            source=first.source,
            catalog_snapshot_id="snapshot-multi-state",
            limit=1,
        )
        assert [row["chart_id"] for row in selected] == [first.chart_id]
        with db._conn:
            db._conn.execute(
                "UPDATE chart_artifact_validations SET checked_at=3 WHERE chart_id=?",
                (first.chart_id,),
            )
        selected = db.list_chart_artifact_validations(
            states=("REJECTED", "UNAVAILABLE"),
            source=first.source,
            catalog_snapshot_id="snapshot-multi-state",
            limit=1,
        )
        assert [row["chart_id"] for row in selected] == [second.chart_id]
    finally:
        db.close()


def test_schema_18_database_adds_validation_history(config):
    path = config.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_version(version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES(18);
        CREATE TABLE chart_artifact_validations (
            source TEXT NOT NULL,
            chart_id INTEGER NOT NULL,
            artifact_ref TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'UNVERIFIED',
            validator_version TEXT NOT NULL DEFAULT '',
            artifact_sha256 TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            format TEXT NOT NULL DEFAULT '',
            playable_note_count INTEGER NOT NULL DEFAULT 0,
            validation_json TEXT NOT NULL DEFAULT '{}',
            reason_code TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            http_status INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            checked_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(source, chart_id)
        );
    """)
    connection.close()

    migrated = Database(path)
    try:
        assert migrated._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chart_artifact_validation_history'"
        ).fetchone() is not None
        columns = {
            row["name"]
            for row in migrated._conn.execute("PRAGMA table_info(chart_artifact_validations)")
        }
        assert "catalog_snapshot_id" in columns
        assert "catalog_row_hash" in columns
        assert migrated.get_blocked_chart_identities() == set()
    finally:
        migrated.close()
