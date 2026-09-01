"""Unit and subprocess coverage for the reusable local acceptance harness."""

from __future__ import annotations

import json
import io
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.local_journey_e2e import (
    AcceptanceError,
    AcceptanceArtifacts,
    DEFAULT_CONFIG,
    DEFAULT_REAL_CATALOG,
    DEFAULT_REAL_DB,
    ExpectedRunContract,
    JourneySnapshotReducer,
    MATCHING_START_NOT_OBSERVABLE,
    MatchingStartRejected,
    PortInUseError,
    OwnedServer,
    RealStateLease,
    _json_safe,
    _assert_partition,
    _acceptance_config,
    _await_expected_run,
    _exception_details,
    _lease_is_available,
    _poll,
    _recover_bound_run_if_dead,
    _start_matching_http,
    assert_port_free,
    main,
    resolve_python,
    run_observe_existing,
)


def _snapshot(
    *,
    revision="r1",
    state="matching_running",
    run_id="run-1",
    processed=1,
    total=3,
    elapsed=1.0,
    diagnostic_id="",
    results=None,
    authoritative=False,
):
    return {
        "revision": revision,
        "screen": state,
        "operation": {"state": "matching" if state.startswith("matching_") else "idle"},
        "music": {"canonical_count": total},
        "matching": {
            "state": state,
            "run_id": run_id,
            "processed": processed,
            "total": total,
            "elapsed_seconds": elapsed,
            "started_at": 1.0,
            "current_song": {"artist": "Real Artist", "title": "Real Song"},
            "error_code": "MATCHING_WORKER_FAILED" if state == "matching_failed" else "",
            "diagnostic_id": diagnostic_id,
            "retry_available": state == "matching_failed",
            "authoritative": authoritative,
            "results": results,
        },
    }


def test_snapshot_reducer_rejects_reappearing_old_revision():
    reducer = JourneySnapshotReducer()
    reducer.reduce(_snapshot(revision="r1", processed=0))
    reducer.reduce(_snapshot(revision="r2", processed=1))
    with pytest.raises(AcceptanceError, match="revision moved backward"):
        reducer.reduce(_snapshot(revision="r1", processed=2))


def test_snapshot_reducer_rejects_backward_progress_and_elapsed():
    reducer = JourneySnapshotReducer()
    reducer.reduce(_snapshot(revision="r1", processed=2, elapsed=5))
    with pytest.raises(AcceptanceError, match="processed moved backward"):
        reducer.reduce(_snapshot(revision="r2", processed=1, elapsed=6))

    reducer = JourneySnapshotReducer()
    reducer.reduce(_snapshot(revision="r1", processed=2, elapsed=5))
    with pytest.raises(AcceptanceError, match="elapsed reset"):
        reducer.reduce(_snapshot(revision="r2", processed=2, elapsed=1))


def test_snapshot_reducer_deduplicates_identical_snapshots():
    reducer = JourneySnapshotReducer()
    value = _snapshot()
    assert reducer.reduce(value) is not None
    assert reducer.reduce(value) is None


def test_current_song_snapshot_identity_is_salted_and_private():
    first = JourneySnapshotReducer().reduce(_snapshot())
    second = JourneySnapshotReducer().reduce(_snapshot())
    assert first["current_song"]["present"] is True
    assert first["current_song"]["identity_sha256"] != second["current_song"]["identity_sha256"]
    assert "Real Artist" not in json.dumps(first)
    assert "Real Song" not in json.dumps(first)


def test_snapshot_reducer_requires_diagnostic_on_failure():
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=True)
    with pytest.raises(AcceptanceError, match="no diagnostic ID"):
        reducer.reduce(_snapshot(state="matching_failed"))
    assert reducer.reduce(
        _snapshot(
            revision="failed-2",
            state="matching_failed",
            diagnostic_id="abc12345",
        )
    )


def test_expected_run_contract_ignores_legacy_failure_without_diagnostic(tmp_path):
    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    contract = ExpectedRunContract(
        expected_run_id="new-run",
        baseline_run_id="old-run",
        baseline_revision="old-revision",
    )
    old_failure = _snapshot(
        revision="old-revision",
        state="matching_failed",
        run_id="old-run",
        diagnostic_id="",
    )
    assert contract.observe(old_failure, reducer, artifacts) == "older_run_ignored"
    assert contract.acknowledged is False


def test_expected_run_contract_rejects_old_response_after_acknowledgement(tmp_path):
    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    contract = ExpectedRunContract(
        expected_run_id="new-run",
        baseline_run_id="old-run",
        baseline_revision="old-revision",
    )
    active = _snapshot(
        revision="new-revision",
        state="matching_restoring",
        run_id="new-run",
        processed=0,
    )
    assert contract.observe(active, reducer, artifacts) == "expected_run_active"
    with pytest.raises(AcceptanceError, match="STALE_MATCHING_RUN_RESPONSE"):
        contract.observe(
            _snapshot(
                revision="old-late-revision",
                state="matching_failed",
                run_id="old-run",
            ),
            reducer,
            artifacts,
        )


def test_expected_worker_failure_requires_and_accepts_diagnostic(tmp_path):
    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    contract = ExpectedRunContract("new-run", "old-run", "old-revision")
    assert contract.observe(
        _snapshot(
            revision="new-active",
            state="matching_running",
            run_id="new-run",
        ),
        reducer,
        artifacts,
    ) == "expected_run_active"
    with pytest.raises(AcceptanceError, match="expected matching worker failure"):
        contract.observe(
            _snapshot(
                revision="new-failed-no-diagnostic",
                state="matching_failed",
                run_id="new-run",
            ),
            reducer,
            artifacts,
        )

    artifacts = AcceptanceArtifacts(tmp_path / "artifacts-with-diagnostic")
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    contract = ExpectedRunContract("new-run", "old-run", "old-revision")
    contract.observe(
        _snapshot(
            revision="new-active",
            state="matching_running",
            run_id="new-run",
        ),
        reducer,
        artifacts,
    )
    assert contract.observe(
        _snapshot(
            revision="new-failed",
            state="matching_failed",
            run_id="new-run",
            diagnostic_id="diag-123",
        ),
        reducer,
        artifacts,
    ) == "expected_run_terminal"


def test_result_partition_invariant():
    results = {
        "best_matches": 1,
        "best_available": 1,
        "recommendations": 2,
        "no_suitable": 1,
        "not_playable": 1,
        "no_candidate": 1,
    }
    snapshot = _snapshot(
        state="charts_found",
        processed=5,
        total=5,
        results=results,
        authoritative=True,
    )
    _assert_partition(snapshot, None)
    snapshot["matching"]["results"]["no_candidate"] = 0
    with pytest.raises(AcceptanceError, match="partition"):
        _assert_partition(snapshot, None)


def test_failure_terminal_stops_polling(tmp_path):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return _snapshot(
                state="matching_failed",
                diagnostic_id="abc12345",
            )

    class Client:
        calls = 0

        def get(self, _url):
            self.calls += 1
            return Response()

    from scripts.local_journey_e2e import AcceptanceArtifacts

    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    client = Client()
    result = _poll(
        client,
        "http://127.0.0.1:1",
        JourneySnapshotReducer(),
        artifacts,
        timeout=1,
    )
    assert result["matching"]["state"] == "matching_failed"
    assert client.calls == 1


def test_start_not_observable_has_distinct_correlation_artifact(
    tmp_path, monkeypatch
):
    import scripts.local_journey_e2e as harness

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return _snapshot(
                revision="old-revision",
                state="matching_failed",
                run_id="old-run",
            )

    class Client:
        def get(self, _url):
            return Response()

    class Server:
        base_url = "http://127.0.0.1:1"
        port = 1
        process = None
        log_path = tmp_path / "server.log"
        config_path = tmp_path / "config.yaml"

    Server.log_path.write_text("server log tail", encoding="utf-8")
    monkeypatch.setattr(
        harness,
        "_capture_start_runtime",
        lambda _server: {"server_log_tail": "server log tail", "lease": "owned"},
    )
    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    contract = ExpectedRunContract("new-run", "old-run", "old-revision")
    evidence = {
        "baseline": JourneySnapshotReducer.project(
            _snapshot(
                revision="old-revision",
                state="matching_failed",
                run_id="old-run",
            )
        ),
        "post_response": {
            "status_code": 202,
            "body": {
                "started": True,
                "run_id": "new-run",
                "workspace_id": 1,
                "state": "matching_restoring",
            },
        },
        "expected_run_id": "new-run",
    }
    with pytest.raises(AcceptanceError, match=MATCHING_START_NOT_OBSERVABLE):
        _await_expected_run(
            Client(),
            Server(),
            reducer,
            artifacts,
            contract,
            evidence,
            timeout=0.01,
            correlation_name="start-correlation.json",
        )
    correlation = json.loads(
        (artifacts.root / "start-correlation.json").read_text(encoding="utf-8")
    )
    assert correlation["outcome"] == MATCHING_START_NOT_OBSERVABLE
    assert correlation["expected_run_id"] == "new-run"
    assert correlation["observed"][0]["run_id"] == "old-run"
    assert correlation["runtime"]["server_log_tail"] == "server log tail"


def test_start_rejection_does_not_poll_for_run():
    class Response:
        status_code = 409
        text = "conflict"

        def json(self):
            return {
                "error": "another worker is running",
                "error_code": "MATCHING_START_CONFLICT",
            }

    class Client:
        posts = 0

        def post(self, _url, json):
            self.posts += 1
            return Response()

    client = Client()
    with pytest.raises(MatchingStartRejected) as exc_info:
        _start_matching_http(client, "http://127.0.0.1:1", 1)
    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["error_code"] == "MATCHING_START_CONFLICT"
    assert client.posts == 1


def test_stale_port_is_reported_with_listener_context():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(PortInUseError, match="already has a listener"):
            assert_port_free(port)
    finally:
        listener.close()
    assert_port_free(port)


def test_server_startup_failure_releases_process_and_port(tmp_path):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = tmp_path / "invalid.yaml"
    config.write_text("[invalid", encoding="utf-8")
    server = OwnedServer(
        Path(__import__("sys").executable),
        config,
        port,
        tmp_path / "failed-server.log",
    )
    with pytest.raises(AcceptanceError):
        server.start(timeout=10)
    assert server.process is None
    assert_port_free(port)


def test_acceptance_artifact_strings_redact_command_credentials():
    value = _json_safe(
        {
            "command": "app --access-token private-token Authorization: Basic private-basic",
            "error": (
                'access_token=private-query {"access_token":"artifact-quoted"} '
                r'{\"client_secret\":\"artifact-escaped\"} '
                r'{\"refresh_token\":\"prefix\\\"artifact-suffix\"}'
            ),
        }
    )
    serialized = json.dumps(value)
    assert "private-token" not in serialized
    assert "private-basic" not in serialized
    assert "private-query" not in serialized
    assert "artifact-quoted" not in serialized
    assert "artifact-escaped" not in serialized
    assert "artifact-suffix" not in serialized
    assert "[REDACTED]" in serialized


def test_real_state_lease_excludes_second_runner(tmp_path):
    path = tmp_path / "real-state.lock"
    with RealStateLease(path):
        with pytest.raises(AcceptanceError, match="another acceptance runner"):
            with RealStateLease(path):
                pytest.fail("second real-state lease was acquired")


def test_empty_exception_has_safe_code_message_and_traceback(tmp_path):
    artifacts = AcceptanceArtifacts(tmp_path / "artifacts")
    try:
        raise AssertionError()
    except AssertionError as exc:
        record = artifacts.failure(exc, {"mode": "test"}, phase="polling")
    assert record["error_code"] == "HARNESS_ASSERTION_FAILED"
    assert record["exception_type"] == "AssertionError"
    assert record["message"] == "An acceptance assertion failed without a message."
    assert "AssertionError" in record["traceback"]
    persisted = json.loads(
        (artifacts.root / "failure.json").read_text(encoding="utf-8")
    )
    assert persisted["message"]
    assert persisted["phase"] == "polling"


def test_cli_never_prints_empty_acceptance_failure(
    tmp_path, monkeypatch, capsys
):
    import scripts.local_journey_e2e as harness

    def fail_without_message(*_args, **_kwargs):
        error = AssertionError()
        error.acceptance_worker_state = {
            "status": "alive_and_preserved",
            "run_id": "live-run",
            "pid": 12345,
            "port": 8767,
        }
        raise error

    monkeypatch.setattr(harness, "run_server_mode", fail_without_message)
    code = harness.main(
        [
            "--matching",
            "--target",
            "isolated",
            "--no-browser",
            "--artifacts-dir",
            str(tmp_path),
        ]
    )
    assert code == 1
    stderr = capsys.readouterr().err
    assert "HARNESS_ASSERTION_FAILED/AssertionError" in stderr
    assert "An acceptance assertion failed without a message." in stderr
    assert "Matching is still running on PID 12345 / port 8767" in stderr
    run = next(tmp_path.iterdir())
    failure = json.loads((run / "failure.json").read_text(encoding="utf-8"))
    assert failure["message"]
    assert "AssertionError" in failure["traceback"]
    assert failure["worker_state"]["status"] == "alive_and_preserved"


def test_preserved_server_context_does_not_stop_live_process(tmp_path):
    class Process:
        pid = 12345

        def poll(self):
            return None

    server = OwnedServer(
        Path(__import__("sys").executable),
        tmp_path / "config.yaml",
        8767,
        tmp_path / "server.log",
    )
    server.process = Process()
    server._log_stream = io.StringIO()
    server.bind_matching_run(tmp_path / "state.db", 1, "run-live")
    server.preserve_matching_run()
    server.__exit__(AssertionError, AssertionError(), None)
    assert server.process is not None
    assert server.process.poll() is None
    assert server._log_stream is None


def test_dead_owned_run_recovers_interrupted_and_releases_lease(
    config, monkeypatch
):
    from csv_to_clonehero.database import Database

    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(
            workspace_key="dead-owned-run",
            name="Dead owned run",
            status="running",
            phase="finding_charts",
            metadata={"run_id": "dead-run"},
        )
        db.update_workspace_progress(
            workspace,
            status="running",
            phase="finding_charts",
            metadata={"run_id": "dead-run"},
        )
    finally:
        db.close()
    server = OwnedServer(
        Path(__import__("sys").executable),
        config.base_dir / "config.yaml",
        65500,
        config.base_dir / "server.log",
    )
    server.bind_matching_run(config.database_path, workspace, "dead-run")
    monkeypatch.setattr(
        "scripts.local_journey_e2e.listener_details",
        lambda _port: {"port": 65500},
    )
    state = _recover_bound_run_if_dead(server)
    assert state["status"] == "terminated_by_harness_and_recovered"
    assert state["matching_lease_released"] is True
    db = Database(config.database_path)
    try:
        assert db.get_workspace(workspace)["status"] == "interrupted"
    finally:
        db.close()


def test_intentional_real_process_stop_recovers_durable_run(tmp_path):
    import httpx
    from csv_to_clonehero.database import Database

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    artifacts = AcceptanceArtifacts(tmp_path / "intentional-stop")
    config_path, database, _catalog = _acceptance_config(
        artifacts,
        target="isolated",
        source_config_path=DEFAULT_CONFIG,
        real_database=DEFAULT_REAL_DB,
        real_catalog=DEFAULT_REAL_CATALOG,
    )
    server = OwnedServer(
        resolve_python(),
        config_path,
        port,
        artifacts.root / "server.log",
    ).start()
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            upload = client.post(
                server.base_url + "/api/sources/soundcloud/import",
                files={
                    "file": (
                        "acceptance.csv",
                        b"artist,title\nAcceptance Artist,Acceptance Song\n",
                        "text/csv",
                    )
                },
            )
            assert upload.status_code == 202
            deadline = __import__("time").monotonic() + 10
            journey = None
            while __import__("time").monotonic() < deadline:
                journey = client.get(server.base_url + "/api/journey").json()
                if journey["screen"] == "music_ready":
                    break
            assert journey["screen"] == "music_ready"
            start = _start_matching_http(
                client,
                server.base_url,
                int(journey["workspace_id"]),
            )
        server.bind_matching_run(
            database,
            int(journey["workspace_id"]),
            str(start["run_id"]),
        )
        server.stop()
        state = _recover_bound_run_if_dead(server)
        assert state["status"] == "terminated_by_harness_and_recovered"
        assert state["matching_lease_released"] is True
        assert_port_free(port)
        db = Database(database)
        try:
            assert db.get_workspace(int(journey["workspace_id"]))["status"] == "interrupted"
        finally:
            db.close()
    finally:
        if server.is_alive():
            server.stop()


def test_live_matching_lease_blocks_probe_then_releases(tmp_path):
    path = tmp_path / "matching.lock"
    with RealStateLease(path):
        assert _lease_is_available(path) is False
    assert _lease_is_available(path) is True


def test_attach_mode_only_gets_and_observes_terminal_run(tmp_path, monkeypatch):
    snapshots = [
        _snapshot(revision="one", state="matching_running", processed=1, elapsed=1),
        _snapshot(revision="two", state="matching_running", processed=2, elapsed=2),
        _snapshot(revision="three", state="charts_found", processed=3, elapsed=3),
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            self.index = 0
            self.posts = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            if url.endswith("/healthz"):
                return Response({"app_surface": "journey"})
            value = snapshots[min(self.index, len(snapshots) - 1)]
            self.index += 1
            return Response(value)

        def post(self, *args, **kwargs):
            self.posts += 1
            pytest.fail("attach mode issued a POST")

    monkeypatch.setattr(
        "scripts.local_journey_e2e.listener_details",
        lambda _port: {
            "pid": 55,
            "port": 8767,
            "command": "python -m csv_to_clonehero web --port 8767",
        },
    )
    monkeypatch.setattr(
        "scripts.local_journey_e2e.local_web_processes",
        lambda: [{"pid": 55, "command": "python csv_to_clonehero web --port 8767"}],
    )
    monkeypatch.setattr(
        "scripts.local_journey_e2e._lease_is_available",
        lambda _path: False,
    )
    monkeypatch.setattr("scripts.local_journey_e2e.httpx.Client", Client)
    artifacts = AcceptanceArtifacts(tmp_path / "attach")
    args = SimpleNamespace(
        port=8767,
        attach_run_id="run-1",
        real_database=tmp_path / "state" / "db.sqlite",
        timeout=2.0,
        wait=True,
        observe_seconds=0.1,
        poll_interval=0.01,
    )
    summary = run_observe_existing(args, artifacts)
    assert summary["run_id"] == "run-1"
    assert summary["terminal"] is True
    assert summary["processed"] == 3
    assert summary["matching_started"] is False
    assert summary["server_preserved"] is True


def test_attach_mode_refuses_wrong_run(tmp_path, monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            if url.endswith("/healthz"):
                return Response({"app_surface": "journey"})
            return Response(_snapshot(run_id="actual-run"))

    monkeypatch.setattr(
        "scripts.local_journey_e2e.listener_details",
        lambda _port: {
            "pid": 55,
            "port": 8767,
            "command": "python -m csv_to_clonehero web --port 8767",
        },
    )
    monkeypatch.setattr(
        "scripts.local_journey_e2e.local_web_processes",
        lambda: [{"pid": 55, "command": "python csv_to_clonehero web --port 8767"}],
    )
    monkeypatch.setattr("scripts.local_journey_e2e.httpx.Client", Client)
    args = SimpleNamespace(
        port=8767,
        attach_run_id="wrong-run",
        real_database=tmp_path / "state" / "db.sqlite",
        timeout=1.0,
        wait=False,
        observe_seconds=0.1,
        poll_interval=0.01,
    )
    with pytest.raises(AcceptanceError, match="not requested run") as exc_info:
        run_observe_existing(args, AcceptanceArtifacts(tmp_path / "wrong"))
    assert exc_info.value.error_code == "OBSERVE_WRONG_RUN"


def test_real_cli_subprocess_owns_cleans_and_completes_journey(tmp_path):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert main(
        [
            "--matching",
            "--target",
            "isolated",
            "--no-browser",
            "--seed-legacy-failure",
            "--port",
            str(port),
            "--timeout",
            "60",
            "--artifacts-dir",
            str(tmp_path),
        ]
    ) == 0
    assert_port_free(port)
    runs = list(tmp_path.iterdir())
    assert len(runs) == 1
    run = runs[0]
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["matching"]["matching_state"] == "charts_found"
    assert summary["matching"]["authoritative"] is True
    assert summary["matching"]["results"]["best_matches"] == 1
    assert summary["warm_restore"]["wall_seconds"] < 10
    assert summary["server_cleanup"] == "passed"
    assert (run / "server.log").is_file()
    assert (run / "server-restart.log").is_file()
    snapshots = [
        json.loads(line)
        for line in (run / "journey-snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    legacy = next(
        item
        for item in snapshots
        if item["run_id"] == "legacy-failure-without-diagnostic"
    )
    assert legacy["matching_state"] == "matching_failed"
    assert legacy["diagnostic_id"] == ""
    restoring = next(
        item
        for item in snapshots
        if item["matching_state"] == "matching_restoring"
    )
    assert restoring["run_id"] != legacy["run_id"]
    assert restoring["is_active_run"] is True
    assert any(item["matching_state"] == "charts_found" for item in snapshots)
    assert not any(item.get("results") and not item["authoritative"] for item in snapshots)
    correlation = json.loads(
        (run / "start-correlation.json").read_text(encoding="utf-8")
    )
    assert correlation["baseline"]["run_id"] == legacy["run_id"]
    assert correlation["expected_run_id"] == restoring["run_id"]
    assert correlation["acknowledged"] is True
