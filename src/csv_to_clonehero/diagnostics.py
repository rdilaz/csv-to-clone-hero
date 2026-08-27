"""Redacted, durable diagnostics for local workflow runs."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping


MAX_SAMPLE = 50
MAX_REASONS = 200


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items() if str(k).casefold() not in {"token", "access_token", "refresh_token", "secret", "cookie", "url", "audio_url"}}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value[:MAX_SAMPLE]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


def diagnostics_path(state_dir: Path | str, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("invalid run id")
    return Path(state_dir) / "runs" / run_id / "diagnostics.json"
