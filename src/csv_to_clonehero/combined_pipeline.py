"""Local catalog-backed discovery for one or more music providers."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from csv_to_clonehero.catalog import CatalogLookup, EnchorCatalog, load_enchor_catalog
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
    catalog_load_seconds: float = 0.0
    artist_found_title_different: int = 0
    title_found_artist_different: int = 0
    broader_retrieval_tracks: int = 0
    zero_candidate_tracks: int = 0
    candidate_charts_evaluated: int = 0
    unique_charts_evaluated: int = 0
    tracks_with_candidates: int = 0
    strong_match_tracks: int = 0
    review_match_tracks: int = 0
    hard_rejected_only_tracks: int = 0
    soft_rejected_only_tracks: int = 0
    no_plausible_tracks: int = 0
    persisted_candidates: int = 0
    retrieval_seconds: float = 0.0
    scoring_seconds: float = 0.0
    discovery_seconds: float = 0.0

    @property
    def stats(self) -> "CombinedDiscoveryResult":
        """Expose the result as stats to the legacy progress renderer."""
        return self

    @property
    def plausible_match_tracks(self) -> int:
        return self.strong_match_tracks + self.review_match_tracks

    @property
    def average_candidates_per_matched_song(self) -> float:
        if not self.tracks_with_candidates:
            return 0.0
        return self.candidate_charts_evaluated / self.tracks_with_candidates

    @property
    def average_eligible_candidates_per_matched_song(self) -> float:
        if not self.plausible_match_tracks:
            return 0.0
        return self.eligible_candidates / self.plausible_match_tracks

    @property
    def funnel(self) -> dict[str, int | float]:
        """Return the stable real-library funnel used by diagnostics/reports."""
        return {
            "imported_canonical_songs": self.tracks,
            "exact_key_hits": self.exact_hits,
            "recovered_through_broader_retrieval": self.broader_retrieval_tracks,
            "any_candidate_songs": self.tracks_with_candidates,
            "strong_matches": self.strong_match_tracks,
            "review_plausible_matches": self.review_match_tracks,
            "hard_rejected_only": self.hard_rejected_only_tracks,
            "soft_rejected_only": self.soft_rejected_only_tracks,
            "no_plausible_catalog_candidate": self.no_plausible_tracks,
            "candidate_charts_evaluated": self.candidate_charts_evaluated,
            "unique_charts_represented": self.unique_charts_evaluated,
            "average_candidates_per_matched_song": round(
                self.average_candidates_per_matched_song, 3
            ),
            "average_eligible_candidates_per_matched_song": round(
                self.average_eligible_candidates_per_matched_song, 3
            ),
        }


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
    result = CombinedDiscoveryResult(
        tracks=len(normalized),
        catalog_available=catalog.available,
        catalog_diagnostic=catalog.diagnostic,
        catalog_snapshot_id=str(getattr(catalog, "snapshot_id", "") or ""),
        catalog_chart_count=int(getattr(catalog, "chart_count", 0) or 0),
        catalog_load_seconds=float(getattr(catalog, "load_seconds", 0.0) or 0.0),
    )
    if diagnostics:
        diagnostics.update("combine", counters={"unique_imported_tracks": len(normalized), "unique_imported_artists": len({track.artist_norm for track in tracks})})
        diagnostics.update(
            "catalog_load",
            counters={
                "catalog_available": catalog.available,
                "catalog_complete": bool(getattr(catalog, "complete", catalog.available)),
                "catalog_chart_count": result.catalog_chart_count,
                "catalog_load_seconds": round(result.catalog_load_seconds, 3),
            },
            catalog_snapshot_id=result.catalog_snapshot_id,
            catalog_diagnostic=catalog.diagnostic,
        )
    for snapshot in snapshots:
        result.source_summaries[snapshot.provider] = {
            "tracks": len(snapshot.tracks),
            "artists": len({track.artist_norm for track in snapshot.tracks}),
            "playlists": len(snapshot.playlists),
            "liked": sum(1 for track in snapshot.tracks if track.liked),
        }
    discovery_started = time.perf_counter()
    if catalog.available:
        search_config = getattr(config, "search", config)
        artist_fuzzy_limit = max(
            0,
            int(getattr(search_config, "catalog_artist_fuzzy_limit", 20) or 0),
        )
        title_fuzzy_limit = max(
            0,
            int(getattr(search_config, "catalog_title_fuzzy_limit", 20) or 0),
        )
        min_artist_similarity = float(
            getattr(search_config, "catalog_min_artist_similarity", 55.0) or 55.0
        )
        min_title_similarity = float(
            getattr(search_config, "catalog_min_title_similarity", 20.0) or 20.0
        )
        evaluated_chart_ids: set[tuple[str, int]] = set()
        for position, track in enumerate(normalized, start=1):
            retrieval_started = time.perf_counter()
            lookup_method = getattr(catalog, "high_recall_candidates", None)
            if callable(lookup_method):
                lookup = lookup_method(
                    track.artist_full,
                    track.title,
                    alternate_artists=tuple(
                        [track.primary_artist, *track.featured_artists]
                    ),
                    artist_fuzzy_limit=artist_fuzzy_limit,
                    title_fuzzy_limit=title_fuzzy_limit,
                    min_artist_similarity=min_artist_similarity,
                    min_title_similarity=min_title_similarity,
                )
            else:
                matches = tuple(catalog.candidates(track.artist_full, track.title))
                lookup = CatalogLookup(
                    candidates=matches,
                    strict_exact=bool(matches),
                    methods=("exact",) if matches else (),
                    retrieved_song_keys=1 if matches else 0,
                    exact_artist=bool(matches),
                    exact_title=bool(matches),
                    candidate_methods={
                        (str(chart.source), int(chart.chart_id)): ("exact",)
                        for chart in matches
                    },
                )
            result.retrieval_seconds += time.perf_counter() - retrieval_started
            matches = lookup.candidates
            if lookup.strict_exact:
                result.exact_hits += 1
            else:
                result.exact_misses += 1
            if lookup.exact_artist and not lookup.strict_exact:
                result.artist_found_title_different += 1
            if lookup.exact_title and not lookup.strict_exact and not lookup.exact_artist:
                result.title_found_artist_different += 1
            if lookup.candidates and not lookup.strict_exact:
                result.broader_retrieval_tracks += 1
            if not matches:
                result.zero_candidate_tracks += 1
            else:
                result.tracks_with_candidates += 1
            result.candidate_charts_evaluated += len(matches)
            evaluated_chart_ids.update(
                (str(chart.source), int(chart.chart_id)) for chart in matches
            )
            if diagnostics and (position <= 50 or position % 25 == 0 or position == len(normalized)):
                diagnostics.update(
                    "catalog_match",
                    samples={
                        "lookups": [
                            {
                                "artist": track.artist_norm,
                                "title": track.title_norm,
                                "candidate_count": len(matches),
                                "strict_exact": lookup.strict_exact,
                                "methods": list(lookup.methods),
                            }
                        ]
                    },
                )
            # Score before persisting so the normal review surface receives all
            # plausible alternatives but not hundreds of clearly wrong rows.
            scored: list[tuple[Any, Any]] = []
            scoring_started = time.perf_counter()
            for chart in matches:
                scored.append((chart, score_candidate(track, chart, config)))
            result.scoring_seconds += time.perf_counter() - scoring_started
            eligible = [
                item
                for item in scored
                if str(getattr(item[1], "eligibility", "")).upper()
                in {"RECOMMENDED", "ELIGIBLE_REVIEW"}
            ]
            strong = any(
                str(getattr(breakdown, "eligibility", "")).upper() == "RECOMMENDED"
                for _, breakdown in scored
            )
            review = any(
                str(getattr(breakdown, "eligibility", "")).upper() == "ELIGIBLE_REVIEW"
                for _, breakdown in scored
            )
            if strong:
                result.strong_match_tracks += 1
            elif review:
                result.review_match_tracks += 1
            elif matches:
                if scored and all(
                    bool(getattr(breakdown, "gate_factors", {}).get("hard_gate", False))
                    for _, breakdown in scored
                ):
                    result.hard_rejected_only_tracks += 1
                else:
                    result.soft_rejected_only_tracks += 1
            else:
                result.no_plausible_tracks += 1

            to_persist = list(eligible)
            if not to_persist and scored:
                # Keep one hard-gated explanation for songs with no usable
                # alternative. Soft score rejects stay out of normal review.
                hard = [
                    item
                    for item in scored
                    if bool(
                        getattr(item[1], "gate_factors", {}).get("hard_gate", False)
                    )
                ]
                if hard:
                    to_persist = [
                        max(hard, key=lambda item: (item[1].final_score, item[1].quality))
                    ]
            for chart, breakdown in to_persist:
                db.upsert_candidate(chart)
                db.upsert_workspace_candidate(
                    workspace_id,
                    track.track_key,
                    chart,
                    score=breakdown.final_score,
                    quality=breakdown.quality,
                    decision=breakdown.decision,
                    eligibility=breakdown.eligibility,
                    breakdown_json=json.dumps(breakdown.to_dict(), sort_keys=True),
                    kind="exact",
                    metadata={
                        "provenance": track.origin_metadata.get("provenance", []),
                        "catalog_retrieval": {
                            "strict_exact": lookup.strict_exact,
                            "methods": list(lookup.methods),
                            "retrieved_song_keys": lookup.retrieved_song_keys,
                            "candidate_methods": list(
                                lookup.candidate_methods.get(
                                    (str(chart.source), int(chart.chart_id)),
                                    lookup.methods,
                                )
                            ),
                        },
                    },
                )
                result.persisted_candidates += 1
            result.candidates += len(matches)
            for _, breakdown in scored:
                if str(getattr(breakdown, "eligibility", "")).upper() not in {
                    "RECOMMENDED",
                    "ELIGIBLE_REVIEW",
                }:
                    result.rejected_candidates += 1
                    reason = str(getattr(breakdown, "eligibility", "") or "rejected")
                    result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1
                else:
                    result.eligible_candidates += 1
        result.unique_charts_evaluated = len(evaluated_chart_ids)
        result.discovery_seconds = time.perf_counter() - discovery_started
        _persist_expansion(db, workspace_id, catalog, normalized, expansion_artists, config, result)
    if not result.discovery_seconds:
        result.discovery_seconds = time.perf_counter() - discovery_started
    result.expansion_artists = rank_expansion_artists(catalog, normalized)
    if diagnostics:
        diagnostics.update(
            "score",
            counters={
                "exact_hits": result.exact_hits,
                "exact_misses": result.exact_misses,
                "artist_found_title_different": result.artist_found_title_different,
                "title_found_artist_different": result.title_found_artist_different,
                "broader_retrieval_tracks": result.broader_retrieval_tracks,
                "zero_candidate_tracks": result.zero_candidate_tracks,
                "tracks_with_candidates": result.tracks_with_candidates,
                "candidate_charts_evaluated": result.candidate_charts_evaluated,
                "unique_charts_evaluated": result.unique_charts_evaluated,
                "strong_match_tracks": result.strong_match_tracks,
                "review_match_tracks": result.review_match_tracks,
                "hard_rejected_only_tracks": result.hard_rejected_only_tracks,
                "soft_rejected_only_tracks": result.soft_rejected_only_tracks,
                "no_plausible_tracks": result.no_plausible_tracks,
                "candidate_charts_created": result.persisted_candidates,
                "eligible_candidates": result.eligible_candidates,
                "rejected_candidates": result.rejected_candidates,
                "retrieval_seconds": round(result.retrieval_seconds, 3),
                "scoring_seconds": round(result.scoring_seconds, 3),
                "discovery_seconds": round(result.discovery_seconds, 3),
                "funnel": result.funnel,
            },
            rejection_reasons=result.rejection_reasons,
        )
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
