"""Read-only Enchor catalog snapshots and the one-shot export helper.

The application never crawls here.  A snapshot is authoritative only when its
header says so and its checksum covers every chart row.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import (
    normalize_artist,
    normalize_title,
    retrieval_artist_forms,
    retrieval_artist_tokens,
    retrieval_title_forms,
    retrieval_title_tokens,
)

FORMAT_VERSION = 1
MAX_CATALOG_HEADER_BYTES = 32 * 1024
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "enchor.jsonl"


def _row_key(artist: str, title: str) -> tuple[str, str]:
    return normalize_artist(artist), normalize_title(title)


@dataclass(frozen=True)
class CatalogLookup:
    """High-recall local lookup result, separate from final score semantics."""

    candidates: tuple[ChartCandidate, ...] = ()
    strict_exact: bool = False
    methods: tuple[str, ...] = ()
    retrieved_song_keys: int = 0
    exact_artist: bool = False
    exact_title: bool = False
    candidate_methods: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)
    fuzzy_fallback_attempted: bool = False


class CatalogLoadError(ValueError):
    """The local snapshot cannot safely be used for negative filtering."""


class EnchorCatalog:
    """Indexed, immutable-in-process catalog snapshot."""

    def __init__(
        self,
        path: Path | str = DEFAULT_CATALOG_PATH,
        *,
        excluded_identities: frozenset[tuple[str, int]] = frozenset(),
        validation_fingerprint: str = "",
    ) -> None:
        self.path = Path(path)
        self.excluded_identities = frozenset(
            (str(source), int(chart_id)) for source, chart_id in excluded_identities
        )
        self.validation_fingerprint = str(validation_fingerprint or "")
        self.complete = False
        self.diagnostic = "catalog unavailable"
        self.snapshot_id = ""
        self.sha256 = ""
        self.load_seconds = 0.0
        self._artists: set[str] = set()
        self._songs: dict[tuple[str, str], list[ChartCandidate]] = defaultdict(list)
        self._songs_by_artist: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._artist_aliases: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._artist_tokens: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._title_aliases: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._title_tokens: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._artist_choices: tuple[str, ...] = ()
        self._title_choices: tuple[str, ...] = ()
        self._artist_forms_by_key: dict[tuple[str, str], tuple[str, ...]] = {}
        self._title_forms_by_key: dict[tuple[str, str], tuple[str, ...]] = {}
        self._artist_fuzzy_cache: dict[tuple[str, int], tuple[str, ...]] = {}
        self._title_fuzzy_cache: dict[tuple[str, int], tuple[str, ...]] = {}
        self._chart_identities: dict[tuple[str, int], ChartCandidate] = {}
        self._known_chart_identities: set[tuple[str, int]] = set()
        self._chart_count: int | None = None
        started = time.perf_counter()
        self._load()
        self.load_seconds = time.perf_counter() - started

    @property
    def available(self) -> bool:
        return self.complete

    @property
    def chart_count(self) -> int:
        # The snapshot is immutable once loaded, and this is read once per
        # song while matching; summing ~94k rows each time dominated runs.
        if self._chart_count is None:
            self._chart_count = sum(len(rows) for rows in self._songs.values())
        return self._chart_count

    @property
    def known_chart_count(self) -> int:
        return len(self._known_chart_identities)

    @property
    def cache_identity(self) -> str:
        return ":".join(
            value for value in (self.sha256, self.validation_fingerprint) if value
        )

    @property
    def manifest(self) -> dict[str, Any]:
        if not self.available:
            raise CatalogLoadError(self.diagnostic)
        return {
            "snapshot_id": self.snapshot_id,
            "sha256": self.sha256,
            "chart_count": self.chart_count,
            "known_chart_count": self.known_chart_count,
            "format_version": FORMAT_VERSION,
            "validation_fingerprint": self.validation_fingerprint,
        }

    @property
    def song_count(self) -> int:
        return len(self._songs)

    def has_artist(self, artist: str) -> bool:
        return normalize_artist(artist) in self._artists

    def has_song(self, artist: str, title: str) -> bool:
        return bool(self._songs.get(_row_key(artist, title)))

    def candidates(self, artist: str, title: str) -> tuple[ChartCandidate, ...]:
        return tuple(self._songs.get(_row_key(artist, title), ()))

    def has_chart(self, chart_id: int, source: str = "enchor") -> bool:
        """Return whether this complete snapshot contains a chart identity."""
        try:
            chart_id = int(chart_id)
        except (TypeError, ValueError):
            return False
        return (str(source), chart_id) in self._chart_identities

    def has_known_chart(self, chart_id: int, source: str = "enchor") -> bool:
        try:
            chart_id = int(chart_id)
        except (TypeError, ValueError):
            return False
        return (str(source), chart_id) in self._known_chart_identities

    def artists(self) -> tuple[str, ...]:
        return tuple(sorted(self._artists))

    def charts(self) -> tuple[ChartCandidate, ...]:
        """Return every indexed chart in deterministic source/id order."""
        return tuple(sorted(
            self._chart_identities.values(),
            key=lambda chart: (str(chart.source), int(chart.chart_id)),
        ))

    def iter_charts(self):
        """Iterate indexed charts without materializing or sorting the catalog."""
        return iter(self._chart_identities.values())

    def get_chart(self, source: str, chart_id: int) -> ChartCandidate | None:
        try:
            identity = (str(source), int(chart_id))
        except (TypeError, ValueError):
            return None
        return self._chart_identities.get(identity)

    def artist_songs(self, artist: str) -> tuple[tuple[str, tuple[ChartCandidate, ...]], ...]:
        key = normalize_artist(artist)
        return tuple(
            (song_key[1], tuple(self._songs[song_key]))
            for song_key in sorted(self._songs_by_artist.get(key, ()))
        )

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        return max(fuzz.ratio(left, right), fuzz.token_set_ratio(left, right))

    @staticmethod
    def _plain_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        return fuzz.ratio(left, right)

    def _artist_similarity(self, query_forms: tuple[str, ...], key: tuple[str, str]) -> float:
        chart_forms = self._artist_forms_by_key.get(key) or retrieval_artist_forms(key[0])
        return max(
            (
                self._similarity(query, chart)
                for query in query_forms
                for chart in chart_forms
            ),
            default=0.0,
        )

    def _title_similarity(self, query_forms: tuple[str, ...], key: tuple[str, str]) -> float:
        chart_forms = self._title_forms_by_key.get(key) or retrieval_title_forms(key[1])
        return max(
            (
                self._similarity(query, chart)
                for query in query_forms
                for chart in chart_forms
            ),
            default=0.0,
        )

    def _artist_plain_similarity(
        self, query_forms: tuple[str, ...], key: tuple[str, str]
    ) -> float:
        chart_forms = self._artist_forms_by_key.get(key) or retrieval_artist_forms(key[0])
        return max(
            (
                self._plain_similarity(query, chart)
                for query in query_forms
                for chart in chart_forms
            ),
            default=0.0,
        )

    def _title_plain_similarity(
        self, query_forms: tuple[str, ...], key: tuple[str, str]
    ) -> float:
        chart_forms = self._title_forms_by_key.get(key) or retrieval_title_forms(key[1])
        return max(
            (
                self._plain_similarity(query, chart)
                for query in query_forms
                for chart in chart_forms
            ),
            default=0.0,
        )

    @staticmethod
    def _leading_artist_symbol(value: str) -> str:
        text = str(value or "").strip()
        return "$" if text.startswith("$") else ""

    @classmethod
    def _artist_symbol_conflict(
        cls, query_values: tuple[str, ...], candidate_artist: str
    ) -> bool:
        """Protect normalized collisions such as the artists ``$NOT``/``Snot``."""
        candidate_norm = normalize_artist(candidate_artist)
        candidate_symbol = cls._leading_artist_symbol(candidate_artist)
        query_symbols = [cls._leading_artist_symbol(value) for value in query_values]
        if candidate_symbol:
            return candidate_symbol not in query_symbols
        if any(query_symbols):
            # An explicit non-symbolic alternate still authorizes that spelling.
            return not any(
                not cls._leading_artist_symbol(value)
                and normalize_artist(value) == candidate_norm
                for value in query_values
            )
        comparable = [
            cls._leading_artist_symbol(value)
            for value in query_values
            if normalize_artist(value) == candidate_norm
        ]
        return bool(
            comparable
            and candidate_symbol not in comparable
            and (candidate_symbol or any(comparable))
        )

    @staticmethod
    def _candidate_shortlist_priority(candidate: ChartCandidate) -> tuple[int, ...]:
        instruments = {str(value).casefold() for value in candidate.instruments}
        five_fret = bool(
            candidate.has_five_fret_guitar
            or instruments & {"guitar", "guitar5", "guitar5fret", "leadguitar"}
        )
        playable = bool(instruments or candidate.difficulty_fields or candidate.difficulties)
        if not playable:
            playable = any(
                getattr(candidate, field_name, None) is not None
                and int(getattr(candidate, field_name)) >= 0
                for field_name in (
                    "diff_guitar", "diff_guitarghl", "diff_bass", "diff_drums",
                    "diff_vocals", "diff_keys", "diff_band",
                )
            )
        return (
            int(five_fret),
            int(playable),
            int(bool(candidate.supports_download)),
            int(not candidate.modchart),
            int(not candidate.has_issues),
            int(candidate.instrument_count),
        )

    def _fuzzy_aliases(
        self,
        query: str,
        choices: tuple[str, ...],
        cache: dict[tuple[str, int], tuple[str, ...]],
        limit: int,
    ) -> tuple[str, ...]:
        cache_key = (query, max(0, int(limit)))
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        if not query or not choices or limit <= 0:
            result: tuple[str, ...] = ()
        else:
            result = tuple(
                str(match[0])
                for match in process.extract(
                    query,
                    choices,
                    scorer=fuzz.ratio,
                    limit=limit,
                    score_cutoff=45,
                )
            )
        cache[cache_key] = result
        return result

    def high_recall_candidates(
        self,
        artist: str,
        title: str,
        *,
        alternate_artists: tuple[str, ...] | list[str] = (),
        artist_fuzzy_limit: int = 20,
        title_fuzzy_limit: int = 20,
        min_artist_similarity: float = 55.0,
        min_title_similarity: float = 20.0,
        song_key_limit: int = 12,
        fuzzy_fallback_key_limit: int = 50,
        candidate_limit: int = 100,
        include_fuzzy_fallback: bool | None = None,
    ) -> CatalogLookup:
        """Return a staged, deterministic shortlist for one canonical song.

        Exact pairs and conservative aliases lead the result. Shared-token and
        fuzzy paths are bounded independently and run only after stronger
        stages. Final match thresholds remain solely in
        :func:`score_candidate`.
        """
        if not self.available:
            return CatalogLookup()

        song_key_limit = max(1, int(song_key_limit))
        fuzzy_fallback_key_limit = max(1, int(fuzzy_fallback_key_limit))
        candidate_limit = max(1, int(candidate_limit))
        query_artist_values = tuple(
            dict.fromkeys(
                str(value)
                for value in (artist, *tuple(alternate_artists))
                if str(value).strip()
            )
        )
        artist_forms: list[str] = []
        for value in query_artist_values:
            for form in retrieval_artist_forms(value):
                if form not in artist_forms:
                    artist_forms.append(form)
        query_artist_forms = tuple(artist_forms)
        query_title_forms = retrieval_title_forms(title)
        strict_key = _row_key(artist, title)
        strict_keys = {strict_key} if strict_key in self._songs else set()
        key_methods: dict[tuple[str, str], set[str]] = defaultdict(set)

        artist_keys: set[tuple[str, str]] = set()
        for form in query_artist_forms:
            artist_keys.update(self._artist_aliases.get(form, ()))
        title_keys: set[tuple[str, str]] = set()
        for form in query_title_forms:
            title_keys.update(self._title_aliases.get(form, ()))
        token_keys: set[tuple[str, str]] = set()
        for token in retrieval_title_tokens(title):
            token_keys.update(self._title_tokens.get(token, ()))

        selected_rank: dict[tuple[str, str], tuple[Any, ...]] = {}

        def ranked(keys: set[tuple[str, str]], *, primary: str) -> list[tuple[str, str]]:
            def scores(key: tuple[str, str]) -> tuple[float, float]:
                artist_score = self._artist_plain_similarity(query_artist_forms, key)
                title_score = self._title_similarity(query_title_forms, key)
                return artist_score, title_score

            return sorted(
                keys,
                key=lambda key: (
                    -scores(key)[1 if primary == "title" else 0],
                    -scores(key)[0 if primary == "title" else 1],
                    key,
                ),
            )

        def add_stage(
            keys: set[tuple[str, str]], method: str, stage: int, limit: int
        ) -> None:
            for key in ranked(keys - set(selected_rank), primary="artist" if method == "title" else "title")[:limit]:
                artist_score = self._artist_plain_similarity(query_artist_forms, key)
                title_score = self._title_similarity(query_title_forms, key)
                selected_rank[key] = (stage, -title_score, -artist_score, key)
                key_methods[key].add(method)

        # A. Strict normalized artist/title.
        add_stage(strict_keys, "exact", 0, 1)

        # B. Exact/alias artist plus exact or annotation-light title.
        add_stage(artist_keys & title_keys, "artist", 1, song_key_limit)

        # C. Exact/alias artist with shared title tokens or a bounded plausible
        # title typo. A similarity around 20 is never enough by itself.
        title_floor = max(45.0, float(min_title_similarity))
        artist_pool = artist_keys - set(selected_rank)
        same_artist_plausible = {
            key
            for key in artist_pool
            if key in token_keys
            or self._title_similarity(query_title_forms, key) >= title_floor
        }
        stage_c = set(ranked(same_artist_plausible, primary="title")[:song_key_limit])
        for key in stage_c:
            method = "title_token" if key in token_keys else "title_fuzzy"
            artist_score = self._artist_plain_similarity(query_artist_forms, key)
            title_score = self._title_similarity(query_title_forms, key)
            selected_rank[key] = (2, -title_score, -artist_score, key)
            key_methods[key].add(method)

        # D. Exact/annotation-light title with the closest bounded artist
        # aliases. Plain similarity prevents token-set inflation here.
        if not any(rank[0] <= 1 for rank in selected_rank.values()):
            exact_title_artist = {
                key
                for key in title_keys - set(selected_rank)
                if self._artist_plain_similarity(query_artist_forms, key)
                >= float(min_artist_similarity)
            }
            add_stage(exact_title_artist, "title", 3, song_key_limit)

        # E. Fuzzy fallback runs last and is always bounded across both axes.
        # It can recover an alternate credit even when a normalized collision
        # produced a weaker strict-key candidate.
        fuzzy_fallback_attempted = bool(
            include_fuzzy_fallback is True
            or (
                include_fuzzy_fallback is None
                and not any(rank[0] <= 1 for rank in selected_rank.values())
            )
        )
        if fuzzy_fallback_attempted and (
            artist_fuzzy_limit > 0 or title_fuzzy_limit > 0
        ):
            fuzzy_artist_aliases: set[str] = set()
            for form in query_artist_forms:
                fuzzy_artist_aliases.update(self._fuzzy_aliases(
                    form, self._artist_choices, self._artist_fuzzy_cache,
                    artist_fuzzy_limit,
                ))
            fuzzy_title_aliases: set[str] = set()
            for form in query_title_forms:
                fuzzy_title_aliases.update(self._fuzzy_aliases(
                    form, self._title_choices, self._title_fuzzy_cache,
                    title_fuzzy_limit,
                ))
            fuzzy_artist_keys = {
                key
                for alias in fuzzy_artist_aliases
                for key in self._artist_aliases.get(alias, ())
            }
            fuzzy_title_keys = {
                key
                for alias in fuzzy_title_aliases
                for key in self._title_aliases.get(alias, ())
            }
            fuzzy_artist_fallback = {
                key
                for key in fuzzy_artist_keys
                if self._title_similarity(query_title_forms, key) >= title_floor
            }
            fuzzy_title_fallback = {
                key
                for key in fuzzy_title_keys
                if self._artist_plain_similarity(query_artist_forms, key)
                >= float(min_artist_similarity)
            }
            def fallback_rank(key: tuple[str, str]) -> tuple[Any, ...]:
                artist_score = self._artist_plain_similarity(query_artist_forms, key)
                title_score = self._title_similarity(query_title_forms, key)
                return (
                    -(title_score * 0.55 + artist_score * 0.35),
                    -title_score,
                    -artist_score,
                    key,
                )

            artist_fallback = set(sorted(
                fuzzy_artist_fallback - set(selected_rank),
                key=fallback_rank,
            )[:fuzzy_fallback_key_limit])
            title_fallback = set(sorted(
                fuzzy_title_fallback - set(selected_rank),
                key=lambda key: (
                    -self._artist_plain_similarity(query_artist_forms, key),
                    -self._title_similarity(query_title_forms, key),
                    key,
                ),
            )[:min(12, fuzzy_fallback_key_limit)])
            fallback = artist_fallback | title_fallback
            for key in fallback:
                artist_score = self._artist_plain_similarity(query_artist_forms, key)
                title_score = self._title_similarity(query_title_forms, key)
                selected_rank[key] = (
                    4,
                    -(title_score * 0.55 + artist_score * 0.35),
                    -title_score,
                    -artist_score,
                    key,
                )
                if key in fuzzy_artist_fallback:
                    key_methods[key].add("artist_fuzzy")
                if key in fuzzy_title_fallback:
                    key_methods[key].add("title_fuzzy")

        candidate_records: dict[
            tuple[str, int], tuple[tuple[Any, ...], ChartCandidate]
        ] = {}
        candidate_method_sets: dict[tuple[str, int], set[str]] = defaultdict(set)
        for key, key_rank in sorted(selected_rank.items(), key=lambda item: item[1]):
            for candidate in self._songs.get(key, ()):
                if self._artist_symbol_conflict(query_artist_values, candidate.artist):
                    continue
                identity = (str(candidate.source), int(candidate.chart_id))
                priority = self._candidate_shortlist_priority(candidate)
                rank = (
                    *key_rank,
                    *(-value for value in priority),
                    str(candidate.source).casefold(),
                    int(candidate.chart_id),
                )
                existing = candidate_records.get(identity)
                if existing is None or rank < existing[0]:
                    candidate_records[identity] = (rank, candidate)
                candidate_method_sets[identity].update(key_methods.get(key, ()))

        ordered_records = sorted(candidate_records.items(), key=lambda item: item[1][0])
        ordered_records = ordered_records[:candidate_limit]
        candidates = [record[1][1] for record in ordered_records]
        candidate_methods = {
            identity: tuple(sorted(candidate_method_sets[identity]))
            for identity, _ in ordered_records
        }
        returned_methods = tuple(dict.fromkeys(
            method
            for identity, _ in ordered_records
            for method in candidate_methods[identity]
        ))
        strict_exact = any("exact" in candidate_methods[identity] for identity, _ in ordered_records)
        return CatalogLookup(
            candidates=tuple(candidates),
            strict_exact=strict_exact,
            methods=returned_methods,
            retrieved_song_keys=len(selected_rank),
            exact_artist=bool(artist_keys),
            exact_title=bool(title_keys),
            candidate_methods=candidate_methods,
            fuzzy_fallback_attempted=fuzzy_fallback_attempted,
        )

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                header_line = stream.readline(MAX_CATALOG_HEADER_BYTES + 1)
                if len(header_line) > MAX_CATALOG_HEADER_BYTES:
                    raise CatalogLoadError("catalog header is too large")
                raw = header_line + stream.read()
            lines = raw.splitlines()
            if not lines:
                raise CatalogLoadError("catalog is empty")
            header = json.loads(lines[0])
            if not isinstance(header, Mapping):
                raise CatalogLoadError("catalog header is not an object")
            if header.get("type") != "enchor-catalog" or header.get("format_version") != FORMAT_VERSION:
                raise CatalogLoadError("unsupported catalog format")
            if header.get("complete") is not True:
                raise CatalogLoadError("catalog snapshot is incomplete")
            expected = str(header.get("sha256") or "")
            actual = hashlib.sha256(b"\n".join(lines[1:]) + (b"\n" if len(lines) > 1 else b"")).hexdigest()
            if expected != actual:
                raise CatalogLoadError("catalog checksum mismatch")
            for raw_line in lines[1:]:
                row = json.loads(raw_line)
                if not isinstance(row, Mapping) or row.get("type") != "chart":
                    raise CatalogLoadError("malformed catalog row")
                candidate_data = row.get("candidate")
                if not isinstance(candidate_data, Mapping):
                    raise CatalogLoadError("catalog row has no candidate object")
                candidate = ChartCandidate(**{k: v for k, v in candidate_data.items() if k in ChartCandidate.__dataclass_fields__})
                candidate._catalog_row_hash = hashlib.sha256(  # type: ignore[attr-defined]
                    json.dumps(
                        dict(candidate_data), sort_keys=True, separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                if not candidate.chart_id:
                    raise CatalogLoadError("catalog row has no chart id")
                identity = (str(candidate.source), int(candidate.chart_id))
                if identity in self._known_chart_identities:
                    raise CatalogLoadError("catalog contains duplicate chart identity")
                self._known_chart_identities.add(identity)
                if identity in self.excluded_identities:
                    continue
                song_key = _row_key(candidate.artist, candidate.name)
                self._artists.add(song_key[0])
                self._songs[song_key].append(candidate)
                self._artist_forms_by_key[song_key] = retrieval_artist_forms(candidate.artist)
                self._title_forms_by_key[song_key] = retrieval_title_forms(candidate.name)
                self._songs_by_artist[song_key[0]].add(song_key)
                self._chart_identities[identity] = candidate
                for alias in retrieval_artist_forms(candidate.artist):
                    self._artist_aliases[alias].add(song_key)
                for alias in retrieval_title_forms(candidate.name):
                    self._title_aliases[alias].add(song_key)
                for token in retrieval_title_tokens(candidate.name):
                    self._title_tokens[token].add(song_key)
                for token in retrieval_artist_tokens(candidate.artist):
                    self._artist_tokens[token].add(song_key)
            self._artist_choices = tuple(sorted(self._artist_aliases))
            self._title_choices = tuple(sorted(self._title_aliases))
            for header_key in ("chart_count", "out_of"):
                reported = header.get(header_key)
                if reported is not None:
                    try:
                        reported = int(reported)
                    except (TypeError, ValueError) as exc:
                        raise CatalogLoadError(f"catalog header has invalid {header_key}") from exc
                    if reported != self.known_chart_count:
                        raise CatalogLoadError(
                            f"catalog {header_key} does not match chart rows"
                        )
            self.snapshot_id = str(header.get("snapshot_id") or expected[:16])
            self.sha256 = expected
            self.complete = True
            self.diagnostic = ""
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError, CatalogLoadError) as exc:
            self.complete = False
            self.diagnostic = f"catalog unavailable: {exc}"


@lru_cache(maxsize=1)
def _load_enchor_catalog_cached(
    path: str,
    modified_ns: int,
    size: int,
    header_fingerprint: str,
    excluded_identities: tuple[tuple[str, int], ...],
    validation_fingerprint: str,
) -> EnchorCatalog:
    del modified_ns, size, header_fingerprint
    return EnchorCatalog(
        path,
        excluded_identities=frozenset(excluded_identities),
        validation_fingerprint=validation_fingerprint,
    )


def load_enchor_catalog(
    path: Path | str = DEFAULT_CATALOG_PATH,
    *,
    excluded_identities: set[tuple[str, int]] | frozenset[tuple[str, int]] = frozenset(),
    validation_fingerprint: str = "",
) -> EnchorCatalog:
    """Load an immutable snapshot, refreshing after atomic same-path replacement."""
    resolved = Path(path).resolve()
    try:
        stat = resolved.stat()
        modified_ns, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        modified_ns, size = 0, 0
    blocked = tuple(sorted((str(source), int(chart_id)) for source, chart_id in excluded_identities))
    # Valid immutable snapshots declare the body checksum in their header.
    # Replacements can preserve mtime and size; fingerprint only the bounded
    # header here and retain full checksum verification on a cache miss.
    try:
        with resolved.open("rb") as stream:
            header_line = stream.readline(MAX_CATALOG_HEADER_BYTES + 1)
        if not header_line or len(header_line) > MAX_CATALOG_HEADER_BYTES:
            raise CatalogLoadError("catalog header is unavailable")
    except (OSError, CatalogLoadError):
        return EnchorCatalog(
            resolved, excluded_identities=frozenset(blocked),
            validation_fingerprint=str(validation_fingerprint or ""),
        )
    header_fingerprint = hashlib.sha256(header_line).hexdigest()
    return _load_enchor_catalog_cached(
        str(resolved), modified_ns, size, header_fingerprint,
        blocked, str(validation_fingerprint or "")
    )


load_enchor_catalog.cache_clear = _load_enchor_catalog_cached.cache_clear  # type: ignore[attr-defined]
load_enchor_catalog.cache_info = _load_enchor_catalog_cached.cache_info  # type: ignore[attr-defined]


def read_catalog_manifest(path: Path | str = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    """Read only the immutable catalog header for cheap identity checks."""
    catalog_path = Path(path)
    with catalog_path.open("rb") as stream:
        header = json.loads(stream.readline())
    if not isinstance(header, Mapping) or header.get("type") != "enchor-catalog" or header.get("complete") is not True:
        raise CatalogLoadError("catalog manifest is incomplete or invalid")
    return {"snapshot_id": str(header.get("snapshot_id") or header.get("sha256", "")[:16]), "sha256": str(header.get("sha256") or ""), "chart_count": int(header.get("chart_count") or header.get("out_of") or 0), "format_version": header.get("format_version")}


def export_catalog(
    source: Any,
    destination: Path | str,
    *,
    checkpoint: Path | str | None = None,
    page_size: int = 250,
) -> dict[str, Any]:
    """Crawl broad Enchor search once, resuming from an external checkpoint.

    ``source`` is an Enchor adapter; this intentionally uses its throttled
    ``_post`` and ``_parse_chart`` methods but does not use the normal result cap.
    """
    destination = Path(destination)
    checkpoint_path = Path(checkpoint or str(destination) + ".checkpoint.json")
    partial_path = Path(str(destination) + ".partial")
    state = {"page": 1, "total": None, "ids": []}
    if checkpoint_path.exists():
        state.update(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    seen = set(int(value) for value in state.get("ids", []))
    rows: dict[int, dict[str, Any]] = {}
    if destination.exists():
        for line in destination.read_text(encoding="utf-8").splitlines():
            if line:
                row = json.loads(line)
                if row.get("type") == "chart":
                    rows[int(row["candidate"]["chart_id"])] = row
    if partial_path.exists():
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            if line:
                row = json.loads(line)
                if row.get("type") == "chart":
                    rows[int(row["candidate"]["chart_id"])] = row
    page = max(1, int(state.get("page", 1)))
    total = state.get("total")
    while total is None or len(seen) < int(total):
        payload = {"search": "", "per_page": min(250, max(1, page_size)), "page": page,
                   "instrument": None, "difficulty": None, "drumType": None,
                   "drumsReviewed": False, "sort": None, "source": "bridge"}
        response = None
        for attempt in range(8):
            try:
                response = source._post("/search", payload)
                break
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in (500, 503) and attempt < 7:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        if response is None:
            raise CatalogLoadError("Enchor request failed")
        data = response.get("data")
        reported = source._response_total(response)
        if not isinstance(data, list) or reported is None or (total is not None and reported != total):
            raise CatalogLoadError("unstable or malformed catalog page")
        total = reported
        if int(response.get("page", page)) != page or not data:
            raise CatalogLoadError("catalog page sequence is incomplete")
        page_rows: list[dict[str, Any]] = []
        for item in data:
            candidate = source._parse_chart(item)
            if candidate.chart_id:
                seen.add(int(candidate.chart_id))
                row = {"type": "chart", "candidate": asdict(candidate)}
                rows[int(candidate.chart_id)] = row
                page_rows.append(row)
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        with partial_path.open("a", encoding="utf-8", newline="\n") as stream:
            for row in page_rows:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_data = json.dumps({"page": page + 1, "total": total, "ids": sorted(seen)})
        fd, temporary = tempfile.mkstemp(prefix=checkpoint_path.name + ".", dir=checkpoint_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(checkpoint_data)
            _atomic_replace(temporary, checkpoint_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        page += 1
    if len(seen) != int(total):
        raise CatalogLoadError("catalog final count does not match reported total")
    body = "".join(json.dumps(rows[key], sort_keys=True, separators=(",", ":")) + "\n" for key in sorted(rows))
    checksum = hashlib.sha256(body.encode()).hexdigest()
    header = json.dumps({"type": "enchor-catalog", "format_version": FORMAT_VERSION,
                         "complete": True, "snapshot_id": str(int(time.time())),
                         "chart_count": len(rows), "out_of": int(total), "sha256": checksum},
                        sort_keys=True, separators=(",", ":")) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(header)
            stream.write(body)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    _atomic_replace(temporary, destination)
    partial_path.unlink(missing_ok=True)
    checkpoint_path.unlink(missing_ok=True)
    return {"complete": True, "chart_count": len(rows), "out_of": int(total), "path": str(destination)}


def _atomic_replace(source: Path, destination: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))
