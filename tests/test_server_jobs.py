"""Tests for the background job queue: queueing, global gating, completion."""

from __future__ import annotations

import time
import threading

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.server.jobs import JobManager, _job_payload
from csv_to_clonehero.server.security import TokenCipher, generate_fernet_key
from csv_to_clonehero.server.throttle import reset_global_gate_for_tests
from csv_to_clonehero.sources.enchor import PREFLIGHT_VERSION


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
    database.set_user_token(int(user["id"]), cipher.encrypt("refresh-token"))
    key = Database.workspace_key_for_user("job-user")
    workspace = database.create_workspace(workspace_key=key, name="job-user")
    database.set_workspace_scope(workspace, include_liked=True)
    database.record_source_preflight(
        {
            "source_name": "enchor",
            "endpoint": "/search",
            "request_version": PREFLIGHT_VERSION,
            "success": True,
            "supported": True,
            "authoritative": True,
            "found": 100,
            "out_of": 100,
        },
        ttl_seconds=3600,
    )
    yield database
    database.close()


def _discovery_result(status="complete"):
    from csv_to_clonehero.spotify_pipeline import DiscoveryResult, DiscoveryStats

    return DiscoveryResult(status=status, phase="complete", stats=DiscoveryStats())


def test_enqueue_and_status(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    workspace_key = Database.workspace_key_for_user("job-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=workspace_key)
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
    workspace_key = Database.workspace_key_for_user("job-user")
    others = db.upsert_user(spotify_user_id="other-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=workspace_key)
        # The other user must not see this job's id in their list.
        assert all(item["id"] != job.id for item in manager.list_for_user(int(others["id"])))
        assert any(item["id"] == job.id for item in manager.list_for_user(int(user["id"])))
    finally:
        manager.shutdown(timeout=2)


def test_failed_job_records_error(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    workspace_key = Database.workspace_key_for_user("job-user")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("csv_to_clonehero.spotify_pipeline.run_spotify_discovery", boom)
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=workspace_key)
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
    workspace_key = Database.workspace_key_for_user("job-user")
    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    sent = []

    class FakeSender:
        def send(self, subject, body, to):
            sent.append((to, body))
            return True

    manager = JobManager(config, cipher, source_factory=lambda cfg: [], sender=FakeSender(), poll_interval=0.05)
    try:
        job = manager.enqueue(
            int(user["id"]),
            "discovery",
            workspace_key=workspace_key,
            notify_email="u@example.com",
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            current = manager.get(job.id)
            if current is not None and current.status == "complete" and sent:
                break
            time.sleep(0.05)
        assert sent[0][0] == "u@example.com"
        assert "the application" in sent[0][1]
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


def test_cancel_is_user_scoped_and_sets_event(config, db, cipher):
    user = db.get_user("job-user")
    key = Database.workspace_key_for_user("job-user")
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], auto_start=False)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=key)
        assert manager.cancel(job.id, int(user["id"]) + 999) is None
        cancelled = manager.cancel(job.id, int(user["id"]))
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested is True
    finally:
        manager.shutdown(timeout=2)


def test_running_cancel_is_durable_and_wins_over_late_completion(config, db, cipher, monkeypatch):
    user = db.get_user("job-user")
    key = Database.workspace_key_for_user("job-user")
    started = threading.Event()

    def cancellable(*args, **kwargs):
        started.set()
        assert kwargs["cancellation_event"].wait(2)
        kwargs["on_progress"]({"phase": "provider_import", "message": "Import returned"})
        return _discovery_result()

    monkeypatch.setattr("csv_to_clonehero.spotify_pipeline.run_spotify_discovery", cancellable)
    manager = JobManager(config, cipher, source_factory=lambda cfg: [], poll_interval=0.05)
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=key)
        assert started.wait(2)
        requested = manager.cancel(job.id, int(user["id"]))
        assert requested is not None and requested.cancel_requested is True
        deadline = time.time() + 5
        while time.time() < deadline and manager.get(job.id).status != "cancelled":
            time.sleep(0.05)
        cancelled = manager.get(job.id)
        assert cancelled.status == "cancelled"
        assert cancelled.heartbeat_at is not None
    finally:
        manager.shutdown(timeout=2)


def test_worker_refreshes_preflight_that_expired_in_queue(config, db, cipher, monkeypatch):
    from csv_to_clonehero.sources.enchor import EnchorCatalogPreflight

    user = db.get_user("job-user")
    workspace_key = Database.workspace_key_for_user("job-user")
    db.record_source_preflight(
        {
            "source_name": "enchor",
            "endpoint": "/search",
            "request_version": PREFLIGHT_VERSION,
            "checked_at": 1.0,
            "success": True,
            "supported": True,
            "authoritative": True,
            "found": 10,
            "out_of": 10,
        },
        ttl_seconds=1,
    )
    probes = []

    class FakeEnchor:
        name = "enchor"
        supports_direct_download = True
        exportable = True
        discovery_only = False

        def preflight_catalog(self):
            probes.append(True)
            return EnchorCatalogPreflight(
                checked_at=time.time(), success=True, supported=True,
                authoritative=True, found=10, out_of=10, returned_count=1, page=1,
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "csv_to_clonehero.spotify_pipeline.run_spotify_discovery",
        lambda *a, **k: _discovery_result(),
    )
    manager = JobManager(
        config,
        cipher,
        source_factory=lambda cfg: [FakeEnchor()],
        poll_interval=0.05,
    )
    try:
        job = manager.enqueue(int(user["id"]), "discovery", workspace_key=workspace_key)
        deadline = time.time() + 5
        while time.time() < deadline and manager.get(job.id).status not in {"complete", "failed"}:
            time.sleep(0.05)
        assert manager.get(job.id).status == "complete"
        assert probes == [True]
    finally:
        manager.shutdown(timeout=2)
