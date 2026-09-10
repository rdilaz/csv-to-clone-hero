"""DTO-faithful, synthetic fixtures for the real public Signal Check UI."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from io import BytesIO
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile


class SignalCheckFixtures:
    """Respond at the browser network boundary without adding product routes.

    ``selected`` holds exact candidate IDs; ``expansion_selected`` holds expansion
    IDs. ``expansion_groups`` is the visible results list (empty until expansion
    completes); ``expansion_catalog`` supplies groups for artist jobs. Both accept
    server-shaped song groups, conveniently built with ``expansion_group``.

    Export controls are candidate-based: ``export_failures`` / ``export_invalid``
    map IDs to reasons, ``export_duplicates`` maps IDs to a shared artifact ID,
    and ``export_remaining`` / ``export_already_present`` are ID sets. Counts are
    derived from current selected candidates. ``export_summary_overrides`` is an
    explicit escape hatch for testing unusual server summaries, not default data.
    Clear failure controls before retrying; ``fail_next_download`` instead fails
    just one POST (True or an error string). ``last_export_summary`` is inspection
    state only and is never included in the journey DTO.

    Download/Done scenario names seed Confirm data, not a fictitious server stage.
    Drive the production buttons to reach those surfaces. A restored Done state
    can be seeded by calling ``respond('POST', '/api/download')`` before loading.
    ``clock`` and ``download_ttl_seconds`` permit deterministic expiry tests.
    Call ``attach_downloads(app)`` for native browser downloads: Chromium can
    bypass Playwright routing for links with the download attribute. This wraps
    the existing token URL at the WSGI boundary, without adding Flask routes.

    Add-started discovery completes on poll 3; expansion jobs complete on poll 2.
    Set ``discovery_complete_after_polls`` / ``expansion_complete_after_polls``
    to another poll count or None to keep running. The initial Find checkpoint
    always stays stationary until a new discovery POST starts an interactive run.
    """

    def __init__(self, scenario: str = "confirm") -> None:
        if scenario not in {"add", "find", "confirm", "download", "done"}:
            raise ValueError(f"unsupported Signal Check scenario: {scenario}")
        self.scenario = scenario
        self.selected = {101, 102, 103} if scenario not in {"add", "find"} else set()
        self.request_log: list[tuple[str, str, dict[str, object]]] = []
        self.unexpected_requests: list[str] = []
        self.fail_next_selection = False
        self.cancelled = False
        self.spotify_scope = {"include_liked": True, "playlist_ids": ["playlist-road"]}
        self.discovery_complete_after_polls: int | None = 3
        self.discovery_polls = 0
        self._interactive_discovery = False
        self._discovery_job_id = 41 if scenario == "find" else 40
        self.expansion_available = True
        self.expansion_artists_selected: set[str] = set()
        self.expansion_selected: set[int] = set()
        self.expansion_catalog = [
            self.expansion_group("Paramore", "That's What You Get", [301, 302], album="Riot!"),
            self.expansion_group("Radiohead", "Bodysnatchers", [401], album="In Rainbows"),
        ]
        self.expansion_groups: list[dict[str, Any]] = []
        self.expansion_complete_after_polls: int | None = 2
        self.expansion_polls = 0
        self._expansion_status: str | None = None
        self.export_failures: dict[int, str] = {}
        self.export_invalid: dict[int, str] = {}
        self.export_duplicates: dict[int, int] = {}
        self.export_remaining: set[int] = set()
        self.export_already_present: set[int] = set()
        self.export_errors: list[str] = []
        self.export_summary_overrides: dict[str, Any] = {}
        self.fail_next_download: bool | str = False
        self.download_attempts = 0
        self.download_ttl_seconds = 900
        self.clock: Callable[[], float] = time.time
        self.last_export_summary: dict[str, Any] | None = None
        self._downloads: dict[str, dict[str, Any]] = {}
        self._download_prefix = f"signal-{uuid4().hex}-"
        self._download_lock = RLock()

    def _scope(self) -> dict[str, object]:
        return deepcopy(self.spotify_scope)

    def spotify_library(self, *, cached: bool = True) -> dict[str, object]:
        scope = self._scope()
        return {
            "liked_count": 5187,
            "playlists": [
                {
                    "id": "playlist-road",
                    "name": "Road trip",
                    "track_count": 310,
                    "owner_name": "Signal Player",
                },
                {
                    "id": "playlist-heavy",
                    "name": "Heavy rotation",
                    "track_count": 124,
                    "owner_name": "Signal Player",
                },
                {
                    "id": "playlist-shared",
                    "name": "Shared discoveries",
                    "track_count": None,
                    "owner_name": "Signal Player",
                },
            ],
            "workspace": {
                "id": 1,
                "exists": True,
                "scope": scope,
                "soundcloud_scope": {},
            },
            "cache_available": True,
            "cached": cached,
            "cache_source": "spotify" if not cached else "latest_import",
            "last_refreshed_at": 1788552000,
            "last_imported_at": 1788551000,
            "known_unique_songs": 5403,
            "import_status": "active",
            "imported_scope": {"known": True, **scope},
        }

    def discovery_job(self) -> dict[str, Any] | None:
        if self.scenario == "find":
            workspace_status = "cancelled" if self.cancelled else "running"
            phase = "cancelled" if self.cancelled else "finding_charts"
            job = {
                "id": self._discovery_job_id,
                "kind": "discovery",
                "status": workspace_status,
                "phase": phase,
                "message": "Search cancelled" if self.cancelled else "Comparing songs with playable community charts...",
                "progress": {
                    "total_tracks": 5403,
                    "processed_tracks": 2184,
                    "elapsed_seconds": 2,
                    "current_song": {
                        "artist": "Paramore",
                        "title": "Misery Business",
                    },
                },
            }
        elif self.scenario != "add":
            job = {
                "id": self._discovery_job_id,
                "kind": "discovery",
                "status": "complete",
                "phase": "complete",
                "message": "Matching complete",
                "progress": {"total_tracks": 5403, "processed_tracks": 5403},
            }
        else:
            job = None
        return job

    def journey(self) -> dict[str, object]:
        job = self.discovery_job()
        workspace_status = job["status"] if job else "ready_for_scope"
        phase = job["phase"] if job else "source_setup"
        summary = self.summary() if self.scenario not in {"add", "find"} else {
            "strong": 0,
            "review": 0,
            "unavailable": 0,
            "selected": 0,
            "selected_tracks": 0,
            "selected_charts": 0,
            "total": 0,
        }
        if self._expansion_status is not None:
            job = self.expansion_job()
        return {
            "workspace_id": 1,
            "workspace_status": workspace_status,
            "workspace": {
                "id": 1,
                "status": workspace_status,
                "phase": phase,
                "updated_at": 1788552000,
            },
            "spotify_scope": self._scope(),
            "spotify_library": self.spotify_library(),
            "soundcloud_library": {
                "status": "",
                "known_unique_songs": None,
                "last_imported_at": None,
                "current_scope": {},
                "imported_scope": {
                    "known": True,
                    "include_liked": False,
                    "playlist_ids": [],
                },
            },
            "sources": [
                {
                    "provider": "spotify",
                    "status": "active",
                    "tracks": 7219,
                    "unique_tracks": 5403,
                    "provenance": "spotify",
                    "playlists": 1,
                    "playlist_memberships": 310,
                    "playlist_unique_tracks": 278,
                    "liked": 5187,
                    "liked_in_playlists": 62,
                    "playlist_only": 216,
                    "liked_only": 5125,
                    "raw_items": 5497,
                    "raw_items_lower_bound": False,
                    "accepted_items": 5497,
                    "skipped_items": 0,
                    "duplicate_items": 94,
                    "include_liked": True,
                    "updated_at": 1788552000,
                }
            ],
            "combined": {"tracks": 5403, "artists": 1288},
            "job": job,
            "review": summary,
            "download": self.download,
        }

    @staticmethod
    def _review(
        *,
        state: str,
        label: str,
        summary: str,
        concerns: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "state": state,
            "label": label,
            "summary": summary,
            "why": [summary],
            "concerns": concerns or [],
            "details": [],
            "score": 94.0,
            "hard_gate": False,
            "selectable": True,
            "action": "Select chart",
            "selected": False,
            "selected_label": "",
        }

    def _candidate(
        self,
        candidate_id: int,
        *,
        title: str,
        charter: str,
        tier: str,
        instruments: list[str],
        difficulty: int | None,
        best: bool = False,
        reasons: list[str] | None = None,
        concerns: list[str] | None = None,
    ) -> dict[str, object]:
        possible = tier == "POSSIBLE_MATCH"
        review = self._review(
            state="needs_review" if possible else "strong",
            label="Possible match" if possible else "Recommended",
            summary=(
                "Song identity needs explicit review."
                if possible
                else "The song and artist identities agree."
            ),
            concerns=concerns,
        )
        selected = candidate_id in self.selected
        review["selected"] = selected
        return {
            "candidate_id": candidate_id,
            "source": "enchor",
            "chart_id": f"signal-{candidate_id}",
            "artist": "Paramore" if candidate_id < 200 else "Radiohead",
            "title": title,
            "charter": charter,
            "album": "Riot!" if candidate_id < 200 else "In Rainbows",
            "pack_name": "Signal Check fixtures",
            "instruments": instruments,
            "diff_guitar": difficulty,
            "url": f"https://files.enchor.us/signal-{candidate_id}.sng",
            "reference": f"signal-{candidate_id}",
            "supports_download": True,
            "score": 94.0,
            "quality": 20.0,
            "eligibility": "ACCEPTED",
            "explanation": review["summary"],
            "selected": selected,
            "best": best,
            "result_tier": tier,
            "result_tier_reasons": reasons or [],
            "selectable": True,
            "review": review,
            "review_state": review["state"],
            "review_label": review["label"],
            "review_summary": review["summary"],
            "selection_action": "clear" if selected else "select",
        }

    def tracks(self) -> list[dict[str, object]]:
        paramore_candidates = [
            self._candidate(
                101,
                title="Misery Business",
                charter="Atlas Charts",
                tier="RECOMMENDED_EXACT",
                instruments=["guitar", "bass", "drums"],
                difficulty=5,
                best=True,
            ),
            self._candidate(
                102,
                title="Misery Business",
                charter="Paper Circuit",
                tier="SAME_RECORDING_CHART_VARIANT",
                instruments=["guitar", "bass"],
                difficulty=0,
                reasons=["additional chart artifact for this recording"],
                concerns=["Chart metadata does not list a release date."],
            ),
            self._candidate(
                103,
                title="Misery Business (Live at the Metro)",
                charter="Signal Fires",
                tier="ALTERNATE_RECORDING",
                instruments=["guitar", "drums"],
                difficulty=4,
                reasons=["meaningful version marker differs: live"],
                concerns=["Different live recording."],
            ),
        ]
        possible = self._candidate(
            201,
            title="Weird Fishes",
            charter="Night Relay",
            tier="POSSIBLE_MATCH",
            instruments=["guitar", "bass"],
            difficulty=-1,
            best=True,
            reasons=["recording duration differs"],
            concerns=["Song identity requires explicit review."],
        )
        return [
            {
                "track_key": "spotify:paramore:misery-business",
                "spotify_id": "paramore-misery-business",
                "artist": "Paramore",
                "title": "Misery Business",
                "album": "Riot!",
                "candidates": paramore_candidates,
                "best_candidate_id": 101,
                "selected_candidate_id": next(
                    (item["candidate_id"] for item in paramore_candidates if item["selected"]),
                    None,
                ),
                "selected_candidate_ids": [
                    item["candidate_id"] for item in paramore_candidates if item["selected"]
                ],
                "selected_count": sum(bool(item["selected"]) for item in paramore_candidates),
                "selected": any(bool(item["selected"]) for item in paramore_candidates),
                "review": paramore_candidates[0]["review"],
                "review_state": "strong",
                "review_label": "Recommended",
                "review_summary": "The song and artist identities agree.",
                "review_notice": "",
            },
            {
                "track_key": "spotify:radiohead:weird-fishes",
                "spotify_id": "radiohead-weird-fishes",
                "artist": "Radiohead",
                "title": "Weird Fishes / Arpeggi",
                "album": "In Rainbows",
                "candidates": [possible],
                "best_candidate_id": 201,
                "selected_candidate_id": 201 if possible["selected"] else None,
                "selected_candidate_ids": [201] if possible["selected"] else [],
                "selected_count": int(bool(possible["selected"])),
                "selected": bool(possible["selected"]),
                "review": possible["review"],
                "review_state": "needs_review",
                "review_label": "Possible match",
                "review_summary": "Song identity needs explicit review.",
                "review_notice": "",
            },
        ]

    @staticmethod
    def expansion_group(
        artist: str, title: str, candidate_ids: list[int], *, album: str = ""
    ) -> dict[str, Any]:
        """Build a synthetic server-shaped group; selections come from the ID set."""
        if not candidate_ids:
            raise ValueError("an expansion song needs at least one candidate")
        norm = artist.casefold()
        return {
            "song_key": f"{norm}|{title.casefold()}",
            "artist": artist,
            "title": title,
            "artist_norm": norm,
            "selected": False,
            "selected_candidate_id": None,
            "selected_candidate_ids": [],
            "selected_count": 0,
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "kind": "expansion",
                    "artist_norm": norm,
                    "source": "enchor",
                    "chart_id": f"signal-{candidate_id}",
                    "artist": artist,
                    "title": title,
                    "charter": "Atlas Charts" if index == 0 else "Paper Circuit",
                    "album": album,
                    "pack_name": "Signal Check fixtures",
                    "instruments": ["guitar", "bass", "drums"],
                    "diff_guitar": 4,
                    "score": 94.0,
                    "quality": 20.0,
                    "eligibility": "RECOMMENDED",
                    "selected": False,
                    "selectable": True,
                    "result_tier": "RECOMMENDED_EXACT" if index == 0 else "SAME_RECORDING_CHART_VARIANT",
                    "result_tier_reasons": [] if index == 0 else ["additional expansion chart artifact"],
                    **({"best": True} if index == 0 else {}),
                    "review": {
                        "state": "strong",
                        "label": "Recommended expansion",
                        "summary": "A playable catalog chart from an artist already in your library.",
                        "concerns": [],
                        "selectable": True,
                        "action": "Select",
                    },
                }
                for index, candidate_id in enumerate(candidate_ids)
            ],
        }

    def expansion_songs(self) -> list[dict[str, Any]]:
        songs = deepcopy(self.expansion_groups)
        for song in songs:
            selected = []
            for candidate in song["candidates"]:
                candidate["selected"] = (
                    candidate["candidate_id"] in self.expansion_selected
                    and bool(candidate.get("selectable"))
                )
                if candidate["selected"]:
                    selected.append(candidate["candidate_id"])
            song.update(
                selected=bool(selected),
                selected_candidate_id=next(iter(selected), None),
                selected_candidate_ids=selected,
                selected_count=len(selected),
            )
        return sorted(songs, key=lambda song: (song["artist"].casefold(), song["title"].casefold()))

    def expansion_results(self, query: dict[str, list[str]]) -> dict[str, Any]:
        # The public endpoint pages song groups and does NOT filter by selection.
        songs = self.expansion_songs()
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        limit = min(100, max(1, int((query.get("limit") or ["25"])[0])))
        return {
            "songs": songs[offset : offset + limit],
            "total": len(songs),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(songs),
            "summary": {
                "total": len(songs),
                "selected": sum(song["selected_count"] for song in songs),
            },
        }

    def expansion_job(self) -> dict[str, Any] | None:
        if self._expansion_status is None:
            return None
        complete = self._expansion_status == "complete"
        return {
            "id": 42,
            "kind": "expansion",
            "status": self._expansion_status,
            "phase": "complete" if complete else "catalog_expansion",
            "message": "Artist recommendations are ready." if complete else "Preparing artist recommendations...",
            "progress": {
                "artists": sorted(self.expansion_artists_selected),
                "exact_generation_id": 1,
                "total_artists": len(self.expansion_artists_selected),
                "completed_artists": len(self.expansion_artists_selected) if complete else 0,
                "network_requests": 0,
            },
        }

    def expansion_artists(self) -> dict[str, Any]:
        available = self.expansion_available and self.scenario not in {"add", "find"}
        artists = []
        for norm in sorted({song["artist_norm"] for song in self.expansion_catalog}):
            songs = [song for song in self.expansion_catalog if song["artist_norm"] == norm]
            imported = [track for track in self.tracks() if track["artist"].casefold() == norm]
            artists.append({
                "artist": songs[0]["artist"],
                "artist_norm": norm,
                "imported_songs": len(imported),
                "catalog_songs": len(songs) + len(imported),
                "catalog_charts": sum(len(song["candidates"]) for song in songs + imported),
                "eligible_songs": len(songs),
                "eligible_charts": sum(len(song["candidates"]) for song in songs),
                "cap": 10 if len(imported) == 1 else 25 if len(imported) < 5 else 50,
                "selected": norm in self.expansion_artists_selected,
            })
        return {
            "enabled": True,
            "available": available,
            "reason": "" if available else "Artist expansion is unavailable in this fixture." if not self.expansion_available else "Complete catalog-backed exact matching before choosing artists.",
            "exact_generation_id": 1 if self.scenario not in {"add", "find"} else None,
            "catalog_snapshot_id": "signal-check-synthetic",
            "max_artists": 25,
            "artists": artists if available else [],
            "job": self.expansion_job(),
        }

    @property
    def selected_candidates(self) -> list[dict[str, Any]]:
        """Unique selected chart candidates across exact and expansion results."""
        return list({
            candidate["candidate_id"]: candidate
            for song in self.tracks() + self.expansion_songs()
            for candidate in song["candidates"]
            if candidate["selected"] and candidate.get("selectable")
        }.values())

    @property
    def download(self) -> dict[str, Any] | None:
        """Newest unconsumed metadata, exactly as returned by GET /api/journey."""
        with self._download_lock:
            now = self.clock()
            self._downloads = {
                token: item for token, item in self._downloads.items()
                if int(item["metadata"]["expires_at"]) >= now
            }
            if not self._downloads:
                return None
            token = next(reversed(self._downloads))
            item = self._downloads[token]
            return {**item["metadata"], "url": f"/api/download/{token}", "created_at": item["created_at"]}

    def attach_downloads(self, app) -> None:
        """Serve only this fixture's synthetic tokens, including native downloads."""
        from werkzeug.wrappers import Response

        original = app.wsgi_app
        prefix = f"/api/download/{self._download_prefix}"

        def synthetic_download(environ, start_response):
            path = environ.get("PATH_INFO", "")
            if environ.get("REQUEST_METHOD") != "GET" or not path.startswith(prefix):
                return original(environ, start_response)
            status, payload = self.respond("GET", path)
            response = Response(
                payload if isinstance(payload, bytes) else json.dumps(payload),
                status=status,
                content_type="application/zip" if isinstance(payload, bytes) else "application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="riff-library.zip"',
                    "X-Content-Type-Options": "nosniff",
                } if isinstance(payload, bytes) else None,
            )
            return response(environ, start_response)

        app.wsgi_app = synthetic_download

    def prepare_download(self) -> tuple[int, dict[str, Any]]:
        """Synchronous export using only tiny, explicitly synthetic ZIP members."""
        self.download_attempts += 1
        self.last_export_summary = None
        if self.fail_next_download:
            message = self.fail_next_download
            self.fail_next_download = False
            return 500, {"error": message if isinstance(message, str) else "Could not create your download right now; try again in a moment."}
        candidates = self.selected_candidates
        if not candidates:
            return 400, {"error": "No charts are selected for download."}

        ids = {candidate["candidate_id"] for candidate in candidates}
        invalid = ids.intersection(self.export_invalid)
        failures = ids.intersection(self.export_failures) - invalid
        remaining = ids.intersection(self.export_remaining) - invalid - failures
        ready = ids - invalid - failures - remaining
        physical = {}
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            if candidate_id in ready:
                artifact_id = self.export_duplicates.get(candidate_id, candidate_id)
                physical.setdefault(artifact_id, candidate)

        content = BytesIO()
        with ZipFile(content, "w", compression=ZIP_DEFLATED) as archive:
            for artifact_id, candidate in physical.items():
                # Not provider bytes or playable charts; browser-download test data only.
                archive.writestr(
                    f"synthetic/chart-{artifact_id}.sng",
                    f"Signal Check synthetic fixture, not a playable chart.\n{candidate['artist']} - {candidate['title']}\n",
                )
        zip_bytes = content.getvalue()
        errors = [
            f"{candidate['artist']} - {candidate['title']} ({candidate['charter']}): "
            f"{self.export_invalid.get(candidate['candidate_id']) or self.export_failures.get(candidate['candidate_id'])}"
            for candidate in candidates if candidate["candidate_id"] in invalid | failures
        ] + list(self.export_errors)
        # failed includes invalid; remaining excludes both failures and ready items.
        summary = {
            "success": bool(physical),
            "published": len(physical),
            "failed": len(invalid | failures),
            "invalid": len(invalid),
            "download_failures": len(failures),
            "already_present": len(ready.intersection(self.export_already_present)),
            "ready": len(ready),
            "selected": len(candidates),
            "remaining": len(remaining),
            "status": "cancelled" if remaining else "success" if physical else "failed",
            "bytes": len(zip_bytes),
            "errors": errors,
            **deepcopy(self.export_summary_overrides),
        }
        self.last_export_summary = deepcopy(summary)
        if not summary["success"] or not physical:
            return 400, {"error": "Could not assemble a download: " + ("; ".join(summary["errors"]) or "no charts exported")}
        now = self.clock()
        expires_at = now + self.download_ttl_seconds
        token = f"{self._download_prefix}{self.download_attempts}.{int(expires_at)}.synthetic"
        metadata = {
            "token": token,
            "expires_at": expires_at,
            "ttl_seconds": self.download_ttl_seconds,
            "size_bytes": len(zip_bytes),
        }
        with self._download_lock:
            self._downloads[token] = {"metadata": metadata, "created_at": now, "body": zip_bytes}
            while len(self._downloads) > 3:
                self._downloads.pop(next(iter(self._downloads)))
        return 200, {"download": dict(metadata), "summary": summary}

    def summary(self) -> dict[str, object]:
        tracks = self.tracks()
        selected_tracks = sum(bool(track["selected"]) for track in tracks)
        selected_charts = sum(track["selected_count"] for track in tracks)
        return {
            "strong": 1,
            "review": 1,
            "unavailable": 0,
            "selected": selected_tracks,
            "selected_tracks": selected_tracks,
            "selected_charts": selected_charts,
            "selection_count": selected_charts,
            "recommended_songs": 1,
            "additional_playable_charts": 3,
            "selectable_charts": 4,
            "all_safe_charts": 2,
            "maximum_playable_charts": 3,
            "possible_match_charts": 1,
            "tier_histogram": {
                "RECOMMENDED_EXACT": 1,
                "SAME_RECORDING_CHART_VARIANT": 1,
                "ALTERNATE_RECORDING": 1,
                "POSSIBLE_MATCH": 1,
                "BLOCKED": 0,
            },
            "selected_strong": sum(bool(track["selected"]) for track in tracks if track["review_state"] == "strong"),
            "selected_review": sum(bool(track["selected"]) for track in tracks if track["review_state"] == "needs_review"),
            "total": len(tracks),
        }

    def workspace(self, query: dict[str, list[str]]) -> dict[str, object]:
        tracks = self.tracks()
        search = (query.get("search") or [""])[0].casefold()
        tiers = {
            value
            for raw in query.get("tier", [])
            for value in raw.split(",")
            if value
        }
        selected_only = (query.get("selected") or [""])[0].casefold() == "true"
        if search:
            tracks = [
                track
                for track in tracks
                if search in f"{track['artist']} {track['title']}".casefold()
            ]
        if tiers:
            tracks = [
                track
                for track in tracks
                if any(candidate["result_tier"] in tiers for candidate in track["candidates"])
            ]
        if selected_only:
            tracks = [track for track in tracks if track["selected"]]
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        limit = min(100, max(1, int((query.get("limit") or ["25"])[0])))
        return {
            "workspace": 1,
            "minimum_score": 50.0,
            "tracks": tracks[offset : offset + limit],
            "total": len(tracks),
            "offset": offset,
            "limit": limit,
            "summary": self.summary(),
        }

    def respond(
        self, method: str, request_url: str, post_data: str | None = None
    ) -> tuple[int, dict[str, Any] | bytes]:
        parsed = urlsplit(request_url)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = json.loads(post_data or "{}") if post_data else {}
        self.request_log.append((method, path, body))
        if parsed.netloc and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            unexpected = f"{method} {request_url}"
            self.unexpected_requests.append(unexpected)
            return 500, {"error": f"unexpected external fixture request: {unexpected}"}

        if path == "/api/journey" and method == "GET":
            return 200, self.journey()
        if path == "/api/auth/soundcloud/status" and method == "GET":
            return 200, {"configured": False, "authenticated": False}
        if path == "/api/spotify/scope" and method in {"GET", "POST"}:
            if method == "GET":
                return 200, self.spotify_library(cached=False)
            self.spotify_scope = deepcopy(body)
            return 200, {
                "ok": True,
                "workspace": {"id": 1, "exists": True, "scope": body, "soundcloud_scope": {}},
            }
        if path == "/api/preflight" and method in {"GET", "POST"}:
            return 200, {"preflight": {"usable": True, "authoritative": True, "message": "Ready"}}
        if path == "/api/jobs" and method == "POST":
            if body.get("kind", "discovery") != "discovery":
                return 400, {"error": "unsupported job kind"}
            if self._expansion_status == "running":
                return 409, {"error": "Wait for the active artist expansion to finish before searching again.", "job": self.expansion_job()}
            self.scenario = "find"
            self._discovery_job_id = 41
            self._interactive_discovery = True
            self.discovery_polls = 0
            self.cancelled = False
            self.selected.clear()
            self.expansion_groups = []
            self.expansion_selected.clear()
            self.expansion_artists_selected.clear()
            self._expansion_status = None
            return 202, {"job": {"id": 41, "kind": "discovery", "status": "queued", "phase": "queued"}}
        if path == f"/api/jobs/{self._discovery_job_id}" and method == "GET":
            if self.scenario == "find" and not self.cancelled:
                self.discovery_polls += 1
                if (
                    self._interactive_discovery
                    and self.discovery_complete_after_polls is not None
                    and self.discovery_polls >= self.discovery_complete_after_polls
                ):
                    self.scenario = "confirm"
            return 200, {"job": self.discovery_job()}
        if path == "/api/jobs/41/songs" and method == "GET":
            songs = [
                {"artist": track["artist"], "title": track["title"], "classification":
                    ("strong" if track["review_state"] == "strong" else "review")
                    if self.scenario != "find" else "checking" if index == 0 else "waiting"}
                for index, track in enumerate(self.tracks())
            ]
            search = (query.get("search") or [""])[0].casefold()
            songs = [song for song in songs if search in f"{song['artist']} {song['title']}".casefold()]
            offset = max(0, int((query.get("offset") or ["0"])[0]))
            limit = min(100, max(1, int((query.get("limit") or ["50"])[0])))
            return 200, {
                "songs": songs[offset : offset + limit],
                "total": len(songs),
                "offset": offset,
                "limit": limit,
            }
        if path == "/api/jobs/41/cancel" and method == "POST":
            self.cancelled = True
            return 200, {"job": self.discovery_job()}
        if path == "/api/workspace" and method == "GET":
            return 200, self.workspace(query)
        if path == "/api/selection" and method == "POST":
            if self.fail_next_selection:
                self.fail_next_selection = False
                return 503, {"error": "Selection service is temporarily unavailable."}
            candidate_id = int(body.get("candidate_id") or 0)
            kind = body.get("kind", "exact")
            if kind not in {"exact", "expansion"}:
                return 400, {"error": "candidate kind must be exact or expansion"}
            groups = self.expansion_songs() if kind == "expansion" else self.tracks()
            candidate = next((
                candidate for group in groups for candidate in group["candidates"]
                if candidate["candidate_id"] == candidate_id
                and (not body.get("track_key") or group.get("track_key") == body["track_key"])
                and (not body.get("artist_norm") or candidate.get("artist_norm") == body["artist_norm"])
            ), None)
            if candidate is None:
                return 400, {"error": "candidate does not belong to this workspace track"}
            clearing = body.get("action") in {"clear", "deselect", "remove"}
            if not clearing and not candidate.get("selectable"):
                return 400, {"error": "candidate is blocked by a hard compatibility gate"}
            if candidate["result_tier"] == "POSSIBLE_MATCH" and not clearing and not body.get("confirm_possible"):
                return 400, {"error": "Possible matches require explicit confirmation."}
            selected = self.expansion_selected if kind == "expansion" else self.selected
            if clearing:
                selected.discard(candidate_id)
            else:
                selected.add(candidate_id)
            return 200, {"ok": True}
        if path == "/api/selection/bulk" and method == "POST":
            action = body.get("action")
            preset = body.get("preset")
            before = set(self.selected)
            if action == "clear":
                self.selected.clear()
                scope = "all"
            elif preset == "best_matches":
                self.selected.clear()
                self.selected.add(101)
                scope = preset
            elif preset == "all_safe_charts":
                self.selected.clear()
                self.selected.update({101, 102})
                scope = preset
            elif preset == "maximum_playable":
                self.selected.clear()
                self.selected.update({101, 102, 103})
                scope = preset
            else:
                return 400, {"error": "Unknown selection preset."}
            return 200, {"ok": True, "changed": len(before.symmetric_difference(self.selected)), "minimum_score": 50.0, "scope": scope}
        if path == "/api/expansion/artists" and method == "GET":
            return 200, self.expansion_artists()
        if path in {"/api/expansion", "/api/expansion/artists/select"} and method == "POST":
            values = body.get("artists")
            if not isinstance(values, list):
                return 400, {"error": "artists must be a list"}
            artists = {str(value or "").strip().casefold() for value in values} - {""}
            context = self.expansion_artists()
            if not artists:
                return 400, {"error": "Choose at least one artist."}
            if len(artists) > context["max_artists"]:
                return 400, {"error": "Choose no more than 25 artists at a time."}
            if not context["available"]:
                return 409, {"error": context["reason"]}
            if artists - {artist["artist_norm"] for artist in context["artists"]}:
                return 400, {"error": "Choose artists from the current eligible list."}
            if self._expansion_status == "running":
                return 409, {"error": "An artist expansion job is already active for this workspace.", "job": self.expansion_job()}
            self.expansion_artists_selected = artists
            self.expansion_polls = 0
            self._expansion_status = "running"
            return 202, {"job": {**self.expansion_job(), "status": "queued", "phase": "queued"}}
        if path == "/api/jobs/42" and method == "GET" and self._expansion_status is not None:
            if self._expansion_status == "running":
                self.expansion_polls += 1
                if self.expansion_complete_after_polls is not None and self.expansion_polls >= self.expansion_complete_after_polls:
                    self._expansion_status = "complete"
                    self.expansion_groups = deepcopy([
                        song for song in self.expansion_catalog
                        if song["artist_norm"] in self.expansion_artists_selected
                    ])
                    self.expansion_selected.clear()
            return 200, {"job": self.expansion_job()}
        if path == "/api/expansion/results" and method == "GET":
            try:
                return 200, self.expansion_results(query)
            except (TypeError, ValueError):
                return 400, {"error": "invalid expansion paging parameters"}
        if path == "/api/download" and method == "POST":
            return self.prepare_download()
        if path.startswith("/api/download/") and method == "GET":
            token = path.removeprefix("/api/download/")
            with self._download_lock:
                item = self._downloads.pop(token, None)
            if item is None or int(item["metadata"]["expires_at"]) < self.clock():
                return 404, {"error": "Download link is invalid or has expired; run the download again."}
            return 200, item["body"]
        unexpected = f"{method} {path}"
        self.unexpected_requests.append(unexpected)
        return 500, {"error": f"unexpected fixture request: {unexpected}"}

    def fulfill(self, route) -> None:
        status, payload = self.respond(
            route.request.method,
            route.request.url,
            route.request.post_data,
        )
        route.fulfill(
            status=status,
            content_type="application/zip" if isinstance(payload, bytes) else "application/json",
            headers={
                "Content-Disposition": 'attachment; filename="riff-library.zip"',
                "Content-Length": str(len(payload)),
                "X-Content-Type-Options": "nosniff",
            } if isinstance(payload, bytes) else {},
            body=payload if isinstance(payload, bytes) else json.dumps(payload),
        )
