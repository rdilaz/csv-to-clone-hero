"""Core data models shared across the application.

These are plain dataclasses: the rest of the app must never depend on
source-specific (e.g. Enchor) payload shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
