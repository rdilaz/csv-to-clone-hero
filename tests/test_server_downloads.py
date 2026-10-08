"""Tests for short-lived signed browser downloads."""

from __future__ import annotations

import hashlib
import threading
import time
import zipfile

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.server.downloads import (
    BrowserDownloads,
    DownloadError,
    _validated_selections,
    build_archive,
)


@pytest.fixture
def config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.base_dir = tmp_path
    cfg.paths.state_dir = "state"
    return cfg


@pytest.fixture
def downloads(config):
    d = BrowserDownloads(config, "test-session-secret", ttl_seconds=60, sweep_interval=0)
    yield d
    d.close()


def test_sign_and_resolve(downloads):
    archive = downloads.root / "a.zip"
    archive.write_bytes(b"PK\x03\x04fakezip")
    meta = downloads.create(7, 99, archive)
    resolved = downloads.resolve(meta["token"])
    assert resolved is not None
    assert resolved["user_id"] == 7
    assert resolved["workspace_id"] == 99


def test_resolve_rejects_tampered_token(downloads):
    archive = downloads.root / "b.zip"
    archive.write_bytes(b"data")
    meta = downloads.create(1, 2, archive)
    key, expiry, sig = meta["token"].split(".")
    tampered = f"{key}.{expiry}.{'0' * len(sig)}"
    assert downloads.resolve(tampered) is None


def test_resolve_rejects_expired(downloads, monkeypatch):
    archive = downloads.root / "c.zip"
    archive.write_bytes(b"data")
    downloads.ttl_seconds = 1
    meta = downloads.create(1, 2, archive)
    now = [time.time()]
    monkeypatch.setattr("csv_to_clonehero.server.downloads.time.time", lambda: now[0] + 9999)
    assert downloads.resolve(meta["token"]) is None


def test_stream_revokes(downloads):
    archive = downloads.root / "d.zip"
    payload = b"x" * 4096
    archive.write_bytes(payload)
    meta = downloads.create(5, 8, archive)
    data = b"".join(downloads.stream(meta["token"]))
    assert data == payload
    assert downloads.resolve(meta["token"]) is None
    assert not archive.exists()


def test_stream_invalid_raises(downloads):
    with pytest.raises(DownloadError):
        list(downloads.stream("not.a.token"))


def test_stream_claim_is_atomic_and_single_use(downloads):
    archive = downloads.root / "atomic.zip"
    payload = b"atomic payload"
    archive.write_bytes(payload)
    token = downloads.create(5, 8, archive)["token"]
    barrier = threading.Barrier(3)
    results = []

    def consume():
        barrier.wait()
        try:
            results.append(b"".join(downloads.stream(token, user_id=5)))
        except DownloadError:
            results.append("rejected")

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert results.count(payload) == 1
    assert results.count("rejected") == 1
    assert downloads.resolve(token) is None
    assert not archive.exists()


def test_wrong_owner_cannot_consume_download(downloads):
    archive = downloads.root / "owned.zip"
    archive.write_bytes(b"owned")
    token = downloads.create(5, 8, archive)["token"]

    with pytest.raises(DownloadError):
        downloads.stream(token, user_id=6)
    assert downloads.resolve(token) is not None
    assert b"".join(downloads.stream(token, user_id=5)) == b"owned"


def test_latest_download_is_owner_scoped_and_only_unconsumed(downloads):
    first = downloads.root / "first.zip"
    other = downloads.root / "other.zip"
    first.write_bytes(b"first")
    other.write_bytes(b"other")
    token = downloads.create(5, 8, first)["token"]
    downloads.create(6, 8, other)

    latest = downloads.latest_for_user_workspace(5, 8)
    assert latest is not None and latest["token"] == token
    assert "path" not in latest
    assert downloads.latest_for_user_workspace(5, 9) is None
    assert b"".join(downloads.stream(token, user_id=5)) == b"first"
    assert downloads.latest_for_user_workspace(5, 8) is None


def test_periodic_sweeper_repeats_and_close_prevents_reschedule(config):
    registry = BrowserDownloads(config, "timer-secret", sweep_interval=3600)
    first = registry._timer
    assert first is not None
    first.cancel()

    registry._sweep_timer()
    repeated = registry._timer
    assert repeated is not None and repeated is not first
    registry.close()
    assert registry._timer is None
    registry._sweep_timer()
    assert registry._timer is None


def test_sweeper_removes_crash_left_assembly_directory(downloads, monkeypatch):
    assembly = downloads.root / ".assembly-crashed"
    assembly.mkdir()
    (assembly / "artifact.sng").write_bytes(b"partial")
    old = time.time() - downloads.ttl_seconds - 120
    monkeypatch.setattr("csv_to_clonehero.server.downloads.time.time", lambda: old + downloads.ttl_seconds + 120)
    import os

    os.utime(assembly, (old, old))
    downloads._sweep(include_crash_state=True)
    assert not assembly.exists()


def test_revoke_workspace_removes_all_transient_archives(downloads):
    first = downloads.root / "workspace-a.zip"
    second = downloads.root / "workspace-b.zip"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    downloads.create(1, 10, first)
    downloads.create(1, 10, second)
    assert downloads.revoke_workspace(10) == 2
    assert not first.exists()
    assert not second.exists()


def test_build_archive_requires_selections(config, db_workspace):
    db, _workspace = db_workspace
    with pytest.raises(DownloadError):
        build_archive(config, db, _workspace, [], config.state_path / "dl")


def test_public_download_ignores_stale_expansion_selection(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import make_chart as chart_fixture

    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="exact-only")
        chart = chart_fixture("Other", "Artist", chart_id=200)
        db.upsert_workspace_candidate(
            workspace,
            None,
            chart,
            kind="expansion",
            artist_norm="artist",
            score=90,
            eligibility="EXPANSION_CONFIRMED",
        )
        db.select_workspace_candidate(
            workspace,
            None,
            chart,
            kind="expansion",
            artist_norm="artist",
        )
        with pytest.raises(DownloadError, match="No downloadable charts"):
            _validated_selections(db, workspace)
    finally:
        db.close()


def test_public_archive_includes_and_revalidates_exact_and_expansion_separately(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    exact_bytes = valid_sng_bytes("Known")
    expansion_bytes = valid_sng_bytes("More")
    exact_md5 = hashlib.md5(exact_bytes).hexdigest()
    expansion_md5 = hashlib.md5(expansion_bytes).hexdigest()
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="exact-plus-expansion")
        track = db.upsert_spotify_tracks([
            make_track("Known", "Artist", "known-track")
        ])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        generation = db.create_discovery_scan(
            workspace, scan_key="current", kind="exact", status="complete",
            metadata={"workflow": "combined_catalog"},
        )
        task = db.upsert_discovery_task(
            workspace, scan_id=generation, source="test", query_key="known", status="complete"
        )
        exact = make_chart("Known", "Artist", chart_id=210, md5=exact_md5)
        expansion = make_chart("More", "Artist", chart_id=211, md5=expansion_md5)
        db.upsert_workspace_candidate(
            workspace, track.track_key, exact, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact", task_id=task,
        )
        db.upsert_workspace_candidate(
            workspace, None, expansion, score=82,
            eligibility="ELIGIBLE_REVIEW", decision="AUTO_ACCEPT", kind="expansion",
            artist_norm="artist", metadata={"exact_generation_id": generation},
        )
        db.select_workspace_candidate(workspace, track.track_key, exact, kind="exact")
        db.select_workspace_candidate(
            workspace, None, expansion, kind="expansion", artist_norm="artist"
        )
        source = FakeSource(files={exact_md5: exact_bytes, expansion_md5: expansion_bytes})

        validated = _validated_selections(db, workspace, config=config)
        assert {row["kind"] for row in validated} == {"exact", "expansion"}
        archive, summary = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )
        assert summary["published"] == 2
        with zipfile.ZipFile(archive) as built:
            assert sum(name.endswith(".sng") for name in built.namelist()) == 2

        expansion.instruments = []
        expansion.diff_guitar = None
        expansion.difficulty_fields = {}
        db.upsert_candidate(expansion)
        remaining = _validated_selections(db, workspace, config=config)
        assert [row["kind"] for row in remaining] == ["exact"]
    finally:
        db.close()


def test_build_archive_uses_and_removes_ephemeral_artifact_cache(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Song")
    digest = hashlib.md5(content).hexdigest()
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="public-download")
        track = db.upsert_spotify_tracks([make_track("Song", "Artist", "download-track")])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        chart = make_chart("Song", "Artist", chart_id=201, md5=digest)
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace, track.track_key, chart, kind="exact")
        source = FakeSource(files={digest: content})

        archive, summary = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )

        assert archive.is_file()
        assert summary["success"] is True
        assert source.downloads == [digest]
        assert not config.artifact_cache_path.exists()
        assert all(path.suffix == ".zip" for path in archive.parent.iterdir())
        with zipfile.ZipFile(archive) as built:
            assert sum(name.endswith(".sng") for name in built.namelist()) == 1
    finally:
        db.close()


def test_build_archive_collapses_duplicate_artifact_identity(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Same Song")
    digest = hashlib.md5(content).hexdigest()
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="public-duplicate")
        tracks = db.upsert_spotify_tracks([
            make_track("Same Song", "Artist", "duplicate-one"),
            make_track("Same Song", "Artist", "duplicate-two"),
        ])
        db.replace_workspace_library_tracks(workspace, tracks)
        chart = make_chart("Same Song", "Artist", chart_id=202, md5=digest)
        for track in tracks:
            db.upsert_workspace_candidate(
                workspace, track.track_key, chart, score=99,
                eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
            )
            db.select_workspace_candidate(workspace, track.track_key, chart, kind="exact")
        source = FakeSource(files={digest: content})

        archive, summary = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )

        assert summary["published"] == 1
        assert source.downloads == [digest]
        with zipfile.ZipFile(archive) as built:
            assert sum(name.endswith(".sng") for name in built.namelist()) == 1
    finally:
        db.close()


def test_public_archive_distinguishes_represented_selections_from_identical_bytes(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Song")
    charts = [make_chart("Song", "Artist", chart_id=300 + index) for index in range(2)]
    source = FakeSource(files={chart.md5: content for chart in charts})
    # Distinct Enchor-style artifact references are not checksums of served bytes.
    source.download_hash_verified = False
    config.export.download_workers = 1
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="public-identical-bytes")
        track = db.upsert_spotify_tracks([make_track("Song", "Artist", "identical-bytes")])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        for chart in charts:
            db.upsert_workspace_candidate(
                workspace, track.track_key, chart, score=99,
                eligibility="RECOMMENDED", decision="AUTO_ACCEPT",
            )
            db.select_workspace_candidate(workspace, track.track_key, chart)
        assert len(db.list_workspace_selections(workspace)) == 2
        assert len(_validated_selections(db, workspace, config=config)) == 2

        archive, summary = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )

        assert summary == {
            "success": True, "published": 1, "failed": 0, "invalid": 0,
            "download_failures": 0, "already_present": 0, "ready": 2,
            "selected": 2, "remaining": 0, "status": "success",
            "bytes": archive.stat().st_size, "errors": [],
        }
        assert source.downloads == [chart.md5 for chart in charts]
        with zipfile.ZipFile(archive) as built:
            songs = [name for name in built.namelist() if name.endswith(".sng")]
            assert len(songs) == summary["published"]
            assert built.read(songs[0]) == content
        assert len(db.list_workspace_selections(workspace)) == 2
        assert not config.artifact_cache_path.exists()
    finally:
        db.close()


def test_public_archive_partial_success_revalidates_ledger_before_retry(config):
    from csv_to_clonehero.database import Database
    from csv_to_clonehero.sources.base import SourceError
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    class RetrySource(FakeSource):
        download_hash_verified = False

        def __init__(self, files):
            super().__init__(files=files)
            self.attempts = []
            self.failures = {312: "temporary download failure", 313: "HTTP 404"}

        def download_chart(self, candidate, destination):
            self.attempts.append(candidate.chart_id)
            if candidate.chart_id in self.failures:
                raise SourceError(self.failures[candidate.chart_id])
            return super().download_chart(candidate, destination)

    titles = ["Good", "Invalid", "Transient", "Unavailable"]
    charts = [make_chart(title, "Artist", chart_id=310 + index) for index, title in enumerate(titles)]
    source = RetrySource({chart.md5: valid_sng_bytes(chart.name) for chart in charts})
    source.files[charts[1].md5] = b"not a playable chart package"
    config.export.download_workers = 1
    config.export.artifact_retries = 0
    config.export.retry_backoff_seconds = 0
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="public-partial-retry")
        tracks = db.upsert_spotify_tracks([
            make_track(title, "Artist", f"partial-{index}")
            for index, title in enumerate(titles)
        ])
        db.replace_workspace_library_tracks(workspace, tracks)
        for track, chart in zip(tracks, charts):
            db.upsert_workspace_candidate(
                workspace, track.track_key, chart, score=99,
                eligibility="RECOMMENDED", decision="AUTO_ACCEPT",
            )
            db.select_workspace_candidate(workspace, track.track_key, chart)
        selections = [dict(row) for row in db.list_workspace_selections(workspace)]

        archive, summary = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )

        assert summary["success"] is True
        assert summary["status"] == "success"
        assert summary["selected"] == 4
        assert summary["ready"] == summary["published"] == 1
        assert summary["failed"] == 3
        assert summary["invalid"] == 1
        assert summary["download_failures"] == 2
        assert summary["remaining"] == summary["already_present"] == 0
        assert summary["bytes"] == archive.stat().st_size
        assert len(summary["errors"]) == 3
        assert any("temporary download failure" in error for error in summary["errors"])
        assert any("HTTP 404" in error for error in summary["errors"])
        assert source.attempts == [chart.chart_id for chart in charts]
        with zipfile.ZipFile(archive) as built:
            assert sum(name.endswith(".sng") for name in built.namelist()) == 1
        observations = [db.get_chart_artifact_validation(chart.source, chart.chart_id) for chart in charts]
        assert [row["state"] for row in observations] == [
            "VERIFIED", "REJECTED", "UNVERIFIED", "UNAVAILABLE",
        ]
        assert [row["reason_code"] for row in observations[1:]] == [
            "ARTIFACT_VALIDATION_FAILED", "TRANSIENT_DOWNLOAD_FAILURE", "SOURCE_UNAVAILABLE",
        ]

        source.failures.clear()
        source.files[charts[1].md5] = valid_sng_bytes("Invalid")
        assert {row["candidate_id"] for row in _validated_selections(db, workspace, config=config)} == {
            charts[0].db_id, charts[2].db_id,
        }
        retry_archive, retry = build_archive(
            config, db, workspace, [source], config.state_path / "downloads"
        )

        assert retry == {
            "success": True, "published": 2, "failed": 0, "invalid": 0,
            "download_failures": 0, "already_present": 0, "ready": 2,
            "selected": 2, "remaining": 0, "status": "success",
            "bytes": retry_archive.stat().st_size, "errors": [],
        }
        assert source.attempts == [310, 311, 312, 313, 310, 312]
        assert [db.get_chart_artifact_validation(chart.source, chart.chart_id)["state"] for chart in charts] == [
            "VERIFIED", "REJECTED", "VERIFIED", "UNAVAILABLE",
        ]
        with zipfile.ZipFile(retry_archive) as built:
            assert sum(name.endswith(".sng") for name in built.namelist()) == 2
        assert [dict(row) for row in db.list_workspace_selections(workspace)] == selections
        assert not config.artifact_cache_path.exists()
        assert all(path.suffix == ".zip" for path in retry_archive.parent.iterdir())
    finally:
        db.close()


def test_build_archive_rejects_stale_wrong_identity_selection(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Completely Different")
    digest = hashlib.md5(content).hexdigest()
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="stale-false-positive")
        track = db.upsert_spotify_tracks([
            make_track("Original Song", "Original Artist", "stale-track")
        ])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        chart = make_chart("Completely Different", "Wrong Artist", chart_id=203, md5=digest)
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=100,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(
            workspace, track.track_key, chart, kind="exact",
            eligibility="RECOMMENDED", force=True,
        )
        source = FakeSource(files={digest: content})

        with pytest.raises(DownloadError, match="No downloadable charts"):
            build_archive(config, db, workspace, [source], config.state_path / "downloads")
        assert source.downloads == []
        assert not config.artifact_cache_path.exists()
    finally:
        db.close()


def test_public_archive_rejects_selection_removed_from_current_library(config):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Removed")
    digest = hashlib.md5(content).hexdigest()
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="removed-track")
        track = db.upsert_spotify_tracks([make_track("Removed", "Artist", "removed")])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        chart = make_chart("Removed", "Artist", chart_id=204, md5=digest)
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace, track.track_key, chart, kind="exact")
        db.replace_workspace_library_tracks(workspace, [])

        with pytest.raises(DownloadError, match="No downloadable charts"):
            build_archive(
                config, db, workspace, [FakeSource(files={digest: content})],
                config.state_path / "downloads",
            )
    finally:
        db.close()


@pytest.fixture
def db_workspace(config):
    from csv_to_clonehero.database import Database

    db = Database(config.database_path)
    yield db, 1  # workspace id 1 has no selections yet
    db.close()


@pytest.mark.parametrize("reserve, from_environment", [
    (0, False), (-1, False), (2048, False), (2048, True),
])
def test_public_archive_honors_configured_disk_reserve(config, monkeypatch, reserve, from_environment):
    from csv_to_clonehero.database import Database
    from tests.conftest import FakeSource, make_chart, make_track, valid_sng_bytes

    content = valid_sng_bytes("Reserve Song")
    digest = hashlib.md5(content).hexdigest()
    if from_environment:
        from csv_to_clonehero.config import load_config

        path = config.base_dir / "config.yaml"
        path.write_text("export:\n  minimum_free_bytes: 0\n", encoding="utf-8")
        monkeypatch.setenv("EXPORT_MINIMUM_FREE_BYTES", str(reserve))
        config = load_config(path)
    else:
        config.export.minimum_free_bytes = reserve
    monkeypatch.setattr(
        "csv_to_clonehero.exporter._default_disk_usage",
        lambda _path: (100_000, 90_000, len(content) + 1024),
    )
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="disk-reserve")
        track = db.upsert_spotify_tracks([make_track("Reserve Song", "Artist", "reserve")])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        chart = make_chart("Reserve Song", "Artist", chart_id=601, md5=digest)
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace, track.track_key, chart, kind="exact")
        selections = [dict(row) for row in db.list_workspace_selections(workspace)]
        source = FakeSource(files={digest: content})
        staging = config.state_path / "downloads"
        if reserve > 0:
            with pytest.raises(DownloadError, match="free|required"):
                build_archive(config, db, workspace, [source], staging)
            assert source.downloads == []
            assert list(staging.iterdir()) == []
            observation = db.get_chart_artifact_validation(chart.source, chart.chart_id)
            assert observation is None or observation["state"] not in {"REJECTED", "UNAVAILABLE"}
        else:
            archive, summary = build_archive(config, db, workspace, [source], staging)
            assert archive.is_file()
            assert summary["success"] is True
            assert source.downloads == [digest]
            assert not list(staging.glob(".assembly-*"))
        assert [dict(row) for row in db.list_workspace_selections(workspace)] == selections
    finally:
        db.close()
