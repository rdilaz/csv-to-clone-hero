"""Tests for the background job queue: queueing, global gating, completion."""

from __future__ import annotations

import time

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.server.jobs import JobManager, _job_payload
from csv_to_clonehero.server.security import TokenCipher, generate_fernet_key
from csv_to_clonehero.server.throttle import reset_global_gate_for_tests


@pytest.fixture
def cipher():
    return TokenCipher(generate_fernet_key())


@pytest.fixture(autouse=True)
def spotify_client_id(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client")


@pytest.fixture(autouse=True)
def clean_gate():
    reset_global_gate_for_tests()
    yield
    reset_global_gate_for_tests()


@pytest.fixture
def db(config, cipher):
    database = Database(config.database_path)
    user = database.upsert_user(spotify_user_id="job-user")
    yield database
    database.close()


def _discovery_result(status="complete"):
    from csv_to_clonehero.spotify_pipeline import DiscoveryResult, DiscoveryStats

    return DiscoveryResult(status=status, phase="complete", stats=DiscoveryStats())


def test_enqueue_and_status(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key="spotify:u:job-user")
        assert job.status == "queued"
        # The single worker should claim and finish it.
        deadline = time.time() + 5
        while time.time() < deadline:
            current = manager.get(job.id)
            if current is not None and current.status == "complete":
                break
            time.sleep(0.05)
        assert manager.get(job.id).status == "complete"
    finally:
        manager.shutdown(timeout=2)


def test_jobs_are_durable_and_per_user(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    others = db.upsert_user(spotify_user_id="other-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key="spotify:u:job-user")
        # The other user must not see this job's id in their list.
        assert all(item["id"] != job.id for item in manager.list_for_user(int(others["id"])))
        assert any(item["id"] == job.id for item in manager.list_for_user(int(user["id"])))
    finally:
        manager.shutdown(timeout=2)


def test_failed_job_records_error(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("csv_to_clonehero.spotify_pipeline.run_spotify_discovery", boom)
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery")
        deadline = time.time() + 5
        while time.time() < deadline:
            current = manager.get(job.id)
            if current is not None and current.status in {"failed", "complete"}:
                break
            time.sleep(0.05)
        assert manager.get(job.id).status == "failed"
        assert "boom" in (manager.get(job.id).error or "")
    finally:
        manager.shutdown(timeout=2)


def test_completion_notification(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    sent = []

    class FakeSender:
        def send(self, subject, body, to):
            sent.append(to)
            return True

    manager = JobManager(config, cipher, source_factory=lambda cfg: [], sender=FakeSender(), poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", notify_email="u@example.com")
        deadline = time.time() + 5
        while time.time() < deadline:
            current = manager.get(job.id)
            if current is not None and current.status == "complete":
                break
            time.sleep(0.05)
        assert sent == ["u@example.com"]
    finally:
        manager.shutdown(timeout=2)


def test_job_payload_typed():
    payload = _job_payload(
        type("Row", (), {
            "id": 1, "kind": "discovery", "status": "running", "phase": "x",
            "message": "m", "error": None, "progress_json": '{"done": 3, "total": 10}',
            "result_json": "{}", "workspace_key": "w", "notify_email": "",
            "started_at": None, "finished_at": None, "created_at": 0.0, "updated_at": 0.0,
        })()
    )
    assert payload["progress"]["done"] == 3