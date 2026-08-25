"""Chorus Encore (Enchor) chart source adapter.

Verified behavior (checked against the live service and the official Bridge
desktop client's source, https://github.com/Geomitron/Bridge):

* Search:  POST https://api.enchor.us/search
           JSON body {search, per_page, page, instrument, difficulty,
           drumType, drumsReviewed, sort, source}
* Advanced search: POST https://api.enchor.us/search/advanced
           text fields as {value, exact, exclude} objects
* Response: {found, out_of, page, data: [chart metadata...]}
* Download: GET https://files.enchor.us/{md5}.sng  (direct file download with
  Content-Length; the same URL the official Bridge client downloads from)

Downloads are .sng files, which Clone Hero scans directly - no extraction
required. The md5 field identifies the source chart; Enchor may re-pack the
served .sng, so it is not used to validate downloaded bytes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from csv_to_clonehero.models import ChartCandidate, DownloadInfo
from csv_to_clonehero.normalization import (
    normalize_artist,
    normalize_title,
    strip_version_noise,
)
from csv_to_clonehero.sources.base import SourceCapabilities, SourceError

log = logging.getLogger(__name__)

API_BASE = "https://api.enchor.us"
FILES_BASE = "https://files.enchor.us"
USER_AGENT = "csv-to-clonehero/0.1 (+https://github.com/ local playlist library builder)"
DEFAULT_RESULT_CAP = 250
GENERAL_PAGE_SIZE = 250
DEFAULT_GENERAL_MAX_PAGES = 20
DEFAULT_ARTIST_MAX_PAGES = 20
DEFAULT_ARTIST_PAGE_SIZE = 250
DEFAULT_ARTIST_RESULT_CAP = DEFAULT_ARTIST_MAX_PAGES * DEFAULT_ARTIST_PAGE_SIZE


def _safe_retry_after(value: Any) -> float | None:
    try:
        return min(float(value), 60.0)
    except (TypeError, ValueError):
        return None


def _looks_like_sng(path: Any) -> bool:
    """Accept both ZIP-based .sng archives and SNGPKG binary packages."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(6)
        return head[:2] == b"PK" or head == b"SNGPKG"
    except OSError:
        return False


_DIFFICULTY_ALIASES: dict[str, tuple[str, ...]] = {
    "diff_band": ("diff_band", "band", "difficulty_band"),
    "diff_guitar": ("diff_guitar", "guitar"),
    "diff_guitar_coop": ("diff_guitar_coop", "guitar_coop", "guitarcoop"),
    "diff_rhythm": ("diff_rhythm", "rhythm"),
    "diff_bass": ("diff_bass", "bass"),
    "diff_drums": ("diff_drums", "drums"),
    "diff_drums_real": ("diff_drums_real", "drums_real", "drumsReal"),
    "diff_keys": ("diff_keys", "keys"),
    "diff_guitarghl": (
        "diff_guitarghl",
        "diff_guitar_ghl",
        "guitarghl",
        "guitar_ghl",
    ),
    "diff_guitar_coop_ghl": (
        "diff_guitar_coop_ghl",
        "diff_guitarghl_coop",
        "diff_guitarghlcoop",
        "guitarcoopghl",
        "guitar_coop_ghl",
    ),
    "diff_rhythm_ghl": (
        "diff_rhythm_ghl",
        "diff_rhythmghl",
        "rhythmghl",
        "rhythm_ghl",
    ),
    "diff_bassghl": (
        "diff_bassghl",
        "diff_bass_ghl",
        "bassghl",
        "bass_ghl",
    ),
    "diff_vocals": ("diff_vocals", "vocals"),
    "diff_vocals_harm1": (
        "diff_vocals_harm1",
        "diff_vocals_harmony1",
        "diff_vocals_harm_1",
    ),
    "diff_vocals_harm2": (
        "diff_vocals_harm2",
        "diff_vocals_harmony2",
        "diff_vocals_harm_2",
    ),
    "diff_vocals_harm3": (
        "diff_vocals_harm3",
        "diff_vocals_harmony3",
        "diff_vocals_harm_3",
    ),
}

_INSTRUMENT_ALIASES = {
    "guitar_ghl": "guitarghl",
    "guitarcoop_ghl": "guitarcoopghl",
    "guitar_coop_ghl": "guitarcoopghl",
    "rhythm_ghl": "rhythmghl",
    "bass_ghl": "bassghl",
    "guitar_coop": "guitarcoop",
}

class EnchorSource:
    name = "enchor"
    supports_direct_download = True
    supports_pagination = True
    supports_result_pagination = True
    supports_result_caps = True
    result_cap = DEFAULT_RESULT_CAP
    search_result_cap = DEFAULT_RESULT_CAP
    max_results = DEFAULT_RESULT_CAP
    artist_result_cap = DEFAULT_ARTIST_RESULT_CAP
    max_artist_results = DEFAULT_ARTIST_RESULT_CAP
    max_artist_pages = DEFAULT_ARTIST_MAX_PAGES
    artist_page_size = DEFAULT_ARTIST_PAGE_SIZE
    # Enchor exposes a legitimate direct .sng URL, so it is an exportable
    # source rather than a discovery-only catalog.
    exportable = True
    supports_export = True
    discovery_only = False
    # The API's md5 identifies the chart but does not match the served .sng
    # bytes (the server re-packs files), so it cannot validate downloads.
    download_hash_verified = False

    def __init__(
        self,
        request_delay: float = 0.25,
        retries: int = 3,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        result_cap: int | None = DEFAULT_RESULT_CAP,
        artist_result_cap: int | None = DEFAULT_ARTIST_RESULT_CAP,
        max_artist_pages: int = DEFAULT_ARTIST_MAX_PAGES,
        artist_page_size: int = DEFAULT_ARTIST_PAGE_SIZE,
        search_result_cap: int | None = None,
        max_results: int | None = None,
        max_search_pages: int = DEFAULT_GENERAL_MAX_PAGES,
        head_size_enabled: bool = False,
        download_request_delay: float = 0.1,
    ) -> None:
        self.request_delay = request_delay
        self.retries = max(1, retries)
        # The file CDN (files.enchor.us) is separate from the search API and
        # tolerates far higher throughput, so downloads get their own, much
        # lighter AIMD throttle instead of inheriting the search delay.
        self.head_size_enabled = bool(head_size_enabled)
        self._download_delay = max(0.0, float(download_request_delay))
        self._download_min_delay = self._download_delay
        self._download_last = 0.0
        self._download_lock = threading.Lock()
        if search_result_cap is not None:
            result_cap = search_result_cap
        if max_results is not None:
            result_cap = max_results
        self.result_cap = self._positive_cap(result_cap, DEFAULT_RESULT_CAP)
        self.max_results = self.result_cap
        self.max_search_pages = max(
            1,
            self._as_int(max_search_pages, DEFAULT_GENERAL_MAX_PAGES),
        )
        # The advanced endpoint accepts at most 250 rows per request.  Keep
        # the existing 20-page/5,000-chart artist sanity bound by default.
        self.max_artist_pages = max(1, self._as_int(max_artist_pages, DEFAULT_ARTIST_MAX_PAGES))
        self.artist_page_size = min(
            GENERAL_PAGE_SIZE,
            max(1, self._as_int(artist_page_size, DEFAULT_ARTIST_PAGE_SIZE)),
        )
        default_artist_cap = self.max_artist_pages * self.artist_page_size
        self.artist_result_cap = self._positive_cap(
            artist_result_cap,
            default_artist_cap,
        )
        self.max_artist_results = self.artist_result_cap
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._last_request = 0.0
        self._throttle_lock = threading.Lock()
        # Adaptive delay (AIMD): starts at request_delay, backs off hard on
        # HTTP 429, relaxes slowly on success. Enchor rate-limits aggressively
        # and its limits vary, so a fixed delay is always wrong somewhere.
        self._min_delay = request_delay
        self._current_delay = request_delay
        self.capabilities = SourceCapabilities(
            supports_pagination=True,
            result_cap=self.result_cap,
            artist_result_cap=self.artist_result_cap,
            exportable=True,
            discovery_only=False,
            supports_direct_download=True,
        )

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _positive_cap(cls, value: Any, default: int) -> int:
        parsed = cls._as_int(value, default)
        return max(1, parsed)

    # ------------------------------------------------------------------ http
    def _throttle(self) -> None:
        # httpx.Client is thread-safe; the lock keeps the rate limit global
        # across worker threads.
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._current_delay:
                time.sleep(self._current_delay - elapsed)
            self._last_request = time.monotonic()

    def _note_success(self) -> None:
        with self._throttle_lock:
            # Additive decrease: ~20 consecutive successes shave 1s off.
            self._current_delay = max(self._min_delay, self._current_delay - 0.05)

    def _note_rate_limited(self, retry_after: float | None = None) -> None:
        with self._throttle_lock:
            floor = retry_after if retry_after else 2.0
            self._current_delay = min(max(self._current_delay * 2, floor), 30.0)
            log.info("Enchor rate limited; request delay now %.1fs", self._current_delay)

    def _post(self, path: str, payload: dict) -> dict:
        rate_limited = False

        @retry(
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            stop=stop_after_attempt(self.retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            reraise=True,
        )
        def _do() -> dict:
            nonlocal rate_limited
            self._throttle()
            response = self._client.post(f"{API_BASE}{path}", json=payload)
            # HTTP 500 is commonly deterministic for a particular query. Let
            # search_song immediately try its title-only fallback instead of
            # waiting through the generic transient retry budget.
            if response.status_code == 500:
                raise SourceError(
                    "Enchor request failed (HTTP 500)",
                    status=500,
                )
            if response.status_code == 429:
                rate_limited = True
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 60.0) if retry_after else None
                except ValueError:
                    delay = None
                self._note_rate_limited(delay)
                response.raise_for_status()  # retried by tenacity
            if response.status_code >= 500:
                response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise SourceError(
                    f"Enchor {path} returned HTTP {response.status_code}: {response.text[:200]}",
                    status=response.status_code,
                )
            self._note_success()
            return response.json()

        try:
            return _do()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            raise SourceError(
                f"Enchor request failed after {self.retries} attempts (HTTP {status})",
                status=status,
                rate_limited=rate_limited,
            ) from exc
        except httpx.TransportError as exc:
            raise SourceError(f"Enchor request failed after retries: {exc}") from exc

    # ---------------------------------------------------------------- search
    def search_song(self, artist: str, title: str) -> list[ChartCandidate]:
        # The general search endpoint occasionally 500s on long queries with
        # version-noise words; search with the noise-stripped title and fall
        # back to a title-only query if the server still errors.
        clean_title = strip_version_noise(title)
        query = f"{artist} {clean_title}".strip()
        try:
            return self._search(query)
        except SourceError as exc:
            # Only fall back for server-side query parsing failures (500) -
            # never compound rate limiting (429) with extra requests.
            if (
                exc.status == 500
                and not getattr(exc, "rate_limited", False)
                and clean_title
                and artist.strip()
            ):
                log.debug("retrying search with title only: %s", clean_title)
                return self._search(clean_title)
            raise

    @classmethod
    def _response_total(cls, data: Mapping[str, Any]) -> int | None:
        for key in ("found", "out_of", "total", "source_total"):
            if key not in data or data[key] is None:
                continue
            value = cls._as_int(data[key], -1)
            if value >= 0:
                return value
        return None

    @staticmethod
    def _chart_seen_key(chart: ChartCandidate, item: Mapping[str, Any]) -> tuple[str, Any]:
        """Return a stable dedupe key without collapsing unknown IDs together."""
        if chart.chart_id:
            return "chart_id", chart.chart_id
        if chart.md5:
            return "md5", chart.md5
        return "row", id(item)

    @staticmethod
    def _stamp_pagination(
        candidates: list[ChartCandidate],
        *,
        endpoint: str,
        query: str,
        result_cap: int,
        source_total: int | None,
        returned_count: int,
        cap_reached: bool,
        pages: int,
    ) -> list[ChartCandidate]:
        metadata = {
            "endpoint": endpoint,
            "query": query,
            "result_cap": result_cap,
            "source_total": source_total,
            "returned_count": returned_count,
            "cap_reached": cap_reached,
            "pages": pages,
            "complete": not cap_reached,
        }
        for candidate in candidates:
            candidate.source_total = source_total
            candidate.returned_count = returned_count
            candidate.cap_reached = cap_reached
            candidate.source_metadata = {
                **candidate.source_metadata,
                "pagination": metadata,
                "result_cap": result_cap,
                "source_total": source_total,
                "returned_count": returned_count,
                "cap_reached": cap_reached,
            }
        return candidates

    def _search(self, query: str, result_cap: int | None = None) -> list[ChartCandidate]:
        cap = self._positive_cap(result_cap, self.result_cap)
        per_page = min(GENERAL_PAGE_SIZE, cap)
        page = 1
        pages = 0
        source_total: int | None = None
        accumulated: list[ChartCandidate] = []
        seen_chart_ids: set[tuple[str, Any]] = set()
        cap_reached = False

        while page <= self.max_search_pages:
            payload = {
                "search": query or "*",
                "per_page": per_page,
                "page": page,
                "instrument": None,
                "difficulty": None,
                "drumType": None,
                "drumsReviewed": False,
                "sort": None,
                "source": "bridge",
            }
            data = self._post("/search", payload)
            pages += 1
            rows = data.get("data") or []
            if not isinstance(rows, list):
                rows = []
            response_total = self._response_total(data)
            if response_total is not None:
                source_total = (
                    response_total
                    if source_total is None
                    else max(source_total, response_total)
                )
            if not rows:
                break

            for item in rows:
                if not isinstance(item, Mapping):
                    continue
                chart = self._parse_chart(dict(item))
                seen_key = self._chart_seen_key(chart, item)
                if seen_key not in seen_chart_ids:
                    seen_chart_ids.add(seen_key)
                    accumulated.append(chart)

            if len(accumulated) >= cap:
                cap_reached = source_total is None or source_total > cap
                break
            if source_total is not None and source_total > 0 and len(accumulated) >= source_total:
                break
            # If the API omits its total, a short page is the only completion
            # signal available.  With a total, keep going even when a page is
            # short because deduplication may have removed overlapping rows.
            if source_total is None and len(rows) < per_page:
                break
            page += 1
        else:
            # A malformed/overlapping response must not turn a capped search
            # into an unbounded stream of duplicate requests.
            cap_reached = source_total is None or source_total > len(accumulated)

        if len(accumulated) > cap:
            accumulated = accumulated[:cap]
            cap_reached = source_total is None or source_total > cap
        return self._stamp_pagination(
            accumulated,
            endpoint="/search",
            query=query,
            result_cap=cap,
            source_total=source_total,
            returned_count=len(accumulated),
            cap_reached=cap_reached,
            pages=pages,
        )

    def search_artist(self, artist: str) -> list[ChartCandidate]:  # noqa: E501
        # Paginate through the artist catalog. The API returns `found` (total
        # hits) and `data` (this page). We stop when a page is empty or we have
        # accumulated at least `found` rows, with a hard cap to keep mistagged
        # prolific artists from running away.
        max_pages = self.max_artist_pages
        per_page = min(self.artist_page_size, self.artist_result_cap)
        result_cap = min(self.artist_result_cap, max_pages * per_page)
        page = 1
        pages = 0
        accumulated: list[ChartCandidate] = []
        seen_chart_ids: set[tuple[str, Any]] = set()
        total_found: int | None = None
        cap_reached = False
        while page <= max_pages:
            payload = {
                "per_page": per_page,
                "page": page,
                "instrument": None,
                "difficulty": None,
                "drumType": None,
                "drumsReviewed": False,
                "sort": {"type": "name", "direction": "asc"},
                "source": "bridge",
                "name": None,
                "artist": {"value": artist, "exact": False, "exclude": False},
                "album": None,
                "genre": None,
                "year": None,
                "charter": None,
            }
            data = self._post("/search/advanced", payload)
            pages += 1
            rows = data.get("data") or []
            if not isinstance(rows, list):
                rows = []
            response_total = self._response_total(data)
            if response_total is not None:
                total_found = response_total if total_found is None else max(total_found, response_total)
            if not rows:
                break
            for item in rows:
                if not isinstance(item, Mapping):
                    continue
                chart = self._parse_chart(dict(item))
                # Deduplicate by chart_id in case the API overlaps pages.
                seen_key = self._chart_seen_key(chart, item)
                if seen_key not in seen_chart_ids:
                    seen_chart_ids.add(seen_key)
                    accumulated.append(chart)
            if len(accumulated) >= result_cap:
                cap_reached = total_found is None or total_found > result_cap
                break
            if total_found is not None and total_found > 0 and len(accumulated) >= total_found:
                break
            if total_found is None and len(rows) < per_page:
                break
            page += 1
        else:
            cap_reached = True

        if len(accumulated) > result_cap:
            accumulated = accumulated[:result_cap]
            cap_reached = total_found is None or total_found > result_cap
        if cap_reached:
            log.warning(
                "Artist catalog for %r hit the %s-page/%s-chart sanity cap; "
                "some charts may be missing.",
                artist,
                max_pages,
                result_cap,
            )
        return self._stamp_pagination(
            accumulated,
            endpoint="/search/advanced",
            query=artist,
            result_cap=result_cap,
            source_total=total_found,
            returned_count=len(accumulated),
            cap_reached=cap_reached,
            pages=pages,
        )

    def get_chart_metadata(self, chart_id: int) -> ChartCandidate | None:
        # The public API has no by-id endpoint; metadata arrives with search
        # results and is cached in our database, so this is a cache concern.
        return None

    # -------------------------------------------------------------- download
    def get_download_info(self, candidate: ChartCandidate) -> DownloadInfo:
        if not candidate.md5:
            return DownloadInfo(
                url="",
                md5="",
                filename="",
                supports_direct_download=False,
                notes="Enchor did not provide a chart hash for direct download",
            )
        url = f"{FILES_BASE}/{candidate.md5}.sng"
        size = self._head_size(url) if self.head_size_enabled else None
        return DownloadInfo(
            url=url,
            md5=candidate.md5,
            size_bytes=size,
            filename=f"{candidate.md5}.sng",
            supports_direct_download=True,
            notes="The Enchor md5 identifies the chart; the service may re-pack the served .sng bytes",
        )

    def head_size(self, candidate: ChartCandidate) -> int | None:
        """Content-Length of the chart download, or None if unknown."""
        if not candidate.md5 or not self.head_size_enabled:
            return None
        return self._head_size(f"{FILES_BASE}/{candidate.md5}.sng")

    def _head_size(self, url: str) -> int | None:
        try:
            self._throttle()
            response = self._client.head(url)
            if response.status_code == 200:
                length = response.headers.get("Content-Length")
                return int(length) if length else None
        except httpx.HTTPError as exc:
            log.debug("HEAD %s failed: %s", url, exc)
        return None

    def _download_throttle(self) -> None:
        with self._download_lock:
            elapsed = time.monotonic() - self._download_last
            if elapsed < self._download_delay:
                time.sleep(self._download_delay - elapsed)
            self._download_last = time.monotonic()

    def _note_download_rate_limited(self) -> None:
        with self._download_lock:
            self._download_delay = min(max(self._download_delay * 2, 0.5), 30.0)

    def _note_download_success(self) -> None:
        with self._download_lock:
            self._download_delay = max(self._download_min_delay, self._download_delay - 0.01)

    def download_chart(self, candidate: ChartCandidate, destination: Path) -> Path:
        """Stream the .sng to ``destination`` (a file path ending in .sng)."""
        url = f"{FILES_BASE}/{candidate.md5}.sng"
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_suffix(destination.suffix + ".part")
        self._download_throttle()
        try:
            with self._client.stream("GET", url, timeout=120) as response:
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    self._note_download_rate_limited()
                    raise SourceError(
                        f"Download of {candidate.name!r} rate limited (HTTP 429)",
                        status=429,
                    )
                if response.status_code != 200:
                    raise SourceError(
                        f"Download of {candidate.name!r} failed: HTTP {response.status_code}"
                    )
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if content_type in {"text/html", "application/json", "text/plain"}:
                    raise SourceError(
                        f"Download of {candidate.name!r} returned {content_type or 'non-chart'} content instead of a chart"
                    )
                with open(part_path, "wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
            if not _looks_like_sng(part_path):
                raise SourceError(f"Download of {candidate.name!r} did not produce a valid .sng archive")
            part_path.replace(destination)  # atomic on same volume
        except httpx.HTTPError as exc:
            part_path.unlink(missing_ok=True)
            raise SourceError(f"Download of {candidate.name!r} failed: {exc}") from exc
        except SourceError:
            part_path.unlink(missing_ok=True)
            raise
        self._note_download_success()
        return destination

    def close(self) -> None:
        self._client.close()

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _first_value(item: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            value = item.get(key)
            if value is not None and value != "":
                return value
        return default

    @classmethod
    def _optional_int(cls, value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _difficulty_sources(cls, item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        sources: list[Mapping[str, Any]] = [item]
        for key in ("difficulty_fields", "difficultyFields", "difficulties"):
            value = item.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
        return sources

    @classmethod
    def _difficulty_values(cls, item: Mapping[str, Any]) -> dict[str, int]:
        sources = cls._difficulty_sources(item)
        values: dict[str, int] = {}
        for field, aliases in _DIFFICULTY_ALIASES.items():
            for source in sources:
                raw_value = cls._first_value(source, *aliases)
                value = cls._optional_int(raw_value)
                if value is not None:
                    values[field] = value
                    break

        # Preserve additional numeric difficulty keys supplied by a newer
        # Enchor payload even when ChartCandidate has no dedicated alias yet.
        for source in sources[1:]:
            for raw_key, raw_value in source.items():
                key = str(raw_key)
                normalized_key = key if key.startswith("diff_") else f"diff_{key}"
                if normalized_key in values or not normalized_key.startswith("diff_"):
                    continue
                value = cls._optional_int(raw_value)
                if value is not None:
                    values[normalized_key] = value
        return values

    @staticmethod
    def _normalise_instrument(value: Any) -> str:
        if isinstance(value, Mapping):
            value = EnchorSource._first_value(value, "instrument", "name", "type", default="")
        text = str(value or "").strip().casefold().replace("-", "_")
        if text in {"vocal", "voice"}:
            return "vocals"
        return _INSTRUMENT_ALIASES.get(text, text.replace("_", ""))

    @classmethod
    def _instruments(
        cls,
        item: Mapping[str, Any],
        notes: Mapping[str, Any],
        difficulty_values: Mapping[str, int],
    ) -> list[str]:
        values: list[str] = []
        raw_instruments = cls._first_value(item, "instruments", default=None)
        if raw_instruments is None:
            raw_instruments = notes.get("instruments")
        if isinstance(raw_instruments, Mapping):
            raw_instruments = [key for key, present in raw_instruments.items() if present]
        if not isinstance(raw_instruments, (list, tuple, set)):
            raw_instruments = [raw_instruments] if raw_instruments else []

        def add(value: Any) -> None:
            normalized = cls._normalise_instrument(value)
            if normalized and normalized not in values:
                values.append(normalized)

        for raw_instrument in raw_instruments:
            add(raw_instrument)

        difficulty_instruments = {
            "diff_guitar": "guitar",
            "diff_guitar_coop": "guitarcoop",
            "diff_rhythm": "rhythm",
            "diff_bass": "bass",
            "diff_drums": "drums",
            "diff_drums_real": "drums",
            "diff_keys": "keys",
            "diff_guitarghl": "guitarghl",
            "diff_guitar_coop_ghl": "guitarcoopghl",
            "diff_rhythm_ghl": "rhythmghl",
            "diff_bassghl": "bassghl",
            "diff_vocals": "vocals",
            "diff_vocals_harm1": "vocals",
            "diff_vocals_harm2": "vocals",
            "diff_vocals_harm3": "vocals",
        }
        for field, instrument in difficulty_instruments.items():
            if field in difficulty_values:
                add(instrument)
        if notes.get("hasVocals") or item.get("hasVocals"):
            add("vocals")
        return values

    @classmethod
    def _issue_detail(cls, category: str, issue: Any) -> tuple[str, str | None]:
        if isinstance(issue, Mapping):
            code = cls._first_value(
                issue,
                "folderIssue",
                "metadataIssue",
                "noteIssue",
                "issue",
                "type",
                "code",
                default="",
            )
            detail = cls._first_value(
                issue,
                "description",
                "detail",
                "message",
                "reason",
                "text",
                default="",
            )
            severity = cls._first_value(issue, "severity", "level", "issueSeverity", default=None)
            parts = [category]
            if code:
                parts.append(str(code))
            if detail:
                parts.append(str(detail))
            if len(parts) == 1:
                parts.append(str(issue))
            return ": ".join(parts), None if severity is None else str(severity)
        return f"{category}: {issue}", None

    @classmethod
    def _issues(
        cls,
        item: Mapping[str, Any],
        notes: Mapping[str, Any],
    ) -> tuple[list[str], str]:
        groups = (
            ("folder", cls._first_value(item, "folderIssues", "folder_issues", default=[])),
            ("metadata", cls._first_value(item, "metadataIssues", "metadata_issues", default=[])),
            (
                "notes",
                cls._first_value(
                    item,
                    "chartIssues",
                    "chart_issues",
                    "notesIssues",
                    "noteIssues",
                    default=notes.get("chartIssues") or notes.get("chart_issues") or [],
                ),
            ),
        )
        details: list[str] = []
        severities: list[str] = []
        for category, raw_issues in groups:
            if isinstance(raw_issues, Mapping):
                raw_issues = [raw_issues]
            if not isinstance(raw_issues, (list, tuple, set)):
                raw_issues = [raw_issues] if raw_issues else []
            for issue in raw_issues:
                detail, severity = cls._issue_detail(category, issue)
                details.append(detail)
                if severity:
                    severities.append(severity)

        explicit_severity = cls._first_value(
            item,
            "issueSeverity",
            "issue_severity",
            "severity",
            default=None,
        )
        if explicit_severity is not None:
            severity = str(explicit_severity).strip().casefold()
            if severity in {"true", "1"}:
                severity = "warning"
            elif severity in {"false", "0", "none", "null"}:
                severity = ""
            return [detail[:500] for detail in details[:20]], severity

        if severities:
            rank = {"info": 0, "notice": 0, "warning": 1, "warn": 1, "minor": 1,
                    "error": 2, "severe": 3, "critical": 4, "fatal": 4}
            normalized = [str(value).strip().casefold() for value in severities]
            selected = max(normalized, key=lambda value: rank.get(value, 1))
            return [detail[:500] for detail in details[:20]], selected
        if details or item.get("hasIssues"):
            return [detail[:500] for detail in details[:20]], "warning"
        return details, ""

    @classmethod
    def _review_fields(cls, item: Mapping[str, Any], notes: Mapping[str, Any]) -> tuple[str, str]:
        review = item.get("review")
        review_data = review if isinstance(review, Mapping) else {}
        status = cls._first_value(
            item,
            "reviewStatus",
            "review_status",
            "reviewedStatus",
            default=cls._first_value(review_data, "status", "reviewStatus", default=None),
        )
        if status is None:
            status = cls._first_value(notes, "reviewStatus", "review_status", default=None)
        if isinstance(status, bool):
            status = "reviewed" if status else "unreviewed"
        if status is None:
            reviewed = cls._first_value(
                item,
                "reviewed",
                "qualityReviewed",
                "notesReviewed",
                default=cls._first_value(review_data, "reviewed", default=None),
            )
            if isinstance(reviewed, bool):
                status = "reviewed" if reviewed else "unreviewed"
        review_url = cls._first_value(
            item,
            "reviewUrl",
            "review_url",
            "reviewHref",
            "reportUrl",
            default=cls._first_value(review_data, "url", "href", default=""),
        )
        return ("" if status is None else str(status), "" if review_url is None else str(review_url))

    @classmethod
    def _source_url(cls, item: Mapping[str, Any], md5: str) -> str:
        supplied = cls._first_value(
            item,
            "sourceUrl",
            "source_url",
            "chartUrl",
            "chart_url",
            "href",
            "url",
            default="",
        )
        if isinstance(supplied, str) and supplied.startswith(("http://", "https://")):
            if "enchor.us" in supplied.casefold():
                return supplied
        return f"https://enchor.us/?md5={md5}" if md5 else ""

    @staticmethod
    def _parse_chart(item: dict) -> ChartCandidate:
        notes_value = item.get("notesData")
        notes = notes_value if isinstance(notes_value, Mapping) else {}
        name = str(EnchorSource._first_value(item, "chartName", "name", default="Unknown Title"))
        artist = str(EnchorSource._first_value(item, "artist", default="Unknown Artist"))
        md5 = str(EnchorSource._first_value(item, "md5", "hash", default="") or "")
        chart_id = EnchorSource._as_int(
            EnchorSource._first_value(item, "chartId", "chart_id", "id", default=0),
            0,
        )
        difficulty_values = EnchorSource._difficulty_values(item)
        instruments = EnchorSource._instruments(item, notes, difficulty_values)
        issue_details, issue_severity = EnchorSource._issues(item, notes)
        review_status, review_url = EnchorSource._review_fields(item, notes)
        artwork_id = str(
            EnchorSource._first_value(item, "albumArtMd5", "album_art_md5", default="") or ""
        )
        artwork_url = f"{FILES_BASE}/{artwork_id}.jpg" if artwork_id else ""
        source_url = EnchorSource._source_url(item, md5)
        download_url = f"{FILES_BASE}/{md5}.sng" if md5 else ""

        difficulty_kwargs: dict[str, int | None] = {
            field: difficulty_values.get(field) for field in _DIFFICULTY_ALIASES
        }
        # These are compatibility aliases retained by ChartCandidate for
        # callers that used the older spelling variants.
        difficulty_kwargs["diff_guitar_ghl"] = difficulty_values.get("diff_guitarghl")
        difficulty_kwargs["diff_bass_ghl"] = difficulty_values.get("diff_bassghl")
        difficulty_kwargs["diff_guitarghl_coop"] = difficulty_values.get("diff_guitar_coop_ghl")
        difficulty_kwargs["diff_rhythmghl"] = difficulty_values.get("diff_rhythm_ghl")

        source_metadata = {
                "source_url": source_url,
                "download_url": download_url,
                "download_filename": f"{md5}.sng" if md5 else "",
                "download_md5": md5,
                "download": {
                    "url": download_url,
                    "md5": md5,
                    "filename": f"{md5}.sng" if md5 else "",
                    "supports_direct_download": bool(md5),
                },
                "artwork_id": artwork_id,
                "artwork_url": artwork_url,
                "difficulty_fields": dict(difficulty_values),
                "issue_details": list(issue_details),
                "issue_severity": issue_severity,
                "review_status": review_status,
                "review_url": review_url,
            }

        return ChartCandidate(
            source="enchor",
            chart_id=chart_id,
            md5=md5,
            name=name,
            artist=artist,
            name_norm=normalize_title(name),
            artist_norm=normalize_artist(artist),
            charter=str(EnchorSource._first_value(item, "charter", default="") or ""),
            album=str(EnchorSource._first_value(item, "chartAlbum", "album", default="") or ""),
            genre=str(EnchorSource._first_value(item, "chartGenre", "genre", default="") or ""),
            year=str(EnchorSource._first_value(item, "chartYear", "year", default="") or ""),
            song_length_ms=EnchorSource._optional_int(
                EnchorSource._first_value(item, "song_length", "songLength", "duration_ms", default=None)
            ),
            **difficulty_kwargs,
            instruments=instruments,
            has_video_background=bool(
                EnchorSource._first_value(item, "hasVideoBackground", "has_video_background", default=False)
            ),
            modchart=bool(item.get("modchart")),
            has_issues=bool(issue_details or item.get("hasIssues")),
            icon=str(EnchorSource._first_value(item, "icon", default="") or ""),
            pack_name=str(EnchorSource._first_value(item, "packName", "pack_name", default="") or ""),
            modified_time=str(
                EnchorSource._first_value(item, "modifiedTime", "modified_time", default="") or ""
            ),
            url=source_url,
            supports_download=bool(md5),
            source_artwork_url=artwork_url,
            source_artwork_id=artwork_id,
            issue_details=issue_details,
            issue_severity=issue_severity,
            review_status=review_status,
            review_url=review_url,
            song_id=EnchorSource._first_value(item, "songId", "song_id", default=None),
            version_id=EnchorSource._first_value(item, "versionId", "version_id", default=None),
            version_group_id=EnchorSource._first_value(
                item,
                "versionGroupId",
                "version_group_id",
                default=None,
            ),
            group_id=EnchorSource._first_value(item, "groupId", "group_id", default=None),
            source_total=EnchorSource._optional_int(
                EnchorSource._first_value(item, "sourceTotal", "source_total", default=None)
            ),
            cap_reached=bool(EnchorSource._first_value(item, "capReached", "cap_reached", default=False)),
            returned_count=EnchorSource._optional_int(
                EnchorSource._first_value(item, "returnedCount", "returned_count", default=None)
            ),
            difficulty_fields=dict(difficulty_values),
            difficulties=dict(difficulty_values),
            source_metadata=source_metadata,
            artwork_url=artwork_url,
            artwork_id=artwork_id,
            issues=list(issue_details),
        )
