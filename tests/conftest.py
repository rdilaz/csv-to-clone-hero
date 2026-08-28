from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.models import ChartCandidate, Track
from csv_to_clonehero.normalization import (
    make_track_key,
    normalize_artist,
    normalize_title,
)

FIXTURES = Path(__file__).parent / "fixtures"


def valid_sng_bytes(name: str = "Song") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", f"[song]\nname = {name}\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
    return payload.getvalue()


@pytest.fixture
def config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.base_dir = tmp_path
    cfg.paths.input_dir = "input_playlists"
    cfg.paths.output_dir = "output_library"
    cfg.paths.reports_dir = "reports"
    cfg.paths.database = "state/csv_to_clonehero.db"
    return cfg


def make_track(
    title: str,
    artist: str,
    spotify_id: str = "",
    album: str = "",
) -> Track:
    from csv_to_clonehero.normalization import split_artists

    primary, featured = split_artists(artist)
    return Track(
        track_key=make_track_key(spotify_id, normalize_artist(primary), normalize_title(title)),
        title=title,
        title_norm=normalize_title(title),
        artist_full=artist,
        artist_norm=normalize_artist(artist),
        primary_artist=primary,
        primary_artist_norm=normalize_artist(primary),
        featured_artists=featured,
        album=album,
        album_norm=normalize_title(album) if album else "",
        spotify_id=spotify_id,
    )


def make_chart(
    name: str,
    artist: str,
    chart_id: int = 1,
    md5: str = "",
    instruments: list[str] | None = None,
    diff_guitar: int | None = 3,
    charter: str = "TestCharter",
    icon: str = "",
    modchart: bool = False,
) -> ChartCandidate:
    return ChartCandidate(
        source="test",
        chart_id=chart_id,
        md5=md5 or f"{chart_id:032d}",
        name=name,
        artist=artist,
        name_norm=normalize_title(name),
        artist_norm=normalize_artist(artist),
        charter=charter,
        instruments=instruments if instruments is not None else ["guitar", "bass", "drums", "vocals"],
        diff_guitar=diff_guitar,
        icon=icon,
        modchart=modchart,
    )


class FakeSource:
    """In-memory ChartSource for tests."""

    name = "test"
    supports_direct_download = True
    download_hash_verified = True

    def __init__(self, song_results=None, artist_results=None, files=None):
        self.song_results = song_results or {}
        self.artist_results = artist_results or {}
        self.files = files or {}  # md5 -> bytes
        self.downloads: list[str] = []

    def search_song(self, artist, title):
        # Real adapters stamp each chart with their own source name.
        results = list(self.song_results.get((artist, title), []))
        for chart in results:
            chart.source = self.name
        return results

    def search_artist(self, artist):
        results = list(self.artist_results.get(artist, []))
        for chart in results:
            chart.source = self.name
        return results

    def get_chart_metadata(self, chart_id):
        return None

    def get_download_info(self, candidate):
        from csv_to_clonehero.models import DownloadInfo

        data = self.files.get(candidate.md5, b"")
        return DownloadInfo(url=f"fake://{candidate.md5}", md5=candidate.md5, size_bytes=len(data))

    def head_size(self, candidate):
        data = self.files.get(candidate.md5)
        return len(data) if data is not None else None

    def download_chart(self, candidate, destination):
        from csv_to_clonehero.sources.base import SourceError

        if candidate.md5 not in self.files:
            raise SourceError("no such file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[candidate.md5])
        self.downloads.append(candidate.md5)
        return destination

    def close(self):
        pass
