"""Bounded, restart-safe validation of catalog chart artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from csv_to_clonehero.catalog import load_enchor_catalog
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import SngValidation, validate_sng
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.sources.base import ChartSource, SourceError

VALIDATOR_VERSION = str(SngValidation.__dataclass_fields__["validation_version"].default)


def inspected_difficulty_coverage(db: Database, chart: ChartCandidate) -> dict[str, Any]:
    """Expose only current, artifact-bound evidence from inspected note data.

    Instrument intensity ratings are not difficulty levels. Older validator
    observations and incomplete/corrupt count evidence remain unknown.
    """
    return inspected_difficulty_coverage_from_row(
        chart, db.get_chart_artifact_validation(chart.source, chart.chart_id)
    )


def inspected_difficulty_coverage_from_row(chart: ChartCandidate, row: Any) -> dict[str, Any]:
    """Use a joined ledger row without another query for each review card."""
    unknown = {
        "evidence": "unknown", "instruments": {}, "guitar_difficulties": [],
        "expert_only": None, "playable_note_count": None,
    }
    if row is None:
        return unknown
    data = dict(row)
    if (
        data.get("state") != "VERIFIED"
        or data.get("validator_version") != VALIDATOR_VERSION
        or not chart.md5
        or data.get("artifact_ref") != chart.md5
        or len(str(data.get("artifact_sha256") or "")) != 64
        or any(value not in "0123456789abcdef" for value in str(data.get("artifact_sha256") or "").casefold())
    ):
        return unknown
    try:
        validation = json.loads(str(data.get("validation_json") or "{}"))
    except (TypeError, ValueError):
        return unknown
    if not isinstance(validation, dict) or (
        validation.get("valid") is not True
        or validation.get("validation_version") != VALIDATOR_VERSION
    ):
        return unknown
    counts = validation.get("note_counts")
    if isinstance(counts, (list, tuple)):
        if len(counts) > 64:
            return unknown
        try:
            converted = dict(counts)
        except (TypeError, ValueError):
            return unknown
        if len(converted) != len(counts):
            return unknown
        counts = converted
    if not isinstance(counts, dict) or not counts or len(counts) > 64:
        return unknown
    instruments = {
        "guitar", "guitarcoop", "rhythm", "bass", "keys", "drums",
        "guitarghl", "guitarcoopghl", "rhythmghl", "bassghl", "keysghl", "vocals",
    }
    order = ("easy", "medium", "hard", "expert", "vocals")
    coverage: dict[str, set[str]] = {}
    total = 0
    for key, count in counts.items():
        if not isinstance(key, str) or key.count(":") != 1 or type(count) is not int or count <= 0:
            return unknown
        instrument, level = key.split(":")
        if instrument not in instruments or level not in order or (level == "vocals") != (instrument == "vocals"):
            return unknown
        coverage.setdefault(instrument, set()).add(level)
        total += count
    if total != data.get("playable_note_count") or total != validation.get("playable_note_count"):
        return unknown
    normalized = {
        instrument: [level for level in order if level in levels]
        for instrument, levels in sorted(coverage.items())
    }
    return {
        "evidence": "inspected_notes",
        "instruments": normalized,
        "guitar_difficulties": normalized.get("guitar", []),
        "expert_only": all(levels == ["expert"] for levels in normalized.values()),
        "playable_note_count": total,
    }


def difficulty_preference_key(coverage: dict[str, Any]) -> tuple[int, int]:
    """Prefer inspected multi-level guitar without excluding unknown charts."""
    levels = coverage.get("guitar_difficulties") or []
    known_multiple = coverage.get("evidence") == "inspected_notes" and len(levels) > 1
    return (-int(known_multiple), -len(levels) if known_multiple else 0)


def validation_summary(
    db: Database, *, catalog_snapshot_id: str = ""
) -> dict[str, int]:
    return db.get_chart_artifact_validation_counts(
        catalog_snapshot_id=catalog_snapshot_id
    )


def validate_catalog_artifacts(
    config: AppConfig,
    db: Database,
    sources: Iterable[ChartSource],
    *,
    states: Iterable[str] = ("UNVERIFIED",),
    limit: int = 100,
) -> dict[str, Any]:
    """Validate at most ``limit`` artifacts and checkpoint each ledger row."""
    limit = min(1000, max(1, int(limit)))
    catalog = load_enchor_catalog(config.catalog_path)
    if not catalog.available:
        raise ValueError(catalog.diagnostic)
    source_map = {str(source.name).casefold(): source for source in sources}
    catalog_snapshot_id = f"{catalog.snapshot_id}:{catalog.sha256}"
    synchronization = db.sync_catalog_artifacts(
        catalog_snapshot_id, catalog.iter_charts()
    )
    requested = {str(state).upper() for state in states}
    rows = db.list_chart_artifact_validations(
        states=requested,
        sources=source_map,
        catalog_snapshot_id=catalog_snapshot_id,
        limit=limit,
    )
    attempted = 0
    with tempfile.TemporaryDirectory(prefix="riff-catalog-validation-") as temporary:
        root = Path(temporary)
        for row in rows:
            identity = (str(row["source"]), int(row["chart_id"]))
            chart = catalog.get_chart(*identity)
            source = source_map.get(identity[0])
            if chart is None or source is None:
                continue
            attempted += 1
            destination = root / f"{identity[0]}-{identity[1]}.sng"
            try:
                source.download_chart(chart, destination)
                result = validate_sng(
                    destination,
                    require_five_fret_guitar=config.instruments.require_five_fret_guitar,
                )
                if result.valid:
                    db.record_chart_artifact_validation(
                        *identity,
                        artifact_ref=chart.md5 or "",
                        state="VERIFIED",
                        validator_version=VALIDATOR_VERSION,
                        artifact_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                        size_bytes=result.size_bytes,
                        format=result.format,
                        playable_note_count=result.playable_note_count,
                        validation=asdict(result),
                    )
                else:
                    db.record_chart_artifact_validation(
                        *identity,
                        artifact_ref=chart.md5 or "",
                        state="REJECTED",
                        validator_version=VALIDATOR_VERSION,
                        size_bytes=result.size_bytes,
                        format=result.format,
                        playable_note_count=result.playable_note_count,
                        validation=asdict(result),
                        reason_code="ARTIFACT_VALIDATION_FAILED",
                        reason=result.error,
                    )
            except SourceError as exc:
                unavailable = getattr(exc, "status", None) in {404, 410}
                db.record_chart_artifact_validation(
                    *identity,
                    artifact_ref=chart.md5 or "",
                    state="UNAVAILABLE" if unavailable else "UNVERIFIED",
                    reason_code="SOURCE_UNAVAILABLE" if unavailable else "TRANSIENT_DOWNLOAD_FAILURE",
                    reason=str(exc),
                    http_status=getattr(exc, "status", None),
                )
            except Exception as exc:  # noqa: BLE001 - transient evidence stays unverified
                db.record_chart_artifact_validation(
                    *identity,
                    artifact_ref=chart.md5 or "",
                    state="UNVERIFIED",
                    reason_code="TRANSIENT_DOWNLOAD_FAILURE",
                    reason=str(exc),
                )
            finally:
                destination.unlink(missing_ok=True)
    return {
        "attempted": attempted,
        "limit": limit,
        "states": validation_summary(db, catalog_snapshot_id=catalog_snapshot_id),
        "catalog_snapshot_id": catalog_snapshot_id,
        "synchronization": synchronization,
    }
