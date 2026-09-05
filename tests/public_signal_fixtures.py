"""DTO-faithful, synthetic fixtures for the real public Signal Check UI."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit


class SignalCheckFixtures:
    """Respond at the browser network boundary without adding product routes."""

    def __init__(self, scenario: str = "confirm") -> None:
        if scenario not in {"add", "find", "confirm"}:
            raise ValueError(f"unsupported Signal Check scenario: {scenario}")
        self.scenario = scenario
        self.selected = {101, 102, 103} if scenario == "confirm" else set()
        self.request_log: list[tuple[str, str, dict[str, object]]] = []
        self.unexpected_requests: list[str] = []
        self.fail_next_selection = False
        self.cancelled = False

    @staticmethod
    def _scope() -> dict[str, object]:
        return {"include_liked": True, "playlist_ids": ["playlist-road"]}

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

    def journey(self) -> dict[str, object]:
        if self.scenario == "find":
            workspace_status = "running"
            phase = "finding_charts"
            job = {
                "id": 41,
                "kind": "discovery",
                "status": "running",
                "phase": "finding_charts",
                "message": "Comparing songs with playable community charts...",
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
        elif self.scenario == "confirm":
            workspace_status = "complete"
            phase = "complete"
            job = {
                "id": 40,
                "kind": "discovery",
                "status": "complete",
                "phase": "complete",
                "message": "Matching complete",
                "progress": {"total_tracks": 5403, "processed_tracks": 5403},
            }
        else:
            workspace_status = "ready_for_scope"
            phase = "source_setup"
            job = None
        summary = self.summary() if self.scenario == "confirm" else {
            "strong": 0,
            "review": 0,
            "unavailable": 0,
            "selected": 0,
            "selected_tracks": 0,
            "selected_charts": 0,
            "total": 0,
        }
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
            "download": None,
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

    def summary(self) -> dict[str, object]:
        selected_paramore = len(self.selected.intersection({101, 102, 103}))
        selected_radiohead = int(201 in self.selected)
        selected_tracks = int(selected_paramore > 0) + selected_radiohead
        return {
            "strong": 1,
            "review": 1,
            "unavailable": 0,
            "selected": selected_tracks,
            "selected_tracks": selected_tracks,
            "selected_charts": len(self.selected),
            "selection_count": len(self.selected),
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
            "selected_strong": int(selected_paramore > 0),
            "selected_review": selected_radiohead,
            "total": 2,
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

    def respond(self, method: str, request_url: str, post_data: str | None = None):
        parsed = urlsplit(request_url)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = json.loads(post_data or "{}") if post_data else {}
        self.request_log.append((method, path, body))

        if path == "/api/journey" and method == "GET":
            return 200, self.journey()
        if path == "/api/auth/soundcloud/status" and method == "GET":
            return 200, {"configured": False, "authenticated": False}
        if path == "/api/spotify/scope":
            if method == "GET":
                return 200, self.spotify_library(cached=False)
            return 200, {
                "ok": True,
                "workspace": {"id": 1, "exists": True, "scope": body, "soundcloud_scope": {}},
            }
        if path == "/api/preflight":
            return 200, {"preflight": {"usable": True, "authoritative": True, "message": "Ready"}}
        if path == "/api/jobs" and method == "POST":
            self.scenario = "find"
            return 202, {"job": {"id": 41, "kind": "discovery", "status": "queued", "phase": "queued"}}
        if path == "/api/jobs/41" and method == "GET":
            job = self.journey()["job"]
            if self.cancelled:
                job = {**job, "status": "cancelled", "message": "Search cancelled"}
            return 200, {"job": job}
        if path == "/api/jobs/41/songs" and method == "GET":
            return 200, {
                "songs": [
                    {"artist": "Paramore", "title": "Misery Business", "classification": "checking"},
                    {"artist": "Radiohead", "title": "Weird Fishes / Arpeggi", "classification": "waiting"},
                ],
                "total": 2,
                "offset": 0,
                "limit": 50,
            }
        if path == "/api/jobs/41/cancel" and method == "POST":
            self.cancelled = True
            return 200, {"ok": True}
        if path == "/api/workspace" and method == "GET":
            return 200, self.workspace(query)
        if path == "/api/selection" and method == "POST":
            if self.fail_next_selection:
                self.fail_next_selection = False
                return 503, {"error": "Selection service is temporarily unavailable."}
            candidate_id = int(body.get("candidate_id") or 0)
            if candidate_id == 201 and body.get("action") == "select" and not body.get("confirm_possible"):
                return 400, {"error": "Possible matches require explicit confirmation."}
            if body.get("action") == "clear":
                self.selected.discard(candidate_id)
            else:
                self.selected.add(candidate_id)
            return 200, {"ok": True}
        if path == "/api/selection/bulk" and method == "POST":
            action = body.get("action")
            preset = body.get("preset")
            if action == "clear":
                self.selected.difference_update({101, 102, 103})
                scope = "all"
            elif preset == "best_matches":
                self.selected.difference_update({101, 102, 103})
                self.selected.add(101)
                scope = preset
            elif preset == "all_safe_charts":
                self.selected.difference_update({101, 102, 103})
                self.selected.update({101, 102})
                scope = preset
            elif preset == "maximum_playable":
                self.selected.difference_update({101, 102, 103})
                self.selected.update({101, 102, 103})
                scope = preset
            else:
                return 400, {"error": "Unknown selection preset."}
            return 200, {"ok": True, "changed": 3, "minimum_score": 50.0, "scope": scope}
        if path == "/api/expansion/artists" and method == "GET":
            return 200, {
                "available": False,
                "reason": "Artist expansion is outside this visual checkpoint.",
                "max_artists": 25,
                "artists": [],
                "job": None,
            }
        if path == "/api/expansion/results" and method == "GET":
            return 200, {"songs": [], "total": 0, "offset": 0, "limit": 25, "has_more": False, "summary": {"total": 0, "selected": 0}}
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
            content_type="application/json",
            body=json.dumps(payload),
        )
