"""Bounded, revision-guarded selection receipts for the local journey only."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

MAX_UNDO_CHOICES = 10_000
MAX_UNDO_BYTES = 2_000_000
UNDO_TTL_SECONDS = 30 * 60


def _key(workspace: int, suffix: str) -> str:
    return f"local_journey_selection:{workspace}:{suffix}"


def choices(db: Any, workspace: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db._conn.execute(
        "SELECT * FROM workspace_selections WHERE workspace_id=? ORDER BY kind,track_id,candidate_id,id",
        (workspace,),
    ).fetchall()]


def generation(db: Any, workspace: int) -> str:
    row = db.get_latest_exact_generation(workspace)
    payload = {
        "generation": dict(row) if row is not None else None,
        "library": db.get_library_matching_fingerprint(workspace),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def revision(db: Any, workspace: int) -> str:
    values = [{key: value for key, value in row.items()
               if key not in {"id", "selected_at", "updated_at"}}
              for row in choices(db, workspace)]
    payload = [generation(db, workspace), db.get_meta(_key(workspace, "clock")), values]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def guard(db: Any, workspace: int, expected: Any) -> None:
    if not isinstance(expected, str) or expected != revision(db, workspace):
        raise ValueError("Chart choices changed. Refresh the selection before trying again.")


def remember(db: Any, workspace: int, before: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Save one exact-choice snapshot inside the caller's selection transaction."""
    operation_id = uuid.uuid4().hex
    db.set_meta(_key(workspace, "clock"), operation_id)
    record = {
        "operation_id": operation_id, "label": label,
        "generation": generation(db, workspace), "after_revision": revision(db, workspace),
        "expires_at": time.time() + UNDO_TTL_SECONDS, "before": before,
    }
    encoded = json.dumps(record, sort_keys=True, default=str)
    if len(before) > MAX_UNDO_CHOICES or len(encoded.encode()) > MAX_UNDO_BYTES:
        db.set_meta(_key(workspace, "undo"), "")
        return {"available": False, "reason": "This selection is too large for the local undo history."}
    db.set_meta(_key(workspace, "undo"), encoded)
    return undo_state(db, workspace)


def undo_record(db: Any, workspace: int) -> dict[str, Any] | None:
    try:
        record = json.loads(db.get_meta(_key(workspace, "undo")) or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    expires = record.get("expires_at")
    before = record.get("before")
    if (not isinstance(expires, (int, float)) or not math.isfinite(expires)
            or expires <= time.time() or not isinstance(before, list)
            or len(before) > MAX_UNDO_CHOICES or any(not isinstance(row, dict) for row in before)
            or not isinstance(record.get("operation_id"), str)
            or not isinstance(record.get("label"), str)
            or record.get("generation") != generation(db, workspace)
            or record.get("after_revision") != revision(db, workspace)):
        return None
    return record


def undo_state(db: Any, workspace: int) -> dict[str, Any]:
    record = undo_record(db, workspace)
    return {"available": record is not None, **({
        "operation_id": record["operation_id"], "label": record["label"],
        "expires_at": record["expires_at"],
    } if record else {})}


def clear_undo(db: Any, workspace: int) -> None:
    db.set_meta(_key(workspace, "undo"), "")
    db.set_meta(_key(workspace, "clock"), uuid.uuid4().hex)
