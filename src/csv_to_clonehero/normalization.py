"""Music-metadata aware string normalization.

Two forms are produced for every string:

* a display-friendly cleaned form (original casing kept by callers);
* ``normalize_text`` - a lowercase comparison key with unicode folded,
  punctuation collapsed and whitespace squeezed.

``normalize_title`` additionally removes common *version noise* (remasters,
deluxe editions, radio edits...) so that e.g. ``Everlong - 2015 Remaster``
matches the chart titled ``Everlong``. It deliberately does NOT strip
meaningful wording: ``(Don't Fear) The Reaper`` keeps its parenthesized part,
and live/remix/acoustic markers are preserved (they identify a *different
version* and are handled as a penalty by the matcher instead).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Version-noise keywords that may appear in trailing " - ..." segments or in
# parentheses/brackets and are safe to drop when comparing titles.
_NOISE_WORDS = (
    r"remaster(?:ed)?(?:\s+\d{4})?",
    r"\d{4}\s+remaster(?:ed)?",
    r"deluxe(?:\s+edition)?",
    r"(?:\d+(?:st|nd|rd|th)\s+)?anniversary\s+edition",
    r"special\s+edition",
    r"expanded\s+edition",
    r"collector'?s\s+edition",
    r"album\s+version",
    r"single\s+version",
    r"original\s+version",
    r"radio\s+edit",
    r"radio\s+mix",
    r"edit",
    r"mono(?:\s+version)?",
    r"stereo(?:\s+version)?",
    r"explicit",
    r"clean",
    r"bonus\s+track",
    r"re-?recorded",
    r"\d{4}\s+mix",
)

# Trailing " - <noise>" or " - <noise> & <noise>" segments, e.g. " - 2015 Remaster".
_TRAILING_NOISE_RE = re.compile(
    rf"\s+[-\u2013\u2014]\s+(?:{'|'.join(_NOISE_WORDS)})(?:\s*[&/,]\s*(?:{'|'.join(_NOISE_WORDS)}))*\s*$",
    re.IGNORECASE,
)

# Parenthesized/bracketed segments that consist purely of noise words,
# e.g. "(Remastered)", "[Deluxe Edition]", "(2009 Remaster)".
_PAREN_NOISE_RE = re.compile(
    rf"\s*[\(\[]\s*(?:{'|'.join(_NOISE_WORDS)})(?:\s*[&/,]\s*(?:{'|'.join(_NOISE_WORDS)}))*\s*[\)\]]",
    re.IGNORECASE,
)

# Markers that identify a *different version* of a song. Kept in the normalized
# title, but compared separately so the matcher can penalize mismatches.
VERSION_MARKERS = (
    "live",
    "remix",
    "acoustic",
    "unplugged",
    "karaoke",
    "instrumental",
    "cover",
    "tribute",
    "demo",
    "orchestral",
    "symphonic",
    "acapella",
    "a cappella",
    "reprise",
    "medley",
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

_FEAT_RE = re.compile(
    r"\s*(?:\(|\[)?\s*(?:feat\.?|ft\.?|featuring)\s+",
    re.IGNORECASE,
)

_ENCHOR_FEATURE_CLAUSE_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\b.*?[\)\]]+",
    re.IGNORECASE,
)
_ENCHOR_NOISE_CLAUSE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:remaster(?:ed)?|live|deluxe|version|edit|mono|stereo|"
    r"soundtrack|anniversary|karaoke)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_ENCHOR_TRAILING_NOISE_RE = re.compile(
    r"\s+[-\u2013\u2014]\s+.*\b(?:remaster(?:ed)?|live|deluxe|version|edit|mono|stereo|"
    r"soundtrack|anniversary|karaoke)\b.*$",
    re.IGNORECASE,
)
_ENCHOR_UNSAFE_RE = re.compile(r'''[\"“”'‘’\(\)\[\]{}\\/]+''')


def _fold_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # Curly apostrophes/quotes/dashes -> plain ASCII equivalents.
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u2026", "...")
    return text


def strip_version_noise(title: str) -> str:
    """Remove remaster/edition/version noise without touching meaningful text."""
    result = _fold_unicode(title)
    previous = None
    while previous != result:
        previous = result
        result = _TRAILING_NOISE_RE.sub("", result)
        result = _PAREN_NOISE_RE.sub("", result)
    return _WS_RE.sub(" ", result).strip()


def normalize_text(text: str) -> str:
    """Lowercase comparison key: unicode folded, punctuation collapsed."""
    text = _fold_unicode(text).lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    """Comparison key for song titles (version noise stripped)."""
    return normalize_text(strip_version_noise(title))


def normalize_artist(artist: str) -> str:
    """Comparison key for artist names."""
    return normalize_text(artist)


def sanitize_query_for_enchor(
    artist: str,
    title: str,
    *,
    max_length: int = 180,
) -> tuple[str, str]:
    """Return compact, parser-safe display text for an Enchor song query."""

    def clean(value: str, *, remove_versions: bool) -> str:
        result = _fold_unicode(str(value or ""))
        previous = None
        while result != previous:
            previous = result
            result = _ENCHOR_FEATURE_CLAUSE_RE.sub("", result)
            if remove_versions:
                result = _ENCHOR_NOISE_CLAUSE_RE.sub("", result)
                result = _ENCHOR_TRAILING_NOISE_RE.sub("", result)
        result = _ENCHOR_UNSAFE_RE.sub(" ", result)
        result = _WS_RE.sub(" ", result).strip(" .,-_")
        return result

    clean_artist = clean(artist, remove_versions=False)
    clean_title = clean(strip_version_noise(title), remove_versions=True)
    if max_length > 0:
        clean_artist = clean_artist[: min(80, max_length)].rstrip()
        remaining = max(1, max_length - len(clean_artist) - bool(clean_artist))
        clean_title = clean_title[:remaining].rstrip()
    return clean_artist, clean_title


def version_markers(title: str) -> set[str]:
    """Return the set of version markers (live/remix/...) present in a title."""
    lowered = normalize_text(title)
    found: set[str] = set()
    for marker in VERSION_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            found.add(marker)
    return found


def split_artists(artist_string: str) -> tuple[str, list[str]]:
    """Split a credited artist string into (primary artist, [other artists]).

    Splits on commas, semicolons and feat./ft./featuring/with markers.
    Deliberately does NOT split on "&" to avoid breaking names such as
    "Earth, Wind & Fire" incorrectly (the comma still splits that one, which
    matches how Spotify credits it).
    """
    text = _fold_unicode(artist_string).strip()
    if not text:
        return "", []
    # Normalize feat./ft. markers to a comma so a single split works.
    text = _FEAT_RE.sub(", ", text)
    text = re.sub(r"\s+\bwith\b\s+", ", ", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"[,;]", text)]
    parts = [p for p in parts if p]
    if not parts:
        return "", []
    return parts[0], parts[1:]


_RETRIEVAL_ARTIST_SUFFIX_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_RETRIEVAL_ANNOTATION_WORDS = frozenset(
    {
        "acapella",
        "acoustic",
        "audio",
        "bootleg",
        "clean",
        "cover",
        "demo",
        "deluxe",
        "edit",
        "explicit",
        "extended",
        "feat",
        "featuring",
        "flip",
        "ft",
        "instrumental",
        "karaoke",
        "live",
        "lyrics",
        "mix",
        "mono",
        "original",
        "prod",
        "produced",
        "radio",
        "remaster",
        "remastered",
        "remix",
        "rework",
        "rmx",
        "slowed",
        "soundtrack",
        "stereo",
        "tribute",
        "unplugged",
        "version",
        "video",
        "visualizer",
        "with",
    }
)
_RETRIEVAL_CLAUSE_RE = re.compile(
    r"\s*[\(\[](?=[^\)\]]*\b(?:"
    + "|".join(sorted(_RETRIEVAL_ANNOTATION_WORDS))
    + r")\b)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_RETRIEVAL_TRAILING_ANNOTATION_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring|with|prod\.?|produced(?:\s+by)?|"
    r"official(?:\s+(?:audio|video))?|audio|video|visualizer|lyrics)\b.*$",
    re.IGNORECASE,
)
_RETRIEVAL_DASH_ANNOTATION_RE = re.compile(
    r"\s+[-\u2013\u2014]\s+.*\b(?:"
    + "|".join(sorted(_RETRIEVAL_ANNOTATION_WORDS))
    + r")\b.*$",
    re.IGNORECASE,
)


@lru_cache(maxsize=16384)
def normalize_title_without_recognized_annotations(title: str) -> str:
    """Normalize a title after removing only existing recognized annotations.

    Unlike :func:`normalize_title`, this is an opt-in identity signal for a
    complete title followed by feature/producer/version text.  It deliberately
    does not remove arbitrary words or generic parenthesized title content.
    """
    simplified = strip_version_noise(_fold_unicode(str(title or "")).strip())
    previous = None
    while simplified != previous:
        previous = simplified
        simplified = _RETRIEVAL_CLAUSE_RE.sub("", simplified)
        simplified = _RETRIEVAL_TRAILING_ANNOTATION_RE.sub("", simplified)
        simplified = _RETRIEVAL_DASH_ANNOTATION_RE.sub("", simplified)
        simplified = _WS_RE.sub(" ", simplified).strip()
    return normalize_title(simplified)


@lru_cache(maxsize=16384)
def retrieval_artist_forms(artist: str) -> tuple[str, ...]:
    """Return conservative artist aliases used only for catalog retrieval.

    These aliases never replace the artist stored on a :class:`Track` or a
    :class:`ChartCandidate`; the deterministic matcher still scores the raw
    metadata.  Keeping the aliases retrieval-only lets names such as
    ``Artist (WaveGroup)`` and credited forms such as ``Artist & Guest`` reach
    the matcher without making those representations identical everywhere.
    """
    text = _fold_unicode(str(artist or "")).strip()
    if not text:
        return ()

    forms: list[str] = []

    def add(value: str) -> None:
        key = normalize_artist(value)
        if key and key not in forms:
            forms.append(key)

    add(text)
    primary, featured = split_artists(text)
    add(primary)
    for value in featured:
        add(value)

    # Some catalog payloads append a charter/source label to the artist.  Keep
    # the complete form above, but also allow the musical name to be retrieved.
    without_suffix = _RETRIEVAL_ARTIST_SUFFIX_RE.sub("", text).strip()
    if without_suffix != text:
        add(without_suffix)
        suffix_primary, suffix_featured = split_artists(without_suffix)
        add(suffix_primary)
        for value in suffix_featured:
            add(value)

    # Ampersands are ambiguous in names, so retain the complete form and add
    # each side only as a secondary retrieval alias.
    for value in re.split(r"\s*&\s*", text):
        add(value)
    return tuple(forms)


@lru_cache(maxsize=16384)
def retrieval_artist_tokens(artist: str) -> tuple[str, ...]:
    """Return tokens for the catalog's secondary artist inverted index."""
    tokens: list[str] = []
    for form in retrieval_artist_forms(artist):
        for token in form.split():
            if len(token) < 2 or token in {"a", "an", "and", "of", "the"}:
                continue
            if token not in tokens:
                tokens.append(token)
    return tuple(tokens)


@lru_cache(maxsize=16384)
def retrieval_title_forms(title: str) -> tuple[str, ...]:
    """Return title aliases for high-recall catalog lookup.

    The display title and ``normalize_title`` remain authoritative for final
    scoring.  This projection removes only recognizable annotation clauses
    (feature/producer/version text) and adds pipe-separated sides as aliases;
    meaningful parenthesized title text is preserved when it has no such
    marker.
    """
    text = _fold_unicode(str(title or "")).strip()
    if not text:
        return ()
    forms: list[str] = []

    def add(value: str) -> None:
        key = normalize_title(value)
        if key and key not in forms:
            forms.append(key)

    add(text)
    simplified = normalize_title_without_recognized_annotations(text)
    add(simplified)

    # SoundCloud titles sometimes carry a second identity after a pipe.  Both
    # sides are aliases only; no orientation decision is made here.
    for value in text.split("|"):
        add(value.strip())
    return tuple(forms)


@lru_cache(maxsize=16384)
def retrieval_title_tokens(title: str) -> tuple[str, ...]:
    """Return annotation-light title tokens for an inverted index."""
    tokens: list[str] = []
    for form in retrieval_title_forms(title):
        for token in form.split():
            if token in _RETRIEVAL_ANNOTATION_WORDS or len(token) < 2:
                continue
            if token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def make_track_key(spotify_id: str, primary_artist_norm: str, title_norm: str) -> str:
    """Deduplication key: Spotify track id when available, else normalized pair."""
    if spotify_id:
        return f"spotify:{spotify_id}"
    return f"{primary_artist_norm}|{title_norm}"


def extract_spotify_id(track_id: str, uri: str) -> str:
    """Extract a Spotify track id from a raw id column and/or a URI column."""
    track_id = (track_id or "").strip()
    if track_id:
        return track_id
    uri = (uri or "").strip()
    match = re.search(r"spotify:track:([A-Za-z0-9]+)", uri)
    if match:
        return match.group(1)
    # Some exporters put the open.spotify.com URL in the URI column.
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", uri)
    if match:
        return match.group(1)
    return ""
