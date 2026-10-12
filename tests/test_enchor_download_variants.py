"""Official optional-video variants share chart identity, not package bytes."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.server.downloads import build_archive
from csv_to_clonehero.sources import build_sources
from csv_to_clonehero.sources.enchor import EnchorSource
from csv_to_clonehero.sources.base import SourceError

from .conftest import make_chart, make_track, valid_sng_bytes


@pytest.mark.parametrize("include_video", [False, True])
@pytest.mark.parametrize("declared_video", [None, False, True])
def test_info_head_and_download_share_variant_without_changing_identity(tmp_path, include_video, declared_video):
    content = valid_sng_bytes("Official playable package")
    md5 = "a" * 32  # Provider identity deliberately is not a hash of the bytes.
    candidate = SimpleNamespace(md5=md5, name="Official playable package")
    if declared_video is not None:
        candidate.has_video_background = declared_video
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, content=content, headers={"Content-Type": "application/octet-stream"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    source = EnchorSource(
        client=client, request_delay=0, download_request_delay=0,
        head_size_enabled=True, include_video_background=include_video,
        catalog_path=tmp_path / "missing.jsonl",
    )
    try:
        suffix = "_novideo" if not include_video and declared_video is True else ""
        expected = f"https://files.enchor.us/{md5}{suffix}.sng"
        info = source.get_download_info(candidate)
        assert info.url == expected
        assert info.filename == f"{md5}{suffix}.sng"
        assert info.size_bytes == source.head_size(candidate) == len(content)
        assert info.md5 == candidate.md5 == md5
        assert source.download_hash_verified is False
        target = tmp_path / "chart.sng"
        assert source.download_chart(candidate, target) == target
        assert target.read_bytes() == content
        assert requests == [("HEAD", expected), ("HEAD", expected), ("GET", expected)]
    finally:
        source.close()


def test_default_local_download_keeps_background_video_without_head_requests(tmp_path):
    content = valid_sng_bytes("Default package")
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, content=content)

    candidate = SimpleNamespace(md5="b" * 32, name="Default package", has_video_background=True)
    source = EnchorSource(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        request_delay=0, download_request_delay=0,
        catalog_path=tmp_path / "missing.jsonl",
    )
    try:
        expected = f"https://files.enchor.us/{candidate.md5}.sng"
        assert source.get_download_info(candidate).url == expected
        assert source.head_size(candidate) is None
        source.download_chart(candidate, tmp_path / "chart.sng")
        assert requests == [("GET", expected)]
    finally:
        source.close()


@pytest.mark.parametrize("optional_status", [404, 410])
@pytest.mark.parametrize("canonical_status", [200, 404, 410, 500])
def test_missing_optional_variant_retries_canonical_once(tmp_path, optional_status, canonical_status):
    content = valid_sng_bytes("Canonical fallback")
    requests = []
    candidate = SimpleNamespace(md5="c" * 32, name="Canonical fallback", has_video_background=True)
    variant_url = f"https://files.enchor.us/{candidate.md5}_novideo.sng"
    canonical_url = f"https://files.enchor.us/{candidate.md5}.sng"

    def respond(request):
        requests.append(str(request.url))
        status = optional_status if str(request.url) == variant_url else canonical_status
        return httpx.Response(status, content=content)

    source = EnchorSource(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        request_delay=0, download_request_delay=0, include_video_background=False,
        catalog_path=tmp_path / "missing.jsonl",
    )
    target = tmp_path / "chart.sng"
    try:
        if canonical_status == 200:
            source.download_chart(candidate, target)
            assert target.read_bytes() == content
        else:
            with pytest.raises(SourceError) as error:
                source.download_chart(candidate, target)
            assert error.value.status == canonical_status
            assert f"HTTP {canonical_status}" in str(error.value)
            assert not target.exists()
        assert requests == [variant_url, canonical_url]
        assert candidate.md5 == "c" * 32
        assert not target.with_suffix(".sng.part").exists()
    finally:
        source.close()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_optional_variant_errors_do_not_fallback(tmp_path, status):
    requests = []
    candidate = SimpleNamespace(md5="d" * 32, name="No fallback", has_video_background=True)
    variant_url = f"https://files.enchor.us/{candidate.md5}_novideo.sng"

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(status)

    source = EnchorSource(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        request_delay=0, download_request_delay=0, include_video_background=False,
        catalog_path=tmp_path / "missing.jsonl",
    )
    try:
        with pytest.raises(SourceError) as error:
            source.download_chart(candidate, tmp_path / "chart.sng")
        assert error.value.status == status
        assert requests == [variant_url]
    finally:
        source.close()


@pytest.mark.parametrize("include_video", [None, False, True])
def test_source_factory_forwards_optional_media_preference(config, monkeypatch, include_video):
    captured = []
    sentinel = object()

    def construct(**kwargs):
        captured.append(kwargs)
        return sentinel

    monkeypatch.setattr("csv_to_clonehero.sources.EnchorSource", construct)
    kwargs = {} if include_video is None else {"include_video_background": include_video}
    assert build_sources(config, **kwargs) == [sentinel]
    assert captured[0]["include_video_background"] is (True if include_video is None else include_video)


@pytest.mark.parametrize("optional_status", [200, 404, 410])
def test_public_zip_internal_factory_omits_optional_video(config, monkeypatch, optional_status):
    content = valid_sng_bytes("Public ZIP package")
    chart = make_chart("Public ZIP package", "Artist", chart_id=910)
    chart.source = "enchor"
    chart.has_video_background = True
    preferences = []
    requests = []

    def respond(request):
        requests.append(str(request.url))
        status = optional_status if "_novideo" in str(request.url) else 200
        return httpx.Response(status, content=content)

    def factory(_config, *, include_video_background=True):
        preferences.append(include_video_background)
        return [EnchorSource(
            client=httpx.Client(transport=httpx.MockTransport(respond)),
            request_delay=0, download_request_delay=0,
            include_video_background=include_video_background,
            catalog_path=config.base_dir / "missing.jsonl",
        )]

    monkeypatch.setattr("csv_to_clonehero.sources.build_sources", factory)
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="public-optional-video")
        track = db.upsert_spotify_tracks([make_track(chart.name, chart.artist, "public-video")])[0]
        db.replace_workspace_library_tracks(workspace, [track])
        db.upsert_workspace_candidate(
            workspace, track.track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
        db.select_workspace_candidate(workspace, track.track_key, chart, kind="exact")
        before = [dict(row) for row in db.list_workspace_selections(workspace)]
        archive, summary = build_archive(config, db, workspace, None, config.state_path / "downloads")
        assert preferences == [False]
        assert archive.is_file() and summary["published"] == 1
        expected = [f"https://files.enchor.us/{chart.md5}_novideo.sng"]
        if optional_status != 200:
            expected.append(f"https://files.enchor.us/{chart.md5}.sng")
        assert requests == expected
        observation = db.get_chart_artifact_validation(chart.source, chart.chart_id)
        assert observation["state"] == "VERIFIED"
        assert observation["artifact_ref"] == chart.md5
        assert observation["artifact_sha256"] == hashlib.sha256(content).hexdigest()
        assert observation["size_bytes"] == len(content)
        assert [dict(row) for row in db.list_workspace_selections(workspace)] == before
        assert not list(archive.parent.glob(".assembly-*"))
    finally:
        db.close()
