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
