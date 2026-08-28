"""Download manager: streaming IO, .part temp files, atomic rename, checksum
validation, duplicate prevention and resume via the downloads table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.filesystem import is_within
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.sources.base import ChartSource, SourceError

log = logging.getLogger(__name__)


def _hash_of(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_of(path: Path) -> str:
    return _hash_of(path, "md5")


def _validation_payload(validation: object) -> str:
    return json.dumps(
        {
            "version": getattr(validation, "validation_version", ""),
            "format": getattr(validation, "format", ""),
            "chart_member": getattr(validation, "chart_member", ""),
            "playable_note_count": int(
                getattr(validation, "playable_note_count", 0) or 0
            ),
            "note_counts": dict(getattr(validation, "note_counts", ()) or ()),
        },
        sort_keys=True,
    )


class DownloadManager:
    """Resume and validate direct chart downloads for a local library build."""

    def __init__(self, config: AppConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.library_root = config.library_root

    def _validate_artifact(self, path: Path):
        from csv_to_clonehero.exporter import validate_sng

        inventory = getattr(self.config, "inventory", None)
        return validate_sng(
            path,
            max_members=int(getattr(inventory, "max_members", 4096) or 4096),
            max_member_bytes=int(
                getattr(inventory, "max_member_bytes", 512 * 1024 * 1024)
                or 512 * 1024 * 1024
            ),
            max_archive_bytes=int(
                getattr(inventory, "max_archive_bytes", 2 * 1024 * 1024 * 1024)
                or 2 * 1024 * 1024 * 1024
            ),
            max_compression_ratio=float(
                getattr(inventory, "max_compression_ratio", 10_000.0) or 10_000.0
            ),
            require_five_fret_guitar=bool(
                getattr(getattr(self.config, "instruments", None), "require_five_fret_guitar", True)
            ),
        )

    def already_downloaded(self, candidate_db_id: int) -> bool:
        """True when a previous run completed this chart and the file exists."""
        row = self.db.get_download_for_candidate(candidate_db_id)
        if row is None or row["status"] != "completed":
            return False
        if not row["path"]:
            return False
        path = Path(row["path"])
        if path.exists():
            validation = self._validate_artifact(path)
            if validation.valid:
                return True
            self.db.record_download(
                candidate_db_id,
                "error",
                error=f"existing chart failed validation: {validation.error}",
            )
            # This path was previously recorded by the app, so it is safe to
            # remove before retrying. Untracked target files are handled below.
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        # File was deleted externally; allow re-download.
        return False

    def download_selection(
        self,
        candidate: ChartCandidate,
        source: ChartSource,
        target_relpath: str,
    ) -> tuple[bool, str, int]:
        """Download one selection. Returns (success, message, bytes_written)."""
        if self.already_downloaded(candidate.db_id):
            return True, "already downloaded (skipped)", 0

        target = (self.library_root / target_relpath).resolve()
        if not is_within(target, self.library_root):
            message = f"refusing to write outside library root: {target}"
            self.db.record_download(candidate.db_id, "error", error=message)
            return False, message, 0

        if not source.supports_direct_download or not candidate.supports_download:
            message = f"source {source.name} does not expose a direct download for this chart"
            self.db.record_download(candidate.db_id, "unsupported", error=message)
            return False, message, 0

        if target.exists():
            # Same target path already on disk (e.g. from a previous library
            # build). Treat as done.
            validation = self._validate_artifact(target)
            if not validation.valid:
                message = f"existing target failed validation: {validation.error}"
                self.db.record_download(candidate.db_id, "error", error=message)
                return False, message, 0
            size = target.stat().st_size
            self.db.record_download(
                candidate.db_id,
                "completed",
                bytes_count=size,
                path=str(target),
                md5=candidate.md5,
                sha256=_hash_of(target, "sha256"),
                validation_json=_validation_payload(validation),
            )
            return True, "already on disk (skipped)", 0

        try:
            result_path = source.download_chart(candidate, target)
        except SourceError as exc:
            self.db.record_download(candidate.db_id, "error", error=str(exc))
            return False, str(exc), 0

        if not result_path.is_file():
            message = "downloaded chart path is not a regular file"
            self.db.record_download(candidate.db_id, "error", error=message)
            return False, message, 0
        size = result_path.stat().st_size
        validation = self._validate_artifact(result_path)
        if not validation.valid:
            result_path.unlink(missing_ok=True)
            message = f"downloaded chart failed validation: {validation.error}"
            self.db.record_download(candidate.db_id, "error", error=message)
            return False, message, 0
        # Optional checksum validation, only for sources whose chart hash is
        # known to be the hash of the downloaded bytes.
        if (
            getattr(source, "download_hash_verified", False)
            and candidate.md5
            and result_path.suffix.lower() == ".sng"
        ):
            actual = _md5_of(result_path)
            if actual != candidate.md5.lower():
                result_path.unlink(missing_ok=True)
                message = f"checksum mismatch (expected {candidate.md5}, got {actual})"
                self.db.record_download(candidate.db_id, "error", error=message)
                return False, message, 0

        self.db.record_download(
            candidate.db_id,
            "completed",
            bytes_count=size,
            path=str(result_path),
            md5=candidate.md5,
            sha256=_hash_of(result_path, "sha256"),
            validation_json=_validation_payload(validation),
        )
        return True, "downloaded", size
