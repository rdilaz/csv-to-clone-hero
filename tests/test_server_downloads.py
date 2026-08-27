"""Tests for short-lived signed browser downloads."""

from __future__ import annotations

import time

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


@pytest.fixture
def db_workspace(config):
    from csv_to_clonehero.database import Database

    db = Database(config.database_path)
    yield db, 1  # workspace id 1 has no selections yet
    db.close()
