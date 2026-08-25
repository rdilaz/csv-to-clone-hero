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
        assert db.claim_job(job["id"]) is None  # only once
        db.update_job(job["id"], status="complete", progress={"done": 5}, result={"ok": True})
        row = db.get_job(job["id"])
        assert row["status"] == "complete"
        assert "5" in row["progress_json"]
        assert row["result_json"] == '{"ok": true}'
    finally:
        db.close()