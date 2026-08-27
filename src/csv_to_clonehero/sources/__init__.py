"""Chart source registry.

Multi-source status (spike, 2026-08): the pipeline, planner and downloads all
operate on ``list[ChartSource]`` and are covered by multi-source tests, but
Enchor is the only built-in adapter. Candidates checked for a second source:

* chorus.fightthe.pw - redirects to enchor.us (same service, already built in)
* rhythmverse.co     - no public search/download API (404 on /api/*)
* db.c3universe.com  - web UI behind Cloudflare (403), no public API

Add a new adapter here only after verifying a legitimate public search +
direct-download API against the ChartSource protocol.
"""

from csv_to_clonehero.sources.base import ChartSource, SourceError
from csv_to_clonehero.sources.enchor import EnchorSource

__all__ = ["ChartSource", "SourceError", "EnchorSource", "build_sources"]


def build_sources(config) -> list[ChartSource]:
    """Instantiate the chart sources enabled in configuration."""
    sources: list[ChartSource] = []
    for name in config.sources.enabled:
        if name == "enchor":
            sources.append(
                EnchorSource(
                    request_delay=config.network.request_delay_seconds,
                    retries=config.network.retries,
                    timeout=config.network.timeout_seconds,
                    head_size_enabled=getattr(config.network, "head_size_enabled", False),
                    download_request_delay=getattr(config.network, "download_request_delay", 0.1),
                    catalog_path=getattr(config, "catalog_path", None),
                )
            )
        else:
            raise ValueError(f"Unknown chart source: {name!r}")
    return sources
