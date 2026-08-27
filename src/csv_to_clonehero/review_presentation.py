"""Human-facing language for persisted chart-match review data.

This module deliberately does not score or select candidates. It translates the
matching state and evidence already persisted by the matching layer into a
small vocabulary that review UIs can share.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STRONG_STATES = frozenset({
    "RECOMMENDED",
    "AUTO_ACCEPT",
    "ACCEPT",
    "EXACT_CONFIRMED",
    "EXPANSION_CONFIRMED",
})
REVIEW_STATES = frozenset({"ELIGIBLE_REVIEW", "REVIEW"})
NOT_PLAYABLE_STATES = frozenset({"INCOMPATIBLE"})
NOT_RECOMMENDED_STATES = frozenset({"EXCLUDED", "REJECT", "REJECTED", "NO_MATCH"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "on", "available", "supported"}:
        return True
    if text in {"false", "0", "no", "off", "unavailable", "unsupported"}:
        return False
    return None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _factor(factors: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = factors.get(name)
    return _mapping(value)


def _quality_value(quality: Mapping[str, Any], name: str) -> float:
    value = _number(quality.get(name))
    return value if value is not None else 0.0


def _labels(values: Any) -> list[str]:
    if isinstance(values, (list, tuple, set)):
        return [str(value).strip().casefold() for value in values if str(value).strip()]
    return []


def _human_version(marker: str) -> str:
    return {
        "live": "live",
        "remix": "remix",
        "acoustic": "acoustic",
        "cover": "cover",
        "karaoke": "karaoke",
        "tribute": "tribute",
        "instrumental": "instrumental",
        "original": "original",
    }.get(marker, marker)


def _detail_rows(
    *,
    title: Mapping[str, Any],
    artist: Mapping[str, Any],
    version: Mapping[str, Any],
    duration: Mapping[str, Any],
    isrc: Mapping[str, Any],
    spotify_identity: Mapping[str, Any],
    gates: Mapping[str, Any],
    quality: Mapping[str, Any],
    score: float | None,
    has_guitar: bool,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    title_similarity = _number(title.get("similarity"))
    artist_similarity = _number(artist.get("similarity"))
    if _bool(title.get("exact_normalized")) is True:
        title_value = "Exact title match"
    elif title_similarity is not None:
        title_value = f"{round(title_similarity):g}% similar"
    else:
        title_value = "Not available"
    if _bool(artist.get("exact_normalized")) is True:
        artist_value = "Exact artist match"
    elif artist_similarity is not None:
        artist_value = f"{round(artist_similarity):g}% similar"
    else:
        artist_value = "Not available"
    rows.extend([
        {"label": "Artist", "value": artist_value},
        {"label": "Title", "value": title_value},
    ])

    track_markers = _labels(version.get("track"))
    candidate_markers = _labels(version.get("candidate"))
    if track_markers == candidate_markers:
        version_value = "Same version markers" if track_markers else "Original version"
    elif candidate_markers:
        version_value = "Possible " + ", ".join(_human_version(item) for item in candidate_markers) + " mismatch"
    elif track_markers:
        version_value = "Music version details are not on the chart"
    else:
        version_value = "No version marker"
    rows.append({"label": "Version", "value": version_value})

    if _bool(gates.get("playable_content")) is False:
        guitar_value = "No playable content"
    elif _bool(gates.get("required_five_fret_guitar")) is False:
        guitar_value = "Five-fret guitar unavailable"
    elif has_guitar:
        guitar_value = "Available"
    else:
        guitar_value = "Not listed"
    rows.append({"label": "Guitar", "value": guitar_value})

    if _quality_value(quality, "official_game_conversion") > 0:
        quality_value = "Official game chart"
    elif _quality_value(quality, "full_band") > 0:
        quality_value = "Strong instrument coverage"
    elif _quality_value(quality, "multi_instrument") > 0:
        quality_value = "Multiple instruments"
    elif _quality_value(quality, "single_instrument") < 0:
        quality_value = "Single instrument"
    else:
        quality_value = "Available chart metadata"
    rows.append({"label": "Chart quality", "value": quality_value})

    if _bool(isrc.get("match")) is True:
        rows.append({"label": "Recording", "value": "Recording identity matches"})
    elif _bool(isrc.get("match")) is False:
        rows.append({"label": "Recording", "value": "Recording identity differs"})
    elif _bool(spotify_identity.get("exact_match")) is True:
        rows.append({"label": "Recording", "value": "Spotify recording matches"})
    if _bool(duration.get("within_tolerance")) is True:
        rows.append({"label": "Length", "value": "Track length is consistent"})
    elif _bool(duration.get("within_tolerance")) is False:
        rows.append({"label": "Length", "value": "Track length differs"})
    if score is not None:
        rows.append({"label": "Match evidence", "value": f"{round(score):g}/100"})
    return rows


def build_review_presentation(
    candidate: Mapping[str, Any],
    *,
    minimum_score: float | None = None,
) -> dict[str, Any]:
    """Return human-facing review data without changing matching semantics."""
    data = dict(candidate)
    breakdown = _mapping(data.get("breakdown"))
    factors = _mapping(breakdown.get("factors"))
    match = _mapping(factors.get("match"))
    if not match:
        match = _mapping(breakdown.get("match_factors"))
    quality = _mapping(factors.get("quality"))
    if not quality:
        quality = _mapping(breakdown.get("quality_factors"))
    gates = _mapping(factors.get("gates"))
    if not gates:
        gates = _mapping(breakdown.get("gate_factors"))
    title = _factor(match, "title")
    artist = _factor(match, "artist")
    version = _factor(match, "version_markers")
    duration = _factor(match, "duration")
    isrc = _factor(match, "isrc")
    spotify_identity = _factor(match, "spotify_identity")
    source_issues = _factor(match, "source_issues")
    source_review = _factor(match, "source_review")
    download = _factor(match, "download")

    raw_state = str(
        data.get("eligibility")
        or breakdown.get("eligibility")
        or breakdown.get("eligibility_state")
        or ""
    ).strip().upper()
    decision = str(data.get("decision") or breakdown.get("decision") or "").strip().upper()
    hard_reasons = [
        str(value).strip()
        for value in (
            gates.get("hard_gate_reasons")
            or breakdown.get("eligibility_reasons")
            or breakdown.get("reasons")
            or data.get("eligibility_reasons")
            or []
        )
        if str(value).strip()
    ]
    hard_gate = _bool(gates.get("hard_gate")) is True
    reason_text = " ".join(hard_reasons).casefold()
    error_state = any(
        marker in reason_text for marker in ("scoring error", "query failed", "source unavailable")
    )
    if hard_gate and (
        raw_state in NOT_PLAYABLE_STATES
        or _bool(gates.get("playable_content")) is False
        or _bool(gates.get("required_five_fret_guitar")) is False
    ):
        state = "not_playable"
    elif raw_state in NOT_PLAYABLE_STATES or decision == "INCOMPATIBLE":
        state = "not_playable"
    elif error_state:
        state = "incomplete"
    elif raw_state in STRONG_STATES or decision == "AUTO_ACCEPT":
        state = "strong"
    elif raw_state in REVIEW_STATES or decision == "REVIEW":
        state = "needs_review"
    elif raw_state in NOT_RECOMMENDED_STATES or decision in {"REJECT", ""}:
        state = "not_recommended"
    else:
        state = "needs_review"

    labels = {
        "strong": "Strong match",
        "needs_review": "Needs review",
        "not_playable": "Not playable",
        "not_recommended": "Not recommended",
        "incomplete": "Could not verify",
    }

    instruments = data.get("instruments") or []
    if isinstance(instruments, str):
        instruments = [item.strip() for item in instruments.split(",") if item.strip()]
    instrument_keys = {str(value).casefold().replace(" ", "") for value in instruments}
    guitar_difficulty = _number(data.get("guitar_difficulty", data.get("diff_guitar")))
    has_guitar = bool(
        instrument_keys.intersection({"guitar", "guitar5", "guitar5fret", "fivefretguitar", "leadguitar"})
        or (guitar_difficulty is not None and guitar_difficulty >= 0)
        or _bool(gates.get("required_five_fret_guitar")) is True
    )

    why: list[str] = []
    concerns: list[str] = []
    if state == "not_playable":
        if _bool(gates.get("playable_content")) is False or "no playable chart content" in reason_text:
            why.append("This chart does not contain playable instrument data.")
        elif _bool(gates.get("required_five_fret_guitar")) is False or "5-fret guitar" in reason_text or "guitar chart" in reason_text:
            why.append("Five-fret guitar is not available.")
        elif "artist conflict" in reason_text or _bool(gates.get("artist_conflict")) is True:
            why.append("The artist does not match your song.")
        elif "modchart" in reason_text:
            why.append("This chart is excluded by your chart settings.")
        else:
            why.append("A compatibility check prevents safe recommendation.")
    elif state == "incomplete":
        why.append("Matching could not be completed for this chart.")
    else:
        title_exact = _bool(title.get("exact_normalized")) is True
        artist_exact = _bool(artist.get("exact_normalized")) is True
        title_similarity = _number(title.get("similarity"))
        artist_similarity = _number(artist.get("similarity"))
        if title_exact and artist_exact:
            why.append("Artist and title match exactly.")
        elif title_similarity is not None and artist_similarity is not None and title_similarity >= 85 and artist_similarity >= 85:
            why.append("Artist and title closely match.")
        elif title_similarity is not None and title_similarity >= 85:
            if artist_similarity is not None and artist_similarity < 75:
                why.append("Title matches, but the artist match is uncertain.")
            else:
                why.append("Title closely matches.")
        elif artist_similarity is not None and artist_similarity >= 85:
            why.append("Artist closely matches.")
        else:
            why.append("The available song details are a partial match.")

        if _bool(spotify_identity.get("exact_match")) is True or _bool(isrc.get("match")) is True:
            why.append("This looks like the same recording.")
        if has_guitar:
            why.append("Guitar available.")
        if _quality_value(quality, "official_game_conversion") > 0:
            why.append("Official game chart.")
        elif _quality_value(quality, "full_band") > 0:
            why.append("Strong instrument coverage.")
        elif _quality_value(quality, "multi_instrument") > 0:
            why.append("Multiple instruments available.")

        track_markers = _labels(version.get("track"))
        candidate_markers = _labels(version.get("candidate"))
        only_chart = _labels(version.get("only_chart"))
        only_track = _labels(version.get("only_track"))
        if not only_chart and not only_track and track_markers != candidate_markers:
            only_chart = [item for item in candidate_markers if item not in track_markers]
            only_track = [item for item in track_markers if item not in candidate_markers]
        if only_chart:
            marker = _human_version(only_chart[0])
            concerns.append(f"Possible {marker}-version mismatch.")
        elif only_track:
            concerns.append("The chart may be a different version of the recording.")
        if _quality_value(quality, "single_instrument") < 0:
            concerns.append("Only one instrument is available.")
        if _quality_value(quality, "unrated_guitar") < 0:
            concerns.append("Guitar difficulty is not rated.")
        if _quality_value(quality, "issue_reports") < 0 or _bool(source_issues.get("has_issues")) is True:
            concerns.append("Chart issues are reported.")
        if _bool(source_review.get("rejected")) is True:
            concerns.append("Chart quality review flagged a concern.")
        elif _bool(source_review.get("approved")) is False and source_review.get("status"):
            concerns.append("Chart quality review is still pending.")
        if _bool(download.get("supported")) is False:
            concerns.append("This chart cannot be downloaded from this source.")

    why = _unique(why)
    concerns = _unique(concerns)
    score_value = data.get("score")
    if score_value is None:
        score_value = breakdown.get("score")
    if score_value is None:
        score_factors = _mapping(factors.get("score"))
        score_value = score_factors.get("final")
    score = _number(score_value)
    detail_rows = _detail_rows(
        title=title,
        artist=artist,
        version=version,
        duration=duration,
        isrc=isrc,
        spotify_identity=spotify_identity,
        gates=gates,
        quality=quality,
        score=score,
        has_guitar=has_guitar,
    )
    below_cutoff = minimum_score is not None and (score is None or score < minimum_score)
    if hard_gate or state == "not_playable":
        selectable = False
    elif state == "incomplete":
        selectable = False
    elif below_cutoff:
        selectable = False
    else:
        # Soft exclusions remain overridable through the existing selection
        # route; hard-gated candidates never become selectable here.
        selectable = True
    selected = bool(data.get("selected"))
    if selected:
        action = "Keep current choice"
    elif not selectable:
        action = "Below current cutoff" if below_cutoff else "Unavailable"
    elif state == "not_recommended":
        action = "Choose anyway"
    else:
        action = "Choose this chart"
    summary_parts = why[:1]
    if state not in {"not_playable", "incomplete"} and concerns:
        summary_parts.append(concerns[0])
    elif len(why) > 1:
        summary_parts.append(why[1])
    return {
        "state": state,
        "label": labels[state],
        "summary": " · ".join(_unique(summary_parts)),
        "why": why,
        "concerns": concerns,
        "details": detail_rows,
        "score": round(score, 1) if score is not None else None,
        "hard_gate": hard_gate,
        "selectable": selectable,
        "action": action,
        "selected": selected,
        "selected_label": "Already selected" if selected else "",
    }
