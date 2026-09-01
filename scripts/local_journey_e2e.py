#!/usr/bin/env python3
"""Real-process acceptance runner for the local Riff journey.

The isolated target is deterministic and offline. The real target never starts
matching unless --allow-cold-real-run is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx
import yaml


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPOSITORY / "config.yaml"
DEFAULT_REAL_DB = REPOSITORY / "state" / "csv_to_clonehero.db"
DEFAULT_REAL_CATALOG = REPOSITORY / "catalog" / "enchor.jsonl"
ACTIVE_MATCHING_STATES = {"matching_restoring", "matching_running"}
TERMINAL_MATCHING_STATES = {
    "charts_found",
    "matching_failed",
    "matching_interrupted",
}
MATCHING_START_NOT_OBSERVABLE = "MATCHING_START_NOT_OBSERVABLE"
MATCHING_START_RESPONSE_INVALID = "MATCHING_START_RESPONSE_INVALID"
MATCHING_WORKER_FAILURE_CODE = "MATCHING_WORKER_FAILED"
STALE_MATCHING_RUN_RESPONSE = "STALE_MATCHING_RUN_RESPONSE"
LEGACY_FAILURE_RUN_ID = "legacy-failure-without-diagnostic"
EXPECTED_REAL_COUNTS = {
    "canonical_count": 5031,
    "best_matches": 568,
    "best_available": 2592,
    "recommendations": 3160,
    "no_suitable": 1768,
    "not_playable": 12,
    "no_candidate": 91,
}
OLD_UI_MARKERS = (
    "WHAT'S HAPPENING",
    "READY TO FIND CHARTS",
    "Preview chart matches",
    "Review your matches",
    "Review and export charts",
    "globalOperation",
    "source-neutral-mode",
    "PROTOTYPE_2_FIXTURES",
    "Visual Prototype 2",
)
FIXTURE_MARKERS = (
    "James Tillman",
    "8m 42s",
    "842 / 3,160",
    "731 downloaded",
)
SECRET_ENVIRONMENT_KEYS = {
    "APP_BASE_URL",
    "DATABASE_URL",
    "SECRET_KEY",
    "TOKEN_ENCRYPTION_KEY",
    "NOTIFICATIONS_API_KEY",
    "GOFUNDME_URL",
    "SPOTIFY_CLIENT_ID",
    "SOUNDCLOUD_CLIENT_ID",
    "SOUNDCLOUD_CLIENT_SECRET",
    "CSV_TO_CLONEHERO_LAN_TOKEN",
    "WERKZEUG_RUN_MAIN",
    "FLASK_DEBUG",
    "FLASK_ENV",
}
_ARTIFACT_SECRET_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|client_secret|authorization|cookie|token|secret|code)\s*[=:]\s*)([^&\s,;]+)"
)
_ARTIFACT_JSON_SECRET_LINE_RE = re.compile(
    r'''(?im)((?:\\?["'])?(?:access_token|refresh_token|client_secret|authorization|cookie|token|secret|code)(?:\\?["'])?\s*:\s*\\?["']).*$'''
)
_ARTIFACT_JSON_SECRET_RE = re.compile(
    r'''(?i)((?:\\?["'])?(?:access_token|refresh_token|client_secret|authorization|cookie|token|secret|code)(?:\\?["'])?\s*:\s*)(?P<quote>\\?["'])(.*?)(?P=quote)'''
)
_ARTIFACT_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:access-token|refresh-token|client-secret|token|secret)\s+)([^\s]+)"
)
_ARTIFACT_AUTH_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"
)


class AcceptanceError(RuntimeError):
    """An acceptance contract was not satisfied."""

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str = "ACCEPTANCE_FAILED",
        phase: str = "acceptance",
    ) -> None:
        safe_message = str(message or "").strip() or (
            "The acceptance contract failed without an error message."
        )
        self.error_code = str(error_code or "ACCEPTANCE_FAILED")
        self.phase = str(phase or "acceptance")
        super().__init__(safe_message)


class PortInUseError(AcceptanceError):
    """The requested listener is not owned by this harness."""


class MatchingStartRejected(AcceptanceError):
    """The real start endpoint rejected the request synchronously."""

    def __init__(self, status_code: int, payload: Mapping[str, Any]) -> None:
        self.status_code = int(status_code)
        self.payload = dict(payload)
        super().__init__(
            f"Find Charts returned HTTP {self.status_code}: "
            f"{json.dumps(_json_safe(self.payload), sort_keys=True)[:500]}",
            error_code="MATCHING_START_REJECTED",
            phase="matching_start",
        )


def _exception_details(
    error: BaseException,
    *,
    phase: str = "acceptance",
) -> dict[str, Any]:
    exception_type = type(error).__name__
    message = str(error or "").strip()
    if isinstance(error, AcceptanceError):
        error_code = error.error_code
        failure_phase = error.phase
    elif isinstance(error, KeyboardInterrupt):
        error_code = "HARNESS_INTERRUPTED"
        failure_phase = phase
        message = message or "The acceptance harness was interrupted before completion."
    elif isinstance(error, TimeoutError):
        error_code = "HARNESS_TIMEOUT"
        failure_phase = phase
        message = message or "The acceptance harness timed out."
    elif isinstance(error, AssertionError):
        error_code = "HARNESS_ASSERTION_FAILED"
        failure_phase = phase
        message = message or "An acceptance assertion failed without a message."
    else:
        error_code = "HARNESS_UNEXPECTED_ERROR"
        failure_phase = phase
        message = message or (
            f"The acceptance harness failed with {exception_type}."
        )
    return {
        "error_code": error_code,
        "phase": failure_phase,
        "exception_type": exception_type,
        "message": message,
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _redact_artifact_text(value: Any) -> str:
    text = str(value or "")
    text = _ARTIFACT_JSON_SECRET_LINE_RE.sub(r"\1[REDACTED]", text)
    text = _ARTIFACT_JSON_SECRET_RE.sub(
        lambda match: (
            f"{match.group(1)}{match.group('quote')}"
            f"[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _ARTIFACT_AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _ARTIFACT_CLI_SECRET_RE.sub(r"\1[REDACTED]", text)
    return _ARTIFACT_SECRET_RE.sub(r"\1[REDACTED]", text)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(child)
            for key, child in value.items()
            if not any(
                marker in str(key).casefold()
                for marker in ("token", "secret", "cookie", "authorization")
            )
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_artifact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _asset_version() -> str:
    source = (REPOSITORY / "src" / "csv_to_clonehero" / "webapp.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^JOURNEY_ASSET_VERSION\s*=\s*"([^"]+)"', source, re.M)
    if not match:
        raise AcceptanceError("could not resolve JOURNEY_ASSET_VERSION")
    return match.group(1)


def resolve_python(requested: str | None = None) -> Path:
    candidates = []
    if requested:
        candidates.append(Path(requested))
    if os.name == "nt":
        candidates.append(REPOSITORY / ".venv" / "Scripts" / "python.exe")
    else:
        candidates.append(REPOSITORY / ".venv" / "bin" / "python")
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise AcceptanceError(
        "repository virtualenv Python was not found; run "
        'python -m pip install -e ".[dev]" first'
    )


def listener_details(port: int) -> dict[str, Any]:
    details: dict[str, Any] = {"host": "127.0.0.1", "port": int(port)}
    if os.name != "nt":
        return details
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$c=Get-NetTCPConnection -State Listen -LocalPort "
            f"{int(port)} -ErrorAction SilentlyContinue; "
            "if($c){$p=Get-CimInstance Win32_Process -Filter "
            "\"ProcessId=$($c[0].OwningProcess)\"; "
            "[pscustomobject]@{pid=$c[0].OwningProcess;"
            "parent_pid=$p.ParentProcessId;command=$p.CommandLine}" 
            "| ConvertTo-Json -Compress}"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.stdout.strip():
            details.update(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return details


def local_web_processes() -> list[dict[str, Any]]:
    """Return existing real local-app processes without matching this runner."""
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "@(Get-CimInstance Win32_Process | Where-Object { "
                "$_.Name -match '^python(?:w)?(.exe)?$' -and "
                "$_.CommandLine -match 'csv_to_clonehero.*\\sweb(?:\\s|$)' } | "
                "ForEach-Object { [pscustomobject]@{pid=$_.ProcessId;"
                "parent_pid=$_.ParentProcessId;command=$_.CommandLine} }) "
                "| ConvertTo-Json -Compress"
            ),
        ]
    else:
        command = ["ps", "-eo", "pid=,ppid=,args="]
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if os.name == "nt":
        if not result.stdout.strip():
            return []
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return []
        values = payload if isinstance(payload, list) else [payload]
        return [dict(value) for value in values if isinstance(value, Mapping)]
    processes = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) == 3 and re.search(
            r"csv_to_clonehero.*\sweb(?:\s|$)", fields[2]
        ):
            processes.append(
                {"pid": int(fields[0]), "parent_pid": int(fields[1]), "command": fields[2]}
            )
    return processes


def assert_port_free(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", int(port)))
    except OSError as exc:
        details = listener_details(port)
        raise PortInUseError(
            f"127.0.0.1:{port} already has a listener: "
            f"{json.dumps(details, sort_keys=True)}"
        ) from exc
    finally:
        probe.close()


def sqlite_backup(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_db = sqlite3.connect(source_uri, uri=True, timeout=30)
    target_db = sqlite3.connect(destination)
    try:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise AcceptanceError(f"SQLite backup integrity check failed: {result}")
    finally:
        target_db.close()
        source_db.close()
    return destination


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AcceptanceError(f"configuration file does not exist: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise AcceptanceError("configuration root must be an object")
    return value


def _configured_path(config_path: Path, config: Mapping[str, Any], key: str, default: str) -> Path:
    value = str((config.get("paths") or {}).get(key) or default)
    path = Path(os.path.expandvars(value)).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def _write_reduced_catalog(path: Path) -> None:
    candidate = {
        "source": "enchor",
        "chart_id": 990000001,
        "md5": "00000000000000000000000099000001",
        "name": "Acceptance Song",
        "artist": "Acceptance Artist",
        "name_norm": "acceptance song",
        "artist_norm": "acceptance artist",
        "instruments": ["guitar", "bass", "drums"],
        "diff_guitar": 3,
        "charter": "Acceptance Harness",
    }
    body = (
        json.dumps(
            {"type": "chart", "candidate": candidate},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    header = {
        "type": "enchor-catalog",
        "format_version": 1,
        "complete": True,
        "snapshot_id": "journey-e2e-reduced-v1",
        "chart_count": 1,
        "out_of": 1,
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        + body
    )


def validate_catalog(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AcceptanceError(f"catalog does not exist: {path}")
    digest = hashlib.sha256()
    count = 0
    identities: set[tuple[str, int]] = set()
    with path.open("rb") as stream:
        header_line = stream.readline()
        header = json.loads(header_line)
        if (
            not isinstance(header, dict)
            or header.get("type") != "enchor-catalog"
            or header.get("complete") is not True
            or header.get("format_version") != 1
        ):
            raise AcceptanceError("catalog header is incomplete or unsupported")
        for raw_line in stream:
            digest.update(raw_line)
            if not raw_line.strip():
                continue
            row = json.loads(raw_line)
            candidate = row.get("candidate") if isinstance(row, dict) else None
            if row.get("type") != "chart" or not isinstance(candidate, dict):
                raise AcceptanceError(f"catalog row {count + 2} is malformed")
            identity = (str(candidate.get("source") or ""), int(candidate.get("chart_id") or 0))
            if not identity[0] or not identity[1] or identity in identities:
                raise AcceptanceError(f"catalog row {count + 2} has an invalid identity")
            identities.add(identity)
            count += 1
    expected = str(header.get("sha256") or "")
    if digest.hexdigest() != expected:
        raise AcceptanceError("catalog checksum does not match its manifest")
    declared = int(header.get("chart_count") or header.get("out_of") or 0)
    if declared != count:
        raise AcceptanceError(
            f"catalog declares {declared} charts but contains {count}"
        )
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "snapshot_id": str(header.get("snapshot_id") or ""),
        "chart_count": count,
        "sha256": expected,
        "valid": True,
    }


def _safe_child_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in SECRET_ENVIRONMENT_KEYS
    }
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


@dataclass
class AcceptanceArtifacts:
    root: Path
    snapshots_path: Path = field(init=False)
    summary_path: Path = field(init=False)
    failure_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.snapshots_path = self.root / "journey-snapshots.jsonl"
        self.summary_path = self.root / "summary.json"
        self.failure_path = self.root / "failure.json"

    def append_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        with self.snapshots_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_json_safe(snapshot), sort_keys=True) + "\n")

    def summary(self, value: Mapping[str, Any]) -> None:
        _write_json(self.summary_path, value)

    def start_correlation(
        self,
        value: Mapping[str, Any],
        *,
        name: str = "start-correlation.json",
    ) -> None:
        _write_json(self.root / name, value)

    def failure(
        self,
        error: BaseException,
        context: Mapping[str, Any],
        *,
        phase: str = "acceptance",
        worker_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        details = _exception_details(error, phase=phase)
        record = {
            **details,
            "context": context,
            "worker_state": dict(worker_state or {}),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.failure_path.is_file():
            try:
                existing = json.loads(
                    self.failure_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError):
                existing = {}
            if isinstance(existing, dict):
                existing.setdefault("subsequent_failures", []).append(record)
                _write_json(self.failure_path, existing)
                return record
        _write_json(self.failure_path, record)
        return record


@dataclass
class RealStateLease:
    """Cross-process lease for one acceptance runner per real state tree."""

    path: Path
    stream: Any = None

    def __enter__(self) -> "RealStateLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise AcceptanceError(
                f"another acceptance runner owns real state: {self.path}"
            ) from exc
        self.stream = stream
        return self

    def __exit__(self, *_args: Any) -> None:
        stream = self.stream
        self.stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def create_artifacts(mode: str, requested: Path | None = None) -> AcceptanceArtifacts:
    parent = requested or REPOSITORY / "state" / "acceptance"
    parent.mkdir(parents=True, exist_ok=True)
    return AcceptanceArtifacts(parent / f"{_utc_stamp()}-{os.getpid()}-{mode}")


@dataclass
class JourneySnapshotReducer:
    """Validate and deduplicate the small journey acceptance projection."""

    strict_failure_diagnostic: bool = False
    seen_revisions: set[str] = field(default_factory=set)
    last_revision: str = ""
    last_signature: str = ""
    run_progress: dict[str, int] = field(default_factory=dict)
    run_elapsed: dict[str, float] = field(default_factory=dict)
    authoritative_totals: dict[str, int] = field(default_factory=dict)
    identity_salt: bytes = field(default_factory=lambda: os.urandom(32), repr=False)

    @staticmethod
    def project(
        snapshot: Mapping[str, Any],
        *,
        identity_salt: bytes | None = None,
    ) -> dict[str, Any]:
        matching = snapshot.get("matching") or {}
        operation = snapshot.get("operation") or {}
        current_song = matching.get("current_song")
        current_song_identity = None
        if isinstance(current_song, Mapping):
            identity = json.dumps(
                {
                    "artist": str(current_song.get("artist") or ""),
                    "title": str(current_song.get("title") or ""),
                },
                sort_keys=True,
            ).encode("utf-8")
            current_song_identity = {"present": True}
            if identity_salt is not None:
                current_song_identity["identity_sha256"] = hashlib.sha256(
                    identity_salt + identity
                ).hexdigest()
        return {
            "observed_at": time.time(),
            "revision": str(snapshot.get("revision") or ""),
            "screen": str(snapshot.get("screen") or ""),
            "operation_state": str(operation.get("state") or ""),
            "matching_state": str(matching.get("state") or ""),
            "run_id": str(matching.get("run_id") or ""),
            "is_active_run": bool(matching.get("is_active_run")),
            "is_newest_run": bool(matching.get("is_newest_run")),
            "generation": str(matching.get("generation") or ""),
            "candidate_generation": matching.get("candidate_generation"),
            "processed": int(matching.get("processed") or 0),
            "total": int(matching.get("total") or 0),
            "current_song": current_song_identity,
            "started_at": matching.get("started_at"),
            "elapsed_seconds": round(float(matching.get("elapsed_seconds") or 0.0), 3),
            "error_code": str(matching.get("error_code") or ""),
            "diagnostic_id": str(matching.get("diagnostic_id") or ""),
            "retry_available": bool(matching.get("retry_available")),
            "authoritative": bool(matching.get("authoritative")),
            "results": matching.get("results"),
            "canonical_count": int((snapshot.get("music") or {}).get("canonical_count") or 0),
        }

    def reduce(self, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        projected = self.project(snapshot, identity_salt=self.identity_salt)
        revision = projected["revision"]
        if not revision:
            raise AcceptanceError("journey snapshot has no revision")
        if revision != self.last_revision and revision in self.seen_revisions:
            raise AcceptanceError(f"journey revision moved backward to {revision}")
        self.seen_revisions.add(revision)
        self.last_revision = revision

        run_id = projected["run_id"]
        if run_id:
            previous_progress = self.run_progress.get(run_id, -1)
            if projected["processed"] < previous_progress:
                raise AcceptanceError(
                    f"processed moved backward in run {run_id}: "
                    f"{previous_progress} -> {projected['processed']}"
                )
            self.run_progress[run_id] = projected["processed"]
            previous_elapsed = self.run_elapsed.get(run_id, -1.0)
            if projected["elapsed_seconds"] + 0.05 < previous_elapsed:
                raise AcceptanceError(
                    f"elapsed reset in run {run_id}: "
                    f"{previous_elapsed} -> {projected['elapsed_seconds']}"
                )
            self.run_elapsed[run_id] = projected["elapsed_seconds"]
            if projected["authoritative"]:
                prior_total = self.authoritative_totals.setdefault(run_id, projected["total"])
                if projected["total"] != prior_total:
                    raise AcceptanceError(
                        f"authoritative total changed in run {run_id}: "
                        f"{prior_total} -> {projected['total']}"
                    )

        if projected["matching_state"] in ACTIVE_MATCHING_STATES:
            if projected["authoritative"] or projected["results"] is not None:
                raise AcceptanceError("a running generation exposed an old final result")
        if (
            projected["matching_state"] == "matching_failed"
            and self.strict_failure_diagnostic
            and not projected["diagnostic_id"]
        ):
            raise AcceptanceError("matching failure has no diagnostic ID")

        encoded = json.dumps(projected, sort_keys=True)
        for marker in FIXTURE_MARKERS:
            if marker in encoded:
                raise AcceptanceError(f"fixture value appeared in journey state: {marker}")
        signature_value = dict(projected)
        signature_value.pop("observed_at", None)
        signature = json.dumps(signature_value, sort_keys=True)
        if signature == self.last_signature:
            return None
        self.last_signature = signature
        return projected


@dataclass
class ExpectedRunContract:
    """Correlate journey reads to one accepted matching start."""

    expected_run_id: str
    baseline_run_id: str
    baseline_revision: str
    acknowledged: bool = False
    observed: list[dict[str, Any]] = field(default_factory=list)

    def observe(
        self,
        snapshot: Mapping[str, Any],
        reducer: JourneySnapshotReducer,
        artifacts: AcceptanceArtifacts,
    ) -> str:
        projected_for_evidence = reducer.project(
            snapshot,
            identity_salt=reducer.identity_salt,
        )
        evidence_signature = dict(projected_for_evidence)
        evidence_signature.pop("observed_at", None)
        previous_signature = None
        if self.observed:
            previous_signature = dict(self.observed[-1])
            previous_signature.pop("observed_at", None)
        if evidence_signature != previous_signature:
            self.observed.append(projected_for_evidence)
        run_id = projected_for_evidence["run_id"]
        state = projected_for_evidence["matching_state"]
        if self.acknowledged and run_id != self.expected_run_id:
            raise AcceptanceError(
                f"{STALE_MATCHING_RUN_RESPONSE}: expected {self.expected_run_id}, "
                f"observed {run_id or '<none>'} in {state or '<none>'}"
            )

        projected = reducer.reduce(snapshot)
        if projected is not None:
            artifacts.append_snapshot(projected)
        if run_id != self.expected_run_id:
            return "older_run_ignored"
        if state in ACTIVE_MATCHING_STATES:
            self.acknowledged = True
            return "expected_run_active"
        if not self.acknowledged:
            return "expected_run_not_active"
        if state in TERMINAL_MATCHING_STATES:
            if (
                state == "matching_failed"
                and projected_for_evidence["error_code"]
                == MATCHING_WORKER_FAILURE_CODE
                and not projected_for_evidence["diagnostic_id"]
            ):
                raise AcceptanceError(
                    "expected matching worker failure has no diagnostic ID"
                )
            return "expected_run_terminal"
        return "expected_run_nonterminal"


@dataclass
class OwnedServer:
    python: Path
    config_path: Path
    port: int
    log_path: Path
    process: subprocess.Popen[Any] | None = None
    _log_stream: Any = None
    preserve_live_run: bool = False
    preserved_run_id: str = ""
    matching_workspace_id: int | None = None
    matching_database: Path | None = None
    matching_terminal: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 30.0) -> "OwnedServer":
        assert_port_free(self.port)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_stream = self.log_path.open("w", encoding="utf-8")
        command = [
            str(self.python),
            "-m",
            "csv_to_clonehero",
            "--config",
            str(self.config_path),
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-browser",
        ]
        kwargs: dict[str, Any] = {
            "cwd": REPOSITORY,
            "env": _safe_child_environment(),
            "stdin": subprocess.DEVNULL,
            "stdout": self._log_stream,
            "stderr": subprocess.STDOUT,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.process = subprocess.Popen(command, **kwargs)
            deadline = time.monotonic() + timeout
            client = httpx.Client(timeout=2, trust_env=False)
            try:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise AcceptanceError(
                            f"server exited during startup with code {self.process.returncode}; "
                            f"see {self.log_path}"
                        )
                    try:
                        response = client.get(self.base_url + "/healthz")
                        if response.status_code == 200:
                            payload = response.json()
                            expected = {
                                "ok": True,
                                "app_surface": "journey",
                                "application_surface": "journey",
                                "schema_version": 1,
                            }
                            if payload != expected:
                                raise AcceptanceError(
                                    f"listener is not the accepted journey: {payload}"
                                )
                            details = listener_details(self.port)
                            listener_command = str(details.get("command") or "")
                            if os.name == "nt" and not details.get("pid"):
                                raise AcceptanceError(
                                    "could not identify the healthy listener process"
                                )
                            owned_listener = bool(
                                int(details.get("pid") or 0) == self.process.pid
                                or int(details.get("parent_pid") or 0)
                                == self.process.pid
                            )
                            if listener_command and (
                                not owned_listener
                                or str(self.config_path) not in listener_command
                                or f"--port {self.port}" not in listener_command
                            ):
                                raise AcceptanceError(
                                    "healthy listener is not owned by this server process: "
                                    f"{json.dumps(details, sort_keys=True)}"
                                )
                            return self
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
            finally:
                client.close()
            raise AcceptanceError(
                f"server did not become healthy; see {self.log_path}"
            )
        except BaseException:
            try:
                self.stop()
            except BaseException:
                pass
            raise

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            if os.name == "nt":
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=15,
                    )
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired as exc:
                    raise AcceptanceError(
                        f"owned server PID {process.pid} did not stop"
                    ) from exc
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        self.process = None

    def bind_matching_run(
        self,
        database: Path,
        workspace_id: int,
        run_id: str,
    ) -> None:
        self.matching_database = Path(database)
        self.matching_workspace_id = int(workspace_id)
        self.preserved_run_id = str(run_id)
        self.matching_terminal = False

    def preserve_matching_run(self) -> None:
        if self.preserved_run_id:
            self.preserve_live_run = True

    def mark_matching_terminal(self) -> None:
        self.matching_terminal = True
        self.preserve_live_run = False

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def preservation_snapshot(self) -> dict[str, Any]:
        details = listener_details(self.port)
        return {
            "status": (
                "alive_and_preserved"
                if self.preserve_live_run and self.is_alive()
                else "already_terminal"
                if self.matching_terminal
                else "server_not_alive"
            ),
            "run_id": self.preserved_run_id,
            "workspace_id": self.matching_workspace_id,
            "pid": self.process.pid if self.process is not None else None,
            "port": self.port,
            "listener": details,
            "database": str(self.matching_database or ""),
        }

    def _close_parent_log(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None

    def __enter__(self) -> "OwnedServer":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        if self.preserve_live_run and self.is_alive():
            self._close_parent_log()
            return
        self.stop()


def _lease_is_available(path: Path) -> bool:
    if not path.is_file():
        return True
    stream = path.open("a+b")
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True
    except (OSError, BlockingIOError):
        return False
    finally:
        stream.close()


def _recover_bound_run_if_dead(server: OwnedServer) -> dict[str, Any]:
    state = server.preservation_snapshot()
    if (
        not server.preserved_run_id
        or server.matching_workspace_id is None
        or server.matching_database is None
        or server.matching_terminal
    ):
        return state
    listener = listener_details(server.port)
    if server.is_alive() or listener.get("pid"):
        state["status"] = "alive_and_preserved"
        return state
    lease_path = server.matching_database.parent / "journey-matching.lock"
    if not _lease_is_available(lease_path):
        state["status"] = "dead_process_but_matching_lease_owned"
        return state
    from csv_to_clonehero.database import Database

    db = Database(server.matching_database)
    try:
        recovered = db.recover_discovery_run(
            server.matching_workspace_id,
            server.preserved_run_id,
        )
    finally:
        db.close()
    state["status"] = (
        "terminated_by_harness_and_recovered"
        if recovered
        else "dead_run_already_recovered"
    )
    state["durable_recovery"] = bool(recovered)
    state["matching_lease_released"] = _lease_is_available(lease_path)
    return state


def _acceptance_config(
    artifacts: AcceptanceArtifacts,
    *,
    target: str,
    source_config_path: Path,
    real_database: Path,
    real_catalog: Path,
) -> tuple[Path, Path, Path]:
    workspace = artifacts.root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    if target == "real":
        database = real_database
        catalog = real_catalog
        state_dir = real_database.parent
    else:
        database = workspace / "state" / "journey-e2e.db"
        catalog = workspace / "catalog" / "reduced.jsonl"
        state_dir = workspace / "state"
        _write_reduced_catalog(catalog)
    source = _read_config(source_config_path)
    selected = {
        key: source[key]
        for key in (
            "matching",
            "instruments",
            "search",
            "network",
            "cache",
            "scan",
        )
        if isinstance(source.get(key), Mapping)
    }
    selected.update(
        {
            "paths": {
                "input_dir": str(workspace / "input"),
                "output_dir": str(workspace / "output"),
                "clone_hero_songs_path": "",
                "reports_dir": str(workspace / "reports"),
                "database": str(database),
                "catalog": str(catalog),
                "state_dir": str(state_dir),
                "artifact_cache_dir": str(workspace / "export-artifacts"),
                "manifest_dir": str(workspace / "export-manifests"),
            },
            "spotify": {
                "client_id": "",
                "client_id_env": "JOURNEY_E2E_UNUSED_SPOTIFY_CLIENT_ID",
            },
            "sources": {"enabled": []},
            "build": {"dry_run": True, "direct_download_enabled": False},
        }
    )
    path = workspace / "acceptance-config.yaml"
    path.write_text(yaml.safe_dump(selected, sort_keys=True), encoding="utf-8")
    return path, database, catalog


def verify_journey_surface(client: httpx.Client, base_url: str) -> dict[str, Any]:
    health = client.get(base_url + "/healthz")
    health.raise_for_status()
    if health.json().get("app_surface") != "journey":
        raise AcceptanceError("health endpoint is not the journey application")
    response = client.get(base_url + "/")
    response.raise_for_status()
    html = response.text
    version = _asset_version()
    required = (
        'data-app-surface="journey"',
        'data-test="journey-loading"',
        "Restoring your library...",
        f"journey.css?v={version}",
        f"journey.js?v={version}",
    )
    for value in required:
        if value not in html:
            raise AcceptanceError(f"accepted root is missing {value!r}")
    for marker in (*OLD_UI_MARKERS, *FIXTURE_MARKERS):
        if marker in html:
            raise AcceptanceError(f"retired or fixture UI marker was served: {marker}")
    if "/advanced" in html:
        raise AcceptanceError("accepted root contains an /advanced navigation")
    if response.headers.get("Cache-Control") != "no-store":
        raise AcceptanceError("accepted root is not served with Cache-Control: no-store")
    journey_response = client.get(base_url + "/api/journey")
    journey_response.raise_for_status()
    journey = journey_response.json()
    if (
        journey.get("schema_version") != 1
        or journey.get("app_surface") != "journey"
        or journey.get("application_surface") != "journey"
        or not isinstance(journey.get("matching"), dict)
        or not isinstance(journey.get("music"), dict)
        or not isinstance(journey.get("operation"), dict)
    ):
        raise AcceptanceError("journey API schema or identity is invalid")
    return journey


def _record(
    reducer: JourneySnapshotReducer,
    artifacts: AcceptanceArtifacts,
    snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    projected = reducer.reduce(snapshot)
    if projected is not None:
        artifacts.append_snapshot(projected)
    return projected


def _poll(
    client: httpx.Client,
    base_url: str,
    reducer: JourneySnapshotReducer,
    artifacts: AcceptanceArtifacts,
    *,
    timeout: float,
    terminal: Iterable[str] = TERMINAL_MATCHING_STATES,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    terminal_states = set(terminal)
    while time.monotonic() < deadline:
        response = client.get(base_url + "/api/journey")
        response.raise_for_status()
        snapshot = response.json()
        _record(reducer, artifacts, snapshot)
        state = str((snapshot.get("matching") or {}).get("state") or "")
        if state in terminal_states:
            return snapshot
        time.sleep(0.25)
    raise AcceptanceError(f"journey did not reach a terminal state within {timeout}s")


def _poll_music_ready(
    client: httpx.Client,
    base_url: str,
    reducer: JourneySnapshotReducer,
    artifacts: AcceptanceArtifacts,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(base_url + "/api/journey").json()
        _record(reducer, artifacts, snapshot)
        if snapshot.get("screen") == "music_ready":
            return snapshot
        if (snapshot.get("operation") or {}).get("state") == "attention":
            raise AcceptanceError("isolated source import failed")
        time.sleep(0.1)
    raise AcceptanceError("isolated source import did not reach music_ready")


def _assert_partition(snapshot: Mapping[str, Any], expected: Mapping[str, int] | None) -> None:
    music = snapshot.get("music") or {}
    matching = snapshot.get("matching") or {}
    results = matching.get("results") or {}
    canonical = int(music.get("canonical_count") or 0)
    partition = sum(
        int(results.get(key) or 0)
        for key in (
            "best_matches",
            "best_available",
            "no_suitable",
            "not_playable",
            "no_candidate",
        )
    )
    if partition != canonical:
        raise AcceptanceError(
            f"result partition {partition} does not equal canonical count {canonical}"
        )
    if int(results.get("recommendations") or 0) != int(
        results.get("best_matches") or 0
    ) + int(results.get("best_available") or 0):
        raise AcceptanceError("recommendation count is not best plus available")
    if expected:
        actual = {"canonical_count": canonical, **{key: int(results.get(key) or 0) for key in expected if key != "canonical_count"}}
        if actual != dict(expected):
            raise AcceptanceError(f"real result counts changed: {actual}")


def _browser_start(
    base_url: str,
    artifacts: AcceptanceArtifacts,
    workspace_id: int,
) -> dict[str, Any]:
    install = [
        f'& "{resolve_python()}" -m pip install -e ".[browser-e2e]"',
        f'& "{resolve_python()}" -m playwright install chromium',
    ]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"available": False, "reason": "Python Playwright is not installed", "install": install}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            return {"available": False, "reason": f"Chromium is unavailable: {exc}", "install": install}
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        requested_paths: list[str] = []
        page.on("request", lambda request: requested_paths.append(request.url))
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.add_style_tag(
            content=(
                ".current-check strong { font-size: 0 !important; }"
                ".current-check strong::after { content: '[REDACTED CURRENT SONG]'; "
                "font-size: 1rem !important; }"
            )
        )
        page.wait_for_selector(
            '[data-test="music-ready"], [data-test="matching_failed"], '
            '[data-test="matching_interrupted"]',
            timeout=15000,
        )
        html = page.content()
        for marker in OLD_UI_MARKERS:
            if marker in html:
                browser.close()
                raise AcceptanceError(f"browser rendered retired marker: {marker}")
        page.screenshot(path=str(artifacts.root / "before-find.png"), full_page=True)
        button = page.locator('[data-test="find-charts"]')
        if button.count() == 0:
            button = page.locator('[data-test="retry-matching"]')
        if button.count() != 1:
            browser.close()
            raise AcceptanceError("browser could not identify one Find Charts action")
        with page.expect_response(
            lambda response: (
                response.request.method == "GET"
                and response.url.split("?", 1)[0].endswith("/api/journey")
            ),
            timeout=15000,
        ) as journey_response_info:
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/discovery/start")
                ),
                timeout=15000,
            ) as response_info:
                button.click()
        start_response = response_info.value
        try:
            start_payload = start_response.json()
        except Exception:
            start_payload = {"error": start_response.text()[:500]}
        if not isinstance(start_payload, Mapping):
            start_payload = {"error": "matching start response was not an object"}
        if start_response.status != 202:
            browser.close()
            raise MatchingStartRejected(start_response.status, start_payload)
        accepted_start = _validate_accepted_start(start_payload, workspace_id)
        journey_response = journey_response_info.value
        try:
            journey_snapshot = journey_response.json()
        except Exception as exc:
            browser.close()
            raise AcceptanceError(
                f"browser journey acknowledgement was not JSON: {exc}"
            ) from exc
        journey_matching = journey_snapshot.get("matching") or {}
        if (
            str(journey_matching.get("run_id") or "")
            != str(accepted_start["run_id"])
            or str(journey_matching.get("state") or "")
            not in ACTIVE_MATCHING_STATES
        ):
            browser.close()
            raise AcceptanceError(
                f"{MATCHING_START_NOT_OBSERVABLE}: browser journey response "
                "did not expose the accepted run"
            )
        page.wait_for_selector(
            '[data-test="matching_restoring"], [data-test="matching_running"]',
            timeout=15000,
        )
        if (
            str(journey_matching.get("state") or "") == "matching_restoring"
            and page.locator("text=Checking saved match results...").count() == 0
        ):
            browser.close()
            raise AcceptanceError("browser did not render saved-result checking")
        if any("/advanced" in url for url in requested_paths):
            browser.close()
            raise AcceptanceError("browser navigated or requested /advanced")
        page.screenshot(path=str(artifacts.root / "after-find.png"), full_page=True)
        browser.close()
    return {
        "available": True,
        "passed": True,
        "requested_advanced": False,
        "start_response": accepted_start,
        "journey_snapshot": journey_snapshot,
    }


def _browser_terminal(base_url: str, artifacts: AcceptanceArtifacts, state: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            return
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url + "/", wait_until="domcontentloaded")
        selector = '[data-test="charts-found"]' if state == "charts_found" else '[data-test="matching_failed"]'
        page.wait_for_selector(selector, timeout=15000)
        if state == "matching_failed":
            detail = page.locator(".failure-reference")
            if detail.count() == 0:
                browser.close()
                raise AcceptanceError("accepted failure UI has no diagnostic details")
        page.screenshot(path=str(artifacts.root / f"terminal-{state}.png"), full_page=True)
        browser.close()


def _validate_accepted_start(
    payload: Mapping[str, Any],
    workspace_id: int,
) -> dict[str, Any]:
    value = dict(payload)
    if (
        value.get("started") is not True
        or not str(value.get("run_id") or "")
        or int(value.get("workspace_id") or 0) != int(workspace_id)
        or str(value.get("state") or "") != "matching_restoring"
    ):
        raise AcceptanceError(
            f"{MATCHING_START_RESPONSE_INVALID}: "
            f"{json.dumps(_json_safe(value), sort_keys=True)[:1000]}"
        )
    return value


def _start_matching_http(
    client: httpx.Client,
    base_url: str,
    workspace_id: int,
    *,
    warm_only: bool = False,
) -> dict[str, Any]:
    response = client.post(
        base_url + "/api/discovery/start",
        json={
            "source": "combined",
            "workspace_id": workspace_id,
            "warm_only": warm_only,
        },
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text[:500]}
    if not isinstance(payload, Mapping):
        payload = {"error": "matching start response was not an object"}
    if response.status_code != 202:
        raise MatchingStartRejected(response.status_code, payload)
    return _validate_accepted_start(payload, workspace_id)


def _server_log_tail(path: Path, limit: int = 16000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[server log unavailable: {exc}]"
    return text[-limit:]


def _matching_lease_path(server: OwnedServer) -> Path:
    config = _read_config(server.config_path)
    state_dir = _configured_path(
        server.config_path,
        config,
        "state_dir",
        "state",
    )
    return state_dir / "journey-matching.lock"


def _capture_start_runtime(server: OwnedServer) -> dict[str, Any]:
    lease_path = _matching_lease_path(server)
    return {
        "server_pid": server.process.pid if server.process is not None else None,
        "server_return_code": (
            server.process.poll() if server.process is not None else None
        ),
        "listener": listener_details(server.port),
        "local_web_processes": local_web_processes(),
        "matching_lease": {
            "path": str(lease_path),
            "exists": lease_path.is_file(),
        },
        "server_log_tail": _server_log_tail(server.log_path),
    }


def _seed_legacy_matching_failure(database: Path, workspace_id: int) -> None:
    """Create production-shaped legacy failure history in isolated state."""
    now = time.time()
    metadata = {
        "workflow": "source_neutral",
        "run_id": LEGACY_FAILURE_RUN_ID,
        "scan_key": "catalog",
        "generation": f"journey-{LEGACY_FAILURE_RUN_ID}",
        "started_at": now - 2.0,
        "finished_at": now - 1.0,
        "error": "Legacy matching result was not authoritative.",
    }
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(
                "UPDATE discovery_workspaces SET status=?,phase=?,metadata_json=?,"
                "updated_at=?,completed_at=? WHERE id=?",
                (
                    "failed",
                    "result_validation",
                    json.dumps(metadata, sort_keys=True),
                    now,
                    now,
                    int(workspace_id),
                ),
            )
            connection.execute(
                "UPDATE discovery_progress SET status=?,phase=?,message=?,error=?,"
                "metadata_json=?,updated_at=? WHERE workspace_id=?",
                (
                    "failed",
                    "result_validation",
                    "Legacy chart finding did not finish.",
                    "We could not finish finding charts. Your music is safe.",
                    json.dumps(metadata, sort_keys=True),
                    now,
                    int(workspace_id),
                ),
            )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("local_current_run_id", LEGACY_FAILURE_RUN_ID),
            )
    finally:
        connection.close()


def _persist_start_correlation(
    artifacts: AcceptanceArtifacts,
    evidence: dict[str, Any],
    contract: ExpectedRunContract | None,
    server: OwnedServer,
    *,
    name: str,
    outcome: str | None = None,
) -> None:
    if contract is not None:
        evidence["acknowledged"] = contract.acknowledged
        evidence["observed"] = contract.observed
    if outcome:
        evidence["outcome"] = outcome
    if outcome not in {
        "matching_start_accepted",
        "expected_run_acknowledged",
    }:
        evidence["runtime"] = _capture_start_runtime(server)
    evidence["updated_at"] = datetime.now(timezone.utc).isoformat()
    artifacts.start_correlation(evidence, name=name)


def _await_expected_run(
    client: httpx.Client,
    server: OwnedServer,
    reducer: JourneySnapshotReducer,
    artifacts: AcceptanceArtifacts,
    contract: ExpectedRunContract,
    evidence: dict[str, Any],
    *,
    timeout: float,
    correlation_name: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(server.base_url + "/api/journey")
        response.raise_for_status()
        snapshot = response.json()
        outcome = contract.observe(snapshot, reducer, artifacts)
        if outcome == "expected_run_active":
            _persist_start_correlation(
                artifacts,
                evidence,
                contract,
                server,
                name=correlation_name,
                outcome="expected_run_acknowledged",
            )
            return snapshot
        time.sleep(0.1)
    _persist_start_correlation(
        artifacts,
        evidence,
        contract,
        server,
        name=correlation_name,
        outcome=MATCHING_START_NOT_OBSERVABLE,
    )
    raise AcceptanceError(
        f"{MATCHING_START_NOT_OBSERVABLE}: accepted run "
        f"{contract.expected_run_id} was not observed in restoring/running "
        f"within {timeout:.1f}s"
    )


def _poll_expected_terminal(
    client: httpx.Client,
    server: OwnedServer,
    reducer: JourneySnapshotReducer,
    artifacts: AcceptanceArtifacts,
    contract: ExpectedRunContract,
    evidence: dict[str, Any],
    *,
    timeout: float,
    correlation_name: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(server.base_url + "/api/journey")
        response.raise_for_status()
        snapshot = response.json()
        try:
            outcome = contract.observe(snapshot, reducer, artifacts)
        except AcceptanceError as exc:
            failure_outcome = (
                STALE_MATCHING_RUN_RESPONSE
                if str(exc).startswith(STALE_MATCHING_RUN_RESPONSE)
                else "EXPECTED_RUN_CONTRACT_FAILED"
            )
            _persist_start_correlation(
                artifacts,
                evidence,
                contract,
                server,
                name=correlation_name,
                outcome=failure_outcome,
            )
            raise
        if outcome == "expected_run_terminal":
            _persist_start_correlation(
                artifacts,
                evidence,
                contract,
                server,
                name=correlation_name,
                outcome="expected_run_terminal",
            )
            return snapshot
        time.sleep(0.25)
    _persist_start_correlation(
        artifacts,
        evidence,
        contract,
        server,
        name=correlation_name,
        outcome="EXPECTED_RUN_DID_NOT_FINISH",
    )
    raise AcceptanceError(
        f"expected run {contract.expected_run_id} did not reach a terminal "
        f"state within {timeout:.1f}s"
    )


def _run_matching_cycle(
    server: OwnedServer,
    artifacts: AcceptanceArtifacts,
    *,
    target: str,
    database: Path,
    timeout: float,
    start_ack_timeout: float,
    browser: bool,
    seed_legacy_failure: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    browser_result: dict[str, Any] = {"requested": browser, "available": False}
    with httpx.Client(timeout=30, trust_env=False) as client:
        initial = verify_journey_surface(client, server.base_url)
        _record(reducer, artifacts, initial)
        if target == "isolated":
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
            if upload.status_code != 202:
                raise AcceptanceError(
                    f"isolated music import returned {upload.status_code}: {upload.text[:500]}"
                )
            initial = _poll_music_ready(client, server.base_url, reducer, artifacts)
            if seed_legacy_failure:
                _seed_legacy_matching_failure(
                    database,
                    int(initial["workspace_id"]),
                )
                initial = client.get(server.base_url + "/api/journey").json()
                _record(reducer, artifacts, initial)
        if initial.get("screen") not in {
            "music_ready",
            "matching_failed",
            "matching_interrupted",
        }:
            raise AcceptanceError(
                f"Find Charts is not available from screen {initial.get('screen')!r}"
            )
        baseline = reducer.project(initial, identity_salt=reducer.identity_salt)
        evidence: dict[str, Any] = {
            "baseline": baseline,
            "post_response": None,
            "expected_run_id": "",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if browser:
                browser_result = _browser_start(
                    server.base_url,
                    artifacts,
                    int(initial["workspace_id"]),
                )
            if browser_result.get("available"):
                start_response = dict(browser_result["start_response"])
            else:
                start_response = _start_matching_http(
                    client,
                    server.base_url,
                    int(initial["workspace_id"]),
                )
        except MatchingStartRejected as exc:
            evidence["post_response"] = {
                "status_code": exc.status_code,
                "body": exc.payload,
            }
            _persist_start_correlation(
                artifacts,
                evidence,
                None,
                server,
                name="start-correlation.json",
                outcome="MATCHING_START_REJECTED",
            )
            raise
        expected_run_id = str(start_response["run_id"])
        server.bind_matching_run(
            database,
            int(initial["workspace_id"]),
            expected_run_id,
        )
        evidence["post_response"] = {
            "status_code": 202,
            "body": start_response,
        }
        evidence["expected_run_id"] = expected_run_id
        contract = ExpectedRunContract(
            expected_run_id=expected_run_id,
            baseline_run_id=str(baseline.get("run_id") or ""),
            baseline_revision=str(baseline.get("revision") or ""),
        )
        _persist_start_correlation(
            artifacts,
            evidence,
            contract,
            server,
            name="start-correlation.json",
            outcome="matching_start_accepted",
        )
        browser_snapshot = browser_result.get("journey_snapshot")
        browser_acknowledged = False
        if isinstance(browser_snapshot, Mapping):
            browser_acknowledged = (
                contract.observe(browser_snapshot, reducer, artifacts)
                == "expected_run_active"
            )
        if browser_acknowledged:
            _persist_start_correlation(
                artifacts,
                evidence,
                contract,
                server,
                name="start-correlation.json",
                outcome="expected_run_acknowledged",
            )
        else:
            _await_expected_run(
                client,
                server,
                reducer,
                artifacts,
                contract,
                evidence,
                timeout=start_ack_timeout,
                correlation_name="start-correlation.json",
            )
        if target == "real":
            server.preserve_matching_run()
        terminal = _poll_expected_terminal(
            client,
            server,
            reducer,
            artifacts,
            contract,
            evidence,
            timeout=timeout,
            correlation_name="start-correlation.json",
        )
        server.mark_matching_terminal()
        state = str((terminal.get("matching") or {}).get("state") or "")
        if state == "matching_failed":
            matching = terminal["matching"]
            if matching.get("error_code") == MATCHING_WORKER_FAILURE_CODE:
                diagnostic_id = str(matching.get("diagnostic_id") or "")
                diagnostic_path = (
                    _matching_lease_path(server).parent
                    / "diagnostics"
                    / f"journey-{diagnostic_id}.json"
                )
                if not diagnostic_id or not diagnostic_path.is_file():
                    raise AcceptanceError(
                        "expected matching worker failure has no private "
                        "diagnostic artifact"
                    )
                try:
                    diagnostic_payload = json.loads(
                        diagnostic_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError) as exc:
                    raise AcceptanceError(
                        "expected matching worker diagnostic artifact is unreadable"
                    ) from exc
                if str(diagnostic_payload.get("run_id") or "") != expected_run_id:
                    raise AcceptanceError(
                        "matching worker diagnostic belongs to a different run"
                    )
            artifacts.failure(
                AcceptanceError(matching.get("error") or "matching failed"),
                {
                    "error_code": matching.get("error_code"),
                    "diagnostic_id": matching.get("diagnostic_id"),
                    "failure_phase": matching.get("failure_phase"),
                    "expected_run_id": expected_run_id,
                },
            )
            _browser_terminal(server.base_url, artifacts, state)
            raise AcceptanceError(
                "matching failed: "
                f"{matching.get('error_code')} diagnostic={matching.get('diagnostic_id')}"
            )
        if state != "charts_found":
            raise AcceptanceError(f"matching stopped in {state}")
        _assert_partition(
            terminal,
            EXPECTED_REAL_COUNTS if target == "real" else None,
        )
        # Slices 3-5 extend here: cohort choice, row/version mutations,
        # preflight, bounded build, cancellation/resume, and folder opening.
        _browser_terminal(server.base_url, artifacts, state)
        return terminal, browser_result


def _run_warm_restore(
    server: OwnedServer,
    artifacts: AcceptanceArtifacts,
    *,
    target: str,
    timeout: float = 60.0,
    start_ack_timeout: float = 15.0,
) -> dict[str, Any]:
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    with httpx.Client(timeout=30, trust_env=False) as client:
        restored = verify_journey_surface(client, server.base_url)
        _record(reducer, artifacts, restored)
        if (restored.get("matching") or {}).get("state") != "charts_found":
            raise AcceptanceError("restart did not restore directly to Charts Found")
        before = dict(restored["matching"]["results"])
        baseline = reducer.project(restored, identity_salt=reducer.identity_salt)
        started = time.monotonic()
        evidence: dict[str, Any] = {
            "baseline": baseline,
            "post_response": None,
            "expected_run_id": "",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "warm_only": True,
        }
        try:
            start_response = _start_matching_http(
                client,
                server.base_url,
                int(restored["workspace_id"]),
                warm_only=True,
            )
        except MatchingStartRejected as exc:
            evidence["post_response"] = {
                "status_code": exc.status_code,
                "body": exc.payload,
            }
            _persist_start_correlation(
                artifacts,
                evidence,
                None,
                server,
                name="warm-start-correlation.json",
                outcome="MATCHING_START_REJECTED",
            )
            raise
        expected_run_id = str(start_response["run_id"])
        evidence["post_response"] = {
            "status_code": 202,
            "body": start_response,
        }
        evidence["expected_run_id"] = expected_run_id
        contract = ExpectedRunContract(
            expected_run_id=expected_run_id,
            baseline_run_id=str(baseline.get("run_id") or ""),
            baseline_revision=str(baseline.get("revision") or ""),
        )
        _await_expected_run(
            client,
            server,
            reducer,
            artifacts,
            contract,
            evidence,
            timeout=start_ack_timeout,
            correlation_name="warm-start-correlation.json",
        )
        terminal = _poll_expected_terminal(
            client,
            server,
            reducer,
            artifacts,
            contract,
            evidence,
            timeout=timeout,
            correlation_name="warm-start-correlation.json",
        )
        wall_seconds = time.monotonic() - started
        if (terminal.get("matching") or {}).get("state") != "charts_found":
            raise AcceptanceError("second Find did not return to Charts Found")
        if terminal["matching"]["results"] != before:
            raise AcceptanceError("warm restore changed the authoritative result")
        if not terminal["matching"]["details"].get("saved_work_reuse"):
            raise AcceptanceError("second Find did not report completed fast-path reuse")
        if wall_seconds > timeout:
            raise AcceptanceError(f"warm restore exceeded {timeout}s")
        _assert_partition(
            terminal,
            EXPECTED_REAL_COUNTS if target == "real" else None,
        )
        return {"wall_seconds": round(wall_seconds, 3), "result": before}


def _real_database_summary(
    database: Path,
    config_path: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        workspace = connection.execute(
            "SELECT * FROM discovery_workspaces WHERE workspace_key="
            "(SELECT value FROM meta WHERE key='local_workspace_key')"
        ).fetchone()
        if workspace is None:
            raise AcceptanceError("real database has no local journey workspace")
        workspace_id = int(workspace["id"])
        progress = connection.execute(
            "SELECT * FROM discovery_progress WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        generation = connection.execute(
            "SELECT * FROM discovery_scans WHERE workspace_id=? AND kind='exact' "
            "AND json_extract(metadata_json,'$.workflow')='combined_catalog' "
            "ORDER BY id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        partition_rows = connection.execute(
            "SELECT status,classification,COUNT(*) AS songs "
            "FROM discovery_song_progress WHERE workspace_id=? "
            "GROUP BY status,classification ORDER BY status,classification",
            (workspace_id,),
        ).fetchall()
        canonical = connection.execute(
            "SELECT COUNT(*) FROM library_tracks t WHERE t.workspace_id=? AND EXISTS ("
            "SELECT 1 FROM library_track_provenance p JOIN library_sources s ON s.id=p.source_id "
            "WHERE p.library_track_id=t.id AND p.source_import_id=(SELECT MAX(i.id) "
            "FROM source_imports i WHERE i.source_id=s.id))",
            (workspace_id,),
        ).fetchone()[0]
        cache = connection.execute(
            "SELECT * FROM matching_scan_cache WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        cache_result = json.loads(cache["result_json"]) if cache is not None else {}
        current_scan_fingerprint = ""
        fingerprint_error = ""
        if config_path is not None and catalog_path is not None:
            try:
                from csv_to_clonehero.catalog import read_catalog_manifest
                from csv_to_clonehero.combined_pipeline import matching_scan_fingerprint
                from csv_to_clonehero.config import load_config

                app_config = load_config(config_path)
                manifest = read_catalog_manifest(catalog_path)
                current_scan_fingerprint = matching_scan_fingerprint(
                    (),
                    manifest,
                    app_config,
                    library_fingerprint=str(
                        workspace["library_matching_fingerprint"] or ""
                    ),
                )
            except Exception as exc:
                fingerprint_error = str(exc)[:500]
        digest_rows = connection.execute(
            "SELECT t.track_key,wc.candidate_id,wc.score,wc.quality,wc.decision,"
            "wc.eligibility,wc.breakdown_json,wc.metadata_json "
            "FROM workspace_candidates wc JOIN spotify_tracks t ON t.id=wc.track_id "
            "JOIN discovery_song_progress dsp ON dsp.workspace_id=wc.workspace_id "
            "AND dsp.track_key=t.track_key WHERE wc.workspace_id=? AND wc.kind='exact' "
            "ORDER BY t.track_key,wc.candidate_id,wc.score,wc.quality,wc.decision,"
            "wc.eligibility,wc.breakdown_json,wc.metadata_json",
            (workspace_id,),
        ).fetchall()
        candidate_digest = hashlib.sha256()
        for row in digest_rows:
            candidate_digest.update(
                json.dumps(tuple(row), sort_keys=True, default=str).encode("utf-8")
            )
            candidate_digest.update(b"\n")
        current_digest = candidate_digest.hexdigest()
        candidates = len(digest_rows)
        active_imports = connection.execute(
            "SELECT COUNT(*) FROM source_imports WHERE status IN ('running','pending')"
        ).fetchone()[0]
        active_exports = connection.execute(
            "SELECT COUNT(*) FROM export_records WHERE status IN "
            "('created','running','pending','downloading','retrying')"
        ).fetchone()[0]
        active_tasks = connection.execute(
            "SELECT COUNT(*) FROM discovery_tasks WHERE status IN ('running','pending')"
        ).fetchone()[0]
        active_workspaces = connection.execute(
            "SELECT COUNT(*) FROM discovery_workspaces WHERE status IN ('running','pending')"
        ).fetchone()[0]
        expected_candidates = int(cache_result.get("persisted_candidates", 0) or 0)
        expected_digest = str(cache_result.get("candidate_relationship_digest") or "")
        progress_total = sum(int(row["songs"] or 0) for row in partition_rows)
        progress_completed = sum(
            int(row["songs"] or 0)
            for row in partition_rows
            if str(row["status"] or "") == "complete"
        )
        classifications = {
            str(row["classification"] or ""): int(row["songs"] or 0)
            for row in partition_rows
            if str(row["status"] or "") == "complete"
        }
        classified = sum(
            classifications.get(key, 0)
            for key in (
                "strong",
                "review",
                "no_suitable",
                "not_playable",
                "no_catalog_candidate",
            )
        )
        generation_metadata = (
            json.loads(generation["metadata_json"]) if generation is not None else {}
        )
        generation_digest = str(
            generation_metadata.get("candidate_relationship_digest") or ""
        ) if isinstance(generation_metadata, Mapping) else ""
        workspace_status = str(workspace["status"] or "").casefold()
        progress_status = str(progress["status"] or "").casefold() if progress else ""
        generation_status = (
            str(generation["status"] or "").casefold() if generation else ""
        )
        active_matching = bool(
            workspace_status in {"running", "pending"}
            or progress_status in {"running", "pending"}
            or generation_status in {"running", "pending"}
            or active_tasks
            or active_workspaces
        )
        authoritative_shape = bool(
            generation is not None
            and generation_status in {"complete", "completed"}
            and int(canonical) > 0
            and progress_total == int(canonical)
            and progress_completed == int(canonical)
            and classified == int(canonical)
            and cache is not None
            and bool(current_scan_fingerprint)
            and str(cache["fingerprint"] or "") == current_scan_fingerprint
            and int(cache["track_count"] or 0) == int(canonical)
            and int(cache_result.get("tracks", -1)) == int(canonical)
            and int(candidates) == expected_candidates
            and bool(expected_digest)
            and current_digest == expected_digest
            and (not generation_digest or generation_digest == current_digest)
        )
        return {
            "database": str(database),
            "workspace_id": workspace_id,
            "workspace_status": str(workspace["status"] or ""),
            "workspace_phase": str(workspace["phase"] or ""),
            "progress_status": str(progress["status"] or "") if progress else "",
            "progress_phase": str(progress["phase"] or "") if progress else "",
            "safe_error": str(progress["error"] or "") if progress else "",
            "canonical_count": int(canonical),
            "latest_generation": {
                "id": int(generation["id"]),
                "scan_key": str(generation["scan_key"]),
                "status": str(generation["status"]),
            } if generation else None,
            "song_progress": [dict(row) for row in partition_rows],
            "candidate_relationships": int(candidates),
            "expected_candidate_relationships": expected_candidates,
            "candidate_relationship_digest": current_digest,
            "expected_candidate_relationship_digest": expected_digest,
            "matching_scan_cache": bool(cache),
            "matching_scan_fingerprint_current": bool(
                cache is not None
                and current_scan_fingerprint
                and str(cache["fingerprint"] or "") == current_scan_fingerprint
            ),
            "matching_scan_fingerprint_error": fingerprint_error,
            "matching_will_be_cold": not authoritative_shape,
            "active_matching": active_matching,
            "active_imports": int(active_imports),
            "active_exports": int(active_exports),
        }
    finally:
        connection.close()


def run_diagnose(args: argparse.Namespace, artifacts: AcceptanceArtifacts) -> dict[str, Any]:
    summary = _real_database_summary(
        args.real_database, args.config, args.real_catalog
    )
    summary.update(
        {
            "mode": "diagnose",
            "repository": str(REPOSITORY),
            "python": str(resolve_python(args.python)),
            "listener": listener_details(args.port),
            "existing_local_web_processes": local_web_processes(),
            "matching_started": False,
        }
    )
    if args.snapshot_real_db:
        copied = sqlite_backup(
            args.real_database,
            artifacts.root / "workspace" / "real-state-copy.db",
        )
        summary["sqlite_safe_copy"] = str(copied)
    artifacts.summary(summary)
    return summary


def run_observe_existing(
    args: argparse.Namespace,
    artifacts: AcceptanceArtifacts,
) -> dict[str, Any]:
    listener = listener_details(args.port)
    listener_pid = int(listener.get("pid") or 0)
    listener_command = str(listener.get("command") or "")
    if not listener_pid or "csv_to_clonehero" not in listener_command:
        raise AcceptanceError(
            f"no csv_to_clonehero server is listening on 127.0.0.1:{args.port}",
            error_code="OBSERVE_SERVER_NOT_FOUND",
            phase="observe_existing",
        )
    process_rows = [
        row
        for row in local_web_processes()
        if f"--port {args.port}" in str(row.get("command") or "")
    ]
    if not process_rows:
        raise AcceptanceError(
            "the listener has no matching csv_to_clonehero process tree",
            error_code="OBSERVE_PROCESS_IDENTITY_MISMATCH",
            phase="observe_existing",
        )
    reducer = JourneySnapshotReducer(strict_failure_diagnostic=False)
    with httpx.Client(timeout=30, trust_env=False) as client:
        health = client.get(f"http://127.0.0.1:{args.port}/healthz")
        health.raise_for_status()
        if health.json().get("app_surface") != "journey":
            raise AcceptanceError(
                "the listener is not the journey application",
                error_code="OBSERVE_SERVER_IDENTITY_MISMATCH",
                phase="observe_existing",
            )
        initial = client.get(
            f"http://127.0.0.1:{args.port}/api/journey"
        ).json()
        current_run_id = str((initial.get("matching") or {}).get("run_id") or "")
        expected_run_id = str(args.attach_run_id or current_run_id)
        if not expected_run_id:
            raise AcceptanceError(
                "the journey has no matching run to observe",
                error_code="OBSERVE_RUN_NOT_FOUND",
                phase="observe_existing",
            )
        if current_run_id != expected_run_id:
            raise AcceptanceError(
                f"listener exposes run {current_run_id or '<none>'}, not "
                f"requested run {expected_run_id}",
                error_code="OBSERVE_WRONG_RUN",
                phase="observe_existing",
            )
        matching_lease = args.real_database.parent / "journey-matching.lock"
        state = str((initial.get("matching") or {}).get("state") or "")
        if state in ACTIVE_MATCHING_STATES and _lease_is_available(matching_lease):
            raise AcceptanceError(
                "journey claims matching is active but no matching lease is owned",
                error_code="OBSERVE_LEASE_NOT_OWNED",
                phase="observe_existing",
            )
        deadline = time.monotonic() + (
            args.timeout if args.wait else args.observe_seconds
        )
        latest = initial
        terminal = False
        while True:
            run_id = str((latest.get("matching") or {}).get("run_id") or "")
            if run_id != expected_run_id:
                raise AcceptanceError(
                    f"observed run changed from {expected_run_id} to "
                    f"{run_id or '<none>'}",
                    error_code="OBSERVE_WRONG_RUN",
                    phase="observe_existing",
                )
            projected = _record(reducer, artifacts, latest)
            matching = latest.get("matching") or {}
            if projected is not None:
                print(
                    f"run={expected_run_id} state={matching.get('state')} "
                    f"processed={int(matching.get('processed') or 0)}/"
                    f"{int(matching.get('total') or 0)} "
                    f"elapsed={float(matching.get('elapsed_seconds') or 0.0):.1f}s"
                )
            if str(matching.get("state") or "") in TERMINAL_MATCHING_STATES:
                terminal = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.1, float(args.poll_interval)))
            latest = client.get(
                f"http://127.0.0.1:{args.port}/api/journey"
            ).json()
    summary = {
        "mode": "observe-existing",
        "run_id": expected_run_id,
        "state": str((latest.get("matching") or {}).get("state") or ""),
        "terminal": terminal,
        "processed": int((latest.get("matching") or {}).get("processed") or 0),
        "total": int((latest.get("matching") or {}).get("total") or 0),
        "elapsed_seconds": float(
            (latest.get("matching") or {}).get("elapsed_seconds") or 0.0
        ),
        "pid": listener_pid,
        "port": args.port,
        "listener": listener,
        "matching_lease_owned": not _lease_is_available(matching_lease),
        "matching_started": False,
        "server_preserved": True,
    }
    artifacts.summary(summary)
    return summary


def run_preflight(args: argparse.Namespace, artifacts: AcceptanceArtifacts) -> dict[str, Any]:
    assert_port_free(args.port)
    conflicts = local_web_processes()
    if conflicts:
        raise AcceptanceError(
            "a real local app process is already running; stop it before preflight: "
            f"{json.dumps(conflicts, sort_keys=True)}"
        )
    real = _real_database_summary(args.real_database, args.config, args.real_catalog)
    backups = sorted(
        (REPOSITORY / "state" / "private-backups").glob("*.db"),
        key=lambda path: path.stat().st_mtime,
    )
    if not backups:
        raise AcceptanceError("no SQLite-safe private backup exists")
    if real["canonical_count"] != EXPECTED_REAL_COUNTS["canonical_count"]:
        raise AcceptanceError(
            f"canonical count is {real['canonical_count']}, expected 5031"
        )
    if real["active_matching"] or real["active_imports"] or real["active_exports"]:
        raise AcceptanceError("matching, import, or export work is active")
    catalog = validate_catalog(args.real_catalog)
    config = _read_config(args.config)
    configured_output = _configured_path(
        args.config, config, "output_dir", "output_library"
    )
    smoke_config, _, _ = _acceptance_config(
        artifacts,
        target="isolated",
        source_config_path=args.config,
        real_database=args.real_database,
        real_catalog=args.real_catalog,
    )
    server = OwnedServer(
        resolve_python(args.python),
        smoke_config,
        args.port,
        artifacts.root / "server.log",
    )
    with server:
        with httpx.Client(timeout=20, trust_env=False) as client:
            verify_journey_surface(client, server.base_url)
    assert_port_free(args.port)
    summary = {
        "mode": "preflight",
        "ready": True,
        "repository": str(REPOSITORY),
        "python": str(resolve_python(args.python)),
        "real_database": str(args.real_database),
        "database_backup_exists": True,
        "latest_database_backup": str(backups[-1]),
        "server_can_start": True,
        "no_conflicting_process": True,
        "port": args.port,
        "port_free_after_smoke": True,
        "catalog": catalog,
        "canonical_count": real["canonical_count"],
        "no_active_matching_import_or_export": True,
        "configured_output": str(configured_output),
        "configured_output_write_planned": False,
        "matching_will_be_cold": bool(real["matching_will_be_cold"]),
        "expected_duration": "20-35 minutes",
        "authorization_required": "--allow-cold-real-run",
    }
    artifacts.summary(summary)
    return summary


def run_server_mode(args: argparse.Namespace, artifacts: AcceptanceArtifacts) -> dict[str, Any]:
    if args.target == "real" and args.mode == "matching" and not args.allow_cold_real_run:
        raise AcceptanceError(
            "refusing to touch real matching state without --allow-cold-real-run"
        )
    assert_port_free(args.port)
    if args.target == "real":
        conflicts = local_web_processes()
        if conflicts:
            raise AcceptanceError(
                "a real local app process is already running; stop it before "
                f"using real state: {json.dumps(conflicts, sort_keys=True)}"
            )
    config_path, database, catalog = _acceptance_config(
        artifacts,
        target=args.target,
        source_config_path=args.config,
        real_database=args.real_database,
        real_catalog=args.real_catalog,
    )
    if args.target == "real":
        preflight = _real_database_summary(database, config_path, catalog)
        backups = list((REPOSITORY / "state" / "private-backups").glob("*.db"))
        if not backups:
            raise AcceptanceError("real matching requires an existing private database backup")
        if preflight["canonical_count"] != EXPECTED_REAL_COUNTS["canonical_count"]:
            raise AcceptanceError(
                f"real canonical count is {preflight['canonical_count']}, expected 5031"
            )
        if (
            preflight["active_matching"]
            or preflight["active_imports"]
            or preflight["active_exports"]
        ):
            raise AcceptanceError(
                "real matching cannot start during matching/import/export work"
            )
        if args.mode == "warm-restore" and preflight["matching_will_be_cold"]:
            raise AcceptanceError(
                "warm restore requires an authoritative completed result; "
                "run the authorized cold acceptance first"
            )
        validate_catalog(catalog)
    server = OwnedServer(
        resolve_python(args.python),
        config_path,
        args.port,
        artifacts.root / "server.log",
    )
    summary: dict[str, Any] = {
        "mode": args.mode,
        "target": args.target,
        "repository": str(REPOSITORY),
        "python": str(resolve_python(args.python)),
        "database": str(database),
        "catalog": str(catalog),
        "port": args.port,
    }
    if args.mode == "matching":
        try:
            with server:
                terminal, browser = _run_matching_cycle(
                    server,
                    artifacts,
                    target=args.target,
                    database=database,
                    timeout=args.timeout,
                    start_ack_timeout=args.start_ack_timeout,
                    browser=not args.no_browser,
                    seed_legacy_failure=args.seed_legacy_failure,
                )
                summary["matching"] = JourneySnapshotReducer.project(terminal)
                summary["browser"] = browser
        except BaseException as exc:
            worker_state = _recover_bound_run_if_dead(server)
            setattr(exc, "acceptance_worker_state", worker_state)
            raise
        assert_port_free(args.port)
        restart = OwnedServer(
            resolve_python(args.python),
            config_path,
            args.port,
            artifacts.root / "server-restart.log",
        )
        with restart:
            summary["warm_restore"] = _run_warm_restore(
                restart,
                artifacts,
                target=args.target,
                start_ack_timeout=args.start_ack_timeout,
            )
        assert_port_free(args.port)
    else:
        with server:
            summary["warm_restore"] = _run_warm_restore(
                server,
                artifacts,
                target=args.target,
                start_ack_timeout=args.start_ack_timeout,
            )
        assert_port_free(args.port)
    summary["server_cleanup"] = "passed"
    artifacts.summary(summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--diagnose", dest="mode", action="store_const", const="diagnose")
    modes.add_argument("--preflight", dest="mode", action="store_const", const="preflight")
    modes.add_argument("--matching", dest="mode", action="store_const", const="matching")
    modes.add_argument("--warm-restore", dest="mode", action="store_const", const="warm-restore")
    modes.add_argument(
        "--observe-existing",
        dest="mode",
        action="store_const",
        const="observe-existing",
    )
    modes.add_argument("--attach-run", dest="attach_run_id", metavar="RUN_ID")
    parser.add_argument("--target", choices=("isolated", "real"), default="isolated")
    parser.add_argument("--allow-cold-real-run", action="store_true")
    parser.add_argument("--snapshot-real-db", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--timeout", type=float, default=2700.0)
    parser.add_argument("--start-ack-timeout", type=float, default=15.0)
    parser.add_argument("--observe-seconds", type=float, default=10.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--seed-legacy-failure", action="store_true")
    parser.add_argument("--python")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--real-database", type=Path, default=DEFAULT_REAL_DB)
    parser.add_argument("--real-catalog", type=Path, default=DEFAULT_REAL_CATALOG)
    parser.add_argument("--artifacts-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.config = args.config.expanduser().resolve()
    args.real_database = args.real_database.expanduser().resolve()
    args.real_catalog = args.real_catalog.expanduser().resolve()
    if args.attach_run_id:
        args.mode = "observe-existing"
    if args.mode in {"diagnose", "preflight"} and args.target != "real":
        raise SystemExit(f"--{args.mode} requires --target real")
    if args.seed_legacy_failure and (
        args.target != "isolated" or args.mode != "matching"
    ):
        raise SystemExit(
            "--seed-legacy-failure is only valid for isolated matching"
        )
    artifacts = create_artifacts(args.mode, args.artifacts_dir)
    try:
        def dispatch() -> dict[str, Any]:
            if args.mode == "diagnose":
                return run_diagnose(args, artifacts)
            if args.mode == "preflight":
                return run_preflight(args, artifacts)
            if args.mode == "observe-existing":
                return run_observe_existing(args, artifacts)
            return run_server_mode(args, artifacts)

        if args.target == "real" and args.mode not in {
            "diagnose",
            "observe-existing",
        }:
            with RealStateLease(
                args.real_database.parent / "journey-e2e-real.lock"
            ):
                summary = dispatch()
        else:
            summary = dispatch()
        print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
        print(f"Acceptance artifacts: {artifacts.root}")
        return 0
    except BaseException as exc:
        worker_state = getattr(exc, "acceptance_worker_state", {})
        record = artifacts.failure(
            exc,
            {
                "mode": args.mode,
                "target": args.target,
                "port": args.port,
                "artifacts": str(artifacts.root),
            },
            phase=f"{args.mode}:{args.target}",
            worker_state=worker_state,
        )
        print(
            "ACCEPTANCE FAILED "
            f"[{record['error_code']}/{record['exception_type']} "
            f"phase={record['phase']}]: {record['message']}",
            file=sys.stderr,
        )
        if worker_state.get("status") == "alive_and_preserved":
            print(
                "Matching is still running on "
                f"PID {worker_state.get('pid')} / port {worker_state.get('port')}; "
                "do not start another run.",
                file=sys.stderr,
            )
        print(f"Acceptance artifacts: {artifacts.root}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
