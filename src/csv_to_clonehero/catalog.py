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


class CatalogLoadError(ValueError):
    """The local snapshot cannot safely be used for negative filtering."""


class EnchorCatalog:
    """Indexed, immutable-in-process catalog snapshot."""

    def __init__(self, path: Path | str = DEFAULT_CATALOG_PATH) -> None:
        self.path = Path(path)
        self.complete = False
        self.diagnostic = "catalog unavailable"
        self.snapshot_id = ""
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
        self._chart_identities: set[tuple[str, int]] = set()
        started = time.perf_counter()
        self._load()
        self.load_seconds = time.perf_counter() - started

    @property
    def available(self) -> bool:
        return self.complete

    @property
    def chart_count(self) -> int:
        return sum(len(rows) for rows in self._songs.values())

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

    def artists(self) -> tuple[str, ...]:
        return tuple(sorted(self._artists))

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
    ) -> CatalogLookup:
        """Retrieve plausible charts without deciding whether they match.

        The exact two-dimensional index remains the fast path.  Secondary
        indexes intentionally retrieve more than exact equality allows; the
        caller must still pass every returned chart through ``score_candidate``.
        Candidate filtering here is only a conservative shortlist bound: a
        candidate must be capable of passing the scorer's hard artist conflict
        gate and have a small amount of title evidence. Exact artist/title
        aliases bypass that bound.
        """
        if not self.available:
            return CatalogLookup()

        query_artist_values = (artist, *tuple(alternate_artists))
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
        for key in strict_keys:
            key_methods[key].add("exact")

        artist_keys: set[tuple[str, str]] = set()
        for form in query_artist_forms:
            artist_keys.update(self._artist_aliases.get(form, ()))
        title_keys: set[tuple[str, str]] = set()
        for form in query_title_forms:
            title_keys.update(self._title_aliases.get(form, ()))

        # Any shared annotation-light token is a recall path.  The final
        # similarity checks below keep common words from turning into a full
        # catalog scan while leaving exact aliases untouched.
        token_keys: set[tuple[str, str]] = set()
        for token in retrieval_title_tokens(title):
            token_keys.update(self._title_tokens.get(token, ()))

        fuzzy_artist_keys: set[tuple[str, str]] = set()
        fuzzy_artist_aliases: set[str] = set()
        for form in query_artist_forms:
            fuzzy_artist_aliases.update(
                self._fuzzy_aliases(
                    form,
                    self._artist_choices,
                    self._artist_fuzzy_cache,
                    artist_fuzzy_limit,
                )
            )
        for alias in fuzzy_artist_aliases:
            fuzzy_artist_keys.update(self._artist_aliases.get(alias, ()))

        artist_token_keys: set[tuple[str, str]] = set()
        for form in query_artist_forms:
            for token in retrieval_artist_tokens(form):
                artist_token_keys.update(self._artist_tokens.get(token, ()))

        # Keep only token hits that have enough title evidence. This is still
        # retrieval, not acceptance: every selected chart is scored later.
        artist_allowed_keys = artist_keys | fuzzy_artist_keys | artist_token_keys
        token_candidate_keys = {
            key
            for key in token_keys & artist_allowed_keys
            if self._artist_similarity(query_artist_forms, key) >= min_artist_similarity
            and self._title_similarity(query_title_forms, key) >= min_title_similarity
        }

        fuzzy_title_keys: set[tuple[str, str]] = set()
        # Fuzzy title retrieval is needed when exact aliases and the indexed
        # token path produced no usable shortlist. This covers spelling and
        # formatting changes without running a fuzzy scan for every track.
        if not title_keys and not token_candidate_keys:
            fuzzy_title_aliases: set[str] = set()
            for form in query_title_forms:
                fuzzy_title_aliases.update(
                    self._fuzzy_aliases(
                        form,
                        self._title_choices,
                        self._title_fuzzy_cache,
                        title_fuzzy_limit,
                    )
                )
            for alias in fuzzy_title_aliases:
                fuzzy_title_keys.update(self._title_aliases.get(alias, ()))

        selected: set[tuple[str, str]] = set(strict_keys)
        methods: list[str] = []
        if strict_keys:
            methods.append("exact")

        # An artist alias narrows the catalog, but an artist's unrelated songs
        # are not plausible exact-song candidates. Keep exact title aliases
        # unconditionally and require a small amount of title evidence for the
        # remaining artist pool before invoking the final scorer.
        exact_artist_keys = artist_keys - strict_keys
        exact_artist_keys = {
            key
            for key in exact_artist_keys
            if key in title_keys
            or self._title_similarity(query_title_forms, key) >= min_title_similarity
        }
        if exact_artist_keys:
            selected.update(exact_artist_keys)
            for key in exact_artist_keys:
                key_methods[key].add("artist")
            methods.append("artist")

        exact_title_keys = title_keys - selected
        for key in exact_title_keys:
            if self._artist_similarity(query_artist_forms, key) >= min_artist_similarity:
                selected.add(key)
                key_methods[key].add("title")
        if exact_title_keys:
            methods.append("title")

        selected_before_broad = set(selected)
        broad_keys = (
            token_candidate_keys | fuzzy_artist_keys | fuzzy_title_keys
        ) - selected
        for key in broad_keys:
            if (
                key in token_candidate_keys
                or (
                    (key in artist_allowed_keys or key in fuzzy_title_keys)
                and self._artist_similarity(query_artist_forms, key) >= min_artist_similarity
                and self._title_similarity(query_title_forms, key) >= min_title_similarity
                )
            ):
                selected.add(key)
                if key in token_candidate_keys:
                    key_methods[key].add("title_token")
                if key in fuzzy_artist_keys:
                    key_methods[key].add("artist_fuzzy")
                if key in fuzzy_title_keys:
                    key_methods[key].add("title_fuzzy")
        if token_candidate_keys & (selected - selected_before_broad):
            methods.append("title_token")
        if fuzzy_artist_keys & (selected - selected_before_broad):
            methods.append("artist_fuzzy")
        if fuzzy_title_keys & (selected - selected_before_broad):
            methods.append("title_fuzzy")

        candidates: list[ChartCandidate] = []
        seen: set[tuple[str, int]] = set()
        candidate_methods: dict[tuple[str, int], tuple[str, ...]] = {}
        for key in sorted(selected):
            for candidate in self._songs.get(key, ()):
                identity = (str(candidate.source), int(candidate.chart_id))
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(candidate)
                candidate_methods[identity] = tuple(sorted(key_methods.get(key, ())))
        return CatalogLookup(
            candidates=tuple(candidates),
            strict_exact=bool(strict_keys),
            methods=tuple(dict.fromkeys(methods)),
            retrieved_song_keys=len(selected),
            exact_artist=bool(artist_keys),
            exact_title=bool(title_keys),
            candidate_methods=candidate_methods,
        )

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read()
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
                if not candidate.chart_id:
                    raise CatalogLoadError("catalog row has no chart id")
                identity = (str(candidate.source), int(candidate.chart_id))
                if identity in self._chart_identities:
                    raise CatalogLoadError("catalog contains duplicate chart identity")
                song_key = _row_key(candidate.artist, candidate.name)
                self._artists.add(song_key[0])
                self._songs[song_key].append(candidate)
                self._artist_forms_by_key[song_key] = retrieval_artist_forms(candidate.artist)
                self._title_forms_by_key[song_key] = retrieval_title_forms(candidate.name)
                self._songs_by_artist[song_key[0]].add(song_key)
                self._chart_identities.add(identity)
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
                    if reported != self.chart_count:
                        raise CatalogLoadError(
                            f"catalog {header_key} does not match chart rows"
                        )
            self.snapshot_id = str(header.get("snapshot_id") or expected[:16])
            self.complete = True
            self.diagnostic = ""
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError, CatalogLoadError) as exc:
            self.complete = False
            self.diagnostic = f"catalog unavailable: {exc}"


@lru_cache(maxsize=4)
def load_enchor_catalog(path: Path | str = DEFAULT_CATALOG_PATH) -> EnchorCatalog:
    """Load one immutable snapshot once per process."""
    return EnchorCatalog(Path(path).resolve())


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
