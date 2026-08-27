"""Local catalog-backed discovery for one or more music providers."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from csv_to_clonehero.catalog import EnchorCatalog, load_enchor_catalog
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.matching import score_candidate, score_expansion_candidate
from csv_to_clonehero.providers.base import LibrarySnapshot, merge_provider_tracks


@dataclass
class CombinedDiscoveryResult:
    status: str = "complete"
    phase: str = "complete"
    error: str = ""
    tracks: int = 0
    exact_hits: int = 0
    exact_misses: int = 0
    candidates: int = 0
    expansion_artists: list[dict[str, Any]] = field(default_factory=list)
    source_summaries: dict[str, dict[str, int]] = field(default_factory=dict)
    catalog_available: bool = False
    catalog_diagnostic: str = ""
    rejected_candidates: int = 0
    eligible_candidates: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    catalog_snapshot_id: str = ""
    catalog_chart_count: int = 0

    @property
    def stats(self) -> "CombinedDiscoveryResult":
        """Expose the result as stats to the legacy progress renderer."""
        return self


def discover_combined(
    config: AppConfig,
    db: Database,
    snapshots: Iterable[LibrarySnapshot],
    *,
    workspace_key: str,
    workspace_name: str = "Combined library",
    catalog: EnchorCatalog | None = None,
    expansion_artists: Iterable[str] = (),
    diagnostics: Any = None,
) -> CombinedDiscoveryResult:
    """Import provider snapshots, match them locally, and persist candidates.

    Provider records are first unioned by normalized artist/title while keeping
    provenance. A complete catalog supplies positive exact and expansion
    candidates; every candidate still passes the normal production matcher and
    quality gates before it reaches the workspace.
    """
    snapshots = list(snapshots)
    tracks = merge_provider_tracks(track for snapshot in snapshots for track in snapshot.tracks)
    normalized = [track for provider_track in tracks for track in [provider_track_to_track(provider_track)]]
    db.upsert_spotify_tracks(normalized)
    workspace = db.get_workspace(workspace_key)
    workspace_id = int(workspace["id"]) if workspace else db.create_workspace(
        workspace_key=workspace_key, name=workspace_name, kind="exact", status="complete", phase="catalog_match",
        metadata={"workflow": "combined_catalog"},
    )
    catalog = catalog or load_enchor_catalog(config.catalog_path)
    result = CombinedDiscoveryResult(tracks=len(normalized), catalog_available=catalog.available, catalog_diagnostic=catalog.diagnostic,
                                     catalog_snapshot_id=str(getattr(catalog, "snapshot_id", "") or ""), catalog_chart_count=int(getattr(catalog, "chart_count", 0) or 0))
    if diagnostics:
        diagnostics.update("combine", counters={"unique_imported_tracks": len(normalized), "unique_imported_artists": len({track.artist_norm for track in tracks})})
        diagnostics.update("catalog_load", counters={"catalog_available": catalog.available, "catalog_complete": bool(getattr(catalog, "complete", catalog.available)), "catalog_chart_count": result.catalog_chart_count}, catalog_snapshot_id=result.catalog_snapshot_id, catalog_diagnostic=catalog.diagnostic)
    for snapshot in snapshots:
        result.source_summaries[snapshot.provider] = {
            "tracks": len(snapshot.tracks),
            "artists": len({track.artist_norm for track in snapshot.tracks}),
            "playlists": len(snapshot.playlists),
            "liked": sum(1 for track in snapshot.tracks if track.liked),
        }
    if catalog.available:
        for position, track in enumerate(normalized, start=1):
            matches = catalog.candidates(track.artist_full, track.title)
            if matches:
                result.exact_hits += 1
            else:
                result.exact_misses += 1
            if diagnostics and (position <= 50 or position % 25 == 0 or position == len(normalized)):
                diagnostics.update("catalog_match", samples={"lookups": [{"artist": track.artist_norm, "title": track.title_norm, "candidate_count": len(matches)}]})
            for chart in matches:
                breakdown = score_candidate(track, chart, config)
                db.upsert_candidate(chart)
                db.upsert_workspace_candidate(
                    workspace_id, track.track_key, chart,
                    score=breakdown.final_score, quality=breakdown.quality,
                    decision=breakdown.decision, eligibility=breakdown.eligibility,
                    breakdown_json=json.dumps(breakdown.to_dict(), sort_keys=True), kind="exact",
                    metadata={"provenance": track.origin_metadata.get("provenance", [])},
                )
                result.candidates += 1
                if str(breakdown.eligibility).upper() not in {"RECOMMENDED", "ELIGIBLE_REVIEW"}:
                    result.rejected_candidates += 1
                    reason = str(breakdown.eligibility or "rejected")
                    result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1
                else:
                    result.eligible_candidates += 1
        _persist_expansion(db, workspace_id, catalog, normalized, expansion_artists, config, result)
    result.expansion_artists = rank_expansion_artists(catalog, normalized)
    if diagnostics:
        diagnostics.update("score", counters={"exact_hits": result.exact_hits, "exact_misses": result.exact_misses, "candidate_charts_created": result.candidates, "eligible_candidates": result.eligible_candidates, "rejected_candidates": result.rejected_candidates}, rejection_reasons=result.rejection_reasons)
    return result


def rank_expansion_artists(catalog: EnchorCatalog, tracks: Iterable[Any]) -> list[dict[str, Any]]:
    """Rank imported primary artists by catalog song and chart coverage."""
    counts = Counter(track.primary_artist_norm for track in tracks)
    ranked = []
    for artist_norm, imported_count in counts.items():
        songs = catalog.artist_songs(artist_norm) if catalog.available else ()
        ranked.append({"artist": artist_norm, "imported_songs": imported_count, "catalog_songs": len(songs), "catalog_charts": sum(len(rows) for _, rows in songs)})
    return sorted(ranked, key=lambda item: (-item["catalog_songs"], -item["catalog_charts"], item["artist"]))


def _persist_expansion(db: Database, workspace_id: int, catalog: EnchorCatalog, tracks: list[Any], artists: Iterable[str], config: AppConfig, result: CombinedDiscoveryResult) -> None:
    imported = {track.primary_artist_norm for track in tracks}
    for artist in artists:
        artist_norm = str(artist).strip().casefold()
        if artist_norm not in imported:
            continue
        for _, charts in catalog.artist_songs(artist_norm):
            for chart in charts:
                breakdown = score_expansion_candidate(artist_norm, chart, config)
                db.upsert_candidate(chart)
                db.upsert_workspace_candidate(
                    workspace_id, None, chart,
                    score=breakdown.final_score, quality=breakdown.quality,
                    decision=breakdown.decision, eligibility=breakdown.eligibility,
                    breakdown_json=json.dumps(breakdown.to_dict(), sort_keys=True), kind="expansion", artist_norm=artist_norm,
                )
                result.candidates += 1


def expand_combined(
    config: AppConfig,
    db: Database,
    workspace_id: int | str,
    artists: Iterable[str],
    *,
    catalog: EnchorCatalog | None = None,
) -> CombinedDiscoveryResult:
    """Persist selected artist expansion candidates without network searches."""
    catalog = catalog or load_enchor_catalog(config.catalog_path)
    neutral_rows = db.get_neutral_library_tracks(workspace_id)
    if neutral_rows:
        from csv_to_clonehero.providers.base import ProviderTrack, provider_track_to_track
        tracks = [provider_track_to_track(ProviderTrack(
            provider="library", provider_id=str(row["id"]), title=str(row["title"]),
            artist=str(row["artist"]), album=str(row["album"] or ""), liked=bool(row["liked"]),
        )) for row in neutral_rows]
    else:
        tracks = db.get_spotify_tracks(True)
    result = CombinedDiscoveryResult(tracks=len(tracks), catalog_available=catalog.available, catalog_diagnostic=catalog.diagnostic)
    if catalog.available:
        _persist_expansion(db, int(workspace_id), catalog, tracks, artists, config, result)
        result.expansion_artists = rank_expansion_artists(catalog, tracks)
    return result


def provider_track_to_track(value: Any) -> Any:
    from csv_to_clonehero.providers.base import provider_track_to_track as convert
    return convert(value)
