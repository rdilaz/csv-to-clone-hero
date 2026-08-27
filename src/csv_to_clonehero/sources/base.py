"""Chart source adapter protocol.

The rest of the application talks only to this interface. Each chart index
(Enchor, future legitimate sources) is isolated behind an adapter so no
source-specific details leak into matching/planning/download logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from csv_to_clonehero.models import ChartCandidate, DownloadInfo


class SourceError(Exception):
    """A chart source failed in a non-fatal, reportable way."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        *,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        # A later retry can fail with another status after a 429.  Adapters
        # use this marker to avoid adding fallback traffic to a rate limit.
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class SourceCapabilities:
    """Optional capabilities exposed by a source adapter.

    The legacy ``ChartSource`` protocol intentionally does not require these
    attributes: existing test doubles and third-party adapters remain valid.
    Use :func:`get_source_capabilities` when a caller needs the optional
    pagination or export details.
    """

    supports_pagination: bool = False
    result_cap: int | None = None
    artist_result_cap: int | None = None
    exportable: bool = False
    discovery_only: bool = False
    supports_direct_download: bool | None = None

    @property
    def max_results(self) -> int | None:
        """Compatibility alias for ``result_cap``."""
        return self.result_cap

    @property
    def max_artist_results(self) -> int | None:
        """Compatibility alias for ``artist_result_cap``."""
        return self.artist_result_cap

    @property
    def supports_result_caps(self) -> bool:
        """Whether the declared result bounds are meaningful."""
        return self.result_cap is not None or self.artist_result_cap is not None


class ChartSourceCapabilities(Protocol):
    """Structural shape for optional source capability declarations."""

    supports_pagination: bool
    result_cap: int | None
    artist_result_cap: int | None
    exportable: bool
    discovery_only: bool


def _capability_value(source: object, name: str, default: Any = None) -> Any:
    """Read an optional capability without imposing it on old adapters."""
    value = getattr(source, name, default)
    if callable(value):
        try:
            return value()
        except TypeError:
            return default
    return value


def get_source_capabilities(source: object) -> SourceCapabilities:
    """Return normalized optional capabilities for any source object.

    ``FakeSource``-style legacy adapters fall back to their existing direct
    download flag and report no pagination cap.  Explicit discovery-only or
    exportability declarations always take precedence over that fallback.
    """
    declared = getattr(source, "capabilities", None)
    declared_object = declared if isinstance(declared, SourceCapabilities) else None
    declared_mapping = declared if isinstance(declared, Mapping) else None

    def declared_value(name: str, default: Any = None) -> Any:
        if declared_object is not None:
            return getattr(declared_object, name, default)
        if declared_mapping is not None:
            return declared_mapping.get(name, default)
        return default

    supports_pagination = _capability_value(source, "supports_pagination", None)
    if supports_pagination is None:
        supports_pagination = _capability_value(source, "supports_result_pagination", None)
    if supports_pagination is None:
        supports_pagination = declared_value("supports_pagination")

    result_cap = _capability_value(source, "result_cap", None)
    if result_cap is None:
        result_cap = _capability_value(source, "search_result_cap", None)
    if result_cap is None:
        result_cap = _capability_value(source, "max_results", None)
    if result_cap is None:
        result_cap = declared_value("result_cap")

    artist_result_cap = _capability_value(source, "artist_result_cap", None)
    if artist_result_cap is None:
        artist_result_cap = _capability_value(source, "max_artist_results", None)
    if artist_result_cap is None:
        artist_result_cap = declared_value("artist_result_cap")

    discovery_only = bool(_capability_value(source, "discovery_only", False))
    discovery_only = discovery_only or bool(declared_value("discovery_only", False))

    explicit_exportable = _capability_value(source, "exportable", None)
    if explicit_exportable is None:
        explicit_exportable = _capability_value(source, "supports_export", None)
    if explicit_exportable is None:
        explicit_exportable = declared_value("exportable")

    direct_download = _capability_value(source, "supports_direct_download", None)
    if direct_download is None:
        direct_download = declared_value("supports_direct_download")

    if result_cap is not None:
        try:
            result_cap = max(1, int(result_cap))
        except (TypeError, ValueError):
            result_cap = None
    if artist_result_cap is not None:
        try:
            artist_result_cap = max(1, int(artist_result_cap))
        except (TypeError, ValueError):
            artist_result_cap = None

    effective_exportable = False if discovery_only else (
        bool(explicit_exportable)
        if explicit_exportable is not None
        else bool(direct_download)
    )
    return SourceCapabilities(
        supports_pagination=bool(supports_pagination),
        result_cap=result_cap,
        artist_result_cap=artist_result_cap,
        exportable=effective_exportable,
        discovery_only=discovery_only,
        supports_direct_download=None if direct_download is None else bool(direct_download),
    )


def source_supports_pagination(source: object) -> bool:
    """Whether ``source`` advertises paginated result support."""
    return get_source_capabilities(source).supports_pagination


def source_result_cap(source: object, kind: str = "song") -> int | None:
    """Return an optional source result cap for a logical query kind."""
    capabilities = get_source_capabilities(source)
    return capabilities.artist_result_cap if kind == "artist" else capabilities.result_cap


def source_is_exportable(source: object) -> bool:
    """Return whether a source may be used for automatic chart export.

    Discovery-only sources are never exportable.  Legacy adapters without the
    new flag retain the old ``supports_direct_download`` behavior.
    """
    capabilities = get_source_capabilities(source)
    if capabilities.discovery_only:
        return False
    return capabilities.exportable


def source_catalog_available(source: object, artist: str, title: str = "") -> bool | None:
    """Return authoritative local catalog membership when a source exposes it.

    ``None`` means the source has no complete local snapshot, so callers must
    preserve the existing network behavior rather than treating it as absent.
    """
    method = getattr(source, "catalog_available", None)
    if not callable(method):
        return None
    try:
        result = method(artist, title)
    except Exception:
        return None
    return result if isinstance(result, bool) else None


# Short aliases keep integrations from having to depend on a particular
# helper naming convention while the optional capability surface evolves.
get_source_cap = get_source_capabilities
get_result_cap = source_result_cap
is_source_exportable = source_is_exportable


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
