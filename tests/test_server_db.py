"""Tests for multi-user schema migrations and workspace isolation helpers."""

from __future__ import annotations

from csv_to_clonehero.database import Database, SCHEMA_VERSION


def test_schema_version_bumped():
    assert SCHEMA_VERSION >= 5


def test_new_tables_exist(config):
    db = Database(config.database_path)
    try:
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in (
            "users",
            "user_sessions",
            "user_tokens",
            "user_emails",
            "background_jobs",
            "source_preflights",
        ):
            assert table in tables, table
    finally:
        db.close()


def test_workspace_key_is_stable_and_unique():
    from csv_to_clonehero.database import Database

    a = Database.workspace_key_for_user("ALICE", base="spotify")
    b = Database.workspace_key_for_user("alice", base="spotify")
    assert a == b
    assert a != Database.workspace_key_for_user("bob", base="spotify")
    assert "alice" in a
    assert not a.endswith("[:64]")


def test_legacy_workspace_key_suffix_is_migrated(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    workspace_id = db.create_workspace(workspace_key="spotify:u:legacy[:64]")
    with db._conn:
        db._conn.execute("UPDATE schema_version SET version = 10")
    db.close()

    migrated = Database(path)
    try:
        workspace = migrated.get_workspace(workspace_id)
        assert workspace["workspace_key"] == "spotify:u:legacy"
    finally:
        migrated.close()


def test_user_tokens_roundtrip_via_db(config):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="tok-user", scopes=["s"])
        db.set_user_token(int(user["id"]), "encrypted-blob", scope=["s"])
        row = db.get_user_token(int(user["id"]))
        assert row["encrypted_refresh_token"] == "encrypted-blob"
        db.clear_user_token(int(user["id"]))
        assert db.get_user_token(int(user["id"])) is None
    finally:
        db.close()


def test_background_job_lifecycle(config):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="job-owner")
        job = db.create_job(int(user["id"]), "discovery", workspace_key="spotify:u:job-owner")
        assert job["status"] == "queued"
        running = db.claim_job(job["id"])
        assert running is not None and running["status"] == "running"
        assert running["heartbeat_at"] is not None
        assert db.claim_job(job["id"]) is None  # only once
        db.update_job(job["id"], status="complete", progress={"done": 5}, result={"ok": True})
        row = db.get_job(job["id"])
        assert row["status"] == "complete"
        assert "5" in row["progress_json"]
        assert row["result_json"] == '{"ok": true}'
    finally:
        db.close()


def test_source_preflight_is_global_and_atomic_job_enqueue_is_idempotent(config):
    db = Database(config.database_path)
    try:
        first_user = db.upsert_user(spotify_user_id="first")
        db.upsert_user(spotify_user_id="second")
        row = db.record_source_preflight(
            {
                "source_name": "enchor",
                "endpoint": "/search",
                "request_version": "enchor-catalog-v1",
                "checked_at": 100.0,
                "success": True,
                "supported": True,
                "authoritative": True,
                "found": 50,
                "out_of": 50,
                "returned_count": 1,
                "page": 1,
                "metadata": {"query": ""},
            },
            ttl_seconds=60,
        )
        assert row["out_of"] == 50
        assert db.get_source_preflight("enchor")["metadata_json"] == '{"query": ""}'

        key = Database.workspace_key_for_user("first")
        first, created = db.create_or_get_active_job(
            int(first_user["id"]), "discovery", workspace_key=key
        )
        duplicate, duplicate_created = db.create_or_get_active_job(
            int(first_user["id"]), "discovery", workspace_key=key
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate["id"] == first["id"]
    finally:
        db.close()


def test_running_jobs_are_recovered_as_interrupted(config):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="restart-user")
        job = db.create_job(int(user["id"]), "discovery", workspace_key="workspace")
        assert db.claim_job(job["id"])["status"] == "running"
        assert db.recover_running_jobs() == 1
        recovered = db.get_job(job["id"])
        assert recovered["status"] == "interrupted"
        assert recovered["finished_at"] is not None
    finally:
        db.close()


def test_queued_job_cancellation_is_terminal(config):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="cancel-user")
        job = db.create_job(int(user["id"]), "discovery", workspace_key="cancel-workspace")
        cancelled = db.request_job_cancel(job["id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancel_requested"] == 1
        assert db.get_running_jobs() == []
    finally:
        db.close()


def test_workspace_reset_and_delete_are_blocked_by_active_job(config):
    db = Database(config.database_path)
    try:
        user = db.upsert_user(spotify_user_id="workspace-owner")
        key = Database.workspace_key_for_user("workspace-owner")
        workspace_id = db.create_workspace(workspace_key=key)
        job = db.create_job(int(user["id"]), "discovery", workspace_key=key)

        assert db.reset_workspace_if_idle(key, int(user["id"])) is False
        assert db.delete_workspace_if_idle(key, int(user["id"])) == (False, False)
        db.request_job_cancel(int(job["id"]))
        assert db.reset_workspace_if_idle(key, int(user["id"])) is True
        assert db.get_workspace(workspace_id)["status"] == "ready_for_scope"
        assert db.delete_workspace_if_idle(key, int(user["id"])) == (True, True)
        assert db.delete_workspace_if_idle(key, int(user["id"])) == (True, False)
    finally:
        db.close()
