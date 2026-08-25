"""Core data models shared across the application.

These are plain dataclasses: the rest of the app must never depend on
source-specific (e.g. Enchor) payload shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Album bucket for tracks with no album metadata (master list + library layout).
SINGLES_ALBUM_LABEL = "Singles"


@dataclass
class Track:
    """A unique Spotify track assembled from one or more playlist CSV rows."""

    track_key: str  # spotify id, or "normalized_primary_artist|normalized_title"
    title: str
    title_norm: str
    artist_full: str
    artist_norm: str
    primary_artist: str
    primary_artist_norm: str
    featured_artists: list[str] = field(default_factory=list)
    album: str = ""
    album_norm: str = ""
    spotify_id: str = ""
    playlists: list[str] = field(default_factory=list)
    db_id: int | None = None
    # Spotify/library metadata is optional so CSV and iTunes callers keep the
    # original constructor contract.
    duration_ms: int | None = None
    isrc: str = ""
    spotify_url: str = ""
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    album_id: str = ""
    added_at: str | None = None
    origin: str = ""


@dataclass
class ChartCandidate:
    """A single chart offered by a chart source."""

    source: str
    chart_id: int
    md5: str
    name: str
    artist: str
    name_norm: str
    artist_norm: str
    charter: str = ""
    album: str = ""
    genre: str = ""
    year: str = ""
    song_length_ms: int | None = None
    diff_guitar: int | None = None
    diff_guitarghl: int | None = None
    diff_bass: int | None = None
    diff_drums: int | None = None
    diff_vocals: int | None = None
    diff_keys: int | None = None
    instruments: list[str] = field(default_factory=list)
    has_video_background: bool = False
    modchart: bool = False
    has_issues: bool = False
    icon: str = ""
    pack_name: str = ""
    modified_time: str = ""
    url: str = ""
    supports_download: bool = True
    db_id: int | None = None
    # Source-owned provenance and review metadata.  ``artwork_url`` and
    # ``artwork_id`` are retained as convenient aliases for integrations that
    # do not use the more explicit source-prefixed names.
    source_artwork_url: str = ""
    source_artwork_id: str = ""
    issue_details: list[str] = field(default_factory=list)
    issue_severity: str = ""
    review_status: str = ""
    review_url: str = ""
    song_id: int | str | None = None
    version_id: int | str | None = None
    version_group_id: int | str | None = None
    group_id: int | str | None = None
    source_total: int | None = None
    cap_reached: bool = False
    returned_count: int | None = None
    # Normalized Bridge/Enchor difficulty fields.  The original six fields
    # above remain untouched for legacy matchers and callers.
    diff_band: int | None = None
    diff_guitar_coop: int | None = None
    diff_rhythm: int | None = None
    diff_drums_real: int | None = None
    diff_guitar_coop_ghl: int | None = None
    diff_rhythm_ghl: int | None = None
    diff_bassghl: int | None = None
    diff_guitar_ghl: int | None = None
    diff_bass_ghl: int | None = None
    diff_vocals_harm1: int | None = None
    diff_vocals_harm2: int | None = None
    diff_vocals_harm3: int | None = None
    difficulty_fields: dict[str, int | None] = field(default_factory=dict)
    difficulties: dict[str, int | None] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    artwork_url: str = ""
    artwork_id: str = ""
    issues: list[str] = field(default_factory=list)
    diff_guitarghl_coop: int | None = None
    diff_rhythmghl: int | None = None

    def __post_init__(self) -> None:
        """Keep source-artwork aliases in sync without changing old callers."""
        if isinstance(self.issue_details, str):
            self.issue_details = [self.issue_details]
        if isinstance(self.issues, str):
            self.issues = [self.issues]
        if not self.source_artwork_url and self.artwork_url:
            self.source_artwork_url = self.artwork_url
        if not self.artwork_url and self.source_artwork_url:
            self.artwork_url = self.source_artwork_url
        if not self.source_artwork_id and self.artwork_id:
            self.source_artwork_id = self.artwork_id
        if not self.artwork_id and self.source_artwork_id:
            self.artwork_id = self.source_artwork_id
        if not self.issue_details and self.issues:
            self.issue_details = list(self.issues)
        if not self.issues and self.issue_details:
            self.issues = list(self.issue_details)

    @property
    def has_five_fret_guitar(self) -> bool:
        if "guitar" in self.instruments:
            return True
        return self.diff_guitar is not None and self.diff_guitar >= 0

    @property
    def has_six_fret_guitar(self) -> bool:
        if "guitarghl" in self.instruments:
            return True
        return self.diff_guitarghl is not None and self.diff_guitarghl >= 0

    @property
    def instrument_count(self) -> int:
        return len(self.instruments)

    @property
    def reference(self) -> str:
        """Human-usable reference for manual lookup."""
        return self.url or f"{self.source}:{self.chart_id} (md5 {self.md5})"


@dataclass
class ScoreBreakdown:
    """Explainable result of scoring one candidate against one query."""

    title_score: float = 0.0
    artist_score: float = 0.0
    base_score: float = 0.0
    bonuses: list[tuple[str, float]] = field(default_factory=list)
    penalties: list[tuple[str, float]] = field(default_factory=list)
    final_score: float = 0.0
    decision: str = "REJECT"  # AUTO_ACCEPT | REVIEW | REJECT | INCOMPATIBLE
    reasons: list[str] = field(default_factory=list)

    @property
    def quality(self) -> float:
        """Net bonus/penalty signal. Breaks ties when final_score saturates
        at the 100 cap, so higher-quality charts still win the plan."""
        return sum(v for _, v in self.bonuses) - sum(v for _, v in self.penalties)

    def explain(self) -> str:
        parts = [
            f"title={self.title_score:.1f}",
            f"artist={self.artist_score:.1f}",
            f"base={self.base_score:.1f}",
        ]
        for label, value in self.bonuses:
            parts.append(f"+{value:.0f} {label}")
        for label, value in self.penalties:
            parts.append(f"-{value:.0f} {label}")
        parts.append(f"final={self.final_score:.1f}")
        parts.append(f"decision={self.decision}")
        if self.reasons:
            parts.append(f"({'; '.join(self.reasons)})")
        return " ".join(parts)


@dataclass
class DownloadInfo:
    """How to download a chart from a source."""

    url: str
    md5: str = ""
    size_bytes: int | None = None
    filename: str = ""
    supports_direct_download: bool = True
    notes: str = ""


@dataclass
class Selection:
    """A chart chosen for the library."""

    kind: str  # "exact" | "expansion"
    candidate: ChartCandidate
    score: float
    explanation: str
    priority_tier: int  # 0 exact, 1 high-expansion, 2 medium, 3 low
    planned_bytes: int
    target_relpath: str
    track_key: str = ""
    artist_name: str = ""  # for expansion
    status: str = "selected"  # selected | skipped_for_space
