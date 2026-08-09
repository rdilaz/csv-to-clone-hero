"""Chart source adapter protocol.

The rest of the application talks only to this interface. Each chart index
(Enchor, future legitimate sources) is isolated behind an adapter so no
source-specific details leak into matching/planning/download logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from csv_to_clonehero.models import ChartCandidate, DownloadInfo


class SourceError(Exception):
    """A chart source failed in a non-fatal, reportable way."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@runtime_checkable
class ChartSource(Protocol):
    name: str
    supports_direct_download: bool
    # True only when the source's chart hash is verifiably the hash of the
    # downloaded file bytes (Enchor's md5 is NOT - the server re-packs .sng).
    download_hash_verified: bool

    def search_song(self, artist: str, title: str) -> list[ChartCandidate]:
        """Find candidate charts for one song."""
        ...

    def search_artist(self, artist: str) -> list[ChartCandidate]:
        """Find charts by an artist (for library expansion)."""
        ...

    def get_chart_metadata(self, chart_id: int) -> ChartCandidate | None:
        """Fetch full metadata for one chart, if the source supports it."""
        ...

    def get_download_info(self, candidate: ChartCandidate) -> DownloadInfo:
        """Describe how to download a chart (URL, size, hash) if supported."""
        ...

    def download_chart(self, candidate: ChartCandidate, destination: Path) -> Path:
        """Download a chart to destination. Only called when the source
        exposes a legitimate direct download mechanism."""
        ...

    def close(self) -> None:
        ...
