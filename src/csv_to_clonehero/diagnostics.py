"""Redacted, durable diagnostics for local workflow runs."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MAX_SAMPLE = 50
MAX_REASONS = 200
_WRITE_LOCK = threading.RLock()
_SECRET_TEXT_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|client_secret|code_verifier|authorization|cookie|token|secret|code)\s*[=:]\s*)([^&\s,;]+)"
)
_JSON_SECRET_LINE_RE = re.compile(
    r'''(?im)((?:\\?["'])?(?:access_token|refresh_token|client_secret|code_verifier|authorization|cookie|token|secret|code)(?:\\?["'])?\s*:\s*\\?["']).*$'''
)
_JSON_SECRET_RE = re.compile(
    r'''(?i)((?:\\?["'])?(?:access_token|refresh_token|client_secret|code_verifier|authorization|cookie|token|secret|code)(?:\\?["'])?\s*:\s*)(?P<quote>\\?["'])(.*?)(?P=quote)'''
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"
)
_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[^\s,]+")


def redact_diagnostic_text(value: Any) -> str:
    """Remove OAuth-shaped values without truncating traceback context."""
    text = str(value or "")
    text = _JSON_SECRET_LINE_RE.sub(r"\1[REDACTED]", text)
    text = _JSON_SECRET_RE.sub(
        lambda match: (
            f"{match.group(1)}{match.group('quote')}"
            f"[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_TEXT_RE.sub(r"\1[REDACTED]", text)
    return _BEARER_SECRET_RE.sub("Bearer [REDACTED]", text)


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items() if str(k).casefold() not in {"token", "access_token", "refresh_token", "secret", "cookie", "url", "audio_url"}}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value[:MAX_SAMPLE]]
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(
    path: Path,
    data: Mapping[str, Any],
    *,
    required: bool = False,
) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(data), indent=2, sort_keys=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    with _WRITE_LOCK:
        temporary.write_text(payload, encoding="utf-8")
        try:
            for attempt in range(8):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 7:
                        if required:
                            raise
                        return False
                    time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return True


class RunDiagnostics:
    """Small JSON artifact, updated atomically after each workflow phase."""

    def __init__(self, state_dir: Path | str, run_id: str | None = None) -> None:
        self.root = Path(state_dir) / "runs"
        self.run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(5)}"
        self.path = self.root / self.run_id / "diagnostics.json"
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("run_id") == self.run_id:
                    self.data = loaded
                    self.data.setdefault("events", [])
                    self.data.setdefault("counters", {})
                    self.data.setdefault("samples", {"tracks": [], "lookups": []})
                    return
            except (OSError, ValueError, TypeError):
                pass
        self.data: dict[str, Any] = {
            "run_id": self.run_id, "started_at": time.time(), "status": "running",
            "events": [], "counters": {}, "samples": {"tracks": [], "lookups": []},
        }
        self._write()

    def update(self, phase: str, **values: Any) -> None:
        counters = values.pop("counters", None)
        samples = values.pop("samples", None)
        if counters:
            self.data["counters"].update(_safe(counters))
        if samples:
            for name, items in samples.items():
                target = self.data["samples"].setdefault(name, [])
                target.extend(_safe(items))
                del target[MAX_SAMPLE:]
        self.data.update(_safe(values))
        self.event(phase, values)

    def event(self, phase: str, values: Mapping[str, Any] | None = None) -> None:
        self.data["events"].append({"ts": time.time(), "phase": str(phase), "data": _safe(values or {})})
        self.data["events"] = self.data["events"][-MAX_SAMPLE:]
        self._write()

    def finish(self, status: str = "complete", **values: Any) -> None:
        counters = values.pop("counters", None)
        if counters:
            self.data["counters"].update(_safe(counters))
        self.data.update(_safe(values))
        self.data["status"] = status
        self.data["finished_at"] = time.time()
        self._write()

    def _write(self) -> None:
        # Windows can reject replacing the destination while a status request
        # still has the previous JSON file open. A unique temp file avoids
        # collisions between writers; retries keep that transient read lock
        # from aborting the actual discovery job.
        _write_json(self.path, self.data)


def write_journey_failure(
    state_dir: Path | str,
    diagnostic_id: str,
    *,
    operation: str,
    error_code: str,
    run_id: str,
    workspace_id: int | str,
    phase: str,
    failed_at: float,
    error: BaseException,
    state_summary: Mapping[str, Any],
    environment: Mapping[str, Any],
) -> Path:
    """Write one private, redacted background-operation failure artifact."""
    if not re.fullmatch(r"[a-z0-9]{8,64}", diagnostic_id):
        raise ValueError("invalid diagnostic id")
    path = Path(state_dir) / "diagnostics" / f"journey-{diagnostic_id}.json"
    traceback_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    _write_json(
        path,
        {
            "schema_version": 1,
            "kind": "journey-background-failure",
            "diagnostic_id": diagnostic_id,
            "operation": operation,
            "error_code": error_code,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "phase": phase,
            "failed_at": failed_at,
            "failed_at_utc": datetime.fromtimestamp(
                failed_at, timezone.utc
            ).isoformat(),
            "exception": {
                "type": type(error).__name__,
                "message": redact_diagnostic_text(error),
                "traceback": redact_diagnostic_text(traceback_text),
            },
            "state_summary": state_summary,
            "environment": environment,
        },
        required=True,
    )
    return path


def diagnostics_path(state_dir: Path | str, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("invalid run id")
    return Path(state_dir) / "runs" / run_id / "diagnostics.json"
