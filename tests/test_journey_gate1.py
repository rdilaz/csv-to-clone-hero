"""E2E Gate 1 matching failure and authority regressions."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from csv_to_clonehero.catalog import CatalogLookup
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.webapp import (
    JOURNEY_MATCHING_FAILURE_CODE,
    JOURNEY_MATCHING_FAILURE_MESSAGE,
    create_app,
)

from .conftest import make_chart


class GateCatalog:
    available = True
    complete = True
    diagnostic = ""
    snapshot_id = "gate-1-catalog"

    def __init__(self, charts):
        self.charts = {chart.name: chart for chart in charts}
        self.chart_count = len(self.charts)

    def candidates(self, artist, title):
        chart = self.charts.get(title)
        return (chart,) if artist == "Acceptance Artist" and chart else ()

    def high_recall_candidates(self, artist, title, **_kwargs):
        candidates = self.candidates(artist, title)
        return CatalogLookup(
            candidates=candidates,
            strict_exact=bool(candidates),
            methods=("exact",) if candidates else (),
            retrieved_song_keys=1 if candidates else 0,
            exact_artist=bool(candidates),
            exact_title=bool(candidates),
        )

    def artist_songs(self, artist):
        if artist != "acceptance artist":
            return ()
        return tuple((title, (chart,)) for title, chart in self.charts.items())


@pytest.fixture
def gate_client(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda _config: [])
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, config, app


def _seed(client, config, count=1):
    workspace = int(client.get("/api/journey").get_json()["workspace_id"])
    tracks = [
        ProviderTrack(
            provider="soundcloud",
            provider_id=f"acceptance-{index}",
            artist="Acceptance Artist",
            title="Acceptance Song" if index == 0 else f"Acceptance Song {index}",
        )
        for index in range(count)
    ]
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", tracks))
    finally:
        db.close()
    return workspace, tracks


def _mark_legacy_failure(config, workspace):
    db = Database(config.database_path)
    try:
        metadata = {
            "workflow": "source_neutral",
            "run_id": "legacy-failure-without-diagnostic",
            "scan_key": "catalog",
            "generation": "journey-legacy-failure-without-diagnostic",
            "started_at": time.time() - 2,
            "finished_at": time.time() - 1,
            "error": "Legacy matching result was not authoritative.",
        }
        db.set_meta("local_current_run_id", metadata["run_id"])
        db.update_workspace(
            workspace,
            status="failed",
            phase="result_validation",
            metadata=metadata,
        )
        db.update_workspace_progress(
            workspace,
            status="failed",
            phase="result_validation",
            message="Legacy chart finding did not finish.",
            error="We could not finish finding charts. Your music is safe.",
            metadata=metadata,
        )
    finally:
        db.close()


def _wait(client, state, timeout=10.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = client.get("/api/journey").get_json()
        if latest["matching"]["state"] == state:
            return latest
        time.sleep(0.01)
    pytest.fail(f"expected {state}, got {latest}")


def _install_catalog(monkeypatch, catalog):
    manifest = {
        "snapshot_id": catalog.snapshot_id,
        "sha256": "gate-1-sha",
        "chart_count": catalog.chart_count,
        "format_version": 1,
    }
    import csv_to_clonehero.catalog as catalog_module
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(catalog_module, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "read_catalog_manifest", lambda _path: manifest)
    monkeypatch.setattr(pipeline, "load_enchor_catalog", lambda _path: catalog)
    return pipeline


def test_old_failure_new_start_is_synchronously_observable_and_succeeds(
    gate_client, monkeypatch
):
    import threading

    client, config, _app = gate_client
    workspace, _tracks = _seed(client, config)
    _mark_legacy_failure(config, workspace)
    baseline = client.get("/api/journey").get_json()["matching"]
    assert baseline["state"] == "matching_failed"
    assert baseline["run_id"] == "legacy-failure-without-diagnostic"
    assert baseline["diagnostic_id"] == ""

    catalog = GateCatalog(
        [make_chart("Acceptance Song", "Acceptance Artist", 8001)]
    )
    pipeline = _install_catalog(monkeypatch, catalog)
    entered = threading.Event()
    release = threading.Event()

    def blocked_fast(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", blocked_fast)
    response = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert response.status_code == 202
    accepted = response.get_json()
    assert accepted["started"] is True
    assert accepted["run_id"] != baseline["run_id"]
    assert accepted["workspace_id"] == workspace
    assert accepted["state"] == "matching_restoring"

    visible = client.get("/api/journey").get_json()["matching"]
    assert visible["run_id"] == accepted["run_id"]
    assert visible["state"] == "matching_restoring"
    assert visible["is_active_run"] is True
    assert visible["is_newest_run"] is True
    assert visible["generation"] == accepted["generation"]
    assert visible["results"] is None
    assert visible["diagnostic_id"] == ""
    assert entered.wait(5)

    release.set()
    found = _wait(client, "charts_found")
    assert found["matching"]["run_id"] == accepted["run_id"]
    assert found["matching"]["authoritative"] is True


def test_worker_exception_is_durable_logged_redacted_and_retry_starts_cleanly(
    gate_client, monkeypatch, caplog
):
    client, config, _app = gate_client
    workspace, tracks = _seed(client, config)
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", lambda *_args, **_kwargs: None)

    def fail_catalog(_path):
        raise RuntimeError(
            'fixture catalog failure access_token=private-value Bearer private-bearer '
            '{"access_token": "quoted-value", "client_secret": "quoted-secret"} '
            r'{\"access_token\": \"escaped-secret\"} '
            r'{\"refresh_token\": \"prefix\\\"secret-suffix\"} '
            "Authorization: Basic basic-secret"
        )

    monkeypatch.setattr(pipeline, "load_enchor_catalog", fail_catalog)
    caplog.set_level(logging.ERROR, logger="csv_to_clonehero.webapp")

    response = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert response.status_code == 202
    first_run = response.get_json()["run_id"]
    failed = _wait(client, "matching_failed")
    matching = failed["matching"]

    assert matching["error"] == JOURNEY_MATCHING_FAILURE_MESSAGE
    assert matching["error_code"] == JOURNEY_MATCHING_FAILURE_CODE
    assert matching["diagnostic_id"]
    assert matching["failure_phase"] == "catalog_load"
    assert matching["retry_available"] is True
    assert matching["authoritative"] is False
    assert matching["results"] is None

    db = Database(config.database_path)
    try:
        workspace_row = db.get_workspace(workspace)
        progress = db.get_workspace_progress(workspace)
        metadata = json.loads(progress["metadata_json"])
        assert workspace_row["status"] == "failed"
        assert workspace_row["phase"] == "catalog_load"
        assert progress["status"] == "failed"
        assert progress["error"] == JOURNEY_MATCHING_FAILURE_MESSAGE
        assert metadata["failure"]["diagnostic_id"] == matching["diagnostic_id"]
        assert metadata["failure"]["error_code"] == JOURNEY_MATCHING_FAILURE_CODE
        assert len(db.get_neutral_library_tracks(workspace)) == len(tracks)
        assert db.get_source_summary(workspace)[0]["tracks"] == len(tracks)
    finally:
        db.close()

    artifact = (
        Path(config.state_path)
        / "diagnostics"
        / f"journey-{matching['diagnostic_id']}.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert payload["run_id"] == first_run
    assert payload["workspace_id"] == workspace
    assert payload["phase"] == "catalog_load"
    assert payload["exception"]["type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in payload["exception"]["traceback"]
    assert "private-value" not in serialized
    assert "private-bearer" not in serialized
    assert "quoted-value" not in serialized
    assert "quoted-secret" not in serialized
    assert "escaped-secret" not in serialized
    assert "secret-suffix" not in serialized
    assert "basic-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert "Acceptance Song" not in serialized

    log_text = caplog.text
    assert f"operation_id={first_run}" in log_text
    assert "phase=catalog_load" in log_text
    assert "exception_type=RuntimeError" in log_text
    assert f"diagnostic_id={matching['diagnostic_id']}" in log_text
    assert "Traceback (most recent call last)" in log_text
    assert "private-value" not in log_text
    assert "private-bearer" not in log_text
    assert "quoted-value" not in log_text
    assert "quoted-secret" not in log_text
    assert "escaped-secret" not in log_text
    assert "secret-suffix" not in log_text
    assert "basic-secret" not in log_text

    catalog = GateCatalog([make_chart("Acceptance Song", "Acceptance Artist", 8101)])
    _install_catalog(monkeypatch, catalog)
    retry = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert retry.status_code == 202
    assert retry.get_json()["run_id"] != first_run
    restored = _wait(client, "charts_found")
    assert restored["matching"]["authoritative"] is True

    db = Database(config.database_path)
    try:
        metadata = json.loads(db.get_workspace(workspace)["metadata_json"])
        assert "failure" not in metadata
        assert "diagnostic_id" not in metadata
        assert len(db.get_neutral_library_tracks(workspace)) == len(tracks)
    finally:
        db.close()


def test_start_publication_exception_returns_diagnostic_without_202(
    gate_client, monkeypatch
):
    import csv_to_clonehero.webapp as webapp_module

    client, config, app = gate_client
    workspace, tracks = _seed(client, config)
    original_set_meta = webapp_module.Database.set_meta

    def fail_run_publication(self, key, value):
        if key == "local_current_run_id":
            raise RuntimeError("start publication failed")
        return original_set_meta(self, key, value)

    monkeypatch.setattr(
        webapp_module.Database,
        "set_meta",
        fail_run_publication,
    )
    response = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error_code"] == JOURNEY_MATCHING_FAILURE_CODE
    assert payload["state"] == "matching_failed"
    assert payload["run_id"]
    assert payload["diagnostic_id"]
    assert app.extensions["spotify_job"].running is False
    artifact = (
        Path(config.state_path)
        / "diagnostics"
        / f"journey-{payload['diagnostic_id']}.json"
    )
    assert artifact.is_file()
    diagnostic = json.loads(artifact.read_text(encoding="utf-8"))
    assert diagnostic["run_id"] == payload["run_id"]
    assert diagnostic["phase"] == "workspace_preparation"
    db = Database(config.database_path)
    try:
        assert len(db.get_neutral_library_tracks(workspace)) == len(tracks)
    finally:
        db.close()


def test_new_in_memory_failure_never_reuses_old_diagnostic_id(gate_client):
    client, config, app = gate_client
    workspace, _tracks = _seed(client, config)
    _mark_legacy_failure(config, workspace)
    db = Database(config.database_path)
    try:
        row = db.get_workspace(workspace)
        metadata = json.loads(row["metadata_json"])
        metadata.update(
            {
                "error_code": JOURNEY_MATCHING_FAILURE_CODE,
                "diagnostic_id": "old-diagnostic",
                "failure_phase": "old-phase",
                "failure": {
                    "error_code": JOURNEY_MATCHING_FAILURE_CODE,
                    "diagnostic_id": "old-diagnostic",
                    "phase": "old-phase",
                },
            }
        )
        db.update_workspace(
            workspace,
            status="failed",
            phase="old-phase",
            metadata=metadata,
        )
        db.update_workspace_progress(
            workspace,
            status="failed",
            phase="old-phase",
            error=JOURNEY_MATCHING_FAILURE_MESSAGE,
            metadata=metadata,
        )
    finally:
        db.close()
    job = app.extensions["spotify_job"]
    job.begin(
        workspace,
        workspace_key="local",
        scan_key="catalog",
        run_id="new-failed-run",
    )
    job.fail(
        JOURNEY_MATCHING_FAILURE_MESSAGE,
        error_code=JOURNEY_MATCHING_FAILURE_CODE,
        diagnostic_id="new-diagnostic",
        failure_phase="new-phase",
    )
    matching = client.get("/api/journey").get_json()["matching"]
    assert matching["run_id"] == "new-failed-run"
    assert matching["diagnostic_id"] == "new-diagnostic"
    assert matching["failure_phase"] == "new-phase"


def test_failed_newer_generation_hides_old_result_and_partial_candidates(
    gate_client, monkeypatch
):
    client, config, _app = gate_client
    workspace, tracks = _seed(client, config, count=2)
    charts = [
        make_chart(track.title, track.artist, 8200 + index)
        for index, track in enumerate(tracks)
    ]
    catalog = GateCatalog(charts)
    pipeline = _install_catalog(monkeypatch, catalog)

    first = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert first.status_code == 202
    complete = _wait(client, "charts_found")
    assert complete["matching"]["results"]["best_matches"] == 2

    db = Database(config.database_path)
    try:
        old_generation = db.get_latest_combined_generation(workspace)
        db._conn.execute("DELETE FROM matching_song_cache")
        db._conn.commit()
    finally:
        db.close()

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", lambda *_args, **_kwargs: None)
    original_score = pipeline.score_candidate

    def fail_second(track, chart, cfg):
        if track.title.endswith(" 1"):
            raise RuntimeError("second-song scoring failure")
        return original_score(track, chart, cfg)

    monkeypatch.setattr(pipeline, "score_candidate", fail_second)
    second = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert second.status_code == 202
    failed = _wait(client, "matching_failed")
    assert failed["matching"]["authoritative"] is False
    assert failed["matching"]["results"] is None
    assert failed["matching"]["diagnostic_id"]
    assert failed["matching"]["failure_phase"] == "scoring"

    db = Database(config.database_path)
    try:
        generations = db.get_discovery_scans(workspace)
        latest = db.get_latest_combined_generation(workspace)
        raw_candidates = db.get_workspace_candidates(workspace, kind="exact")
        current_candidates = db.get_workspace_candidates(
            workspace, kind="exact", current_only=True
        )
        tasks = [
            row for row in db.get_discovery_tasks(workspace)
            if row["scan_id"] == latest["id"]
        ]
        assert old_generation["status"] == "complete"
        assert len(generations) >= 2
        assert latest["id"] != old_generation["id"]
        assert latest["status"] == "failed"
        assert raw_candidates
        assert current_candidates == []
        assert tasks and all(row["status"] == "failed" for row in tasks)
        assert len(db.get_neutral_library_tracks(workspace)) == len(tracks)
    finally:
        db.close()


def test_invalid_completed_scan_cache_cannot_bypass_candidate_validation(
    gate_client, monkeypatch
):
    client, config, _app = gate_client
    workspace, _tracks = _seed(client, config)
    catalog = GateCatalog([make_chart("Acceptance Song", "Acceptance Artist", 8301)])
    pipeline = _install_catalog(monkeypatch, catalog)

    first = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert first.status_code == 202
    expected = _wait(client, "charts_found")["matching"]["results"]

    db = Database(config.database_path)
    try:
        scan_count = len(db.get_discovery_scans(workspace))
        cache = db._conn.execute(
            "SELECT result_json,candidate_revision FROM matching_scan_cache "
            "WHERE workspace_id=?",
            (workspace,),
        ).fetchone()
        expected_candidates = json.loads(cache["result_json"])["persisted_candidates"]
        assert expected_candidates > 0
        assert db.clear_workspace_candidates(workspace, kind="exact") == expected_candidates
        assert db.workspace_exact_candidate_count(workspace) == 0
        assert db.get_workspace_candidate_revision(workspace) != cache["candidate_revision"]
    finally:
        db.close()

    monkeypatch.setattr(
        pipeline,
        "score_candidate",
        lambda *_args, **_kwargs: pytest.fail("song-cache restoration rescored"),
    )
    second = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert second.status_code == 202
    restored = _wait(client, "charts_found")
    assert restored["matching"]["authoritative"] is True
    assert restored["matching"]["results"] == expected

    db = Database(config.database_path)
    try:
        assert len(db.get_discovery_scans(workspace)) == scan_count + 1
        assert db.workspace_exact_candidate_count(workspace) == expected_candidates
        assert db.get_workspace_candidates(
            workspace, kind="exact", current_only=True
        )
        progress = db.get_workspace_progress(workspace)
        metadata = json.loads(progress["metadata_json"])
        assert metadata["cached_tracks"] == 1
        assert metadata["recomputed_tracks"] == 0
    finally:
        db.close()


def test_failure_does_not_allow_retry_until_old_worker_finishes_persistence(
    gate_client, monkeypatch
):
    client, config, _app = gate_client
    workspace, _tracks = _seed(client, config)
    import csv_to_clonehero.combined_pipeline as pipeline
    import csv_to_clonehero.webapp as webapp_module
    import threading

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "load_enchor_catalog",
        lambda _path: (_ for _ in ()).throw(RuntimeError("blocked persistence failure")),
    )
    entered = threading.Event()
    release = threading.Event()
    original_update = webapp_module.Database.update_workspace

    def blocked_update(self, workspace_id, updates=None, **values):
        data = dict(updates or {})
        data.update(values)
        if data.get("status") == "failed":
            entered.set()
            assert release.wait(5)
        return original_update(self, workspace_id, updates, **values)

    monkeypatch.setattr(webapp_module.Database, "update_workspace", blocked_update)
    started = client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    )
    assert started.status_code == 202
    assert client.get("/api/journey").get_json()["matching"]["state"] == "matching_restoring"
    assert entered.wait(5)
    while_persisting = client.get("/api/journey").get_json()["matching"]
    assert while_persisting["state"] == "matching_running"
    assert while_persisting["retry_available"] is False
    assert client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    ).status_code == 409
    release.set()
    assert _wait(client, "matching_failed")["matching"]["diagnostic_id"]


def test_changed_snapshot_cannot_reuse_library_fingerprint_cache(
    gate_client, monkeypatch
):
    client, config, _app = gate_client
    workspace, _tracks = _seed(client, config)
    first_chart = make_chart("Acceptance Song", "Acceptance Artist", 8401)
    second_chart = make_chart("Changed Song", "Acceptance Artist", 8402)
    catalog = GateCatalog([first_chart, second_chart])
    pipeline = _install_catalog(monkeypatch, catalog)

    assert client.post(
        "/api/discovery/start",
        json={"source": "combined", "workspace_id": workspace},
    ).status_code == 202
    assert _wait(client, "charts_found")["matching"]["results"]["best_matches"] == 1

    calls = []
    original_score = pipeline.score_candidate

    def counted(track, chart, cfg):
        calls.append(track.title)
        return original_score(track, chart, cfg)

    monkeypatch.setattr(pipeline, "score_candidate", counted)
    db = Database(config.database_path)
    try:
        key = db.get_meta("local_workspace_key")
        result = pipeline.discover_combined(
            config,
            db,
            [
                LibrarySnapshot(
                    "soundcloud",
                    [
                        ProviderTrack(
                            provider="soundcloud",
                            provider_id="changed",
                            artist="Acceptance Artist",
                            title="Changed Song",
                        )
                    ],
                )
            ],
            workspace_key=key,
        )
        assert result.tracks == 1
        assert calls == ["Changed Song"]
        progress = db.get_discovery_song_progress(workspace)
        assert [row["title"] for row in progress] == ["Changed Song"]
    finally:
        db.close()


def test_warm_only_fast_path_miss_fails_without_starting_cold_generation(
    gate_client, monkeypatch
):
    client, config, _app = gate_client
    workspace, _tracks = _seed(client, config)
    import csv_to_clonehero.combined_pipeline as pipeline

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "discover_combined",
        lambda *_args, **_kwargs: pytest.fail("warm-only request entered cold matching"),
    )
    response = client.post(
        "/api/discovery/start",
        json={
            "source": "combined",
            "workspace_id": workspace,
            "warm_only": True,
        },
    )
    assert response.status_code == 202
    failure = _wait(client, "matching_failed")["matching"]
    assert failure["failure_phase"] == "completed_scan_validation"
    assert failure["diagnostic_id"]
    db = Database(config.database_path)
    try:
        assert db.get_latest_combined_generation(workspace) is None
        assert len(db.get_neutral_library_tracks(workspace)) == 1
    finally:
        db.close()


def test_matching_file_lease_excludes_second_app_process_path(config, monkeypatch):
    import threading
    import csv_to_clonehero.combined_pipeline as pipeline

    config.input_path.mkdir(parents=True, exist_ok=True)
    first_app = create_app(config, source_factory=lambda _config: [])
    first_app.config["TESTING"] = True
    entered = threading.Event()
    release = threading.Event()

    def blocked_fast(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("finish lease test")

    monkeypatch.setattr(pipeline, "completed_scan_fast_path", blocked_fast)
    first = first_app.test_client()
    try:
        workspace = int(first.get("/api/journey").get_json()["workspace_id"])
        db = Database(config.database_path)
        try:
            db.upsert_library_snapshot(
                workspace,
                LibrarySnapshot(
                    "soundcloud",
                    [
                        ProviderTrack(
                            provider="soundcloud",
                            provider_id="lease",
                            artist="Acceptance Artist",
                            title="Acceptance Song",
                        )
                    ],
                ),
            )
        finally:
            db.close()
        assert first.post(
            "/api/discovery/start",
            json={"source": "combined", "workspace_id": workspace},
        ).status_code == 202
        assert first.get("/api/journey").get_json()["matching"]["state"] == "matching_restoring"
        assert entered.wait(5)
        second_app = create_app(config, source_factory=lambda _config: [])
        second_app.config["TESTING"] = True
        second = second_app.test_client()
        db = Database(config.database_path)
        try:
            assert db.get_workspace(workspace)["status"] == "running"
        finally:
            db.close()
        conflict = second.post(
            "/api/discovery/start",
            json={"source": "combined", "workspace_id": workspace},
        )
        assert conflict.status_code == 409
        assert "another local chart-finding worker" in conflict.get_json()["error"]
        release.set()
        assert _wait(first, "matching_failed")["matching"]["diagnostic_id"]
    finally:
        release.set()


def test_real_web_process_lease_blocks_second_startup_before_recovery(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    first = create_app(
        config,
        source_factory=lambda _config: [],
        enforce_single_process=True,
    )
    lease = first.extensions["journey_web_process_lease"]
    try:
        db = Database(config.database_path)
        try:
            workspace = db.create_workspace(
                workspace_key="startup-lease",
                name="Startup lease",
                status="running",
                phase="source_import",
            )
        finally:
            db.close()
        with pytest.raises(RuntimeError, match="another local web application"):
            create_app(
                config,
                source_factory=lambda _config: [],
                enforce_single_process=True,
            )
        db = Database(config.database_path)
        try:
            assert db.get_workspace(workspace)["status"] == "running"
        finally:
            db.close()
    finally:
        lease.release()


def test_reloader_parent_can_skip_startup_recovery(config):
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(
            workspace_key="reload-parent",
            name="Reload parent",
            status="running",
            phase="source_import",
        )
    finally:
        db.close()
    create_app(
        config,
        source_factory=lambda _config: [],
        recover_interrupted_state=False,
    )
    db = Database(config.database_path)
    try:
        assert db.get_workspace(workspace)["status"] == "running"
    finally:
        db.close()
