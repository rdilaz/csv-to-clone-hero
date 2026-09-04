"""Local catalog-backed discovery for one or more music providers."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from csv_to_clonehero.catalog import (
    CatalogLoadError,
    CatalogLookup,
    EnchorCatalog,
    load_enchor_catalog,
    read_catalog_manifest,
)
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.matching import ELIGIBLE_REVIEW, RECOMMENDED, score_candidate, score_expansion_candidate
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title
from csv_to_clonehero.providers.base import (
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    SourceImportReport,
    merge_provider_tracks,
    playlist_identity,
    provider_track_playlist_memberships,
)


TERMINAL_YIELD_CLASSES = (
    "recommended_strong",
    "recommended_review",
    "no_plausible_catalog_candidate",
    "hard_rejected",
    "soft_rejected",
    "incomplete_evidence",
)


class LibraryLimitExceeded(ValueError):
    """The final deduplicated provider library exceeds the public hard bound."""


def _empty_yield_histogram() -> dict[str, int]:
    return {name: 0 for name in TERMINAL_YIELD_CLASSES}


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
    yield_histogram: dict[str, int] = field(default_factory=_empty_yield_histogram)
    rejection_song_reasons: dict[str, int] = field(default_factory=dict)

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
            "yield_histogram": dict(self.yield_histogram),
            "rejection_song_reasons": dict(self.rejection_song_reasons),
        }


MATCH_CACHE_VERSION = "combined-match-v8"


def matching_cache_fingerprint(track: Any, catalog: Any, config: AppConfig, *, config_fingerprint: str = "") -> str:
    """Fingerprint every input that can affect a song's match result."""
    track_payload = {
        name: getattr(track, name, None)
        for name in (
            "track_key", "title", "title_norm", "artist_full", "artist_norm",
            "primary_artist", "primary_artist_norm", "featured_artists", "album",
            "album_norm", "duration_ms", "isrc", "spotify_id",
        )
    }
    if ":" in str(track_payload.get("spotify_id") or ""):
        track_payload["spotify_id"] = ""
    payload = {"version": MATCH_CACHE_VERSION, "track": track_payload, "catalog_snapshot": getattr(catalog, "snapshot_id", ""), "catalog_checksum": getattr(catalog, "sha256", ""), "catalog_validation": getattr(catalog, "validation_fingerprint", ""), "catalog_chart_count": getattr(catalog, "chart_count", 0), "config": config_fingerprint or matching_config_fingerprint(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def matching_scan_fingerprint(tracks: Iterable[Any], manifest: Mapping[str, Any], config: AppConfig, *, library_fingerprint: str = "") -> str:
    """Fingerprint the complete input for the completed-scan fast path."""
    track_fingerprints = [] if library_fingerprint else [matching_cache_fingerprint(track, type("Manifest", (), manifest)(), config) for track in tracks]
    catalog_identity = {
        key: manifest.get(key)
        for key in ("snapshot_id", "sha256", "chart_count", "format_version")
        if key in manifest
    }
    if manifest.get("validation_fingerprint"):
        catalog_identity["validation_fingerprint"] = manifest["validation_fingerprint"]
    payload = {"version": MATCH_CACHE_VERSION, "library": library_fingerprint, "tracks": track_fingerprints, "catalog": catalog_identity, "config": matching_config_fingerprint(config)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def matching_config_fingerprint(config: AppConfig) -> str:
    values = {name: getattr(config, name, None) for name in ("matching", "search", "instruments")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _catalog_manifest(catalog: Any, path: Any) -> dict[str, Any]:
    manifest = getattr(catalog, "manifest", None)
    if isinstance(manifest, Mapping):
        return dict(manifest)
    # Retain the test/injected-catalog extension seam while production uses
    # the already validated immutable catalog manifest above.
    try:
        return dict(read_catalog_manifest(path))
    except OSError:
        return {
            "snapshot_id": str(getattr(catalog, "snapshot_id", "") or ""),
            "sha256": str(getattr(catalog, "sha256", "") or ""),
            "chart_count": int(getattr(catalog, "chart_count", 0) or 0),
            "validation_fingerprint": str(
                getattr(catalog, "validation_fingerprint", "") or ""
            ),
        }


def completed_scan_fast_path(config: AppConfig, db: Database, workspace_id: int | str, library_fingerprint: str, *, tracks: Iterable[Any] = (), catalog: EnchorCatalog | None = None, on_progress: Any = None, miss_diagnostic: dict[str, str] | None = None) -> CombinedDiscoveryResult | None:
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
        catalog = catalog or load_enchor_catalog(config.catalog_path)
        if not catalog.available:
            miss("catalog unavailable")
            return None
        manifest = _catalog_manifest(catalog, config.catalog_path)
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
        result.retrieval_seconds = 0.0
        result.scoring_seconds = 0.0
        result.persistence_seconds = 0.0
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
    catalog = load_enchor_catalog(config.catalog_path)
    if not catalog.available:
        raise ValueError(catalog.diagnostic)
    manifest = _catalog_manifest(catalog, config.catalog_path)
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
    snapshots = sorted(
        list(snapshots),
        key=lambda snapshot: (
            str(snapshot.provider).casefold() != "spotify",
            str(snapshot.provider).casefold(),
        ),
    )
    current_source_summaries = _source_summaries(snapshots)
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
    library_fingerprint = db.get_library_matching_fingerprint(workspace_id)
    canonical_rows = db.get_neutral_library_tracks(workspace_id)
    if canonical_rows:
        canonical_identities = {
            (str(row["normalized_artist"]), str(row["normalized_title"]))
            for row in canonical_rows
        }
        input_identities = {
            (str(track.primary_artist_norm), str(track.title_norm))
            for track in normalized
        }
        if canonical_identities != input_identities:
            # Direct callers may supply a not-yet-persisted snapshot. Never let
            # the workspace's older canonical fingerprint validate new inputs.
            library_fingerprint = ""
    if catalog is not None and catalog.available and library_fingerprint:
        set_stage("completed_scan_validation")
        cached_result = completed_scan_fast_path(
            config,
            db,
            workspace_id,
            library_fingerprint,
            tracks=normalized,
            catalog=catalog,
            on_progress=on_progress,
        )
        if cached_result is not None:
            cached_result.source_summaries = current_source_summaries
            return cached_result
    if catalog is None:
        set_stage("completed_scan_validation")
        if library_fingerprint:
            cached_result = completed_scan_fast_path(
                config,
                db,
                workspace_id,
                library_fingerprint,
                tracks=normalized,
                on_progress=on_progress,
            )
            if cached_result is not None:
                cached_result.source_summaries = current_source_summaries
                return cached_result
        set_stage("catalog_load")
        catalog = load_enchor_catalog(config.catalog_path)
        if catalog.available:
            catalog_manifest = _catalog_manifest(catalog, config.catalog_path)
            scan_fingerprint = matching_scan_fingerprint(normalized, catalog_manifest, config, library_fingerprint=library_fingerprint)
    else:
        set_stage("catalog_load")
        catalog_manifest = _catalog_manifest(catalog, config.catalog_path)
        scan_fingerprint = matching_scan_fingerprint(
            normalized,
            catalog_manifest,
            config,
            library_fingerprint=library_fingerprint,
        )
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

    progress_started = time.monotonic()

    def emit(**payload: Any) -> None:
        if on_progress is not None:
            processed = max(0, int(payload.get("processed_tracks") or 0))
            elapsed = max(0.0, time.monotonic() - progress_started)
            estimate = estimate_remaining_range(processed, len(normalized), elapsed)
            on_progress({
                "total_tracks": len(normalized),
                "catalog_chart_count": result.catalog_chart_count,
                "elapsed_seconds": elapsed,
                **estimate,
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
    result.source_summaries = current_source_summaries
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
        song_key_limit = max(
            1,
            int(getattr(search_config, "catalog_song_key_limit", 12) or 12),
        )
        fuzzy_fallback_key_limit = max(
            1,
            int(
                getattr(
                    search_config,
                    "catalog_fuzzy_fallback_key_limit",
                    50,
                )
                or 50
            ),
        )
        candidate_limit = max(
            1,
            int(getattr(search_config, "catalog_candidate_limit", 100) or 100),
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
                    expected_ids = {
                        (
                            str(entry["candidate"].get("source") or ""),
                            int(entry["candidate"].get("chart_id", 0)),
                        )
                        for entry in entries
                    }
                    evaluated_ids = {
                        (str(value[0]), int(value[1]))
                        for value in cached_result.get("evaluated_chart_ids", ())
                        if isinstance(value, (list, tuple)) and len(value) == 2
                    } or expected_ids
                    evaluated_chart_ids.update(evaluated_ids)
                    _, _, existing_ids = db.workspace_current_candidate_digest(workspace_id, track.track_key)
                    if existing_ids != expected_ids:
                        db.clear_workspace_track_candidates(workspace_id, track.track_key)
                        for entry in entries:
                            chart = ChartCandidate(**dict(entry["candidate"]))
                            db.upsert_candidate(chart)
                            metadata = dict(entry.get("metadata") or {})
                            metadata["provenance"] = track.origin_metadata.get("provenance", [])
                            db.upsert_workspace_candidate(workspace_id, track.track_key, chart, score=float(entry["score"]), quality=float(entry["quality"]), decision=str(entry["decision"]), eligibility=str(entry["eligibility"]), breakdown_json=str(entry["breakdown_json"]), kind="exact", task_id=task_ids[track.track_key], metadata=metadata)
                    else:
                        db.assign_workspace_track_candidates_to_task(workspace_id, track.track_key, task_ids[track.track_key])
                    result.cached_tracks += 1
                    result.candidates += candidate_count
                    result.eligible_candidates += int(cached_result.get("eligible_count", 0))
                    result.rejected_candidates += int(
                        cached_result.get(
                            "rejected_count",
                            max(0, candidate_count - int(cached_result.get("eligible_count", 0))),
                        )
                    )
                    result.candidate_charts_evaluated += candidate_count
                    if bool(cached_result.get("strict_exact")):
                        result.exact_hits += 1
                    else:
                        result.exact_misses += 1
                    result.broader_retrieval_tracks += int(
                        bool(cached_result.get("broader_retrieval"))
                    )
                    result.artist_found_title_different += int(
                        bool(cached_result.get("artist_found_title_different"))
                    )
                    result.title_found_artist_different += int(
                        bool(cached_result.get("title_found_artist_different"))
                    )
                    if candidate_count > 0:
                        result.tracks_with_candidates += 1
                    else:
                        result.zero_candidate_tracks += 1
                    primary_reason = str(cached_result.get("primary_reason") or "")
                    _record_terminal_yield(result, classification, primary_reason)
                    result.persisted_candidates += len(entries)
                    cached_progress.append((track.track_key, {"status": "complete", "candidate_count": int(cached_result.get("candidate_count", len(entries))), "provenance": ", ".join(cached_result.get("provenance", [])), "classification": classification, "reason": primary_reason}))
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
            lookup_kwargs: dict[str, Any] = {}
            if callable(lookup_method):
                lookup_kwargs = {
                    "alternate_artists": tuple(
                        [track.primary_artist, *track.featured_artists]
                    ),
                    "artist_fuzzy_limit": artist_fuzzy_limit,
                    "title_fuzzy_limit": title_fuzzy_limit,
                    "min_artist_similarity": min_artist_similarity,
                    "min_title_similarity": min_title_similarity,
                    "song_key_limit": song_key_limit,
                    "fuzzy_fallback_key_limit": fuzzy_fallback_key_limit,
                    "candidate_limit": candidate_limit,
                }
                lookup = lookup_method(
                    track.artist_full,
                    track.title,
                    **lookup_kwargs,
                    include_fuzzy_fallback=False,
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
            initial_broader_retrieval = bool(matches and not lookup.strict_exact)
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
            fallback_recovered = False
            if (
                not eligible
                and callable(lookup_method)
                and not lookup.fuzzy_fallback_attempted
            ):
                fallback_started = time.perf_counter()
                fallback_lookup = lookup_method(
                    track.artist_full,
                    track.title,
                    **lookup_kwargs,
                    include_fuzzy_fallback=True,
                )
                result.retrieval_seconds += time.perf_counter() - fallback_started
                scored_by_identity = {
                    (str(chart.source), int(chart.chart_id)): (chart, breakdown)
                    for chart, breakdown in scored
                }
                new_charts = [
                    chart
                    for chart in fallback_lookup.candidates
                    if (str(chart.source), int(chart.chart_id)) not in scored_by_identity
                ]
                if new_charts:
                    fallback_scoring_started = time.perf_counter()
                    for chart in new_charts:
                        scored_by_identity[(str(chart.source), int(chart.chart_id))] = (
                            chart,
                            score_candidate(track, chart, config),
                        )
                    result.scoring_seconds += time.perf_counter() - fallback_scoring_started
                    result.candidate_charts_evaluated += len(new_charts)
                    evaluated_chart_ids.update(
                        (str(chart.source), int(chart.chart_id)) for chart in new_charts
                    )
                    if not matches:
                        result.zero_candidate_tracks -= 1
                        result.tracks_with_candidates += 1
                    matches = fallback_lookup.candidates
                    lookup = fallback_lookup
                    scored = [
                        scored_by_identity[(str(chart.source), int(chart.chart_id))]
                        for chart in matches
                    ]
                    eligible = [
                        item
                        for item in scored
                        if str(getattr(item[1], "eligibility", "")).upper()
                        in {"RECOMMENDED", "ELIGIBLE_REVIEW"}
                    ]
                    fallback_recovered = bool(eligible)
                if fallback_recovered and not initial_broader_retrieval:
                    result.broader_retrieval_tracks += 1
            strong = any(
                str(getattr(breakdown, "eligibility", "")).upper() == "RECOMMENDED"
                for _, breakdown in scored
            )
            review = any(
                str(getattr(breakdown, "eligibility", "")).upper() == "ELIGIBLE_REVIEW"
                for _, breakdown in scored
            )
            to_persist = list(eligible)
            rejection_exemplar = None
            if not to_persist and scored:
                # Keep the exact exemplar that supplies the terminal reason.
                # It remains noneligible, but gives review/audit surfaces one
                # deterministic explanation for either hard or soft rejection.
                rejection_exemplar = _rejection_exemplar(scored)
                if rejection_exemplar is not None:
                    to_persist = [rejection_exemplar]
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
            primary_reason = _primary_rejection_reason(
                classification,
                rejection_exemplar if rejection_exemplar is not None else scored,
            )
            _record_terminal_yield(result, classification, primary_reason)
            db.save_matching_song_cache(
                fingerprint,
                track.track_key,
                classification,
                {
                    "classification": classification,
                    "candidate_count": len(matches),
                    "eligible_count": len(eligible),
                    "rejected_count": max(0, len(matches) - len(eligible)),
                    "primary_reason": primary_reason,
                    "provenance": list(lookup.methods),
                    "strict_exact": bool(lookup.strict_exact),
                    "broader_retrieval": bool(
                        lookup.candidates and not lookup.strict_exact
                    ) or fallback_recovered,
                    "artist_found_title_different": bool(
                        lookup.exact_artist and not lookup.strict_exact
                    ),
                    "title_found_artist_different": bool(
                        lookup.exact_title
                        and not lookup.strict_exact
                        and not lookup.exact_artist
                    ),
                    "evaluated_chart_ids": [
                        [str(chart.source), int(chart.chart_id)] for chart in matches
                    ],
                    "candidates": [
                        {"candidate": asdict(chart), "score": breakdown.final_score, "quality": breakdown.quality, "decision": breakdown.decision, "eligibility": breakdown.eligibility, "breakdown_json": json.dumps(breakdown.to_dict(), sort_keys=True), "metadata": {"catalog_retrieval": {"strict_exact": lookup.strict_exact, "methods": list(lookup.methods), "retrieved_song_keys": lookup.retrieved_song_keys, "candidate_methods": list(lookup.candidate_methods.get((str(chart.source), int(chart.chart_id)), lookup.methods))}}}
                        for chart, breakdown in to_persist
                    ],
                },
            )
            db.update_discovery_song_progress(
                workspace_id, track.track_key, status="complete",
                candidate_count=len(matches), provenance=", ".join(lookup.methods),
                classification=classification, reason=primary_reason,
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
    else:
        result.status = "partial"
        result.phase = "catalog_unavailable"
        result.error = result.catalog_diagnostic
        result.yield_histogram["incomplete_evidence"] = len(normalized)
        for track in normalized:
            db.update_discovery_song_progress(
                workspace_id,
                track.track_key,
                status="complete",
                classification="incomplete_evidence",
                reason="catalog unavailable or invalid",
            )
    if not result.discovery_seconds:
        result.discovery_seconds = time.perf_counter() - discovery_started
    set_stage("generation_finalization")
    result.expansion_artists = rank_expansion_artists(catalog, normalized)
    if scan_fingerprint:
        _, result.candidate_relationship_digest, _ = db.workspace_current_candidate_digest(workspace_id)
        db.save_matching_scan_cache(workspace_id, scan_fingerprint, len(normalized), asdict(result))
    else:
        _, result.candidate_relationship_digest, _ = db.workspace_current_candidate_digest(workspace_id)
    terminal_status = "complete" if result.status == "complete" else "partial"
    db.update_discovery_task(
        generation_task_id, status=terminal_status, completed=len(normalized), total=len(normalized)
    )
    # The scan transition is the final commit marker for this generation.
    db.create_discovery_scan(
        workspace_id,
        scan_key=generation_key,
        kind="exact",
        status=terminal_status,
        metadata={
            "workflow": "combined_catalog",
            "generation": generation_key,
            "run_id": operation_id,
            "candidate_relationship_digest": result.candidate_relationship_digest,
        },
    )
    emit(
        phase=result.phase, status=result.status, message="Chart matching complete" if result.status == "complete" else "Catalog matching evidence is incomplete",
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


def estimate_remaining_range(
    processed: int,
    total: int,
    elapsed_seconds: float,
    *,
    minimum_samples: int = 10,
) -> dict[str, float]:
    """Return a conservative observed-rate ETA with finite nonnegative bounds."""
    processed = max(0, int(processed))
    total = max(0, int(total))
    elapsed = max(0.0, float(elapsed_seconds))
    if processed < minimum_samples or processed <= 0 or total <= processed or elapsed <= 0:
        return {}
    rate = processed / elapsed
    if not rate or rate == float("inf"):
        return {}
    remaining = max(0.0, (total - processed) / rate)
    return {
        "estimated_remaining_seconds": remaining,
        "estimated_lower_seconds": max(0.0, remaining * 0.75),
        "estimated_upper_seconds": max(0.0, remaining * 1.35),
        "observed_songs_per_second": rate,
    }


def _scored_rank(item: tuple[Any, Any]) -> tuple[Any, ...]:
    chart, breakdown = item
    return (
        -float(getattr(breakdown, "final_score", 0.0) or 0.0),
        -float(getattr(breakdown, "quality", 0.0) or 0.0),
        str(getattr(chart, "source", "") or "").casefold(),
        int(getattr(chart, "chart_id", 0) or 0),
    )


def _rejection_exemplar(
    scored: Iterable[tuple[Any, Any]],
) -> tuple[Any, Any] | None:
    """Choose the one rejected candidate that owns the song explanation."""
    values = list(scored)
    if not values:
        return None
    soft = [
        item
        for item in values
        if not bool(getattr(item[1], "gate_factors", {}).get("hard_gate", False))
    ]
    return min(soft or values, key=_scored_rank)


def _primary_rejection_reason(
    classification: str,
    scored: Iterable[tuple[Any, Any]] | tuple[Any, Any],
) -> str:
    if classification == "no_catalog_candidate":
        return "no plausible candidate in current catalog snapshot"
    if classification in {"strong", "review"}:
        return ""
    if (
        isinstance(scored, tuple)
        and len(scored) == 2
        and hasattr(scored[1], "final_score")
    ):
        exemplar = scored
    else:
        exemplar = _rejection_exemplar(scored)
    if exemplar is None:
        return "no plausible candidate in current catalog snapshot"
    _, breakdown = exemplar
    reasons = [
        str(reason).casefold()
        for reason in (
            list(getattr(breakdown, "gate_factors", {}).get("hard_gate_reasons", ()))
            + list(getattr(breakdown, "eligibility_reasons", ()))
            + [label for label, _ in getattr(breakdown, "penalties", ())]
        )
    ]
    joined = " | ".join(reasons)
    gates = getattr(breakdown, "gate_factors", {})
    version_factors = getattr(breakdown, "match_factors", {}).get(
        "version_markers", {}
    )
    priorities = (
        ("no 5-fret", "no required 5-fret guitar"),
        ("no playable chart content", "no playable chart content"),
        ("external song identity conflict", "source song identity conflict"),
        ("artist conflict", "artist identity conflict"),
        ("title conflict", "title identity/version conflict"),
        ("modchart", "severe source issue/modchart"),
        ("severe source issue", "severe source issue/modchart"),
        ("not exportable", "chart download unavailable"),
        ("download", "chart download unavailable"),
    )
    for needle, label in priorities:
        if needle in joined:
            return label
    if (
        isinstance(version_factors, Mapping)
        and version_factors.get("matched") is False
        and (version_factors.get("track") or version_factors.get("candidate"))
    ) or any(marker in joined for marker in ("cover", "live", "remix", "version")):
        return "cover/live/remix/version conflict"
    if isinstance(gates, Mapping) and gates.get("hard_gate"):
        return "hard compatibility gate"
    return "below safe score threshold"


def _record_terminal_yield(result: CombinedDiscoveryResult, classification: str, reason: str = "") -> None:
    mapping = {
        "strong": ("recommended_strong", "strong_match_tracks"),
        "review": ("recommended_review", "review_match_tracks"),
        "no_catalog_candidate": ("no_plausible_catalog_candidate", "no_plausible_tracks"),
        "not_playable": ("hard_rejected", "hard_rejected_only_tracks"),
        "no_suitable": ("soft_rejected", "soft_rejected_only_tracks"),
        "incomplete_evidence": ("incomplete_evidence", None),
    }
    bucket, counter = mapping.get(classification, ("incomplete_evidence", None))
    result.yield_histogram[bucket] = result.yield_histogram.get(bucket, 0) + 1
    if counter:
        setattr(result, counter, int(getattr(result, counter)) + 1)
    if reason and bucket not in {"recommended_strong", "recommended_review"}:
        result.rejection_song_reasons[reason] = result.rejection_song_reasons.get(reason, 0) + 1
        result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1


def _source_summaries(
    snapshots: Iterable[LibrarySnapshot],
) -> dict[str, dict[str, int]]:
    summaries: dict[str, dict[str, int]] = {}
    for snapshot in snapshots:
        tracks = list(snapshot.tracks)
        report = snapshot.import_report
        memberships_by_track = {
            id(track): provider_track_playlist_memberships(track)
            for track in tracks
        }
        playlist_tracks = [
            track for track in tracks if memberships_by_track[id(track)]
        ]
        playlist_track_ids = {id(track) for track in playlist_tracks}
        provider = str(snapshot.provider)
        summaries[provider] = {
            "tracks": len(tracks),
            "unique": len(tracks),
            "artists": len({track.artist_norm for track in tracks}),
            "playlists": len(snapshot.playlists),
            "playlist_memberships": sum(
                len({
                    playlist_identity(provider_id, name)
                    for provider_id, name in memberships_by_track[id(track)]
                    if playlist_identity(provider_id, name)
                })
                for track in tracks
            ),
            "playlist_unique": len(playlist_tracks),
            "liked": sum(1 for track in tracks if track.liked),
            "liked_in_playlists": sum(
                1
                for track in playlist_tracks
                if track.liked
            ),
            "playlist_only": sum(
                1
                for track in playlist_tracks
                if not track.liked
            ),
            "liked_only": sum(
                1
                for track in tracks
                if track.liked and id(track) not in playlist_track_ids
            ),
            "raw_items": int(
                (snapshot.metadata or {}).get(
                    "source_item_count",
                    report.total_rows if report else len(tracks),
                )
            ),
            "raw_items_lower_bound": int(bool(
                (snapshot.metadata or {}).get("source_item_count_is_lower_bound")
            )),
            "accepted_items": int(report.accepted_rows) if report else len(tracks),
            "skipped_items": int(report.skipped_rows) if report else 0,
            "duplicate_items": int(report.duplicate_rows) if report else 0,
            "include_liked": int(bool((snapshot.metadata or {}).get("include_liked"))),
        }
    return summaries


def workspace_snapshots(db: Database, workspace_id: int | str) -> list[LibrarySnapshot]:
    """Reconstruct current provider-neutral snapshots from durable provenance."""
    membership_by_track: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for row in db.get_library_playlist_memberships(workspace_id):
        membership_by_track.setdefault(
            (int(row["source_id"]), int(row["library_track_id"])), []
        ).append(row)

    grouped: dict[str, list[ProviderTrack]] = {}
    for row in db.get_track_provenance(workspace_id):
        provider = str(row["provider"])
        memberships = membership_by_track.get(
            (int(row["source_id"]), int(row["library_track_id"])), []
        )
        grouped.setdefault(provider, []).append(ProviderTrack(
            provider=provider,
            provider_id=str(row["provider_id"]),
            title=str(row["title"]),
            artist=str(row["artist"]),
            album=str(row["album"] or ""),
            playlist_ids=[str(value["playlist_provider_id"]) for value in memberships],
            playlist_names=[str(value["playlist_name"] or "") for value in memberships],
            liked=bool(row["liked"]),
            isrc=str(row["isrc"] or ""),
            url=str(row["url"] or ""),
            metadata=json.loads(row["metadata_json"] or "{}"),
        ))

    playlists_by_provider: dict[str, dict[str, ProviderPlaylist]] = {}
    for row in db.get_library_playlists(workspace_id):
        provider = str(row["provider"])
        provider_id = str(row["provider_id"])
        playlist = ProviderPlaylist(
            provider=provider,
            provider_id=provider_id,
            name=str(row["name"] or ""),
            liked=bool(row["liked"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
        current = playlists_by_provider.setdefault(provider, {}).get(provider_id)
        if current is None:
            playlists_by_provider[provider][provider_id] = playlist
        else:
            current.name = current.name or playlist.name
            current.liked = current.liked or playlist.liked
            current.metadata.update(playlist.metadata)

    reports_by_provider: dict[str, SourceImportReport] = {}
    include_liked_by_provider: dict[str, bool] = {}
    source_items_by_provider: dict[str, int] = {}
    source_items_lower_bound_by_provider: dict[str, bool] = {}
    for row in db.get_source_summary(workspace_id):
        provider = str(row["provider"])
        report = reports_by_provider.setdefault(provider, SourceImportReport())
        report.total_rows += int(row["total_rows"] or 0)
        report.accepted_rows += int(row["accepted_rows"] or 0)
        report.skipped_rows += int(row["skipped_rows"] or 0)
        report.duplicate_rows += int(row["duplicate_rows"] or 0)
        report.unique_tracks += int(row["unique_tracks"] or 0)
        report.liked_count += int(row["liked_count"] or 0)
        source_items_by_provider[provider] = (
            source_items_by_provider.get(provider, 0)
            + int(row["source_item_count"] or 0)
        )
        source_items_lower_bound_by_provider[provider] = bool(
            source_items_lower_bound_by_provider.get(provider)
            or row["source_item_count_is_lower_bound"]
        )
        try:
            scope = json.loads(row["scope_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            scope = {}
        include_liked_by_provider[provider] = bool(
            include_liked_by_provider.get(provider) or scope.get("include_liked")
        )

    providers = set(grouped) | set(playlists_by_provider)
    return [
        LibrarySnapshot(
            provider,
            merge_provider_tracks(grouped.get(provider, [])),
            list(playlists_by_provider.get(provider, {}).values()),
            metadata={
                "import": "persisted",
                "include_liked": include_liked_by_provider.get(provider, False),
                "source_item_count": source_items_by_provider.get(provider, 0),
                "source_item_count_is_lower_bound": source_items_lower_bound_by_provider.get(provider, False),
            },
            import_report=reports_by_provider.get(provider),
        )
        for provider in sorted(providers, key=lambda value: (value != "spotify", value))
    ]


def enforce_library_limit(snapshots: Iterable[LibrarySnapshot], limit: int) -> int:
    """Bound the final cross-provider deduplicated library before matching."""
    count = len(merge_provider_tracks(
        track for snapshot in snapshots for track in snapshot.tracks
    ))
    if count > int(limit):
        raise LibraryLimitExceeded(
            f"The deduplicated library has {count:,} songs; the safe limit is {int(limit):,}."
        )
    return count


def _expansion_cap(config: AppConfig, imported_count: int) -> int:
    if imported_count >= int(config.expansion.min_songs_many):
        return max(0, int(config.expansion.cap_many_songs))
    if imported_count >= 2:
        return max(0, int(config.expansion.cap_some_songs))
    return max(0, int(config.expansion.cap_one_song))


def _canonical_imported_identities(tracks: Iterable[Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for track in tracks:
        title = str(getattr(track, "title_norm", "") or normalize_title(str(getattr(track, "title", "") or "")))
        for artist in (
            getattr(track, "artist_norm", ""),
            getattr(track, "primary_artist_norm", ""),
            normalize_artist(str(getattr(track, "artist_full", "") or "")),
        ):
            if artist and title:
                identities.add((str(artist), title))
    return identities


def _eligible_artist_catalog(
    catalog: EnchorCatalog,
    artist_norm: str,
    config: AppConfig,
    imported_identities: set[tuple[str, str]],
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Return eligible catalog songs, grouped by normalized song identity."""
    grouped: dict[tuple[str, str], list[tuple[ChartCandidate, Any]]] = {}
    for _title, charts in catalog.artist_songs(artist_norm):
        for chart in charts:
            identity = (
                normalize_artist(str(chart.artist or "")),
                normalize_title(str(chart.name or "")),
            )
            if not all(identity) or identity in imported_identities:
                continue
            if db is not None and not db.is_chart_artifact_eligible(chart.source, chart.chart_id):
                continue
            breakdown = score_expansion_candidate(artist_norm, chart, config)
            if (
                str(getattr(breakdown, "eligibility", "")).upper()
                not in {RECOMMENDED, ELIGIBLE_REVIEW}
                or bool(getattr(breakdown, "gate_factors", {}).get("hard_gate"))
            ):
                continue
            values = grouped.setdefault(identity, [])
            if not any(
                str(existing.source) == str(chart.source)
                and int(existing.chart_id) == int(chart.chart_id)
                for existing, _ in values
            ):
                values.append((chart, breakdown))

    songs = []
    for identity, values in grouped.items():
        values.sort(
            key=lambda item: (
                -float(item[1].final_score),
                -float(item[1].quality),
                str(item[0].source),
                int(item[0].chart_id),
            )
        )
        songs.append({"identity": identity, "charts": values})
    songs.sort(
        key=lambda item: (
            -float(item["charts"][0][1].final_score),
            -float(item["charts"][0][1].quality),
            item["identity"],
        )
    )
    return songs


def rank_expansion_artists(
    catalog: EnchorCatalog,
    tracks: Iterable[Any],
    config: AppConfig | None = None,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Rank imported primary artists by eligible, non-imported catalog coverage."""
    track_values = list(tracks)
    counts = Counter(str(track.primary_artist_norm) for track in track_values if track.primary_artist_norm)
    display_names: dict[str, str] = {}
    for track in track_values:
        norm = str(getattr(track, "primary_artist_norm", "") or "")
        display_names.setdefault(norm, str(getattr(track, "primary_artist", "") or norm))
    imported = _canonical_imported_identities(track_values)
    ranked = []
    for artist_norm, imported_count in counts.items():
        catalog_songs = catalog.artist_songs(artist_norm) if catalog.available else ()
        if config is None:
            eligible_songs = len(catalog_songs)
            eligible_charts = sum(len(rows) for _, rows in catalog_songs)
        else:
            songs = _eligible_artist_catalog(catalog, artist_norm, config, imported, db)
            eligible_songs = len(songs)
            eligible_charts = sum(len(song["charts"]) for song in songs)
        if eligible_songs <= 0:
            continue
        ranked.append({
            "artist": display_names.get(artist_norm, artist_norm),
            "artist_norm": artist_norm,
            "imported_songs": imported_count,
            "catalog_songs": len(catalog_songs),
            "catalog_charts": sum(len(rows) for _, rows in catalog_songs),
            "eligible_songs": eligible_songs,
            "eligible_charts": eligible_charts,
            "cap": _expansion_cap(config, imported_count) if config is not None else eligible_songs,
        })
    return sorted(
        ranked,
        key=lambda item: (
            -item["eligible_songs"],
            -item["eligible_charts"],
            -item["imported_songs"],
            item["artist"].casefold(),
        ),
    )


def _persist_expansion(
    db: Database,
    workspace_id: int,
    catalog: EnchorCatalog,
    tracks: list[Any],
    artists: Iterable[str],
    config: AppConfig,
    result: CombinedDiscoveryResult,
    *,
    exact_generation_id: int | None = None,
    on_progress: Any = None,
) -> None:
    choices = rank_expansion_artists(catalog, tracks, config, db=db)
    by_norm = {item["artist_norm"]: item for item in choices}
    selected: list[str] = []
    for value in artists:
        artist_norm = normalize_artist(str(value or ""))
        if artist_norm in by_norm and artist_norm not in selected:
            selected.append(artist_norm)

    imported = _canonical_imported_identities(tracks)
    prepared: dict[str, list[dict[str, Any]]] = {}
    claimed_song_identities: set[tuple[str, str]] = set()
    for index, artist_norm in enumerate(selected, start=1):
        choice = by_norm[artist_norm]
        rows: list[dict[str, Any]] = []
        accepted_songs = 0
        if int(choice["cap"]) <= 0:
            prepared[artist_norm] = rows
            continue
        for song in _eligible_artist_catalog(catalog, artist_norm, config, imported, db):
            identity = song["identity"]
            if identity in claimed_song_identities:
                continue
            claimed_song_identities.add(identity)
            accepted_songs += 1
            for chart, breakdown in song["charts"]:
                rows.append({
                    "candidate": chart,
                    "score": breakdown.final_score,
                    "quality": breakdown.quality,
                    "decision": breakdown.decision,
                    "eligibility": breakdown.eligibility,
                    "breakdown_json": breakdown.to_dict(),
                    "metadata": {
                        "song_identity": list(identity),
                        "artist_cap": int(choice["cap"]),
                        "cap_rank": accepted_songs,
                        "exact_generation_id": exact_generation_id,
                        "catalog_snapshot_id": str(getattr(catalog, "snapshot_id", "") or ""),
                    },
                })
            if accepted_songs >= int(choice["cap"]):
                break
        prepared[artist_norm] = rows
        if on_progress is not None:
            on_progress({
                "phase": "catalog_expansion",
                "message": f"Preparing recommendations for {choice['artist']}",
                "completed_artists": index,
                "total_artists": len(selected),
            })

    old_artists = {
        str(row["artist_norm"] or "")
        for row in db.get_workspace_candidates(workspace_id, kind="expansion")
    }
    for artist_norm in sorted(old_artists - set(selected)):
        db.replace_expansion_artist_candidates(workspace_id, artist_norm, ())
    for artist_norm in selected:
        db.replace_expansion_artist_candidates(
            workspace_id,
            artist_norm,
            prepared.get(artist_norm, ()),
            request_count=0,
        )
        result.candidates += len(prepared.get(artist_norm, ()))
    result.eligible_candidates = result.candidates
    result.expansion_artists = choices


def workspace_expansion_choices(
    config: AppConfig,
    db: Database,
    workspace_id: int | str,
    *,
    catalog: EnchorCatalog | None = None,
) -> list[dict[str, Any]]:
    """Compute public choices from the workspace's canonical imported library."""
    catalog = catalog or load_enchor_catalog(config.catalog_path)
    return rank_expansion_artists(
        catalog,
        _workspace_expansion_tracks(db, workspace_id),
        config,
        db=db,
    ) if catalog.available else []


def _workspace_expansion_tracks(db: Database, workspace_id: int | str) -> list[Any]:
    neutral_rows = db.get_neutral_library_tracks(workspace_id)
    if neutral_rows:
        from csv_to_clonehero.providers.base import provider_track_to_track

        return [provider_track_to_track(ProviderTrack(
            provider="library",
            provider_id=str(row["id"]),
            title=str(row["title"]),
            artist=str(row["artist"]),
            album=str(row["album"] or ""),
            liked=bool(row["liked"]),
        )) for row in neutral_rows]
    return db.get_spotify_tracks(True)


def expand_combined(
    config: AppConfig,
    db: Database,
    workspace_id: int | str,
    artists: Iterable[str],
    *,
    catalog: EnchorCatalog | None = None,
    exact_generation_id: int | None = None,
    on_progress: Any = None,
) -> CombinedDiscoveryResult:
    """Persist selected artist expansion candidates without network searches."""
    catalog = catalog or load_enchor_catalog(config.catalog_path)
    tracks = _workspace_expansion_tracks(db, workspace_id)
    result = CombinedDiscoveryResult(
        tracks=len(tracks),
        catalog_available=catalog.available,
        catalog_diagnostic=catalog.diagnostic,
        catalog_snapshot_id=str(getattr(catalog, "snapshot_id", "") or ""),
        catalog_chart_count=int(getattr(catalog, "chart_count", 0) or 0),
    )
    if catalog.available:
        if exact_generation_id is None:
            generation = db.get_current_exact_generation(workspace_id)
            exact_generation_id = int(generation["id"]) if generation is not None else None
        _persist_expansion(
            db,
            int(workspace_id),
            catalog,
            tracks,
            artists,
            config,
            result,
            exact_generation_id=exact_generation_id,
            on_progress=on_progress,
        )
    else:
        result.status = "partial"
        result.phase = "catalog_unavailable"
        result.error = catalog.diagnostic
    return result


def provider_track_to_track(value: Any) -> Any:
    from csv_to_clonehero.providers.base import provider_track_to_track as convert
    return convert(value)
