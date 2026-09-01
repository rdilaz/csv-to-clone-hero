"""Deterministic chart matching, quality scoring, and export eligibility.

The matcher deliberately keeps two concepts separate:

* ``decision`` is the legacy pipeline value (``AUTO_ACCEPT``, ``REVIEW``,
  ``REJECT`` or ``INCOMPATIBLE``); and
* ``eligibility`` is the Spotify workflow value used by the browser/export
  boundary (``RECOMMENDED``, ``ELIGIBLE_REVIEW``, ``EXCLUDED`` or
  ``INCOMPATIBLE``).

All evidence is derived from normalized metadata.  There is no popularity or
model-based signal, and a score threshold can never override a hard gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.models import ChartCandidate, ScoreBreakdown, Track
from csv_to_clonehero.normalization import (
    normalize_artist,
    normalize_text,
    normalize_title_without_recognized_annotations,
    split_artists,
    version_markers,
)

# Known "official game conversion" icon/charter signals on Enchor.
_OFFICIAL_ICONS = {
    "gh1", "gh2", "gh2dlc", "gh3", "gh3dlc", "gh80s", "gha", "ghm", "ghsh",
    "ghwt", "ghwtdlc", "ghvh", "gh5", "gh5dlc", "ghwor", "ghwordlc", "ghl",
    "rb1", "rb1dlc", "rb2", "rb2dlc", "rb3", "rb3dlc", "rb4", "rb4dlc",
    "rbblitz", "rbtb", "rbgd", "rblego", "rbpearljam", "rbgreenday",
    "fusion", "ghx", "ghx2",
}
_OFFICIAL_CHARTERS = {"harmonix", "neversoft", "wavegroup", "budcat", "vicarious visions"}

# Score composition.  The 0.10 complement leaves room for deterministic
# metadata evidence without making title/artist similarity disappear.
TITLE_WEIGHT = 0.55
ARTIST_WEIGHT = 0.35
BASE_COMPLEMENT = 0.10

# Ranking can remain high-recall, but automatic identity uses independent plain
# similarity and conservative deterministic exceptions.  The 55 conflict floor
# remains the clear-conflict boundary for the ranking signal.
TITLE_IDENTITY_CONFLICT_THRESHOLD = 55.0
TITLE_IDENTITY_PLAIN_AUTO_THRESHOLD = 80.0
ARTIST_IDENTITY_PLAIN_AUTO_THRESHOLD = 80.0

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20",
}

DEFAULT_AUTO_ACCEPT_THRESHOLD = 90.0
DEFAULT_REVIEW_THRESHOLD = 70.0
DEFAULT_DURATION_TOLERANCE_MS = 5_000

RECOMMENDED = "RECOMMENDED"
ELIGIBLE_REVIEW = "ELIGIBLE_REVIEW"
EXCLUDED = "EXCLUDED"
INCOMPATIBLE = "INCOMPATIBLE"
ELIGIBILITY_STATES = frozenset({RECOMMENDED, ELIGIBLE_REVIEW, EXCLUDED, INCOMPATIBLE})

_DIFF_FIELDS = (
    "diff_guitar", "diff_guitarghl", "diff_bass",
    "diff_drums", "diff_vocals", "diff_keys",
    "diff_band", "diff_guitar_coop", "diff_rhythm", "diff_drums_real",
    "diff_guitar_coop_ghl", "diff_rhythm_ghl", "diff_bassghl",
    "diff_guitar_ghl", "diff_bass_ghl", "diff_vocals_harm1",
    "diff_vocals_harm2", "diff_vocals_harm3", "diff_guitarghl_coop",
    "diff_rhythmghl",
)

_FIVE_FRET_INSTRUMENTS = {
    "guitar", "guitar5", "guitar5fret", "fivefretguitar", "fivefretleadguitar",
    "leadguitar", "realguitar",
}
_SIX_FRET_INSTRUMENTS = {"guitarghl", "guitar6", "sixfretguitar", "ghl"}
_SEVERE_ISSUE_LEVELS = {
    "blocker", "blocking", "critical", "fatal", "high", "severe", "unsafe", "invalid",
}

_QUALITY_FACTOR_KEYS = (
    "official_game_conversion",
    "curated_pack",
    "full_band",
    "multi_instrument",
    "rated_guitar",
    "metadata_completeness",
    "source_review",
    "chart_grouping",
    "direct_download",
    "single_instrument",
    "unrated_guitar",
    "version_mismatch",
    "issue_reports",
    "cover_version",
    "modchart",
)

_MISSING = object()


@dataclass
class EligibilityResult:
    """Small result object that works as both an object and a pair.

    The mapping-style access keeps this helper convenient for web/API callers,
    while iteration preserves the natural ``state, reasons = ...`` usage.
    """

    state: str
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.state in {RECOMMENDED, ELIGIBLE_REVIEW}

    @property
    def exportable(self) -> bool:
        return self.eligible

    def __getitem__(self, key: str | int) -> Any:
        if key in (0, "state", "eligibility", "eligibility_state"):
            return self.state
        if key in (1, "reasons", "eligibility_reasons"):
            return self.reasons
        if key == "eligible":
            return self.eligible
        if key == "exportable":
            return self.exportable
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __iter__(self) -> Iterator[Any]:
        yield self.state
        yield self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reasons": list(self.reasons),
            "eligible": self.eligible,
            "exportable": self.exportable,
        }

    def __str__(self) -> str:
        return self.state

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.state == other
        if isinstance(other, EligibilityResult):
            return self.state == other.state and self.reasons == other.reasons
        if isinstance(other, Mapping):
            if set(other).issubset({"state", "reasons"}):
                return other.get("state") == self.state and list(other.get("reasons", ())) == self.reasons
            return self.as_dict() == dict(other)
        return NotImplemented


@dataclass
class SpotifyScoreBreakdown(ScoreBreakdown):
    """Backwards-compatible score result with persisted Spotify factors.

    ``ScoreBreakdown`` is intentionally not changed here because legacy
    callers construct it directly.  The matcher returns this subclass, so
    normal ``isinstance`` checks, existing properties, and ``dataclasses``
    serialization continue to work while the new fields are available to
    callers that persist workspace scores.
    """

    factors: dict[str, Any] = field(default_factory=dict)
    match_factors: dict[str, Any] = field(default_factory=dict)
    quality_factors: dict[str, float] = field(default_factory=dict)
    gate_factors: dict[str, Any] = field(default_factory=dict)
    eligibility: str = ""
    eligibility_reasons: list[str] = field(default_factory=list)

    @property
    def eligibility_state(self) -> str:
        return self.eligibility

    @property
    def machine_factors(self) -> dict[str, Any]:
        return self.factors

    @property
    def factor_values(self) -> dict[str, Any]:
        return self.factors

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-ready fields, including the legacy computed quality."""
        data = asdict(self)
        data["quality"] = self.quality
        data["eligibility_state"] = self.eligibility
        data["machine_factors"] = self.factors
        return data

    to_dict = as_dict


# A short alias is useful to integrations that do not want to couple their
# import name to the Spotify workflow while retaining the concrete type above.
ScoredBreakdown = SpotifyScoreBreakdown


def _new_breakdown() -> SpotifyScoreBreakdown:
    return SpotifyScoreBreakdown(
        quality_factors={key: 0.0 for key in _QUALITY_FACTOR_KEYS},
        gate_factors={
            "playable_content": None,
            "required_five_fret_guitar": None,
            "artist_match": None,
            "artist_conflict": False,
            "artist_identity": None,
            "artist_auto_identity": False,
            "title_similarity": None,
            "title_identity": None,
            "title_auto_identity": False,
            "title_conflict": False,
            "external_identity_match": False,
            "external_identity_conflict": False,
            "modchart_allowed": None,
            "severe_issue": None,
            "download_supported": None,
            "hard_gate": False,
            "hard_gate_reasons": [],
        },
    )


def _normal_key(value: Any) -> str:
    return _cached_normal_key(str(value))


@lru_cache(maxsize=8192)
def _cached_normal_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _mapping_value(mapping: Mapping[str, Any], names: tuple[str, ...] | list[str] | set[str]) -> Any:
    wanted = {_normal_key(name) for name in names}
    for key, value in mapping.items():
        if _normal_key(key) in wanted:
            return value
    return _MISSING


def _iter_mappings(value: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    if depth > 4 or not isinstance(value, Mapping):
        return
    yield value
    for child in value.values():
        if isinstance(child, Mapping):
            yield from _iter_mappings(child, depth + 1)
        elif isinstance(child, (list, tuple, set)):
            for item in child:
                if isinstance(item, Mapping):
                    yield from _iter_mappings(item, depth + 1)


def _candidate_metadata(chart: ChartCandidate) -> list[Mapping[str, Any]]:
    metadata = getattr(chart, "source_metadata", None)
    if not isinstance(metadata, Mapping):
        return []
    return list(_iter_mappings(metadata))


def _first_value(owner: Any, names: tuple[str, ...] | list[str] | set[str]) -> Any:
    if isinstance(owner, Mapping):
        return _mapping_value(owner, names)
    for name in names:
        value = getattr(owner, name, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _first_candidate_value(chart: ChartCandidate, names: tuple[str, ...] | list[str]) -> Any:
    direct = _first_value(chart, names)
    if direct is not _MISSING and direct not in (None, ""):
        return direct
    for mapping in _candidate_metadata(chart):
        value = _mapping_value(mapping, names)
        if value is not _MISSING and value not in (None, ""):
            return value
    return _MISSING


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y", "on", "enabled", "supported", "available"}:
        return True
    if text in {"false", "0", "no", "n", "off", "disabled", "unsupported", "unavailable"}:
        return False
    return default


def _config_value(config: Any, names: tuple[str, ...], default: Any) -> Any:
    section = getattr(config, "matching", config)
    if section is None:
        return default
    for name in names:
        value = _first_value(section, (name,))
        if value is not _MISSING and value is not None:
            return value
    return default


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _thresholds(config: Any) -> tuple[float, float]:
    auto = _number(
        _config_value(
            config,
            (
                "auto_accept_threshold",
                "recommended_threshold",
                "recommendation_threshold",
            ),
            DEFAULT_AUTO_ACCEPT_THRESHOLD,
        ),
        DEFAULT_AUTO_ACCEPT_THRESHOLD,
    )
    review = _number(
        _config_value(
            config,
            (
                "review_threshold",
                "eligible_threshold",
                "eligibility_threshold",
                "export_floor",
                "export_eligibility_floor",
            ),
            DEFAULT_REVIEW_THRESHOLD,
        ),
        DEFAULT_REVIEW_THRESHOLD,
    )
    return auto, review


def _duration_tolerance_ms(config: Any) -> int:
    milliseconds = _config_value(
        config,
        (
            "spotify_duration_tolerance_ms",
            "duration_tolerance_ms",
            "duration_match_tolerance_ms",
            "max_duration_delta_ms",
        ),
        _MISSING,
    )
    if milliseconds is not _MISSING:
        return max(0, int(_number(milliseconds, DEFAULT_DURATION_TOLERANCE_MS)))

    seconds = _config_value(
        config,
        (
            "spotify_duration_tolerance_seconds",
            "duration_tolerance_seconds",
            "duration_match_tolerance_seconds",
        ),
        _MISSING,
    )
    if seconds is not _MISSING:
        return max(0, int(_number(seconds, DEFAULT_DURATION_TOLERANCE_MS / 1000) * 1000))

    generic = _config_value(config, ("duration_tolerance",), _MISSING)
    if generic is not _MISSING:
        raw = _number(generic, DEFAULT_DURATION_TOLERANCE_MS)
        # The unit-free compatibility setting is treated as seconds for the
        # small values people normally put in YAML and milliseconds otherwise.
        return max(0, int(raw * 1000 if raw <= 120 else raw))
    return DEFAULT_DURATION_TOLERANCE_MS


def _duration_ms(value: Any) -> int | None:
    if value is _MISSING or value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Some chart APIs expose seconds despite the legacy field name.  Spotify
    # durations are always milliseconds and normal chart lengths are > 1000.
    if number < 1000:
        number *= 1000
    return int(round(number))


def _similarity(a: str, b: str) -> float:
    """Best of plain ratio and token_set ratio for normalized strings."""
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_set_ratio(a, b))


def _plain_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.ratio(a, b)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in str(value or "").split() if token)


def _is_token_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return bool(shorter and len(shorter) < len(longer) and longer[: len(shorter)] == shorter)


def _contains_token_sequence(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    if not shorter or len(shorter) > len(longer):
        return False
    width = len(shorter)
    return any(longer[index:index + width] == shorter for index in range(len(longer) - width + 1))


def _number_word_equivalent(left: str, right: str) -> bool:
    left_raw = _tokens(left)
    right_raw = _tokens(right)
    left_tokens = tuple(_NUMBER_WORDS.get(token, token) for token in left_raw)
    right_tokens = tuple(_NUMBER_WORDS.get(token, token) for token in right_raw)
    changed = left_tokens != left_raw or right_tokens != right_raw
    return bool(changed and left_raw != right_raw and left_tokens == right_tokens)


def _recognized_title_suffix_relationship(
    track_title: str,
    chart_title: str,
    track_norm: str,
    chart_norm: str,
) -> bool:
    """True only when a complete title prefix has a known annotation suffix."""
    track_tokens = _tokens(track_norm)
    chart_tokens = _tokens(chart_norm)
    if _is_token_prefix(track_tokens, chart_tokens):
        return normalize_title_without_recognized_annotations(chart_title) == track_norm
    if _is_token_prefix(chart_tokens, track_tokens):
        return normalize_title_without_recognized_annotations(track_title) == chart_norm
    return False


def _artist_identity_forms(track: Track, chart: ChartCandidate) -> tuple[set[str], set[str]]:
    track_primary, _ = split_artists(str(getattr(track, "artist_full", "") or ""))
    chart_primary, _ = split_artists(str(getattr(chart, "artist", "") or ""))
    track_forms = {
        str(getattr(track, "primary_artist_norm", "") or ""),
        str(getattr(track, "artist_norm", "") or ""),
        normalize_artist(track_primary),
    }
    chart_forms = {
        _normalised_chart_artist(chart),
        normalize_artist(str(getattr(chart, "artist", "") or "")),
        normalize_artist(chart_primary),
    }
    return {value for value in track_forms if value}, {value for value in chart_forms if value}


def _directional_artist_credit_extension(shorter: str, longer: str) -> bool:
    shorter = str(shorter or "").strip()
    longer = str(longer or "").strip()
    if not shorter or not longer:
        return False
    return bool(re.match(
        rf"^\s*{re.escape(shorter)}(?:\s*(?:&|,|;)|\s+(?:and|feat\.?|ft\.?|featuring|with)\b)",
        longer,
        flags=re.IGNORECASE,
    ))


def _title_identity_evidence(
    track: Track,
    chart: ChartCandidate,
    track_norm: str,
    chart_norm: str,
    ranking_score: float,
    *,
    external_match: bool,
) -> dict[str, Any]:
    plain_ratio = _plain_similarity(track_norm, chart_norm)
    exact = bool(track_norm and track_norm == chart_norm)
    number_equivalent = _number_word_equivalent(track_norm, chart_norm)
    annotation_suffix = _recognized_title_suffix_relationship(
        str(getattr(track, "title", "") or ""),
        str(getattr(chart, "name", "") or ""),
        track_norm,
        chart_norm,
    )
    auto = bool(
        external_match
        or exact
        or number_equivalent
        or plain_ratio >= TITLE_IDENTITY_PLAIN_AUTO_THRESHOLD
        or annotation_suffix
    )
    track_tokens = _tokens(track_norm)
    chart_tokens = _tokens(chart_norm)
    containment = _contains_token_sequence(track_tokens, chart_tokens) or _contains_token_sequence(chart_tokens, track_tokens)
    state = "strong" if auto else "conflict" if ranking_score < TITLE_IDENTITY_CONFLICT_THRESHOLD and not containment else "borderline"
    return {
        "state": state,
        "auto": auto,
        "plain_ratio": plain_ratio,
        "ranking_score": ranking_score,
        "exact_normalized": exact,
        "number_word_equivalent": number_equivalent,
        "recognized_annotation_suffix": annotation_suffix,
        "token_containment": containment,
        "plain_auto_threshold": TITLE_IDENTITY_PLAIN_AUTO_THRESHOLD,
        "conflict_threshold": TITLE_IDENTITY_CONFLICT_THRESHOLD,
        "external_identity": external_match,
    }


def _artist_identity_evidence(
    track: Track,
    chart: ChartCandidate,
    ranking_score: float,
    *,
    external_match: bool,
    conflict_threshold: float,
) -> dict[str, Any]:
    track_forms, chart_forms = _artist_identity_forms(track, chart)
    exact = bool(track_forms & chart_forms)
    plain_ratio = max(
        (_plain_similarity(track_form, chart_form) for track_form in track_forms for chart_form in chart_forms),
        default=0.0,
    )
    # Exact split-primary equality is the conservative credit-extension signal;
    # arbitrary token containment is never sufficient.
    track_primary, _ = split_artists(str(getattr(track, "artist_full", "") or ""))
    chart_primary, _ = split_artists(str(getattr(chart, "artist", "") or ""))
    credit_prefix = bool(
        normalize_artist(track_primary)
        and normalize_artist(track_primary) == normalize_artist(chart_primary)
        and normalize_artist(str(getattr(track, "artist_full", "") or ""))
        != normalize_artist(str(getattr(chart, "artist", "") or ""))
    ) or any(
        _directional_artist_credit_extension(shorter, longer)
        for shorter, longer in (
            (str(getattr(track, "primary_artist", "") or ""), str(getattr(chart, "artist", "") or "")),
            (str(getattr(track, "artist_full", "") or ""), str(getattr(chart, "artist", "") or "")),
            (chart_primary, str(getattr(track, "artist_full", "") or "")),
        )
    )
    auto = bool(
        external_match
        or exact
        or plain_ratio >= ARTIST_IDENTITY_PLAIN_AUTO_THRESHOLD
        or credit_prefix
    )
    state = "strong" if auto else "conflict" if ranking_score < conflict_threshold else "borderline"
    return {
        "state": state,
        "auto": auto,
        "plain_ratio": plain_ratio,
        "ranking_score": ranking_score,
        "exact_normalized": exact,
        "credit_prefix": credit_prefix,
        "plain_auto_threshold": ARTIST_IDENTITY_PLAIN_AUTO_THRESHOLD,
        "conflict_threshold": conflict_threshold,
        "external_identity": external_match,
    }


def _external_identity_state(breakdown: SpotifyScoreBreakdown) -> tuple[bool, bool]:
    isrc = breakdown.match_factors.get("isrc", {})
    spotify = breakdown.match_factors.get("spotify_identity", {})
    values = (
        isrc.get("match") if isinstance(isrc, Mapping) else None,
        spotify.get("exact_match") if isinstance(spotify, Mapping) else None,
    )
    return any(value is True for value in values), any(value is False for value in values)


def _normalised_chart_artist(chart: ChartCandidate) -> str:
    return str(getattr(chart, "artist_norm", "") or normalize_text(getattr(chart, "artist", "")))


def _has_difficulty_value(chart: ChartCandidate) -> bool:
    for field_name in _DIFF_FIELDS:
        value = getattr(chart, field_name, None)
        if value is not None and _number(value, -1) >= 0:
            return True
    for field_name in ("difficulty_fields", "difficulties"):
        values = getattr(chart, field_name, None)
        if isinstance(values, Mapping) and any(
            value is not None and _number(value, -1) >= 0 for value in values.values()
        ):
            return True
    return False


def has_playable_content(chart: ChartCandidate) -> bool:
    """True when the chart declares at least one instrument or difficulty."""
    return bool(getattr(chart, "instruments", None)) or _has_difficulty_value(chart)


def _instrument_names(chart: ChartCandidate) -> set[str]:
    result: set[str] = set()
    for value in getattr(chart, "instruments", ()) or ():
        result.add(_normal_key(value))
    return result


def _difficulty_value(chart: ChartCandidate, names: tuple[str, ...]) -> Any:
    direct = _first_value(chart, names)
    if direct is not _MISSING and direct is not None:
        return direct
    for field_name in ("difficulty_fields", "difficulties"):
        values = getattr(chart, field_name, None)
        if isinstance(values, Mapping):
            value = _mapping_value(values, names)
            if value is not _MISSING:
                return value
    return _MISSING


def _has_five_fret_guitar(chart: ChartCandidate) -> bool:
    if _instrument_names(chart) & {_normal_key(value) for value in _FIVE_FRET_INSTRUMENTS}:
        return True
    value = _difficulty_value(chart, ("diff_guitar", "guitar", "guitar5", "five_fret_guitar"))
    return value is not _MISSING and value is not None and _number(value, -1) >= 0


def _has_six_fret_guitar(chart: ChartCandidate) -> bool:
    if _instrument_names(chart) & {_normal_key(value) for value in _SIX_FRET_INSTRUMENTS}:
        return True
    value = _difficulty_value(chart, ("diff_guitarghl", "diff_guitar_ghl", "guitarghl", "six_fret_guitar"))
    return value is not _MISSING and value is not None and _number(value, -1) >= 0


def _best_artist_similarity(track: Track, chart: ChartCandidate) -> float:
    """Best artist match across the track's primary/full/featured forms."""
    candidates = [track.primary_artist_norm, track.artist_norm]
    chart_forms = {_normalised_chart_artist(chart), normalize_text(getattr(chart, "artist", ""))}
    best = 0.0
    for track_form in candidates:
        for chart_form in chart_forms:
            best = max(best, _similarity(track_form, chart_form))
    return best


def instrument_compatible(chart: ChartCandidate, config: AppConfig) -> tuple[bool, str]:
    """Check the chart satisfies the configured instrument requirements."""
    instr = getattr(config, "instruments", None)
    exclude_modcharts = bool(getattr(instr, "exclude_modcharts", True))
    require_five = bool(getattr(instr, "require_five_fret_guitar", True))
    allow_six = bool(getattr(instr, "allow_six_fret_only", False))
    allow_no_guitar = bool(getattr(instr, "allow_no_guitar", False))

    if exclude_modcharts and bool(getattr(chart, "modchart", False)):
        return False, "modchart excluded"

    has_five = _has_five_fret_guitar(chart)
    has_six = _has_six_fret_guitar(chart)
    if has_five:
        return True, ""
    if has_six and allow_six and not require_five:
        return True, ""
    if allow_no_guitar and not require_five:
        return True, ""
    if has_six:
        return False, "no 5-fret guitar (6-fret only)"
    return False, "no 5-fret guitar chart"


def _clean_isrc(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, Mapping):
        nested = _mapping_value(value, ("isrc", "value", "id"))
        return _clean_isrc(nested)
    text = str(value).strip().upper()
    text = re.sub(r"^ISRC\s*[:\-]?\s*", "", text)
    return re.sub(r"[^A-Z0-9]", "", text)


def _track_isrc(track: Track) -> str:
    value = getattr(track, "isrc", "")
    if value:
        return _clean_isrc(value)
    metadata = getattr(track, "origin_metadata", None)
    if isinstance(metadata, Mapping):
        for mapping in _iter_mappings(metadata):
            value = _mapping_value(mapping, ("isrc",))
            if value is not _MISSING:
                cleaned = _clean_isrc(value)
                if cleaned:
                    return cleaned
    return ""


def _candidate_isrc(chart: ChartCandidate) -> str:
    direct = _first_value(chart, ("isrc",))
    if direct is not _MISSING:
        cleaned = _clean_isrc(direct)
        if cleaned:
            return cleaned
    for mapping in _candidate_metadata(chart):
        value = _mapping_value(mapping, ("isrc",))
        if value is not _MISSING:
            cleaned = _clean_isrc(value)
            if cleaned:
                return cleaned
    return ""


def _spotify_identifier(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, Mapping):
        nested = _mapping_value(value, ("spotify_id", "spotify_track_id", "track_id", "id", "uri", "url"))
        return _spotify_identifier(nested)
    text = str(value).strip()
    if not text:
        return ""
    match = re.search(r"spotify:(?:track:)?([A-Za-z0-9]+)$", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    # Explicit Spotify-id fields may contain the bare base-62 ID.  Numeric
    # chart IDs are deliberately ignored by the caller's context.
    return text


def _candidate_spotify_ids(chart: ChartCandidate) -> set[str]:
    ids: set[str] = set()

    def add(value: Any, *, allow_plain: bool = True) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                add(child, allow_plain=allow_plain)
            return
        if value is _MISSING or value is None:
            return
        text = str(value).strip()
        identifier = _spotify_identifier(text)
        if identifier and (allow_plain or "spotify:" in text.casefold() or "open.spotify.com" in text.casefold()):
            ids.add(identifier)

    for name in ("spotify_id", "spotify_track_id", "spotify_uri", "spotify_url", "spotify_track_uri"):
        value = _first_value(chart, (name,))
        if value is not _MISSING:
            add(value)

    def walk(value: Any, spotify_context: bool = False, depth: int = 0) -> None:
        if depth > 4 or not isinstance(value, Mapping):
            return
        for key, child in value.items():
            key_name = _normal_key(key)
            context = spotify_context or "spotify" in key_name
            if key_name in {
                "spotifyid", "spotifytrackid", "spotifyuri", "spotifyurl", "spotifytrackuri",
            }:
                add(child)
            elif context and key_name in {"id", "trackid", "uri", "url"}:
                add(child)
            elif key_name == "trackid" and context:
                add(child)
            if isinstance(child, Mapping):
                walk(child, context, depth + 1)

    metadata = getattr(chart, "source_metadata", None)
    if isinstance(metadata, Mapping):
        walk(metadata)
    return {value.casefold(): value for value in ids}


def _grouping_values(chart: ChartCandidate) -> dict[str, str]:
    aliases = {
        "song_id": ("song_id", "songId"),
        "version_id": ("version_id", "versionId"),
        "version_group_id": ("version_group_id", "versionGroupId"),
        "group_id": ("group_id", "groupId", "chart_group_id", "chartGroupId"),
    }
    result: dict[str, str] = {}
    for field_name, names in aliases.items():
        value = _first_candidate_value(chart, list(names))
        if value is not _MISSING and value not in (None, ""):
            result[field_name] = str(value)

    # A few source payloads nest these IDs as ``song: {id: ...}`` and
    # ``version: {id: ...}`` rather than exposing snake/camel-case fields.
    containers = {
        "song_id": ("song", "song_data"),
        "version_id": ("version", "version_data"),
        "version_group_id": ("version_group", "version_group_data"),
        "group_id": ("group", "group_data"),
    }
    for field_name, names in containers.items():
        if field_name in result:
            continue
        container = _first_candidate_value(chart, list(names))
        if isinstance(container, Mapping):
            value = _mapping_value(container, ("id", "value", field_name))
            if value is not _MISSING and value not in (None, ""):
                result[field_name] = str(value)
    return result


def _version_markers_for_chart(chart: ChartCandidate) -> set[str]:
    markers = version_markers(str(getattr(chart, "name", "") or ""))
    for field_name in ("version", "version_name", "chart_version", "version_title"):
        value = _first_candidate_value(chart, (field_name,))
        if value is not _MISSING and value:
            markers.update(version_markers(str(value)))
    return markers


def _issue_info(chart: ChartCandidate) -> dict[str, Any]:
    details: list[str] = []
    embedded_severities: list[str] = []

    def consume(raw: Any) -> None:
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for value in values:
            if not value:
                continue
            if isinstance(value, Mapping):
                severity = _mapping_value(value, ("severity", "level"))
                if severity is not _MISSING and severity not in (None, ""):
                    embedded_severities.append(str(severity).strip().casefold())
                detail = _mapping_value(value, ("message", "detail", "text", "issue", "name"))
                details.append(str(detail if detail is not _MISSING else value))
            else:
                details.append(str(value))

    for field_name in ("issue_details", "issues"):
        raw = getattr(chart, field_name, None)
        consume(raw)
    for mapping in _candidate_metadata(chart):
        raw = _mapping_value(mapping, ("issue_details", "issues", "chart_issues", "metadata_issues"))
        if raw is not _MISSING:
            consume(raw)

    severity = _first_value(chart, ("issue_severity", "severity"))
    if severity is _MISSING or severity in (None, ""):
        severity = _first_candidate_value(chart, ("issue_severity", "severity"))
    if isinstance(severity, Mapping):
        severity = _mapping_value(severity, ("severity", "level", "value"))
    severity_text = "" if severity in (_MISSING, None) else str(severity).strip().casefold()
    if not severity_text:
        severity_text = next(
            (value for value in embedded_severities if value in _SEVERE_ISSUE_LEVELS),
            "",
        )
    if not severity_text:
        # Some source payloads encode severity in the detail prefix.
        for detail in details:
            prefix = detail.split(":", 1)[0].strip().casefold()
            if prefix in _SEVERE_ISSUE_LEVELS:
                severity_text = prefix
                break
    has_issues = bool(getattr(chart, "has_issues", False) or details or severity_text)
    return {
        "has_issues": has_issues,
        "details": list(dict.fromkeys(details)),
        "severity": severity_text,
        "severe": severity_text in _SEVERE_ISSUE_LEVELS,
    }


def _review_info(chart: ChartCandidate) -> dict[str, Any]:
    status = _first_value(chart, ("review_status",))
    if status is _MISSING or status in (None, ""):
        status = _first_candidate_value(chart, ("review_status", "reviewed_status", "reviewed", "review"))
    if isinstance(status, Mapping):
        status = _mapping_value(status, ("status", "value", "reviewed"))
    if isinstance(status, bool):
        status = "reviewed" if status else "unreviewed"
    status_text = "" if status in (_MISSING, None) else str(status).strip().casefold()
    approved = status_text in {"approved", "passed", "reviewed", "verified", "accepted", "complete", "ok"}
    rejected = status_text in {"rejected", "failed", "blocked", "unsafe"}
    return {"status": status_text, "approved": approved, "rejected": rejected}


def _download_info(chart: ChartCandidate, source: Any = None) -> dict[str, Any]:
    reasons: list[str] = []
    discovery_only = _as_bool(_first_candidate_value(chart, ("discovery_only",)), False) is True
    source_metadata: Mapping[str, Any] | None = None
    declared_capabilities = getattr(source, "capabilities", None) if source is not None else None
    if isinstance(declared_capabilities, Mapping):
        source_metadata = declared_capabilities

    def source_value(names: tuple[str, ...]) -> Any:
        if source is None:
            return _MISSING
        value = _first_value(source, names)
        if value is not _MISSING:
            return value
        if source_metadata is not None:
            return _mapping_value(source_metadata, names)
        if declared_capabilities is not None:
            value = _first_value(declared_capabilities, names)
            if value is not _MISSING:
                return value
        return _MISSING

    source_discovery = _as_bool(source_value(("discovery_only",)), None)
    if source_discovery is True:
        discovery_only = True
    if discovery_only:
        reasons.append("discovery-only source")

    supports = _as_bool(getattr(chart, "supports_download", True), True)
    source_supports = _as_bool(source_value(("supports_direct_download", "direct_download")), None)
    if source_supports is False:
        supports = False
    if supports is False:
        reasons.append("direct download unsupported")

    direct = _first_candidate_value(
        chart,
        (
            "supports_direct_download",
            "direct_download",
            "downloadable",
            "supports_download",
        ),
    )
    direct_bool = _as_bool(direct, None)
    if source_supports is not None:
        direct_bool = source_supports
    if direct_bool is False and "direct download unsupported" not in reasons:
        reasons.append("direct download unsupported")

    exportable = _as_bool(_first_candidate_value(chart, ("exportable", "supports_export")), None)
    source_exportable = _as_bool(source_value(("exportable", "supports_export")), None)
    if source_exportable is False:
        exportable = False
    if exportable is False:
        reasons.append("source is not exportable")

    for mapping in _candidate_metadata(chart):
        download = _mapping_value(mapping, ("download", "download_info"))
        if isinstance(download, Mapping):
            download_support = _as_bool(
                _mapping_value(download, ("supports_direct_download", "direct_download", "downloadable")),
                None,
            )
            if download_support is False and "direct download unsupported" not in reasons:
                reasons.append("direct download unsupported")
        if _as_bool(_mapping_value(mapping, ("discovery_only",)), None) is True:
            discovery_only = True
            if "discovery-only source" not in reasons:
                reasons.append("discovery-only source")

    return {
        "supported": not reasons,
        "discovery_only": discovery_only,
        "explicit_support": direct_bool,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _record_factor(breakdown: SpotifyScoreBreakdown, name: str, value: Any) -> None:
    breakdown.match_factors[name] = value


def _add_bonus(
    breakdown: SpotifyScoreBreakdown,
    label: str,
    value: float,
    *,
    quality_key: str | None = None,
) -> None:
    numeric: float | int = int(value) if float(value).is_integer() else float(value)
    breakdown.bonuses.append((label, numeric))
    if quality_key:
        breakdown.quality_factors[quality_key] = breakdown.quality_factors.get(quality_key, 0.0) + float(value)


def _add_penalty(
    breakdown: SpotifyScoreBreakdown,
    label: str,
    value: float,
    *,
    quality_key: str | None = None,
) -> None:
    numeric: float | int = int(value) if float(value).is_integer() else float(value)
    breakdown.penalties.append((label, numeric))
    if quality_key:
        breakdown.quality_factors[quality_key] = breakdown.quality_factors.get(quality_key, 0.0) - float(value)


def _set_eligibility(
    breakdown: SpotifyScoreBreakdown,
    state: str,
    reasons: list[str] | tuple[str, ...],
) -> None:
    breakdown.eligibility = state if state in ELIGIBILITY_STATES else EXCLUDED
    breakdown.eligibility_reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))


def _hard_gate(
    breakdown: SpotifyScoreBreakdown,
    state: str,
    reasons: list[str],
    *,
    decision: str = "REJECT",
    final_score: float = 0.0,
) -> SpotifyScoreBreakdown:
    unique_reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))
    breakdown.gate_factors["hard_gate"] = True
    breakdown.gate_factors["hard_gate_reasons"] = unique_reasons
    breakdown.reasons.extend(reason for reason in unique_reasons if reason not in breakdown.reasons)
    breakdown.decision = decision
    breakdown.final_score = max(0.0, min(100.0, final_score))
    _set_eligibility(breakdown, state, unique_reasons)
    _sync_factors(breakdown)
    return breakdown


def _sync_factors(breakdown: SpotifyScoreBreakdown) -> None:
    """Refresh the JSON-ready factor envelope after each scoring phase."""
    breakdown.factors = {
        "match": dict(breakdown.match_factors),
        "quality": dict(breakdown.quality_factors),
        "gates": dict(breakdown.gate_factors),
        "score": {
            "title": breakdown.title_score,
            "artist": breakdown.artist_score,
            "base": breakdown.base_score,
            "final": breakdown.final_score,
            "quality_net": breakdown.quality,
        },
        "eligibility": {
            "state": breakdown.eligibility,
            "reasons": list(breakdown.eligibility_reasons),
        },
    }
    # Keep common factors addressable without requiring callers to understand
    # the envelope, while retaining the grouped form for future additions.
    breakdown.factors.update(breakdown.match_factors)
    isrc = breakdown.match_factors.get("isrc", {})
    spotify_identity = breakdown.match_factors.get("spotify_identity", {})
    version = breakdown.match_factors.get("version_markers", {})
    source_review = breakdown.match_factors.get("source_review", {})
    source_issues = breakdown.match_factors.get("source_issues", {})
    download = breakdown.match_factors.get("download", {})
    breakdown.factors.update(
        {
            "isrc_match": isrc.get("match") if isinstance(isrc, Mapping) else None,
            "spotify_identity_match": (
                spotify_identity.get("exact_match") if isinstance(spotify_identity, Mapping) else None
            ),
            "version_marker_match": version.get("match") if isinstance(version, Mapping) else None,
            "source_review_status": source_review.get("status") if isinstance(source_review, Mapping) else "",
            "issue_severity": source_issues.get("severity") if isinstance(source_issues, Mapping) else "",
            "download_capability": download.get("supported") if isinstance(download, Mapping) else None,
        }
    )
    breakdown.factors["quality_factors"] = dict(breakdown.quality_factors)
    breakdown.factors["gate_factors"] = dict(breakdown.gate_factors)
    breakdown.factors["eligibility_state"] = breakdown.eligibility
    breakdown.factors["eligibility_reasons"] = list(breakdown.eligibility_reasons)


def _source_evidence(
    breakdown: SpotifyScoreBreakdown,
    chart: ChartCandidate,
    source: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    issues = _issue_info(chart)
    review = _review_info(chart)
    download = _download_info(chart, source)

    review["score_contribution"] = 3 if review["approved"] else (-8 if review["rejected"] else (-2 if review["status"] else 0))
    issues["score_contribution"] = -10 if issues["has_issues"] else 0
    download["score_contribution"] = 2 if download["supported"] else 0

    _record_factor(breakdown, "source_review", review)
    _record_factor(breakdown, "source_issues", issues)
    _record_factor(breakdown, "download", download)
    breakdown.gate_factors["severe_issue"] = not issues["severe"]
    breakdown.gate_factors["download_supported"] = download["supported"]

    if review["approved"]:
        _add_bonus(breakdown, "source review approved", 3, quality_key="source_review")
    elif review["rejected"]:
        _add_penalty(breakdown, "source review rejected", 8, quality_key="source_review")
    elif review["status"]:
        _add_penalty(breakdown, "source review pending", 2, quality_key="source_review")

    if issues["has_issues"]:
        # Keep this label/value stable for legacy reports and tests.  Severity
        # itself is persisted in the machine-readable factor above.
        _add_penalty(
            breakdown,
            "chart/metadata issues reported",
            10,
            quality_key="issue_reports",
        )

    if download["supported"]:
        _add_bonus(breakdown, "direct download capability", 2, quality_key="direct_download")
    return issues, review, download


def _quality_metadata(breakdown: SpotifyScoreBreakdown, chart: ChartCandidate) -> None:
    duration = _first_candidate_value(chart, ("song_length_ms", "duration_ms", "song_length", "songLength"))
    if duration is _MISSING:
        duration = None
    metadata_fields = [
        getattr(chart, "charter", ""),
        getattr(chart, "album", ""),
        getattr(chart, "genre", ""),
        getattr(chart, "year", ""),
        duration,
    ]
    completeness = sum(1 for value in metadata_fields if value) / len(metadata_fields)
    _record_factor(
        breakdown,
        "metadata_completeness",
        {"ratio": round(completeness, 3), "complete": completeness >= 0.8},
    )
    if completeness >= 0.8:
        _add_bonus(breakdown, "complete metadata", 2, quality_key="metadata_completeness")


def _duration_evidence(
    breakdown: SpotifyScoreBreakdown,
    track: Track,
    chart: ChartCandidate,
    config: AppConfig,
) -> None:
    track_duration = _duration_ms(getattr(track, "duration_ms", None))
    chart_duration_raw = _first_candidate_value(chart, ("song_length_ms", "duration_ms", "song_length", "songLength"))
    chart_duration = _duration_ms(chart_duration_raw)
    tolerance = _duration_tolerance_ms(config)
    evidence: dict[str, Any] = {
        "available": track_duration is not None and chart_duration is not None,
        "track_ms": track_duration,
        "chart_ms": chart_duration,
        "tolerance_ms": tolerance,
        "delta_ms": None,
        "within_tolerance": None,
        "score_contribution": 0,
    }
    if track_duration is None or chart_duration is None:
        _record_factor(breakdown, "duration", evidence)
        _record_factor(breakdown, "duration_match", None)
        return

    delta = abs(track_duration - chart_duration)
    within = delta <= tolerance
    evidence.update({"delta_ms": delta, "within_tolerance": within})
    if within:
        evidence["score_contribution"] = 5
        _add_bonus(breakdown, "Spotify duration within tolerance", 5)
    else:
        over_ratio = delta / max(tolerance, 1)
        penalty = min(12.0, max(3.0, round(2.0 + (over_ratio - 1.0) * 3.0, 1)))
        evidence["score_contribution"] = -penalty
        _add_penalty(breakdown, "Spotify duration outside tolerance", penalty)
    _record_factor(breakdown, "duration", evidence)
    # Flat aliases make persisted factors easy to query without knowing the
    # nested schema used by the browser.
    _record_factor(breakdown, "duration_delta_ms", delta)
    _record_factor(breakdown, "duration_within_tolerance", within)
    _record_factor(breakdown, "duration_match", within)


def _identity_evidence(
    breakdown: SpotifyScoreBreakdown,
    track: Track,
    chart: ChartCandidate,
) -> None:
    track_isrc = _track_isrc(track)
    chart_isrc = _candidate_isrc(chart)
    isrc_match: bool | None = None
    isrc_score = 0
    if track_isrc and chart_isrc:
        isrc_match = track_isrc == chart_isrc
        if isrc_match:
            isrc_score = 12
            _add_bonus(breakdown, "ISRC identity match", 12)
        else:
            isrc_score = -8
            _add_penalty(breakdown, "ISRC identity mismatch", 8)
    _record_factor(
        breakdown,
        "isrc",
        {
            "track": track_isrc or None,
            "candidate": chart_isrc or None,
            "both_provided": bool(track_isrc and chart_isrc),
            "match": isrc_match,
            "matched": isrc_match,
            "score_contribution": isrc_score,
        },
    )

    track_spotify_id = _spotify_identifier(getattr(track, "spotify_id", ""))
    chart_spotify_ids = _candidate_spotify_ids(chart)
    identity_match: bool | None = None
    identity_score = 0
    if track_spotify_id and chart_spotify_ids:
        identity_match = track_spotify_id.casefold() in {value.casefold() for value in chart_spotify_ids}
        if identity_match:
            identity_score = 14
            _add_bonus(breakdown, "exact Spotify track identity", 14)
        else:
            identity_score = -10
            _add_penalty(breakdown, "Spotify track identity mismatch", 10)
    _record_factor(
        breakdown,
        "spotify_identity",
        {
            "track_id": track_spotify_id or None,
            "candidate_ids": sorted(chart_spotify_ids, key=str.casefold),
            "provided": bool(track_spotify_id and chart_spotify_ids),
            "exact_match": identity_match,
            "matched": identity_match,
            "score_contribution": identity_score,
        },
    )


def _grouping_and_versions(
    breakdown: SpotifyScoreBreakdown,
    track: Track,
    chart: ChartCandidate,
) -> None:
    grouping = _grouping_values(chart)
    grouping_evidence = {
        **grouping,
        "has_song_group": bool(grouping.get("song_id") or grouping.get("group_id")),
        "has_version_group": bool(grouping.get("version_id") or grouping.get("version_group_id")),
        "matched": bool(grouping),
        "score_contribution": 0,
    }
    _record_factor(breakdown, "grouping", grouping_evidence)
    group_bonus = 0
    if grouping.get("song_id") or grouping.get("group_id"):
        group_bonus += 1
    if grouping.get("version_id") or grouping.get("version_group_id"):
        group_bonus += 2
    if grouping.get("song_id") and (grouping.get("version_id") or grouping.get("version_group_id")):
        group_bonus += 2
    if group_bonus:
        grouping_evidence["score_contribution"] = group_bonus
        _add_bonus(breakdown, "chart/song/version grouping evidence", group_bonus, quality_key="chart_grouping")

    track_markers = version_markers(str(getattr(track, "title", "") or ""))
    chart_markers = _version_markers_for_chart(chart)
    common = sorted(track_markers & chart_markers)
    only_track = track_markers - chart_markers
    only_chart = chart_markers - track_markers
    marker_evidence = {
        "track": sorted(track_markers),
        "candidate": sorted(chart_markers),
        "common": common,
        "match": track_markers == chart_markers,
        "matched": track_markers == chart_markers,
        "score_contribution": 0,
    }
    _record_factor(breakdown, "version_markers", marker_evidence)
    if track_markers == chart_markers:
        if track_markers:
            marker_evidence["score_contribution"] = 3
            _add_bonus(breakdown, "version markers agree", 3)
        else:
            marker_evidence["score_contribution"] = 1
            _add_bonus(breakdown, "original version markers agree", 1)
    elif only_chart:
        marker_evidence["score_contribution"] = -25
        _add_penalty(
            breakdown,
            f"version mismatch (chart is {'/'.join(sorted(only_chart))})",
            25,
            quality_key="version_mismatch",
        )
    elif only_track:
        marker_evidence["score_contribution"] = -15
        _add_penalty(
            breakdown,
            "version mismatch (chart is original)",
            15,
            quality_key="version_mismatch",
        )


def _set_threshold_eligibility(
    breakdown: SpotifyScoreBreakdown,
    config: AppConfig,
    *,
    update_decision: bool = True,
) -> None:
    if breakdown.gate_factors.get("hard_gate"):
        return
    auto_threshold, review_threshold = _thresholds(config)
    borderline_title = breakdown.gate_factors.get("title_identity") == "borderline"
    borderline_artist = breakdown.gate_factors.get("artist_identity") == "borderline"
    identity_review = borderline_title or borderline_artist
    if breakdown.final_score >= auto_threshold and not identity_review:
        state = RECOMMENDED
        reasons = ["score meets recommendation threshold"]
        decision = "AUTO_ACCEPT"
    elif breakdown.final_score >= review_threshold:
        state = ELIGIBLE_REVIEW
        identity_reasons = []
        if borderline_title:
            identity_reasons.append("borderline title identity requires review")
        if borderline_artist:
            identity_reasons.append("borderline artist identity requires review")
        reasons = identity_reasons or ["score meets export review threshold"]
        decision = "REVIEW"
    else:
        state = EXCLUDED
        reasons = ["score below export eligibility threshold"]
        if borderline_title:
            reasons.append("borderline title identity below review threshold")
        if borderline_artist:
            reasons.append("borderline artist identity below review threshold")
        decision = "REJECT"
    _set_eligibility(breakdown, state, reasons)
    if update_decision:
        breakdown.decision = decision
    _sync_factors(breakdown)


def score_candidate(
    track: Track,
    chart: ChartCandidate,
    config: AppConfig,
    source: Any = None,
) -> ScoreBreakdown:
    """Score one chart candidate against one Spotify track.

    The returned object is a ``ScoreBreakdown`` subclass, so old callers can
    continue to use ``decision``, ``quality``, ``bonuses`` and ``penalties``.
    """
    breakdown = _new_breakdown()

    title_norm = str(getattr(track, "title_norm", "") or normalize_text(getattr(track, "title", "")))
    chart_name_norm = str(getattr(chart, "name_norm", "") or normalize_text(getattr(chart, "name", "")))
    title_score = _similarity(title_norm, chart_name_norm)
    artist_score = _best_artist_similarity(track, chart)
    breakdown.title_score = title_score
    breakdown.artist_score = artist_score
    artist_threshold = _number(_config_value(config, ("artist_conflict_threshold",), 55.0), 55.0)
    base = title_score * TITLE_WEIGHT + artist_score * ARTIST_WEIGHT + 100 * BASE_COMPLEMENT
    breakdown.base_score = base

    # External IDs are pair-level identity and must be known before textual
    # identity gates.  Any explicit disagreement is stronger than fuzzy text.
    _identity_evidence(breakdown, track, chart)
    external_match, external_conflict = _external_identity_state(breakdown)
    breakdown.gate_factors["external_identity_match"] = external_match
    breakdown.gate_factors["external_identity_conflict"] = external_conflict
    if external_conflict:
        review_threshold = _thresholds(config)[1]
        return _hard_gate(
            breakdown,
            INCOMPATIBLE,
            ["external song identity conflict"],
            decision="REJECT",
            final_score=min(base, max(0.0, review_threshold - 1)),
        )

    title_identity = _title_identity_evidence(
        track,
        chart,
        title_norm,
        chart_name_norm,
        title_score,
        external_match=external_match,
    )
    artist_identity = _artist_identity_evidence(
        track,
        chart,
        artist_score,
        external_match=external_match,
        conflict_threshold=artist_threshold,
    )
    _record_factor(
        breakdown,
        "title",
        {
            "similarity": title_score,
            "plain_similarity": title_identity["plain_ratio"],
            "exact_normalized": title_norm == chart_name_norm,
            "conflict_threshold": TITLE_IDENTITY_CONFLICT_THRESHOLD,
            "auto_trust_threshold": TITLE_IDENTITY_PLAIN_AUTO_THRESHOLD,
            "identity": title_identity,
        },
    )
    _record_factor(
        breakdown,
        "artist",
        {
            "similarity": artist_score,
            "plain_similarity": artist_identity["plain_ratio"],
            "conflict_threshold": artist_threshold,
            "auto_trust_threshold": ARTIST_IDENTITY_PLAIN_AUTO_THRESHOLD,
            "identity": artist_identity,
        },
    )
    breakdown.gate_factors["artist_match"] = artist_score
    breakdown.gate_factors["title_similarity"] = title_score
    breakdown.gate_factors["title_identity"] = title_identity["state"]
    breakdown.gate_factors["title_auto_identity"] = title_identity["auto"]
    breakdown.gate_factors["artist_identity"] = artist_identity["state"]
    breakdown.gate_factors["artist_auto_identity"] = artist_identity["auto"]

    if artist_identity["state"] == "conflict":
        breakdown.gate_factors["artist_match"] = False
        breakdown.gate_factors["artist_conflict"] = True
        breakdown.base_score = title_score * TITLE_WEIGHT
        review_threshold = _thresholds(config)[1]
        final_score = min(breakdown.base_score, max(0.0, review_threshold - 1))
        return _hard_gate(
            breakdown,
            INCOMPATIBLE,
            [f"artist conflict ({track.primary_artist!r} vs {chart.artist!r})"],
            decision="REJECT",
            final_score=final_score,
        )

    if title_identity["state"] == "conflict":
        breakdown.gate_factors["title_conflict"] = True
        breakdown.base_score = base
        review_threshold = _thresholds(config)[1]
        final_score = min(base, max(0.0, review_threshold - 1))
        return _hard_gate(
            breakdown,
            INCOMPATIBLE,
            [
                f"title conflict ({title_identity['plain_ratio']:.1f}% plain similarity; "
                f"{title_score:.1f}% ranking similarity: "
                f"{track.title!r} vs {chart.name!r})"
            ],
            decision="REJECT",
            final_score=final_score,
        )

    # Compatibility follows identity: chart quality or playability cannot
    # change whether the metadata identifies the requested song.
    if not has_playable_content(chart):
        breakdown.gate_factors["playable_content"] = False
        return _hard_gate(
            breakdown,
            INCOMPATIBLE,
            ["no playable chart content"],
            decision="REJECT",
        )
    breakdown.gate_factors["playable_content"] = True

    compatible, reason = instrument_compatible(chart, config)
    if not compatible:
        breakdown.gate_factors["required_five_fret_guitar"] = _has_five_fret_guitar(chart)
        breakdown.gate_factors["modchart_allowed"] = not reason.startswith("modchart")
        state = EXCLUDED if reason.startswith("modchart") else INCOMPATIBLE
        return _hard_gate(breakdown, state, [reason], decision="INCOMPATIBLE")
    breakdown.gate_factors["required_five_fret_guitar"] = _has_five_fret_guitar(chart)
    breakdown.gate_factors["modchart_allowed"] = True

    issues, _review, download = _source_evidence(breakdown, chart, source)
    source_gate_reasons: list[str] = []
    if not download["supported"]:
        source_gate_reasons.extend(download["reasons"])
        breakdown.gate_factors["download_supported"] = False
    if issues["severe"]:
        source_gate_reasons.append("severe source issue")
        breakdown.gate_factors["severe_issue"] = False
    if source_gate_reasons:
        return _hard_gate(breakdown, EXCLUDED, source_gate_reasons, decision="REJECT")

    # ---- deterministic match evidence ------------------------------------
    if title_norm == chart_name_norm:
        _add_bonus(breakdown, "exact normalized title", 6)
    if track.primary_artist_norm == _normalised_chart_artist(chart):
        _add_bonus(breakdown, "exact normalized artist", 6)
    chart_album_norm = normalize_text(str(getattr(chart, "album", "") or ""))
    if getattr(chart, "album", "") and getattr(track, "album_norm", "") and track.album_norm == chart_album_norm:
        _add_bonus(breakdown, "album match", 4)

    _duration_evidence(breakdown, track, chart, config)
    _grouping_and_versions(breakdown, track, chart)

    # ---- quality bonuses --------------------------------------------------
    icon = str(getattr(chart, "icon", "") or "").casefold()
    if icon in _OFFICIAL_ICONS or normalize_text(str(getattr(chart, "charter", "") or "")) in _OFFICIAL_CHARTERS:
        _add_bonus(breakdown, "official game conversion", 10, quality_key="official_game_conversion")
    if getattr(chart, "pack_name", ""):
        _add_bonus(breakdown, "curated pack source", 2, quality_key="curated_pack")
    instrument_count = int(getattr(chart, "instrument_count", len(getattr(chart, "instruments", ()) or ())))
    if instrument_count >= 4:
        _add_bonus(breakdown, "full band chart", 8, quality_key="full_band")
    elif instrument_count >= 2:
        _add_bonus(breakdown, "multi-instrument chart", 4, quality_key="multi_instrument")
    _quality_metadata(breakdown, chart)

    # ---- quality penalties ------------------------------------------------
    if instrument_count == 1:
        _add_penalty(breakdown, "single-instrument chart", 8, quality_key="single_instrument")
    guitar_difficulty = _difficulty_value(chart, ("diff_guitar", "guitar", "guitar5"))
    if _has_five_fret_guitar(chart):
        if guitar_difficulty in (_MISSING, None):
            _add_penalty(breakdown, "guitar difficulty unrated", 3, quality_key="unrated_guitar")
        else:
            # A rated guitar is positive evidence, but intentionally smaller
            # than the unrated penalty so catalog fallback behavior remains
            # stable around configurable review thresholds.
            _add_bonus(breakdown, "5-fret guitar difficulty rated", 0.5, quality_key="rated_guitar")
    if bool(getattr(chart, "modchart", False)):
        # This path is only reachable when advanced settings allow modcharts.
        _add_penalty(breakdown, "modchart allowed by configuration", 10, quality_key="modchart")

    total = base + sum(value for _, value in breakdown.bonuses) - sum(value for _, value in breakdown.penalties)
    breakdown.final_score = max(0.0, min(100.0, total))
    review_identities = [
        label
        for label in ("title", "artist")
        if breakdown.gate_factors.get(f"{label}_identity") == "borderline"
    ]
    if review_identities:
        auto_threshold = _thresholds(config)[0]
        breakdown.final_score = min(breakdown.final_score, max(0.0, auto_threshold - 1))
        for label in review_identities:
            breakdown.reasons.append(
                f"borderline {label} identity; automatic recommendation blocked"
            )
    _set_threshold_eligibility(breakdown, config)
    return breakdown


def score_expansion_candidate(
    artist_norm: str,
    chart: ChartCandidate,
    config: AppConfig,
    source: Any = None,
) -> ScoreBreakdown:
    """Score a chart for artist-expansion (artist must match strongly)."""
    breakdown = _new_breakdown()

    if not has_playable_content(chart):
        breakdown.gate_factors["playable_content"] = False
        return _hard_gate(breakdown, INCOMPATIBLE, ["no playable chart content"], decision="REJECT")
    breakdown.gate_factors["playable_content"] = True

    compatible, reason = instrument_compatible(chart, config)
    if not compatible:
        breakdown.gate_factors["required_five_fret_guitar"] = _has_five_fret_guitar(chart)
        breakdown.gate_factors["modchart_allowed"] = not reason.startswith("modchart")
        state = EXCLUDED if reason.startswith("modchart") else INCOMPATIBLE
        return _hard_gate(breakdown, state, [reason], decision="INCOMPATIBLE")
    breakdown.gate_factors["required_five_fret_guitar"] = _has_five_fret_guitar(chart)
    breakdown.gate_factors["modchart_allowed"] = True

    artist_score = _similarity(artist_norm, _normalised_chart_artist(chart))
    breakdown.artist_score = artist_score
    breakdown.title_score = 0.0
    _record_factor(breakdown, "artist", {"similarity": artist_score, "expansion_minimum": 80.0})
    if artist_score < 80.0:
        breakdown.gate_factors["artist_match"] = False
        breakdown.gate_factors["artist_conflict"] = True
        return _hard_gate(
            breakdown,
            INCOMPATIBLE,
            [f"expansion artist mismatch ({artist_norm!r} vs {chart.artist!r})"],
            decision="REJECT",
        )
    breakdown.gate_factors["artist_match"] = artist_score

    issues, _review, download = _source_evidence(breakdown, chart, source)
    source_gate_reasons: list[str] = []
    if not download["supported"]:
        source_gate_reasons.extend(download["reasons"])
    if issues["severe"]:
        source_gate_reasons.append("severe source issue")
    if source_gate_reasons:
        return _hard_gate(breakdown, EXCLUDED, source_gate_reasons, decision="REJECT")

    # Quality-only base: artist identity contributes up to 60 points.
    base = 30.0 + (artist_score - 80.0) * 1.5
    breakdown.base_score = base

    icon = str(getattr(chart, "icon", "") or "").casefold()
    if icon in _OFFICIAL_ICONS or normalize_text(str(getattr(chart, "charter", "") or "")) in _OFFICIAL_CHARTERS:
        _add_bonus(breakdown, "official game conversion", 20, quality_key="official_game_conversion")
    if getattr(chart, "pack_name", ""):
        _add_bonus(breakdown, "curated pack source", 3, quality_key="curated_pack")
    if _has_five_fret_guitar(chart) and _difficulty_value(chart, ("diff_guitar", "guitar", "guitar5")) not in (_MISSING, None):
        _add_bonus(breakdown, "5-fret guitar difficulty rated", 10, quality_key="rated_guitar")
    if int(getattr(chart, "instrument_count", len(getattr(chart, "instruments", ()) or ()))) >= 4:
        _add_bonus(breakdown, "full band chart", 12, quality_key="full_band")
    elif int(getattr(chart, "instrument_count", len(getattr(chart, "instruments", ()) or ()))) >= 2:
        _add_bonus(breakdown, "multi-instrument chart", 4, quality_key="multi_instrument")
    _quality_metadata(breakdown, chart)
    grouping = _grouping_values(chart)
    _record_factor(
        breakdown,
        "grouping",
        {
            **grouping,
            "has_song_group": bool(grouping.get("song_id") or grouping.get("group_id")),
            "has_version_group": bool(grouping.get("version_id") or grouping.get("version_group_id")),
        },
    )
    if grouping:
        _add_bonus(breakdown, "chart/song/version grouping evidence", 2, quality_key="chart_grouping")
    chart_markers = _version_markers_for_chart(chart)
    _record_factor(breakdown, "version_markers", {"candidate": sorted(chart_markers)})
    if chart_markers & {"karaoke", "tribute", "cover"}:
        _add_penalty(breakdown, "cover/karaoke version", 30, quality_key="cover_version")
    if int(getattr(chart, "instrument_count", len(getattr(chart, "instruments", ()) or ()))) == 1:
        _add_penalty(breakdown, "single-instrument chart", 10, quality_key="single_instrument")
    if bool(getattr(chart, "modchart", False)):
        _add_penalty(breakdown, "modchart allowed by configuration", 10, quality_key="modchart")

    total = base + sum(value for _, value in breakdown.bonuses) - sum(value for _, value in breakdown.penalties)
    breakdown.final_score = max(0.0, min(100.0, total))
    expansion_min = _number(getattr(getattr(config, "expansion", None), "min_score", 80.0), 80.0)
    breakdown.decision = "AUTO_ACCEPT" if breakdown.final_score >= expansion_min else "REJECT"
    # Eligibility still uses the shared 90/70 export thresholds.  The legacy
    # expansion minimum remains in ``decision`` for old planner callers.
    _set_threshold_eligibility(breakdown, config, update_decision=False)
    return breakdown


def _eligibility_from_breakdown(breakdown: ScoreBreakdown, config: Any = None) -> EligibilityResult:
    state = getattr(breakdown, "eligibility", "")
    reasons = list(getattr(breakdown, "eligibility_reasons", []) or [])
    if state in ELIGIBILITY_STATES:
        return EligibilityResult(state, reasons)

    factors = getattr(breakdown, "gate_factors", {})
    if isinstance(factors, Mapping) and factors.get("hard_gate"):
        fallback_state = INCOMPATIBLE if str(getattr(breakdown, "decision", "")) == INCOMPATIBLE else EXCLUDED
        return EligibilityResult(
            fallback_state,
            list(factors.get("hard_gate_reasons", ())) or list(getattr(breakdown, "reasons", ())),
        )
    if getattr(breakdown, "decision", "") == INCOMPATIBLE:
        return EligibilityResult(INCOMPATIBLE, list(getattr(breakdown, "reasons", ())))

    auto, review = _thresholds(config)
    score = _number(getattr(breakdown, "final_score", 0.0), 0.0)
    if score >= auto:
        return EligibilityResult(RECOMMENDED, ["score meets recommendation threshold"])
    if score >= review:
        return EligibilityResult(ELIGIBLE_REVIEW, ["score meets export review threshold"])
    return EligibilityResult(EXCLUDED, ["score below export eligibility threshold"])


def eligibility_for(
    value: Track | ScoreBreakdown | Mapping[str, Any] | None = None,
    chart: ChartCandidate | AppConfig | None = None,
    config: AppConfig | None = None,
    *,
    track: Track | None = None,
    breakdown: ScoreBreakdown | None = None,
) -> EligibilityResult:
    """Return the export state and reasons for a score or candidate pair.

    Supported forms are ``eligibility_for(breakdown, config)``,
    ``eligibility_for(track, chart, config)``, and keyword forms using
    ``track=``/``breakdown=``.  The result supports attribute, mapping, and
    two-value-unpacking access.
    """
    if breakdown is not None:
        return _eligibility_from_breakdown(breakdown, config)
    if track is not None:
        value = track
    if isinstance(value, ScoreBreakdown):
        if config is None and chart is not None and not isinstance(chart, ChartCandidate):
            config = chart  # type: ignore[assignment]
        return _eligibility_from_breakdown(value, config)
    if isinstance(value, Mapping):
        state = value.get("eligibility") or value.get("eligibility_state") or value.get("state")
        if state in ELIGIBILITY_STATES:
            raw_reasons = value.get("reasons") or value.get("eligibility_reasons") or []
            return EligibilityResult(str(state), [str(reason) for reason in raw_reasons])
        if config is None and chart is not None and not isinstance(chart, ChartCandidate):
            config = chart  # type: ignore[assignment]
        temporary = _new_breakdown()
        temporary.final_score = _number(value.get("final_score", value.get("score", 0.0)), 0.0)
        temporary.decision = str(value.get("decision", "REJECT"))
        return _eligibility_from_breakdown(temporary, config)
    if isinstance(value, Track) and isinstance(chart, ChartCandidate):
        return _eligibility_from_breakdown(score_candidate(value, chart, config or AppConfig()), config)
    raise TypeError("eligibility_for expects a ScoreBreakdown or (Track, ChartCandidate) pair")


def export_eligibility(
    value: Track | ScoreBreakdown | Mapping[str, Any] | None = None,
    chart: ChartCandidate | AppConfig | None = None,
    config: AppConfig | None = None,
    *,
    track: Track | None = None,
    breakdown: ScoreBreakdown | None = None,
) -> EligibilityResult:
    """Alias for :func:`eligibility_for` used by export-facing callers."""
    return eligibility_for(value, chart, config, track=track, breakdown=breakdown)


def pick_best(
    track: Track, charts: list[ChartCandidate], config: AppConfig
) -> tuple[ChartCandidate | None, ScoreBreakdown | None, list[tuple[ChartCandidate, ScoreBreakdown]]]:
    """Score every candidate and pick the best one for a track.

    Returns ``(best_chart, best_breakdown, all_scored)``.  Quality is a stable
    tie-break after the capped score, followed by source and chart ID so the
    result is reproducible across runs.
    """
    scored = [(chart, score_candidate(track, chart, config)) for chart in charts]
    scored.sort(
        key=lambda pair: (
            -pair[1].final_score,
            -pair[1].quality,
            str(pair[0].source).casefold(),
            int(pair[0].chart_id),
        )
    )
    if not scored:
        return None, None, scored
    return scored[0][0], scored[0][1], scored
