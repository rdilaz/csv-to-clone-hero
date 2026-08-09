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
                )
            )
        else:
            raise ValueError(f"Unknown chart source: {name!r}")
    return sources
