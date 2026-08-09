"""Candidate scoring engine.

Produces an explainable 0-100 score for each (Spotify track, chart) pair and
for each (artist, chart) expansion pair. Never accepts on title equality
alone: an artist mismatch is a hard conflict.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.models import ChartCandidate, ScoreBreakdown, Track
from csv_to_clonehero.normalization import normalize_text, version_markers

# Known "official game conversion" icon/charter signals on Enchor.
_OFFICIAL_ICONS = {
    "gh1", "gh2", "gh2dlc", "gh3", "gh3dlc", "gh80s", "gha", "ghm", "ghsh",
    "ghwt", "ghwtdlc", "ghvh", "gh5", "gh5dlc", "ghwor", "ghwordlc", "ghl",
    "rb1", "rb1dlc", "rb2", "rb2dlc", "rb3", "rb3dlc", "rb4", "rb4dlc",
    "rbblitz", "rbtb", "rbgd", "rblego", "rbpearljam", "rbgreenday",
    "fusion", "ghx", "ghx2",
}
_OFFICIAL_CHARTERS = {"harmonix", "neversoft", "wavegroup", "budcat", "vicarious visions"}

# Score composition.
TITLE_WEIGHT = 0.55
ARTIST_WEIGHT = 0.35
BASE_COMPLEMENT = 0.10  # reserved headroom so bonuses matter at the top end


def _similarity(a: str, b: str) -> float:
    """Best of plain ratio and token_set ratio for normalized strings."""
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_set_ratio(a, b))


def _best_artist_similarity(track: Track, chart: ChartCandidate) -> float:
    """Best artist match across the track's primary/full/featured forms."""
    candidates = [track.primary_artist_norm, track.artist_norm]
    best = 0.0
    chart_forms = {chart.artist_norm, normalize_text(chart.artist)}
    for track_form in candidates:
        for chart_form in chart_forms:
            best = max(best, _similarity(track_form, chart_form))
    return best


def instrument_compatible(chart: ChartCandidate, config: AppConfig) -> tuple[bool, str]:
    """Check the chart satisfies the configured instrument requirements."""
    instr = config.instruments
    if instr.exclude_modcharts and chart.modchart:
        return False, "modchart excluded"
    if chart.has_five_fret_guitar:
        return True, ""
    if chart.has_six_fret_guitar and not chart.has_five_fret_guitar:
        if instr.allow_six_fret_only and not instr.require_five_fret_guitar:
            return True, ""
        return False, "no 5-fret guitar (6-fret only)"
    if instr.allow_no_guitar:
        return True, ""
    return False, "no 5-fret guitar chart"


def score_candidate(track: Track, chart: ChartCandidate, config: AppConfig) -> ScoreBreakdown:
    """Score one chart candidate against one Spotify track."""
    breakdown = ScoreBreakdown()

    compatible, reason = instrument_compatible(chart, config)
    if not compatible:
        breakdown.decision = "INCOMPATIBLE"
        breakdown.reasons.append(reason)
        breakdown.final_score = 0.0
        return breakdown

    title_score = _similarity(track.title_norm, chart.name_norm)
    artist_score = _best_artist_similarity(track, chart)
    breakdown.title_score = title_score
    breakdown.artist_score = artist_score

    # Hard artist conflict: identical/similar title but clearly different artist.
    if artist_score < config.matching.artist_conflict_threshold:
        breakdown.decision = "REJECT"
        breakdown.reasons.append(
            f"artist conflict ({track.primary_artist!r} vs {chart.artist!r})"
        )
        breakdown.base_score = title_score * TITLE_WEIGHT
        breakdown.final_score = min(breakdown.base_score, config.matching.review_threshold - 1)
        return breakdown

    base = title_score * TITLE_WEIGHT + artist_score * ARTIST_WEIGHT
    base += 100 * BASE_COMPLEMENT  # headroom component, scaled below
    breakdown.base_score = base

    # ---- bonuses ----------------------------------------------------------
    if track.title_norm == chart.name_norm:
        breakdown.bonuses.append(("exact normalized title", 6))
    if track.primary_artist_norm == chart.artist_norm:
        breakdown.bonuses.append(("exact normalized artist", 6))
    if chart.album and track.album_norm and track.album_norm == normalize_text(chart.album):
        breakdown.bonuses.append(("album match", 4))
    icon = chart.icon.lower()
    if icon in _OFFICIAL_ICONS or normalize_text(chart.charter) in _OFFICIAL_CHARTERS:
        breakdown.bonuses.append(("official game conversion", 5))
    if chart.instrument_count >= 4:
        breakdown.bonuses.append(("full band chart", 3))
    elif chart.instrument_count >= 2:
        breakdown.bonuses.append(("multi-instrument chart", 2))
    metadata_fields = [chart.charter, chart.album, chart.genre, chart.year, chart.song_length_ms]
    completeness = sum(1 for f in metadata_fields if f) / len(metadata_fields)
    if completeness >= 0.8:
        breakdown.bonuses.append(("complete metadata", 2))

    # ---- penalties --------------------------------------------------------
    track_markers = version_markers(track.title)
    chart_markers = version_markers(chart.name)
    if track_markers != chart_markers:
        only_track = track_markers - chart_markers
        only_chart = chart_markers - track_markers
        if only_chart:
            breakdown.penalties.append(
                (f"version mismatch (chart is {'/'.join(sorted(only_chart))})", 25)
            )
        elif only_track:
            breakdown.penalties.append(("version mismatch (chart is original)", 15))
    if chart.has_issues:
        breakdown.penalties.append(("chart/metadata issues reported", 5))

    total = base
    total += sum(v for _, v in breakdown.bonuses)
    total -= sum(v for _, v in breakdown.penalties)
    breakdown.final_score = max(0.0, min(100.0, total))

    if breakdown.final_score >= config.matching.auto_accept_threshold:
        breakdown.decision = "AUTO_ACCEPT"
    elif breakdown.final_score >= config.matching.review_threshold:
        breakdown.decision = "REVIEW"
    else:
        breakdown.decision = "REJECT"
    return breakdown


def score_expansion_candidate(
    artist_norm: str, chart: ChartCandidate, config: AppConfig
) -> ScoreBreakdown:
    """Score a chart for artist-expansion (artist must match strongly)."""
    breakdown = ScoreBreakdown()

    compatible, reason = instrument_compatible(chart, config)
    if not compatible:
        breakdown.decision = "INCOMPATIBLE"
        breakdown.reasons.append(reason)
        return breakdown

    artist_score = _similarity(artist_norm, chart.artist_norm)
    breakdown.artist_score = artist_score
    breakdown.title_score = 0.0

    # Expansion requires a strong artist identity match.
    if artist_score < 80.0:
        breakdown.decision = "REJECT"
        breakdown.reasons.append(
            f"expansion artist mismatch ({artist_norm!r} vs {chart.artist_norm!r})"
        )
        breakdown.final_score = 0.0
        return breakdown

    # Quality-only base: artist identity contributes up to 60 points.
    base = 30.0 + (artist_score - 80.0) * 1.5  # 30..60
    breakdown.base_score = base

    icon = chart.icon.lower()
    if icon in _OFFICIAL_ICONS or normalize_text(chart.charter) in _OFFICIAL_CHARTERS:
        breakdown.bonuses.append(("official game conversion", 20))
    if chart.diff_guitar is not None:
        breakdown.bonuses.append(("5-fret guitar difficulty rated", 10))
    if chart.instrument_count >= 4:
        breakdown.bonuses.append(("full band chart", 8))
    elif chart.instrument_count >= 2:
        breakdown.bonuses.append(("multi-instrument chart", 4))
    metadata_fields = [chart.charter, chart.album, chart.genre, chart.year, chart.song_length_ms]
    completeness = sum(1 for f in metadata_fields if f) / len(metadata_fields)
    breakdown.bonuses.append(("metadata completeness", round(completeness * 5, 1)))
    if chart.pack_name:
        breakdown.bonuses.append(("curated pack source", 3))

    if chart.has_issues:
        breakdown.penalties.append(("chart/metadata issues reported", 10))
    chart_markers = version_markers(chart.name)
    if chart_markers & {"karaoke", "tribute", "cover"}:
        breakdown.penalties.append(("cover/karaoke version", 30))

    total = base + sum(v for _, v in breakdown.bonuses) - sum(v for _, v in breakdown.penalties)
    breakdown.final_score = max(0.0, min(100.0, total))
    breakdown.decision = (
        "AUTO_ACCEPT" if breakdown.final_score >= config.expansion.min_score else "REJECT"
    )
    return breakdown


def pick_best(
    track: Track, charts: list[ChartCandidate], config: AppConfig
) -> tuple[ChartCandidate | None, ScoreBreakdown | None, list[tuple[ChartCandidate, ScoreBreakdown]]]:
    """Score every candidate and pick the best one for a track.

    Returns (best_chart, best_breakdown, all_scored). best_chart is None when
    no candidates were found.
    """
    scored = [(chart, score_candidate(track, chart, config)) for chart in charts]
    scored.sort(key=lambda pair: pair[1].final_score, reverse=True)
    if not scored:
        return None, None, scored
    return scored[0][0], scored[0][1], scored
