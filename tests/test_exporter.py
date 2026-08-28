from __future__ import annotations

import hashlib
import io
import struct
import zipfile

from csv_to_clonehero.exporter import ExportSelection, ExportService, validate_sng
from csv_to_clonehero.models import ChartCandidate, DownloadInfo
from csv_to_clonehero.normalization import normalize_artist, normalize_title


def _chart_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Test\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n"
            "[ExpertSingle]\n{\n0 = N 0 0\n}\n",
        )
    return payload.getvalue()


def _sngpkg_bytes(
    name: str = "Test Song",
    artist: str = "Test Artist",
    album: str = "Test Album",
) -> bytes:
    """Synthesize an SNGPKG-format .sng package (the format Enchor serves)."""

    def pair(key: str, value: str) -> bytes:
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        return (
            len(key_bytes).to_bytes(4, "little")
            + key_bytes
            + len(value_bytes).to_bytes(4, "little")
            + value_bytes
        )

    metadata = b"".join(
        [
            pair("name", name),
            pair("artist", artist),
            pair("album", album),
            pair("genre", "Rock"),
            pair("year", "2024"),
            pair("song_length", "185000"),
            pair("charter", "Charter"),
        ]
    )
    chart = (
        "[Song]\n{\nResolution = 192\n}\n"
        "[ExpertSingle]\n{\n0 = N 0 0\n}\n"
    ).encode("utf-8")
    mask = bytes(range(16))
    files = [("notes.chart", chart), ("guitar.opus", b"audio")]
    index_length = sum(1 + len(name.encode("utf-8")) + 16 for name, _ in files)
    data_start = 26 + 16 + len(metadata) + 16 + index_length + 8
    index = bytearray()
    encoded: list[bytes] = []
    offset = data_start
    for name, plain in files:
        masked = bytes(
            value ^ mask[index_value % 16] ^ (index_value % 256)
            for index_value, value in enumerate(plain)
        )
        encoded.append(masked)
        name_bytes = name.encode("utf-8")
        index.extend(bytes([len(name_bytes)]))
        index.extend(name_bytes)
        index.extend(struct.pack("<QQ", len(masked), offset))
        offset += len(masked)
    return (
        b"SNGPKG"
        + struct.pack("<I", 1)
        + mask
        + struct.pack("<QQ", 8 + len(metadata), 7)
        + metadata
        + struct.pack("<QQ", 8 + len(index), len(files))
        + index
        + struct.pack("<Q", sum(map(len, encoded)))
        + b"".join(encoded)
    )


def test_export_validates_sng_and_supports_folder_then_zip(config, tmp_path):
    chart_bytes = _chart_bytes()
    md5 = hashlib.md5(chart_bytes).hexdigest()
    candidate = ChartCandidate(
        source="fake",
        chart_id=1,
        md5=md5,
        name="Test Song",
        artist="Test Artist",
        name_norm=normalize_title("Test Song"),
        artist_norm=normalize_artist("Test Artist"),
        charter="Charter",
        instruments=["guitar"],
        diff_guitar=3,
    )

    class Source:
        name = "fake"
        supports_direct_download = True
        download_hash_verified = True

        def get_download_info(self, value):
            return DownloadInfo("fake://chart", md5=value.md5, size_bytes=len(chart_bytes))

        def download_chart(self, value, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(chart_bytes)
            return destination

    selection = ExportSelection(candidate, score=95, eligibility="RECOMMENDED")
    service = ExportService(config, [Source()])
    folder_result = service.export_folder([selection], tmp_path / "Songs")
    zip_result = service.export_zip([selection], tmp_path / "charts.zip")

    assert validate_sng(folder_result.published[0].target_path)
    assert folder_result.success
    assert zip_result.success
    with zipfile.ZipFile(tmp_path / "charts.zip") as archive:
        assert any(name.endswith(".sng") for name in archive.namelist())
        assert "csv-to-clonehero-manifest.json" in archive.namelist()
    service.close()


def test_export_reports_progress_per_chart(config, tmp_path):
    files = {
        1: _chart_bytes() + b"-one",
        2: _chart_bytes() + b"-two",
    }

    def make_candidate(chart_id, name):
        content = files[chart_id]
        return ChartCandidate(
            source="fake",
            chart_id=chart_id,
            md5=hashlib.md5(content).hexdigest(),
            name=name,
            artist="Test Artist",
            name_norm=normalize_title(name),
            artist_norm=normalize_artist("Test Artist"),
            charter="Charter",
            instruments=["guitar"],
            diff_guitar=3,
        )

    class Source:
        name = "fake"
        supports_direct_download = True
        download_hash_verified = True

        def get_download_info(self, value):
            return DownloadInfo("fake://chart", md5=value.md5, size_bytes=len(files[value.chart_id]))

        def download_chart(self, value, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(files[value.chart_id])
            return destination

    selections = [
        ExportSelection(make_candidate(1, "Song One"), score=95, eligibility="RECOMMENDED"),
        ExportSelection(make_candidate(2, "Song Two"), score=95, eligibility="RECOMMENDED"),
    ]
    events: list[tuple[int, int, str]] = []
    service = ExportService(config, [Source()])
    result = service.export_folder(
        selections, tmp_path / "Songs", progress=lambda done, total, label: events.append((done, total, label))
    )
    service.close()

    assert result.success
    totals = {total for _done, total, _label in events}
    assert totals == {2}
    # Two phases: preflight size checks, then per-chart materialize, then write.
    preflight = [event for event in events if event[2] == "Checking download sizes"]
    assert preflight and preflight[0][0] == 0
    assert events[-1] == (2, 2, "Writing files")
    materialize_done = [done for done, _total, label in events if label not in {"Checking download sizes", "Writing files"}]
    assert materialize_done == [1, 2]


def test_export_continues_past_failing_chart_and_reports_partial_failures(config, tmp_path):
    files = {
        1: _chart_bytes() + b"-good",
        2: b"definitely not a zip file",
    }

    def make_candidate(chart_id, name):
        content = files[chart_id]
        return ChartCandidate(
            source="bad",
            chart_id=chart_id,
            md5=hashlib.md5(content).hexdigest(),
            name=name,
            artist="Bad Artist",
            name_norm=normalize_title(name),
            artist_norm=normalize_artist("Bad Artist"),
            charter="Charter",
            instruments=["guitar"],
            diff_guitar=3,
        )

    class Source:
        name = "bad"
        supports_direct_download = True
        download_hash_verified = True

        def get_download_info(self, value):
            return DownloadInfo("bad://chart", md5=value.md5, size_bytes=len(files[value.chart_id]))

        def download_chart(self, value, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(files[value.chart_id])
            return destination

    selections = [
        ExportSelection(make_candidate(1, "Good"), score=95, eligibility="RECOMMENDED"),
        ExportSelection(make_candidate(2, "Bad"), score=95, eligibility="RECOMMENDED"),
    ]
    from csv_to_clonehero.exporter import ExportOptions
    opts = ExportOptions(continue_on_error=True, artifact_retries=0)
    service = ExportService(config, [Source()])
    result = service.export_folder(selections, tmp_path / "Songs", options=opts)
    service.close()

    assert result.success
    assert result.published_count == 1
    assert result.failed_count == 1
    assert "selection 1" in result.errors[0]
    assert result.failed[0].result == "artifact_failed"


def test_validate_sng_accepts_sngpkg_packages(tmp_path):
    from csv_to_clonehero.exporter import parse_sngpkg_metadata, validate_sng

    package = tmp_path / "chart.sng"
    package.write_bytes(_sngpkg_bytes(name="Lost", artist="Linkin Park"))

    validation = validate_sng(package)
    assert validation.valid, validation.error

    metadata = parse_sngpkg_metadata(package.read_bytes()[:8192])
    assert metadata["name"] == "Lost"
    assert metadata["artist"] == "Linkin Park"
    assert metadata["album"] == "Test Album"
    assert metadata["song_length"] == "185000"

    # Junk that merely starts with the magic is still rejected.
    broken = tmp_path / "broken.sng"
    broken.write_bytes(b"SNGPKG" + b"\x01\x00\x00\x00" + b"garbage without metadata keys")
    assert not validate_sng(broken).valid


def test_export_publishes_sngpkg_charts(config, tmp_path):
    chart_bytes = _sngpkg_bytes(name="Believing", artist="BABYMETAL")
    candidate = ChartCandidate(
        source="enchor",
        chart_id=328338,
        md5=hashlib.md5(chart_bytes).hexdigest(),
        name="Believing",
        artist="BABYMETAL",
        name_norm=normalize_title("Believing"),
        artist_norm=normalize_artist("BABYMETAL"),
        charter="KupatMen",
        instruments=["guitar"],
        diff_guitar=3,
    )

    class Source:
        name = "enchor"
        supports_direct_download = True
        download_hash_verified = False

        def get_download_info(self, value):
            return DownloadInfo("fake://chart", md5=value.md5, size_bytes=len(chart_bytes))

        def download_chart(self, value, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(chart_bytes)
            return destination

    selection = ExportSelection(candidate, score=95, eligibility="RECOMMENDED")
    service = ExportService(config, [Source()])
    result = service.export_folder([selection], tmp_path / "Songs")
    service.close()

    assert result.success, result.errors
    assert result.published_count == 1
    published = result.published[0]
    assert published.target_path is not None
    assert published.target_path.read_bytes()[:6] == b"SNGPKG"
