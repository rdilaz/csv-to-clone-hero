"""Local catalog-backed discovery for one or more music providers."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Mapping

from csv_to_clonehero.catalog import CatalogLookup, EnchorCatalog, load_enchor_catalog, read_catalog_manifest
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.matching import score_candidate, score_expansion_candidate
from csv_to_clonehero.models import ChartCandidate
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
    persistence_seconds: float = 0.0
    fast_path_timings: dict[str, float] = field(default_factory=dict)
    candidate_relationship_digest: str = ""
    cached_tracks: int = 0
    recomputed_tracks: int = 0

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
            "cached_tracks": self.cached_tracks,
            "recomputed_tracks": self.recomputed_tracks,
            "persistence_seconds": round(self.persistence_seconds, 6),
        }


MATCH_CACHE_VERSION = "combined-match-v3"


def matching_cache_fingerprint(track: Any, catalog: Any, config: AppConfig, *, config_fingerprint: str = "") -> str:
    """Fingerprint every input that can affect a song's match result."""
    def plain(value: Any) -> Any:
        if is_dataclass(value):
            return plain(asdict(value))
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [plain(item) for item in value]
        if hasattr(value, "__dict__"):
            return {key: plain(item) for key, item in vars(value).items() if not key.startswith("_")}
        return value
    payload = {"version": MATCH_CACHE_VERSION, "track": plain(track), "catalog_snapshot": getattr(catalog, "snapshot_id", ""), "catalog_chart_count": getattr(catalog, "chart_count", 0), "config": config_fingerprint or matching_config_fingerprint(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def matching_scan_fingerprint(tracks: Iterable[Any], manifest: Mapping[str, Any], config: AppConfig, *, library_fingerprint: str = "") -> str:
    """Fingerprint the complete input for the completed-scan fast path."""
    track_fingerprints = [] if library_fingerprint else [matching_cache_fingerprint(track, type("Manifest", (), manifest)(), config) for track in tracks]
    payload = {"version": MATCH_CACHE_VERSION, "library": library_fingerprint, "tracks": track_fingerprints, "catalog": dict(manifest), "config": matching_config_fingerprint(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def matching_config_fingerprint(config: AppConfig) -> str:
    values = {name: getattr(config, name, None) for name in ("matching", "search", "instruments")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def completed_scan_fast_path(config: AppConfig, db: Database, workspace_id: int | str, library_fingerprint: str, *, tracks: Iterable[Any] = (), on_progress: Any = None, miss_diagnostic: dict[str, str] | None = None) -> CombinedDiscoveryResult | None:
    """Reuse a structurally valid completed scan without rebuilding the library."""
    def miss(reason: str) -> None:
        if miss_diagnostic is not None:
            miss_diagnostic["reason"] = reason
    track_values = list(tracks)
    if not library_fingerprint and not track_values:
        miss("missing library fingerprint")
        return None


    started = time.perf_counter()
    timings: dict[str, float] = {}
    try:
        manifest_started = time.perf_counter()
        manifest = read_catalog_manifest(config.catalog_path)
        timings["catalog_manifest_seconds"] = time.perf_counter() - manifest_started
        config_started = time.perf_counter()
        fingerprint = matching_scan_fingerprint(track_values, manifest, config, library_fingerprint=library_fingerprint)
        timings["config_fingerprint_seconds"] = time.perf_counter() - config_started
        lookup_started = time.perf_counter()
        row = db.get_matching_scan_cache(workspace_id, fingerprint)
        timings["scan_cache_lookup_seconds"] = time.perf_counter() - lookup_started
        validation_started = time.perf_counter()
        progress = db.get_discovery_song_progress_summary(workspace_id)
        if row is None:
            miss("no scan record")
            return None
        if track_values and int(row["track_count"]) != len(track_values):
            miss("current input track count mismatch")
            return None
        if progress["total"] != int(row["track_count"]) or progress["completed"] != progress["total"]:
            miss("progress incomplete")
            return None
        result_data = json.loads(row["result_json"])
        current_revision = db.get_workspace_candidate_revision(workspace_id)
        cached_revision = int(row["candidate_revision"] or 0)
        if int(result_data.get("tracks", -1)) != int(row["track_count"]):
            miss("current candidate relationship mismatch")
            return None
        if cached_revision != current_revision:
            expected_digest = str(result_data.get("candidate_relationship_digest") or "")
            current_count, current_digest, _ = db.workspace_current_candidate_digest(workspace_id)
            if current_count != int(result_data.get("persisted_candidates", -1)) or not expected_digest or current_digest != expected_digest:
                miss("current candidate relationship mismatch")
                return None
            db.update_matching_scan_cache_revision(workspace_id, current_revision)
        latest_generation = db.get_latest_combined_generation(workspace_id)
        if latest_generation is not None and str(latest_generation["status"] or "").lower() not in {"complete", "completed"}:
            miss("latest combined generation is not complete")
            return None
        if latest_generation is None:
            expected_digest = str(result_data.get("candidate_relationship_digest") or "")
            current_count, current_digest, _ = db.workspace_current_candidate_digest(workspace_id)
            if (
                not expected_digest
                or current_count != int(result_data.get("persisted_candidates", -1))
                or current_digest != expected_digest
            ):
                miss("cannot materialize combined generation")
                return None
            generation_key = f"combined-migration-{uuid.uuid4().hex}"
            scan_id = db.create_discovery_scan(
                workspace_id, scan_key=generation_key, kind="exact", status="running",
                metadata={"workflow": "combined_catalog", "generation": generation_key, "migration": True},
            )
            task_id = db.upsert_discovery_task(
                workspace_id, source="combined", kind="generation",
                query_key=generation_key, query_text="validated persisted combined result",
                scan_id=scan_id, status="complete", total=int(result_data["tracks"]),
                completed=int(result_data["tracks"]),
            )
            db.assign_current_progress_candidates_to_task(workspace_id, task_id)
            db.create_discovery_scan(
                workspace_id, scan_key=generation_key, kind="exact", status="complete",
                metadata={"workflow": "combined_catalog", "generation": generation_key, "migration": True},
            )
        timings["result_validation_seconds"] = time.perf_counter() - validation_started
        result = CombinedDiscoveryResult(**result_data)
        result.cached_tracks = int(row["track_count"])
        result.recomputed_tracks = 0
        result.discovery_seconds = 0.0
        timings["total_seconds"] = time.perf_counter() - started
        result.fast_path_timings = timings
        if on_progress is not None:
            on_progress({"total_tracks": result.tracks, "catalog_chart_count": manifest["chart_count"], "phase": "complete", "status": "complete", "message": f"Reusing {result.tracks:,} existing matches", "processed_tracks": result.tracks, "cached_tracks": result.tracks, "recomputed_tracks": 0, "fast_path_timings": timings})
        return result
    except json.JSONDecodeError:
        miss("corrupt result JSON")
        return None
    except (OSError, TypeError, ValueError, KeyError):
        miss("cache validation error")
        return None


def materialize_completed_scan_cache(config: AppConfig, db: Database, workspace_id: int | str) -> tuple[CombinedDiscoveryResult, dict[str, Any]]:
    """Backfill a v3 scan record from proven completed persisted state only."""
    workspace = db.get_workspace(workspace_id)
    progress_row = db.get_workspace_progress(workspace_id)
    if workspace is None or progress_row is None or str(workspace["status"]) != "complete" or str(progress_row["status"]) != "complete":
        raise ValueError("workspace is not complete")
    progress = db.get_discovery_song_progress_summary(workspace_id)
    classified = sum(progress[key] for key in ("strong", "review", "no_suitable", "not_playable", "no_catalog_candidate"))
    if progress["total"] <= 0 or progress["completed"] != progress["total"]:
        raise ValueError("progress is incomplete")
    if classified != progress["total"]:
        raise ValueError(f"classification total {classified} does not equal {progress['total']}")
    library_fingerprint = db.get_library_matching_fingerprint(workspace_id)
    if not library_fingerprint:
        raise ValueError("missing canonical library fingerprint")
    manifest = read_catalog_manifest(config.catalog_path)
    candidate_count, candidate_digest, _ = db.workspace_current_candidate_digest(workspace_id)
    result = CombinedDiscoveryResult(status="complete", phase="complete", tracks=progress["total"], catalog_available=True, catalog_snapshot_id=str(manifest["snapshot_id"]), catalog_chart_count=int(manifest["chart_count"]), candidate_charts_evaluated=candidate_count, unique_charts_evaluated=candidate_count, tracks_with_candidates=progress["total"] - progress["no_catalog_candidate"], strong_match_tracks=progress["strong"], review_match_tracks=progress["review"], soft_rejected_only_tracks=progress["no_suitable"], hard_rejected_only_tracks=progress["not_playable"], no_plausible_tracks=progress["no_catalog_candidate"], persisted_candidates=candidate_count, candidate_relationship_digest=candidate_digest)
    scan_fingerprint = matching_scan_fingerprint((), manifest, config, library_fingerprint=library_fingerprint)
    db.save_matching_scan_cache(workspace_id, scan_fingerprint, result.tracks, asdict(result))
    return result, {"library_fingerprint": library_fingerprint, "catalog_manifest": manifest, "scan_fingerprint": scan_fingerprint, "candidate_count": candidate_count, "candidate_digest": candidate_digest}


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
    on_progress: Any = None,
    on_stage: Any = None,
    operation_id: str = "",
    cancellation_event: Any = None,
) -> CombinedDiscoveryResult:
    """Import provider snapshots, match them locally, and persist candidates.

    Provider records are first unioned by normalized artist/title while keeping
    provenance. A complete catalog supplies positive exact and expansion
    candidates; every candidate still passes the normal production matcher and
    quality gates before it reaches the workspace.
    """
    def set_stage(value: str) -> None:
        if on_stage is not None:
            on_stage(value)

    set_stage("source_library_loading")
    snapshots = list(snapshots)
    tracks = merge_provider_tracks(track for snapshot in snapshots for track in snapshot.tracks)
    normalized = [track for provider_track in tracks for track in [provider_track_to_track(provider_track)]]
    db.upsert_spotify_tracks(normalized)
    workspace = db.get_workspace(workspace_key)
    workspace_id = int(workspace["id"]) if workspace else db.create_workspace(
        workspace_key=workspace_key, name=workspace_name, kind="exact", status="complete", phase="catalog_match",
        metadata={"workflow": "combined_catalog"},
    )
    catalog_manifest = None
    scan_fingerprint = ""
    config_fingerprint = matching_config_fingerprint(config)
    if catalog is None:
        set_stage("completed_scan_validation")
        library_fingerprint = db.get_library_matching_fingerprint(workspace_id)
        if not library_fingerprint:
            cached_result = completed_scan_fast_path(
                config,
                db,
                workspace_id,
                library_fingerprint,
                tracks=normalized,
                on_progress=on_progress,
            )
            if cached_result is not None:
                return cached_result
        try:
            set_stage("catalog_manifest_load")
            catalog_manifest = read_catalog_manifest(config.catalog_path)
            scan_fingerprint = matching_scan_fingerprint(normalized, catalog_manifest, config, library_fingerprint=library_fingerprint)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            catalog_manifest = None
            scan_fingerprint = ""
        set_stage("catalog_load")
        catalog = load_enchor_catalog(config.catalog_path)
    else:
        set_stage("catalog_load")
        catalog_manifest = {"snapshot_id": getattr(catalog, "snapshot_id", ""), "chart_count": getattr(catalog, "chart_count", 0), "sha256": ""}
        scan_fingerprint = matching_scan_fingerprint(normalized, catalog_manifest, config)
    result = CombinedDiscoveryResult(
        tracks=len(normalized),
        catalog_available=catalog.available,
        catalog_diagnostic=catalog.diagnostic,
        catalog_snapshot_id=str(getattr(catalog, "snapshot_id", "") or ""),
        catalog_chart_count=int(getattr(catalog, "chart_count", 0) or 0),
        catalog_load_seconds=float(getattr(catalog, "load_seconds", 0.0) or 0.0),
    )
    set_stage("generation_preparation")
    generation_key = f"combined-{uuid.uuid4().hex}"
    scan_id = db.create_discovery_scan(
        workspace_id, scan_key=generation_key, kind="exact", status="running",
        metadata={"workflow": "combined_catalog", "generation": generation_key, "run_id": operation_id},
    )
    generation_task_id = db.upsert_discovery_task(
        workspace_id, source="combined", kind="generation", query_key=generation_key,
        query_text="combined candidate generation", scan_id=scan_id,
        status="running", total=len(normalized), metadata={"run_id": operation_id},
    )
    task_ids = {track.track_key: generation_task_id for track in normalized}
    db.initialize_discovery_song_progress(workspace_id, normalized)

    def emit(**payload: Any) -> None:
        if on_progress is not None:
            on_progress({
                "total_tracks": len(normalized),
                "catalog_chart_count": result.catalog_chart_count,
                **payload,
            })

    emit(
        phase="catalog_prepare", message="Preparing local Enchor search index",
        catalog_chart_count=result.catalog_chart_count, processed_tracks=0,
        scoring_tasks_completed=0,
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
        cached_progress: list[tuple[str, Mapping[str, Any]]] = []
        emit(phase="finding_charts", message="Finding possible charts", processed_tracks=0)
        for position, track in enumerate(normalized, start=1):
            if cancellation_event is not None and cancellation_event.is_set():
                from csv_to_clonehero.spotify_pipeline import DiscoveryCancelled
                raise DiscoveryCancelled("matching cancelled")
            set_stage("cache_lookup")
            fingerprint = matching_cache_fingerprint(track, catalog, config, config_fingerprint=config_fingerprint)
            cached = db.get_matching_song_cache(fingerprint)
            if cached is not None:
                try:
                    set_stage("candidate_restoration")
                    cached_result = json.loads(cached["result_json"])
                    classification = str(cached_result["classification"])
                    entries = cached_result.get("candidates", [])
                    candidate_count = int(cached_result.get("candidate_count", len(entries)))
                    expected_ids = {int(entry["candidate"].get("chart_id", 0)) for entry in entries}
                    _, _, existing_ids = db.workspace_current_candidate_digest(workspace_id, track.track_key)
                    if existing_ids != expected_ids:
                        db.clear_workspace_track_candidates(workspace_id, track.track_key)
                        for entry in entries:
                            chart = ChartCandidate(**dict(entry["candidate"]))
                            evaluated_chart_ids.add((str(chart.source), int(chart.chart_id)))
                            db.upsert_candidate(chart)
                            db.upsert_workspace_candidate(workspace_id, track.track_key, chart, score=float(entry["score"]), quality=float(entry["quality"]), decision=str(entry["decision"]), eligibility=str(entry["eligibility"]), breakdown_json=str(entry["breakdown_json"]), kind="exact", task_id=task_ids[track.track_key], metadata=dict(entry.get("metadata") or {}))
                    else:
                        db.assign_workspace_track_candidates_to_task(workspace_id, track.track_key, task_ids[track.track_key])
                    result.cached_tracks += 1
                    result.candidates += candidate_count
                    result.eligible_candidates += int(cached_result.get("eligible_count", 0))
                    result.candidate_charts_evaluated += candidate_count
                    if candidate_count > 0:
                        result.tracks_with_candidates += 1
                    else:
                        result.zero_candidate_tracks += 1
                    if classification == "strong": result.strong_match_tracks += 1
                    elif classification == "review": result.review_match_tracks += 1
                    elif classification == "not_playable": result.hard_rejected_only_tracks += 1
                    elif classification == "no_suitable": result.soft_rejected_only_tracks += 1
                    else: result.no_plausible_tracks += 1
                    result.persisted_candidates += len(entries)
                    cached_progress.append((track.track_key, {"status": "complete", "candidate_count": int(cached_result.get("candidate_count", len(entries))), "provenance": ", ".join(cached_result.get("provenance", [])), "classification": classification}))
                    if len(cached_progress) >= 50:
                        db.update_discovery_song_progress_batch(workspace_id, cached_progress)
                        cached_progress.clear()
                    if position <= 10 or position % 25 == 0 or position == len(normalized):
                        emit(phase="finding_charts", message="Reusing cached match", current_song={"artist": track.artist_full, "title": track.title}, processed_tracks=position, scoring_tasks_completed=position, cached_tracks=result.cached_tracks, recomputed_tracks=result.recomputed_tracks, strong_matches=result.strong_match_tracks, review_matches=result.review_match_tracks, songs_with_options=result.plausible_match_tracks)
                    continue
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    pass
            set_stage("database_persistence")
            result.recomputed_tracks += 1
            db.update_discovery_song_progress(workspace_id, track.track_key, status="checking")
            db.clear_workspace_track_candidates(workspace_id, track.track_key)
            set_stage("retrieval")
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
            set_stage("scoring")
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
            set_stage("database_persistence")
            persistence_started = time.perf_counter()
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
                    task_id=task_ids[track.track_key],
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
            result.persistence_seconds += time.perf_counter() - persistence_started
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
            if matches and strong:
                classification = "strong"
            elif matches and review:
                classification = "review"
            elif matches and scored and all(
                bool(getattr(breakdown, "gate_factors", {}).get("hard_gate", False))
                for _, breakdown in scored
            ):
                classification = "not_playable"
            elif matches:
                classification = "no_suitable"
            else:
                classification = "no_catalog_candidate"
            db.save_matching_song_cache(
                fingerprint,
                track.track_key,
                classification,
                {
                    "classification": classification,
                    "candidate_count": len(matches),
                    "eligible_count": len(eligible),
                    "provenance": list(lookup.methods),
                    "candidates": [
                        {"candidate": asdict(chart), "score": breakdown.final_score, "quality": breakdown.quality, "decision": breakdown.decision, "eligibility": breakdown.eligibility, "breakdown_json": json.dumps(breakdown.to_dict(), sort_keys=True), "metadata": {"provenance": track.origin_metadata.get("provenance", []), "catalog_retrieval": {"strict_exact": lookup.strict_exact, "methods": list(lookup.methods), "retrieved_song_keys": lookup.retrieved_song_keys, "candidate_methods": list(lookup.candidate_methods.get((str(chart.source), int(chart.chart_id)), lookup.methods))}}}
                        for chart, breakdown in to_persist
                    ],
                },
            )
            db.update_discovery_song_progress(
                workspace_id, track.track_key, status="complete",
                candidate_count=len(matches), provenance=", ".join(lookup.methods),
                classification=classification,
            )
            emit(
                phase="finding_charts", message="Finding possible charts",
                current_song={"artist": track.artist_full, "title": track.title},
                processed_tracks=position, scoring_tasks_completed=position,
                song_status=classification, candidate_count=len(matches),
                provenance=list(lookup.methods),
                strong_matches=result.strong_match_tracks,
                review_matches=result.review_match_tracks,
                no_suitable_matches=result.soft_rejected_only_tracks,
                not_playable_matches=result.hard_rejected_only_tracks,
                no_catalog_candidates=result.zero_candidate_tracks,
                songs_with_options=result.plausible_match_tracks,
                cached_tracks=result.cached_tracks,
                recomputed_tracks=result.recomputed_tracks,
            )
        if cached_progress:
            db.update_discovery_song_progress_batch(workspace_id, cached_progress)
        result.unique_charts_evaluated = len(evaluated_chart_ids)
        result.discovery_seconds = time.perf_counter() - discovery_started
        _persist_expansion(db, workspace_id, catalog, normalized, expansion_artists, config, result)
    if not result.discovery_seconds:
        result.discovery_seconds = time.perf_counter() - discovery_started
    set_stage("generation_finalization")
    result.expansion_artists = rank_expansion_artists(catalog, normalized)
    if scan_fingerprint:
        _, result.candidate_relationship_digest, _ = db.workspace_current_candidate_digest(workspace_id)
        db.save_matching_scan_cache(workspace_id, scan_fingerprint, len(normalized), asdict(result))
    else:
        _, result.candidate_relationship_digest, _ = db.workspace_current_candidate_digest(workspace_id)
    db.update_discovery_task(
        generation_task_id, status="complete", completed=len(normalized), total=len(normalized)
    )
    # The scan transition is the final commit marker for this generation.
    db.create_discovery_scan(
        workspace_id,
        scan_key=generation_key,
        kind="exact",
        status="complete",
        metadata={
            "workflow": "combined_catalog",
            "generation": generation_key,
            "run_id": operation_id,
            "candidate_relationship_digest": result.candidate_relationship_digest,
        },
    )
    emit(
        phase="complete", status="complete", message="Chart matching complete",
        processed_tracks=len(normalized), scoring_tasks_completed=len(normalized),
        strong_matches=result.strong_match_tracks,
        review_matches=result.review_match_tracks,
        no_suitable_matches=result.soft_rejected_only_tracks,
        not_playable_matches=result.hard_rejected_only_tracks,
        no_catalog_candidates=result.zero_candidate_tracks,
        songs_with_options=result.plausible_match_tracks,
        cached_tracks=result.cached_tracks,
        recomputed_tracks=result.recomputed_tracks,
        retrieval_seconds=round(result.retrieval_seconds, 3),
        scoring_seconds=round(result.scoring_seconds, 3),
        persistence_seconds=round(result.persistence_seconds, 3),
        discovery_seconds=round(result.discovery_seconds, 3),
    )
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
