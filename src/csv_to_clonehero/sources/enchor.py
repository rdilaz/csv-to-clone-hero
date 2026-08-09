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
required. The md5 field is the MD5 of the .sng file and is used for optional
checksum validation.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

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
from csv_to_clonehero.sources.base import SourceError

log = logging.getLogger(__name__)

API_BASE = "https://api.enchor.us"
FILES_BASE = "https://files.enchor.us"
USER_AGENT = "csv-to-clonehero/0.1 (+https://github.com/ local playlist library builder)"


class EnchorSource:
    name = "enchor"
    supports_direct_download = True
    # The API's md5 identifies the chart but does not match the served .sng
    # bytes (the server re-packs files), so it cannot validate downloads.
    download_hash_verified = False

    def __init__(
        self,
        request_delay: float = 0.25,
        retries: int = 3,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.request_delay = request_delay
        self.retries = max(1, retries)
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
        @retry(
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            stop=stop_after_attempt(self.retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            reraise=True,
        )
        def _do() -> dict:
            self._throttle()
            response = self._client.post(f"{API_BASE}{path}", json=payload)
            if response.status_code == 429:
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
            if exc.status == 500 and clean_title and clean_title != query:
                log.debug("retrying search with title only: %s", clean_title)
                return self._search(clean_title)
            raise

    def _search(self, query: str) -> list[ChartCandidate]:
        payload = {
            "search": query or "*",
            "per_page": 20,
            "page": 1,
            "instrument": None,
            "difficulty": None,
            "drumType": None,
            "drumsReviewed": False,
            "sort": None,
            "source": "bridge",
        }
        data = self._post("/search", payload)
        return [self._parse_chart(item) for item in data.get("data", [])]

    def search_artist(self, artist: str) -> list[ChartCandidate]:  # noqa: E501
        payload = {
            "per_page": 250,
            "page": 1,
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
        return [self._parse_chart(item) for item in data.get("data", [])]

    def get_chart_metadata(self, chart_id: int) -> ChartCandidate | None:
        # The public API has no by-id endpoint; metadata arrives with search
        # results and is cached in our database, so this is a cache concern.
        return None

    # -------------------------------------------------------------- download
    def get_download_info(self, candidate: ChartCandidate) -> DownloadInfo:
        url = f"{FILES_BASE}/{candidate.md5}.sng"
        size = self._head_size(url)
        return DownloadInfo(
            url=url,
            md5=candidate.md5,
            size_bytes=size,
            filename=f"{candidate.md5}.sng",
            supports_direct_download=True,
        )

    def head_size(self, candidate: ChartCandidate) -> int | None:
        """Content-Length of the chart download, or None if unknown."""
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

    def download_chart(self, candidate: ChartCandidate, destination: Path) -> Path:
        """Stream the .sng to ``destination`` (a file path ending in .sng)."""
        url = f"{FILES_BASE}/{candidate.md5}.sng"
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._client.stream("GET", url, timeout=120) as response:
                if response.status_code != 200:
                    raise SourceError(
                        f"Download of {candidate.name!r} failed: HTTP {response.status_code}"
                    )
                with open(part_path, "wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
            part_path.replace(destination)  # atomic on same volume
        except httpx.HTTPError as exc:
            part_path.unlink(missing_ok=True)
            raise SourceError(f"Download of {candidate.name!r} failed: {exc}") from exc
        return destination

    def close(self) -> None:
        self._client.close()

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _parse_chart(item: dict) -> ChartCandidate:
        notes = item.get("notesData") or {}
        instruments = list(notes.get("instruments") or [])
        folder_issues = item.get("folderIssues") or []
        metadata_issues = item.get("metadataIssues") or []
        chart_issues = notes.get("chartIssues") or []

        def _diff(key: str) -> int | None:
            value = item.get(key)
            if value is None or value < 0:
                return None
            return value

        name = item.get("name") or "Unknown Title"
        artist = item.get("artist") or "Unknown Artist"
        md5 = item.get("md5") or ""
        chart_id = item.get("chartId") or 0

        return ChartCandidate(
            source="enchor",
            chart_id=chart_id,
            md5=md5,
            name=name,
            artist=artist,
            name_norm=normalize_title(name),
            artist_norm=normalize_artist(artist),
            charter=item.get("charter") or "",
            album=item.get("album") or "",
            genre=item.get("genre") or "",
            year=str(item.get("year") or ""),
            song_length_ms=item.get("song_length"),
            diff_guitar=_diff("diff_guitar"),
            diff_guitarghl=_diff("diff_guitarghl"),
            diff_bass=_diff("diff_bass"),
            diff_drums=_diff("diff_drums"),
            diff_vocals=_diff("diff_vocals"),
            diff_keys=_diff("diff_keys"),
            instruments=instruments,
            has_video_background=bool(item.get("hasVideoBackground")),
            modchart=bool(item.get("modchart")),
            has_issues=bool(folder_issues or metadata_issues or chart_issues),
            icon=item.get("icon") or "",
            pack_name=item.get("packName") or "",
            modified_time=item.get("modifiedTime") or "",
            url=f"https://enchor.us/?md5={md5}" if md5 else "",
            supports_download=bool(md5),
        )
