"""The persisted job separates measured work, worker contact and selection size."""

import threading
from types import SimpleNamespace

from csv_to_clonehero.database import Database
from csv_to_clonehero.server.downloads import BrowserDownloads, selection_snapshot
from csv_to_clonehero.server.jobs import JobManager
from csv_to_clonehero.server.security import TokenCipher, generate_fernet_key
from tests.test_server_web import _seed_async_download_choice


def _manager(config, builder):
    db = Database(config.database_path)
    user = db.upsert_user(spotify_user_id="progress-user")
    key = Database.workspace_key_for_user("progress-user")
    workspace = db.create_workspace(workspace_key=key, name="progress")
    db.close()
    _seed_async_download_choice(config, workspace)
    db = Database(config.database_path)
    snapshot = selection_snapshot(db, workspace)
    db.close()
    registry = BrowserDownloads(config, "progress-test-secret", sweep_interval=0)
    manager = JobManager(config, TokenCipher(generate_fernet_key()), downloads=registry,
                         archive_builder=builder, auto_start=False)
    job = manager.enqueue(int(user["id"]), "download", workspace_key=key, initial_progress=snapshot)
    manager._cancel_events[job.id] = threading.Event()
    return manager, registry, job


def test_measured_job_progress_is_durable_throttled_and_heartbeat_does_not_claim_work(config, monkeypatch):
    import csv_to_clonehero.server.jobs as jobs

    clock = [1000.25]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    observed = []

    def builder(_config, _db, _workspace, _sources, staging, *, progress, progress_details, cancellation_event):
        progress_details({"phase": "validating", "completed": 0, "total": 1, "current_item": "C:\\private\\Chart.sng"})
        assert manager.get(job.id).progress.get("last_work_at") is None
        clock[0] += 1
        progress_details({"phase": "validating", "completed": 1, "total": 1, "work_advanced": True})
        validated = manager.get(job.id)
        observed.append(validated)
        assert validated.progress["last_work_at"] == clock[0]
        assert validated.progress["current_item"] == "Chart.sng"
        clock[0] += 1
        progress_details({"phase": "packaging", "completed": 0, "total": 1, "files_completed": 0,
                          "files_total": 1, "bytes_completed": 0, "bytes_total": 2048, "bytes_kind": "source"})
        packed = manager.get(job.id)
        assert packed.progress["completed"] == 0
        assert packed.progress["selection_count"] == 1
        assert packed.progress["last_work_at"] == validated.progress["last_work_at"]
        clock[0] += 1
        progress_details({"phase": "packaging", "completed": 0, "total": 1,
                          "bytes_completed": 1024, "work_advanced": True})
        advanced = manager.get(job.id)
        assert advanced.progress["last_work_at"] == clock[0]
        # A burst remains in memory until the next bounded persistence tick.
        clock[0] += 0.1
        progress_details({"phase": "packaging", "completed": 0, "total": 1,
                          "bytes_completed": 1536, "work_advanced": True})
        assert manager.get(job.id).progress["bytes_completed"] == 1024
        clock[0] += 1
        progress_details({"phase": "packaging", "completed": 0, "total": 1,
                          "bytes_completed": 1536, "work_advanced": True})
        contact = manager.get(job.id)
        assert contact.progress["last_work_at"] == advanced.progress["last_work_at"] + 0.1
        assert contact.heartbeat_at > contact.progress["last_work_at"]
        clock[0] += 1
        progress_details({"phase": "packaging", "completed": 1, "total": 1, "files_completed": 1,
                          "bytes_completed": 2048, "work_advanced": True})
        archive = staging / "measured.zip"
        archive.write_bytes(b"PK\x03\x04fixture")
        return archive, {"selected": 1, "published": 1, "bytes": archive.stat().st_size}

    manager, registry, job = _manager(config, builder)
    try:
        manager._run_download(job)
        finished = manager.get(job.id)
        assert finished.status == "complete"
        assert finished.progress["bytes_completed"] == 2048
        assert finished.progress["archive_bytes"] == len(b"PK\x03\x04fixture")
        assert finished.progress["last_work_at"] == clock[0]
        # The final receipt is durable and never contains delivery authority.
        assert "token" not in str(finished.progress)
        assert "private" not in str(finished.progress)
    finally:
        manager.shutdown()
        registry.close()


def test_legacy_builder_callback_stays_supported_without_fake_packaging_percentage(config):
    seen = []

    def builder(_config, _db, _workspace, _sources, staging, *, progress, cancellation_event):
        progress(1, 1, "Validated Artist/Chart.sng")
        progress(1, 1, "Writing ZIP archive")
        seen.append(manager.get(job.id))
        archive = staging / "legacy.zip"
        archive.write_bytes(b"legacy fixture")
        return archive, {"selected": 1, "published": 1}

    manager, registry, job = _manager(config, builder)
    try:
        manager._run_download(job)
        assert seen[0].phase == "packaging"
        assert seen[0].progress["completed"] == seen[0].progress["total"] == 0
        assert "bytes_total" not in seen[0].progress
        assert manager.get(job.id).status == "complete"
    finally:
        manager.shutdown()
        registry.close()
