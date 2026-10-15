"""The public companion is an audited download, never a local library server."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

from csv_to_clonehero.server.companion_release import (
    COMPANION_ARCHIVE, COMPANION_CHECKSUM, COMPANION_VERSION,
    is_companion_release_path,
)
from tests.test_server_web import app, client, config, spotify_client_id


@pytest.fixture
def release_dir(app, tmp_path):
    directory = tmp_path / "public-release"
    directory.mkdir()
    app.config["RIFF_COMPANION_RELEASE_DIR"] = directory
    return directory


def _publish_fixture(directory: Path, *, member="riff-companion/README.txt", symlink=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        if symlink:
            info = zipfile.ZipInfo(member)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(info, "outside")
        else:
            package.writestr(member, "Local-only companion. No catalog or audio included.\n")
    body = buffer.getvalue()
    if "\\" in member:
        # ZipInfo normalizes Windows separators while writing; preserve a
        # deliberately hostile ZIP's original names in both header records.
        body = body.replace(member.replace("\\", "/").encode(), member.encode())
    (directory / COMPANION_ARCHIVE).write_bytes(body)
    manifest = {"version": COMPANION_VERSION, "private_operator_path": "must-not-be-public",
                "artifacts": [{"filename": COMPANION_ARCHIVE,
                               "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                               "local_path": "must-not-be-public"}]}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return body, manifest


def test_missing_companion_is_public_and_honestly_unavailable(client, release_dir):
    response = client.get("/companion")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Your little" in html and "listening room." in html
    assert "Download is not available yet." in html
    assert f'href="/downloads/{COMPANION_ARCHIVE}"' not in html
    assert "64-bit CPython 3.12" in html and "45 seconds" in html
    assert "START_COMPANION.cmd" in html and "CONFIGURE_COMPANION.cmd" in html
    assert "does not install into your real song library" in html
    assert client.get(f"/downloads/{COMPANION_ARCHIVE}").status_code == 404
    assert client.get("/downloads/manifest.json").status_code == 404


def test_guest_download_checksum_and_manifest_match_verified_archive(client, release_dir):
    body, fixture = _publish_fixture(release_dir)
    response = client.get("/companion")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'href="/downloads/{COMPANION_ARCHIVE}"' in html
    assert "Requires an existing 64-bit CPython 3.12" in html
    assert "not available yet" not in html
    downloaded = client.get(f"/downloads/{COMPANION_ARCHIVE}")
    assert downloaded.status_code == 200
    assert downloaded.data == body
    assert downloaded.mimetype == "application/zip"
    assert COMPANION_ARCHIVE in downloaded.headers["Content-Disposition"]
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    expected = fixture["artifacts"][0]["sha256"]
    checksum = client.get(f"/downloads/{COMPANION_CHECKSUM}")
    assert checksum.status_code == 200
    checksum_alias = client.get("/downloads/SHA256SUMS.txt")
    assert checksum_alias.status_code == 200
    assert checksum_alias.data == checksum.data
    assert checksum.data.decode() == f"{expected}  {COMPANION_ARCHIVE}\n"
    manifest = client.get("/downloads/manifest.json")
    assert manifest.status_code == 200
    assert manifest.get_json() == {"version": COMPANION_VERSION, "artifacts": [{
        "filename": COMPANION_ARCHIVE, "sha256": expected, "size_bytes": len(body),
    }]}
    assert b"must-not-be-public" not in response.data + manifest.data
    # The browser can resume an interrupted transfer of the same immutable ZIP.
    partial = client.get(f"/downloads/{COMPANION_ARCHIVE}", headers={"Range": "bytes=0-15"})
    assert partial.status_code == 206 and partial.data == body[:16]
    repeated = client.get(f"/downloads/{COMPANION_ARCHIVE}")
    assert repeated.status_code == 200 and repeated.data == body


@pytest.mark.parametrize("failure", ["checksum", "size", "version", "malformed", "missing", "duplicate", "nonzip"])
def test_incomplete_or_changed_release_is_not_offered(client, release_dir, failure):
    body, manifest = _publish_fixture(release_dir)
    if failure == "checksum":
        manifest["artifacts"][0]["sha256"] = "0" * 64
    elif failure == "size":
        manifest["artifacts"][0]["size_bytes"] += 1
    elif failure == "version":
        manifest["version"] = "future-release"
    elif failure == "missing":
        (release_dir / COMPANION_ARCHIVE).unlink()
    elif failure == "duplicate":
        manifest["artifacts"] *= 2
    elif failure == "nonzip":
        body = b"not-a-zip"
        (release_dir / COMPANION_ARCHIVE).write_bytes(body)
        manifest["artifacts"][0].update(size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    (release_dir / "manifest.json").write_text("{" if failure == "malformed" else json.dumps(manifest))
    assert "Download is not available yet." in client.get("/companion").get_data(as_text=True)
    for name in (COMPANION_ARCHIVE, COMPANION_CHECKSUM, "manifest.json"):
        assert client.get(f"/downloads/{name}").status_code == 404


@pytest.mark.parametrize("member,symlink", [
    ("../outside.py", False), ("/absolute.py", False), ("C:/outside.py", False),
    ("dir\\outside.py", False), ("riff-companion/link", True),
])
def test_publication_rejects_unsafe_archive_paths(client, release_dir, member, symlink):
    _publish_fixture(release_dir, member=member, symlink=symlink)
    assert "Download is not available yet." in client.get("/companion").get_data(as_text=True)
    assert client.get(f"/downloads/{COMPANION_ARCHIVE}").status_code == 404


def test_only_named_artifacts_are_public_and_local_apis_are_not_mounted(app, client, release_dir):
    _publish_fixture(release_dir)
    assert is_companion_release_path("/companion")
    assert is_companion_release_path(f"/downloads/{COMPANION_ARCHIVE}")
    for path in ("/downloads/.env", "/downloads/other.zip", "/downloads/../config.yaml",
                 "/api/companion/library", "/api/companion/folder", "/api/companion/install"):
        assert not is_companion_release_path(path)
    rules = {str(rule) for rule in app.url_map.iter_rules()}
    assert not any(rule.startswith(("/api/companion", "/api/local-preview")) for rule in rules)
    assert not rules.intersection({"/api/spotify/library/link", "/api/spotify/library/clean",
                                   "/api/export/folder", "/api/folder/pick"})
    for name in (COMPANION_ARCHIVE, COMPANION_CHECKSUM, "manifest.json"):
        assert client.get(f"/static/releases/{name}").status_code == 404
    # Reading the public landing neither creates a workspace nor authenticates a guest.
    with client.session_transaction() as session:
        assert "sid" not in session and "anonymous_workspace_key" not in session


def test_release_replacement_invalidates_verification_cache(client, release_dir):
    _publish_fixture(release_dir)
    assert client.get(f"/downloads/{COMPANION_ARCHIVE}").status_code == 200
    path = release_dir / COMPANION_ARCHIVE
    path.write_bytes(path.read_bytes() + b"changed")
    assert client.get(f"/downloads/{COMPANION_ARCHIVE}").status_code == 404


@pytest.fixture
def release_public_server(tmp_path, monkeypatch):
    # Browser tooling stays optional; importing it here cannot skip API tests.
    from tests.test_public_browser import public_server
    yield from public_server.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.browser
@pytest.mark.parametrize("width", [320, 390, 1440])
def test_companion_guest_landing_download_and_mobile_layout(release_public_server, tmp_path, width):
    from tests.test_public_browser import _assert_no_overflow, _capture, playwright

    app, base_url, _, _ = release_public_server
    directory = tmp_path / "browser-release"
    directory.mkdir()
    app.config["RIFF_COMPANION_RELEASE_DIR"] = directory
    body, _ = _publish_fixture(directory)
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, timeout=15_000)
        except playwright.Error as exc:
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(base_url + "/companion", wait_until="networkidle")
            _assert_no_overflow(page)
            download = page.get_by_role("link", name="Download for Windows")
            download.scroll_into_view_if_needed()
            box = download.bounding_box()
            assert box and box["height"] >= 44
            assert download.evaluate("node => {const b=node.getBoundingClientRect();const hit=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);return hit===node || node.contains(hit);}")
            for name in ("SHA-256 checksum", "Setup and limits"):
                control = page.get_by_role("link", name=name)
                assert control.bounding_box()["height"] >= 44
            _capture(page, f"companion-download-{width}.png")
            with page.expect_download() as transfer:
                download.focus()
                page.keyboard.press("Enter")
            assert transfer.value.suggested_filename == COMPANION_ARCHIVE
            assert Path(transfer.value.path()).read_bytes() == body
            page.get_by_role("link", name="Setup and limits").click()
            _assert_no_overflow(page)
            assert not errors
            assert all(url.startswith(base_url) for url in requests), requests
            assert not any("/api/companion" in url for url in requests)
        finally:
            browser.close()
