"""Measured ZIP work is observable and cancellation never publishes a part."""

import io
import threading
import zipfile

import pytest

from csv_to_clonehero.exporter import ExportOptions, ExportService
from tests.conftest import valid_sng_bytes
from tests.test_bulk_build import OfflineSource, _selection


def _large_chart(name="Large"):
    buffer = io.BytesIO(valid_sng_bytes(name))
    with zipfile.ZipFile(buffer, "a") as package:
        package.writestr("song.ogg", b"audio fixture" * 300_000)
    return buffer.getvalue()


def _source(*payloads):
    choices = [_selection(f"Chart {index}", payload, chart_id=index + 1)
               for index, payload in enumerate(payloads)]
    return choices, OfflineSource({choice.candidate.md5: payload
                                  for choice, payload in zip(choices, payloads)})


def test_zip_reports_actual_source_bytes_and_current_item_before_download(config, tmp_path):
    payloads = [_large_chart(), valid_sng_bytes("Small")]
    choices, source = _source(*payloads)
    events, legacy, download_counts = [], [], []

    def detail(payload):
        events.append(dict(payload))
        download_counts.append(len(source.downloads))

    with ExportService(config, [source]) as service:
        result = service.export_zip(
            choices, tmp_path / "charts.zip", options=ExportOptions(download_workers=1),
            progress=lambda done, total, label: legacy.append((done, total, label)),
            progress_details=detail,
        )
    assert result.success, result.errors
    assert events[0]["phase"] == "validating"
    assert events[0]["current_item"] == "Chart 0.sng"
    assert download_counts[0] == 0
    packed = [event for event in events if event["phase"] == "packaging"]
    assert packed[0]["completed"] == packed[0]["bytes_completed"] == 0
    assert {event["bytes_total"] for event in packed} == {sum(map(len, payloads))}
    assert {event["files_total"] for event in packed} == {2}
    assert {event["bytes_kind"] for event in packed} == {"source"}
    assert [event["bytes_completed"] for event in packed] == sorted(event["bytes_completed"] for event in packed)
    assert any(0 < event["bytes_completed"] < len(payloads[0]) and event["files_completed"] == 0 for event in packed)
    assert packed[-1]["bytes_completed"] == sum(map(len, payloads))
    assert packed[-1]["files_completed"] == 2
    assert events[-1]["phase"] == "finalizing"
    assert legacy and all(len(item) == 3 for item in legacy)
    assert all("/" not in event["current_item"] and "\\" not in event["current_item"] for event in events)
    with zipfile.ZipFile(tmp_path / "charts.zip") as archive:
        assert archive.testzip() is None
        assert archive.read("Test Artist/Chart 0.sng") == payloads[0]


@pytest.mark.parametrize("cancel_phase", ["mid_file", "finalizing", "validated"])
def test_cancel_during_zip_work_keeps_existing_archive_and_removes_part(config, tmp_path, cancel_phase):
    choices, source = _source(_large_chart())
    event = threading.Event()
    destination = tmp_path / "charts.zip"
    destination.write_bytes(b"prior archive remains intact")
    seen = []

    def detail(payload):
        seen.append(dict(payload))
        if ((cancel_phase == "mid_file" and payload["phase"] == "packaging" and payload["bytes_completed"] > 0)
                or (cancel_phase == "finalizing" and payload["phase"] == "finalizing")
                or (cancel_phase == "validated" and payload["phase"] == "validating" and payload["completed"] == 1)):
            event.set()

    with ExportService(config, [source]) as service:
        result = service.export_zip(choices, destination, progress_details=detail, cancellation_event=event)
    assert event.is_set()
    assert result.cancelled and not result.success
    assert destination.read_bytes() == b"prior archive remains intact"
    assert not destination.with_name("charts.zip.part").exists()
    assert result.items[0].status == "cancelled"
    if cancel_phase == "mid_file":
        copied = max(item.get("bytes_completed", 0) for item in seen)
        assert 0 < copied < len(source.files[choices[0].candidate.md5])


def test_zip_progress_counts_deduplicated_artifacts_and_observer_failure_is_harmless(config, tmp_path):
    content = valid_sng_bytes("Shared")
    choices, source = _source(content, content)
    events = []

    def broken_legacy(*_args):
        raise RuntimeError("observer failed")

    with ExportService(config, [source]) as service:
        result = service.export_zip(choices, tmp_path / "charts.zip", progress=broken_legacy,
                                    progress_details=lambda payload: events.append(dict(payload)))
    assert result.success
    packed = [item for item in events if item["phase"] == "packaging"]
    assert {item["files_total"] for item in packed} == {1}
    assert packed[-1]["bytes_completed"] == packed[-1]["bytes_total"] == len(content)
    assert result.published_count == 1


def test_detail_observer_failure_never_prevents_valid_archive_publication(config, tmp_path):
    choices, source = _source(valid_sng_bytes("Observer"))

    def broken_details(_payload):
        raise RuntimeError("detail observer failed")

    with ExportService(config, [source]) as service:
        result = service.export_zip(choices, tmp_path / "charts.zip", progress_details=broken_details)
    assert result.success
    with zipfile.ZipFile(tmp_path / "charts.zip") as archive:
        assert archive.testzip() is None
