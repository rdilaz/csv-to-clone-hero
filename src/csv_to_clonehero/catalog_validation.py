"""Bounded, restart-safe validation of catalog chart artifacts."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from csv_to_clonehero.catalog import load_enchor_catalog
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import SngValidation, validate_sng
from csv_to_clonehero.sources.base import ChartSource, SourceError

VALIDATOR_VERSION = str(SngValidation.__dataclass_fields__["validation_version"].default)


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
