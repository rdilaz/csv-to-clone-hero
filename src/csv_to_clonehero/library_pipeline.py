"""Source-neutral library import orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibraryProvider


@dataclass
class SourceImportResult:
    provider: str
    status: str
    tracks: int = 0
    error: str = ""
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class LibraryImportResult:
    status: str
    workspace_id: int | str
    sources: dict[str, SourceImportResult] = field(default_factory=dict)
    combined_summary: dict[str, int] = field(default_factory=dict)


def run_library_import(
    config: Any,
    db: Database,
    workspace_id: int | str,
    selected_sources: Mapping[str, LibraryProvider],
    scopes: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    on_progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> LibraryImportResult:
    """Import selected providers independently and persist successful snapshots."""
    del config  # Providers own their configuration; import never touches Enchor.
    result = LibraryImportResult("complete", workspace_id)
    scopes = scopes or {}
    for name, provider in selected_sources.items():
        provider_name = str(name or getattr(provider, "name", "")).casefold()
        if on_progress:
            on_progress({"phase": "import", "provider": provider_name})
        try:
            snapshot = provider.import_library(scope=scopes.get(provider_name) or {})

            def persistence_progress(payload: Mapping[str, Any]) -> None:
                if on_progress:
                    on_progress({"provider": provider_name, **dict(payload)})

            persisted = db.upsert_library_snapshot(
                workspace_id,
                snapshot,
                scope=scopes.get(provider_name),
                on_progress=persistence_progress if on_progress else None,
            )
            report = dict(persisted.get("report") or {})
            result.sources[provider_name] = SourceImportResult(provider_name, "complete", len(snapshot.tracks), report=report)
        except Exception as exc:  # one provider must not erase another
            result.status = "partial"
            result.sources[provider_name] = SourceImportResult(provider_name, "failed", error=str(exc))
        finally:
            try:
                provider.close()
            except Exception:
                pass
    if on_progress:
        on_progress({"phase": "combine"})
    result.combined_summary = db.get_combined_summary(workspace_id)
    return result
