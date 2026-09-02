from __future__ import annotations

import hashlib
import io
import threading
import time
import zipfile

from csv_to_clonehero.exporter import ExportOptions, ExportSelection, ExportService
from csv_to_clonehero.database import Database
from csv_to_clonehero.filesystem import destination_path
from csv_to_clonehero.models import DownloadInfo
from csv_to_clonehero.sources.base import SourceError

from .conftest import make_chart, valid_sng_bytes


class OfflineSource:
    name = "test"
    supports_direct_download = True
    download_hash_verified = False

    def __init__(self, files: dict[str, bytes], failures=None):
        self.files = files
        self.failures = failures
        self.downloads: list[str] = []

    def get_download_info(self, candidate):
        return DownloadInfo(
            url=f"offline://{candidate.md5}",
            md5=candidate.md5,
            size_bytes=len(self.files.get(candidate.md5, b"")),
        )

    def download_chart(self, candidate, destination):
        self.downloads.append(candidate.md5)
        if self.failures is not None:
            failure = self.failures(candidate)
            if failure is not None:
                raise failure
        if candidate.md5 not in self.files:
            raise SourceError("offline chart is missing")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[candidate.md5])
        return destination

    def close(self):
        pass


def _selection(name: str, content: bytes, chart_id: int = 1, **kwargs):
    candidate = make_chart(name, "Test Artist", chart_id=chart_id, md5=hashlib.md5(content).hexdigest())
    return ExportSelection(
        candidate,
        score=95,
        eligibility="RECOMMENDED",
        target_relpath=kwargs.pop("target_relpath", f"Test Artist/{name}.sng"),
        **kwargs,
    )


def _zero_note_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Empty\n")
        archive.writestr(
            "notes.chart",
            "[Song]\n{\nResolution = 192\n}\n[ExpertSingle]\n{\n}\n",
        )
    return payload.getvalue()


def _options(**kwargs) -> ExportOptions:
    values = {
        "continue_on_error": True,
        "artifact_retries": 0,
        "retry_backoff_seconds": 0,
    }
    values.update(kwargs)
    return ExportOptions(**values)


def test_empty_selection_is_a_noop_and_does_not_create_output(config, tmp_path):
    service = ExportService(config, [])
    result = service.export_folder([], tmp_path / "Songs")
    service.close()

    assert not result.success
    assert any("no persisted charts" in error for error in result.errors)
    assert not (tmp_path / "Songs").exists()


def test_valid_single_and_multi_chart_songs_folder_build(config, tmp_path):
    first = valid_sng_bytes("First")
    second = valid_sng_bytes("Second")
    selections = [_selection("First", first), _selection("Second", second, chart_id=2)]
    source = OfflineSource({
        selections[0].candidate.md5: first,
        selections[1].candidate.md5: second,
    })
    service = ExportService(config, [source])
    result = service.export_folder(selections, tmp_path / "Songs", options=_options(download_workers=2))
    service.close()

    assert result.success
    assert result.published_count == 2
    assert result.invalid_count == 0
    assert len(list((tmp_path / "Songs").rglob("*.sng"))) == 2
    assert len(source.downloads) == 2


def test_zero_note_and_corrupt_artifacts_never_publish(config, tmp_path):
    zero = _zero_note_bytes()
    corrupt = b"not a chart package"
    zero_selection = _selection("Zero", zero, chart_id=10)
    corrupt_selection = _selection("Corrupt", corrupt, chart_id=11)
    source = OfflineSource({
        zero_selection.candidate.md5: zero,
        corrupt_selection.candidate.md5: corrupt,
    })
    service = ExportService(config, [source])
    result = service.export_folder(
        [zero_selection, corrupt_selection],
        tmp_path / "Songs",
        options=_options(),
    )
    service.close()

    assert not result.success
    assert result.invalid_count == 2
    assert result.published_count == 0
    assert not list((tmp_path / "Songs").rglob("*.sng"))
    assert all(item.status == "invalid" for item in result.items)


def test_transient_failure_retries_and_permanent_failure_isolated(config, tmp_path):
    retry_content = valid_sng_bytes("Good")
    permanent_content = valid_sng_bytes("Permanent")
    retry_selection = _selection("Retry", retry_content, chart_id=20)
    permanent_selection = _selection("Permanent", permanent_content, chart_id=21)
    attempts: dict[str, int] = {}

    def failures(candidate):
        attempts[candidate.md5] = attempts.get(candidate.md5, 0) + 1
        if candidate.chart_id == 20 and attempts[candidate.md5] == 1:
            return SourceError("temporary network failure")
        if candidate.chart_id == 21:
            return SourceError("permanent network failure")
        return None

    source = OfflineSource(
        {
            retry_selection.candidate.md5: retry_content,
            permanent_selection.candidate.md5: permanent_content,
        },
        failures=failures,
    )
    service = ExportService(config, [source])
    result = service.export_folder(
        [retry_selection, permanent_selection],
        tmp_path / "Songs",
        options=_options(artifact_retries=1),
    )
    service.close()

    assert result.success
    assert result.published_count == 1
    assert result.download_failure_count == 1
    assert attempts[retry_selection.candidate.md5] == 2
    assert attempts[permanent_selection.candidate.md5] == 2
    assert any(item.status == "failed" for item in result.items)


def test_interruption_resume_uses_cached_validated_charts(config, tmp_path):
    first = valid_sng_bytes("First")
    second = valid_sng_bytes("Second")
    selections = [_selection("First", first, chart_id=30), _selection("Second", second, chart_id=31)]
    cancel = threading.Event()
    calls = 0

    def interrupt_after_first(_candidate):
        nonlocal calls
        calls += 1
        if calls == 1:
            cancel.set()
        return None

    source = OfflineSource({
        selections[0].candidate.md5: first,
        selections[1].candidate.md5: second,
    }, failures=interrupt_after_first)
    service = ExportService(config, [source])
    interrupted = service.export_folder(
        selections,
        tmp_path / "Songs",
        options=_options(cancellation_event=cancel),
    )
    assert interrupted.cancelled
    assert interrupted.published_count == 1
    assert source.downloads == [selections[0].candidate.md5]

    cancel.clear()
    resumed = service.export_folder(
        selections,
        tmp_path / "Songs",
        options=_options(cancellation_event=cancel),
    )
    service.close()

    assert resumed.success
    assert resumed.published_count == 2
    assert source.downloads == [
        selections[0].candidate.md5,
        selections[1].candidate.md5,
    ]


def test_cancellation_stops_retry_backoff_without_publishing_partial_file(config, tmp_path):
    content = valid_sng_bytes("Cancel")
    selection = _selection("Cancel", content, chart_id=40)
    cancel = threading.Event()

    def fail_and_cancel(_candidate):
        cancel.set()
        return SourceError("rate limited")

    source = OfflineSource({selection.candidate.md5: content}, failures=fail_and_cancel)
    events = []
    service = ExportService(config, [source])
    result = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(cancellation_event=cancel, artifact_retries=3),
        item_progress=lambda item: events.append((item.status, item.result)),
    )
    service.close()

    assert result.cancelled
    assert result.items[0].status == "cancelled"
    assert source.downloads == [selection.candidate.md5]
    assert "retrying" in {status for status, _result in events}
    assert not list((tmp_path / "Songs").rglob("*.part"))
    assert not list((tmp_path / "Songs").rglob("*.sng"))


def test_already_completed_chart_is_not_redownloaded(config, tmp_path):
    content = valid_sng_bytes("Completed")
    selection = _selection("Completed", content, chart_id=50)
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    second = service.export_folder([selection], tmp_path / "Songs", options=_options())
    service.close()

    assert first.success and second.success
    assert source.downloads == [selection.candidate.md5]
    assert second.items[0].result == "already_present"
    assert second.preflight.reusable_count == 1
    assert len(list((tmp_path / "Songs").rglob("*.sng"))) == 1


def test_owned_artifact_changed_target_relocates_to_one_physical_copy(config, tmp_path):
    content = valid_sng_bytes("Relocated")
    selection = _selection(
        "Relocated", content, chart_id=51,
        selection_id=1, target_relpath="Old/Relocated.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    ownership = tuple(first.manifest.records)
    first.manifest_path.unlink()

    selection.selection_id = 999
    selection.target_relpath = "New/Relocated.sng"
    second = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership),
    )
    third = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership),
    )
    service.close()

    assert first.success and second.success and third.success
    assert source.downloads == [selection.candidate.md5]
    assert not (tmp_path / "Songs" / "Old" / "Relocated.sng").exists()
    assert (tmp_path / "Songs" / "New" / "Relocated.sng").read_bytes() == content
    assert len(list((tmp_path / "Songs").rglob("*.sng"))) == 1
    assert [record.target_relpath for record in second.manifest.records] == ["New/Relocated.sng"]
    assert [record.target_relpath for record in third.manifest.records] == ["New/Relocated.sng"]


def test_changed_owned_file_is_preserved_and_not_claimed(config, tmp_path):
    content = valid_sng_bytes("Owned original")
    changed = valid_sng_bytes("User changed")
    selection = _selection(
        "Owned original", content, chart_id=52,
        target_relpath="Old/Owned.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    ownership = tuple(first.manifest.records)
    first.manifest_path.unlink()
    old = tmp_path / "Songs" / "Old" / "Owned.sng"
    old.write_bytes(changed)

    selection.target_relpath = "New/Owned.sng"
    second = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership),
    )
    service.close()

    assert second.success
    assert old.read_bytes() == changed
    assert (tmp_path / "Songs" / "New" / "Owned.sng").read_bytes() == content
    assert [record.target_relpath for record in second.manifest.records] == ["New/Owned.sng"]


def test_identical_non_riff_file_elsewhere_is_never_removed(config, tmp_path):
    content = valid_sng_bytes("Unowned")
    selection = _selection(
        "Unowned", content, chart_id=53,
        target_relpath="Old/Owned.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    ownership = tuple(first.manifest.records)
    first.manifest_path.unlink()
    unowned = tmp_path / "Songs" / "User Library" / "Unowned.sng"
    unowned.parent.mkdir(parents=True)
    unowned.write_bytes(content)

    selection.target_relpath = "New/Owned.sng"
    second = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership),
    )
    service.close()

    assert second.success
    assert unowned.read_bytes() == content
    assert not (tmp_path / "Songs" / "Old" / "Owned.sng").exists()
    assert (tmp_path / "Songs" / "New" / "Owned.sng").is_file()


def test_cancelled_relocation_keeps_last_valid_owned_copy(config, tmp_path):
    content = valid_sng_bytes("Cancel relocation")
    selection = _selection(
        "Cancel relocation", content, chart_id=54,
        target_relpath="Old/Cancel.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    ownership = tuple(first.manifest.records)
    first.manifest_path.unlink()
    selection.target_relpath = "New/Cancel.sng"
    cancelled = threading.Event()
    cancelled.set()

    result = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership, cancellation_event=cancelled),
    )
    service.close()

    assert result.cancelled
    assert (tmp_path / "Songs" / "Old" / "Cancel.sng").read_bytes() == content
    assert not (tmp_path / "Songs" / "New" / "Cancel.sng").exists()


def test_failed_new_publication_keeps_prior_valid_owned_copy(config, tmp_path):
    content = valid_sng_bytes("Failed relocation")
    selection = _selection(
        "Failed relocation", content, chart_id=55,
        target_relpath="Old/Failed.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    first = service.export_folder([selection], tmp_path / "Songs", options=_options())
    ownership = tuple(first.manifest.records)
    first.manifest_path.unlink()
    blocked_parent = tmp_path / "Songs" / "Blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    selection.target_relpath = "Blocked/Failed.sng"

    result = service.export_folder(
        [selection],
        tmp_path / "Songs",
        options=_options(ownership_records=ownership),
    )
    service.close()

    assert not result.success
    assert (tmp_path / "Songs" / "Old" / "Failed.sng").read_bytes() == content
    assert not (blocked_parent / "Failed.sng").exists()


def test_ambiguous_audit_cases_use_candidate_metadata_for_canonical_targets(config):
    cases = [
        ("Juice WRLD", "Sexual Healing", "Sexual Healing", "Wagsii"),
        ("Justin Bieber", "Boyfriend", "Believe", "Diego'nt"),
        ("Her's", "Low Beam", "Invitation to Her's", "7777villain"),
        ("Oliver Tree (feat. Blink 182)", "Let Me Down", "Let Me Down Single", "Chainsaw Octopus"),
    ]
    for index, (artist, title, album, charter) in enumerate(cases, start=1):
        candidate = make_chart(title, artist, chart_id=550 + index, charter=charter)
        candidate.album = album
        relative = destination_path(config, candidate, "exact", track=None)
        assert relative.parts == (
            artist,
            album,
            f"{artist} - {title} [{charter}].sng",
        )


def test_duplicate_authoritative_md5_downloads_once(config, tmp_path):
    content = valid_sng_bytes("Duplicate")
    first = _selection("Duplicate A", content, chart_id=60)
    second = _selection("Duplicate B", content, chart_id=61)
    second.candidate.md5 = first.candidate.md5
    source = OfflineSource({first.candidate.md5: content})
    service = ExportService(config, [source])
    result = service.export_folder([first, second], tmp_path / "Songs", options=_options())
    service.close()

    assert result.success
    assert result.published_count == 1
    assert result.duplicate_count == 1
    assert source.downloads == [first.candidate.md5]


def test_simultaneous_builds_share_one_authoritative_artifact_download(config, tmp_path):
    content = valid_sng_bytes("Concurrent")
    first = _selection(
        "Concurrent A",
        content,
        chart_id=62,
        target_relpath="First/Concurrent.sng",
    )
    second = _selection(
        "Concurrent B",
        content,
        chart_id=63,
        target_relpath="Second/Concurrent.sng",
    )
    second.candidate.md5 = first.candidate.md5

    class SlowSource(OfflineSource):
        def __init__(self):
            super().__init__({first.candidate.md5: content})
            self.started = threading.Event()
            self.release = threading.Event()

        def download_chart(self, candidate, destination):
            if not self.started.is_set():
                self.started.set()
                assert self.release.wait(5)
            return super().download_chart(candidate, destination)

    source = SlowSource()
    results = []
    service_one = ExportService(config, [source])
    service_two = ExportService(config, [source])
    thread_one = threading.Thread(
        target=lambda: results.append(
            service_one.export_folder([first], tmp_path / "First", options=_options())
        )
    )
    thread_two = threading.Thread(
        target=lambda: results.append(
            service_two.export_folder([second], tmp_path / "Second", options=_options())
        )
    )
    thread_one.start()
    assert source.started.wait(5)
    thread_two.start()
    time.sleep(0.05)
    source.release.set()
    thread_one.join(5)
    thread_two.join(5)
    service_one.close()
    service_two.close()

    assert len(results) == 2
    assert all(result.success for result in results)
    assert source.downloads == [first.candidate.md5]


def test_zip_contains_only_validated_charts_and_streams_to_disk(config, tmp_path):
    good = valid_sng_bytes("Zip good")
    bad = b"bad"
    good_selection = _selection("Zip good", good, chart_id=70)
    bad_selection = _selection("Zip bad", bad, chart_id=71)
    source = OfflineSource({
        good_selection.candidate.md5: good,
        bad_selection.candidate.md5: bad,
    })
    service = ExportService(config, [source])
    archive_path = tmp_path / "charts.zip"
    result = service.export_zip(
        [good_selection, bad_selection],
        archive_path,
        options=_options(),
    )
    service.close()

    assert result.success
    assert result.published_count == 1
    assert result.invalid_count == 1
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.namelist()
        assert sum(name.casefold().endswith(".sng") for name in members) == 1
        assert "csv-to-clonehero-manifest.json" in members


def test_unsafe_target_path_is_rejected_before_download(config, tmp_path):
    content = valid_sng_bytes("Unsafe")
    selection = _selection(
        "Unsafe",
        content,
        chart_id=80,
        target_relpath="../outside.sng",
    )
    source = OfflineSource({selection.candidate.md5: content})
    service = ExportService(config, [source])
    result = service.export_folder([selection], tmp_path / "Songs", options=_options())
    service.close()

    assert not result.success
    assert result.items[0].status == "preflight_failed"
    assert source.downloads == []
    assert not (tmp_path / "outside.sng").exists()


def test_partial_success_summary_separates_added_invalid_and_failures(config, tmp_path):
    good = valid_sng_bytes("Good")
    invalid = b"broken"
    good_selection = _selection("Good", good, chart_id=90)
    invalid_selection = _selection("Invalid", invalid, chart_id=91)
    source = OfflineSource({
        good_selection.candidate.md5: good,
        invalid_selection.candidate.md5: invalid,
    })
    service = ExportService(config, [source])
    result = service.export_folder(
        [good_selection, invalid_selection],
        tmp_path / "Songs",
        options=_options(),
    )
    service.close()

    assert result.success
    assert result.ready_count == 1
    assert result.published_count == 1
    assert result.invalid_count == 1
    assert result.download_failure_count == 0
    assert result.remaining_count == 0


def test_interrupted_export_checkpoints_are_recovered_from_sqlite(config):
    db = Database(config.database_path)
    export_id = db.create_export_record(
        export_key="interrupted-test",
        kind="folder",
        destination=str(config.library_root),
        status="running",
    )
    db.record_export_item(
        export_id,
        {
            "source": "test",
            "chart_id": "999",
            "target_relpath": "Artist/Song.sng",
            "status": "downloading",
            "error": "",
        },
    )
    assert db.recover_running_exports() == 1
    record = db.get_export_record(export_id)
    item = db.get_export_items(export_id)[0]
    db.close()

    assert record["status"] == "interrupted"
    assert item["status"] == "interrupted"
    assert "retry" in item["error"]
