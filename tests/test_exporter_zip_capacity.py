"""Unknown chart sizes do not block safe public ZIP preparation."""

from __future__ import annotations

import json
import zipfile

import pytest

from csv_to_clonehero.exporter import ExportOptions, ExportService, MANIFEST_FILENAME
from csv_to_clonehero.models import DownloadInfo

from .conftest import valid_sng_bytes
from .test_bulk_build import OfflineSource, _selection


class UnknownSizeSource(OfflineSource):
    def get_download_info(self, candidate):
        return DownloadInfo(url=f"offline://{candidate.md5}", md5=candidate.md5)


def _batch(count=8):
    payloads = [valid_sng_bytes(f"Song {index}") for index in range(count)]
    selections = [
        _selection(f"Song {index}", payload, chart_id=index + 1)
        for index, payload in enumerate(payloads)
    ]
    source = UnknownSizeSource({
        selection.candidate.md5: payload
        for selection, payload in zip(selections, payloads)
    })
    return selections, source, sum(map(len, payloads))


def test_zip_ignores_unknown_size_estimate_but_exports_actual_under_budget(config, tmp_path):
    selections, source, actual = _batch()
    budget = actual + 1
    reserve = 512
    archive_path = tmp_path / "charts.zip"
    with ExportService(config, [source], disk_usage=lambda _path: (1_000_000, 0, 3 * actual + reserve)) as service:
        result = service.export_zip(
            selections, archive_path,
            options=ExportOptions(budget_bytes=budget, minimum_free_bytes=reserve, download_workers=2),
        )

    assert result.preflight.estimated_bytes > budget
    assert all(item.known_bytes is None for item in result.preflight.items)
    assert result.success, result.errors
    assert not result.errors
    assert result.ready_count == result.published_count == len(selections)
    assert len(source.downloads) == len(selections)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read(MANIFEST_FILENAME))
        assert len(manifest["records"]) == len(selections)


@pytest.mark.parametrize("size_from", ["planned", "source", "mixed"])
def test_zip_known_sizes_still_block_over_budget_before_download(config, tmp_path, size_from):
    selections, unknown_source, actual = _batch(2)
    source = unknown_source
    if size_from in {"planned", "mixed"}:
        selections[0].planned_bytes = actual
    if size_from == "source":
        source = OfflineSource(unknown_source.files)
    with ExportService(config, [source]) as service:
        result = service.export_zip(selections, tmp_path / "charts.zip", options=ExportOptions(budget_bytes=actual - 1))
    assert not result.success
    assert any(issue.code == "configured_budget_exceeded" for issue in result.preflight.errors)
    assert not source.downloads
    assert not (tmp_path / "charts.zip").exists()


def test_zip_unknown_sizes_still_fail_actual_contents_budget(config, tmp_path):
    selections, source, actual = _batch(2)
    archive_path = tmp_path / "charts.zip"
    with ExportService(config, [source]) as service:
        result = service.export_zip(selections, archive_path, options=ExportOptions(budget_bytes=actual - 1))
    assert result.preflight.ok
    assert not result.success
    assert len(source.downloads) == len(selections)
    assert any("actual ZIP contents" in error for error in result.errors)
    assert not archive_path.exists()


def test_zip_unknown_sizes_still_require_disk_reserve_before_download(config, tmp_path):
    selections, source, _actual = _batch(1)
    with ExportService(config, [source], disk_usage=lambda _path: (1000, 900, 100)) as service:
        result = service.export_zip(
            selections, tmp_path / "charts.zip",
            options=ExportOptions(minimum_free_bytes=101),
        )
    assert not result.success
    assert any(issue.code == "insufficient_disk_space" for issue in result.preflight.errors)
    assert not source.downloads


def test_zip_staging_rechecks_actual_bytes_and_reserve_without_retrying(config, tmp_path):
    selections, source, actual = _batch(1)
    reserve = 100

    def disk_usage(_path):
        free = reserve + actual // 2 if source.downloads else reserve + actual * 3
        return (1_000_000, 0, free)

    with ExportService(config, [source], disk_usage=disk_usage) as service:
        result = service.export_zip(
            selections, tmp_path / "charts.zip",
            options=ExportOptions(minimum_free_bytes=reserve, artifact_retries=2),
        )
    assert not result.success
    assert len(source.downloads) == 1
    assert any("disk free space" in error for error in result.errors)
    assert not (tmp_path / "charts.zip").exists()


def test_zip_packaging_rechecks_disk_reserve(config, tmp_path):
    selections, source, actual = _batch(2)
    reserve = 100
    archive_path = tmp_path / "charts.zip"

    def disk_usage(path):
        free = actual * 3 + reserve
        if path == archive_path.parent and len(source.downloads) == len(selections):
            free = actual + reserve - 1
        return (1_000_000, 0, free)

    with ExportService(config, [source], artifact_cache_dir=tmp_path / "cache", disk_usage=disk_usage) as service:
        result = service.export_zip(selections, archive_path, options=ExportOptions(minimum_free_bytes=reserve))
    assert result.preflight.ok
    assert not result.success
    assert len(source.downloads) == len(selections)
    assert any("disk free space" in error for error in result.errors)
    assert not archive_path.exists()


def test_zip_unknown_sizes_preserve_partial_download_behavior(config, tmp_path):
    selections, source, actual = _batch(2)
    source.files.pop(selections[1].candidate.md5)
    archive_path = tmp_path / "charts.zip"
    with ExportService(config, [source]) as service:
        result = service.export_zip(
            selections, archive_path,
            options=ExportOptions(budget_bytes=actual, artifact_retries=0, continue_on_error=True),
        )
    assert result.success
    assert result.published_count == result.failed_count == 1
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert sum(name.endswith(".sng") for name in archive.namelist()) == 1


def test_folder_planning_retains_fallback_estimate_budget(config, tmp_path):
    selections, source, actual = _batch(1)
    with ExportService(config, [source]) as service:
        result = service.export_folder(selections, tmp_path / "Songs", options=ExportOptions(budget_bytes=actual + 1))
    assert not result.success
    assert any(issue.code == "configured_budget_exceeded" for issue in result.preflight.errors)
    assert not source.downloads
