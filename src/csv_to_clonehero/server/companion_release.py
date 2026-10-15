"""Public, read-only distribution of one audited local companion release.

This module registers no companion filesystem, library, picker or audio API.
Only an exact release filename can be served, after its manifest and ZIP have
been checked. A missing or incomplete deployment remains visibly unavailable.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from flask import abort, current_app, g, jsonify, render_template, send_file, url_for


COMPANION_VERSION = "2026.10.15"
COMPANION_ARCHIVE = f"riff-companion-{COMPANION_VERSION}-windows-py312-x64.zip"
COMPANION_CHECKSUM = f"{COMPANION_ARCHIVE}.sha256"
_PUBLIC_FILES = frozenset({COMPANION_ARCHIVE, COMPANION_CHECKSUM, "SHA256SUMS.txt", "manifest.json"})
_MAX_MANIFEST_BYTES = 16_384


def is_companion_release_path(path: str) -> bool:
    """Allow public access only to the landing and named release artifacts."""
    return path == "/companion" or path in {f"/downloads/{name}" for name in _PUBLIC_FILES}


@lru_cache(maxsize=4)
def _archive_digest(path: str, size: int, modified_ns: int) -> str:
    # Size and mtime invalidate the cache when deployment replaces an artifact.
    archive = Path(path)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    # Published ZIPs must be safe to extract even before the user launches them.
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if not members:
            raise ValueError("empty companion archive")
        for member in members:
            # ZipInfo.filename normalizes Windows separators and truncates NULs;
            # inspect the original header spelling before accepting the ZIP.
            name = member.orig_filename
            parts = PurePosixPath(name).parts
            if (not name or "\\" in name or ":" in name or "\x00" in name
                    or name.startswith("/") or ".." in parts
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError("unsafe companion archive member")
    return digest.hexdigest()


def _release_metadata() -> dict | None:
    directory = Path(current_app.config.get(
        "RIFF_COMPANION_RELEASE_DIR", Path(__file__).parent / "releases"
    )).resolve()
    manifest = directory / "manifest.json"
    archive = directory / COMPANION_ARCHIVE
    try:
        if (manifest.is_symlink() or archive.is_symlink()
                or not manifest.is_file() or not archive.is_file()
                or manifest.stat().st_size > _MAX_MANIFEST_BYTES
                or archive.resolve().parent != directory):
            return None
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if value.get("version") != COMPANION_VERSION:
            return None
        candidates = value.get("artifacts")
        if not isinstance(candidates, list):
            return None
        matches = [item for item in candidates if isinstance(item, dict)
                   and item.get("filename") == COMPANION_ARCHIVE]
        if len(matches) != 1:
            return None
        item = matches[0]
        checksum = item.get("sha256", "")
        size = item.get("size_bytes")
        if (not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum)
                or type(size) is not int or size <= 0):
            return None
        info = archive.stat()
        if info.st_size != size or _archive_digest(str(archive), size, info.st_mtime_ns) != checksum:
            return None
    except (OSError, ValueError, TypeError, AttributeError, zipfile.BadZipFile):
        return None
    # Never return any operator-supplied path or extra manifest fields publicly.
    return {"version": COMPANION_VERSION, "filename": COMPANION_ARCHIVE,
            "sha256": checksum, "size_bytes": size, "path": archive}


def register_companion_release(app) -> None:
    """Register the public landing and immutable, allowlisted file download."""

    @app.get("/companion")
    def companion_download_page():
        release = _release_metadata()
        if release:
            release = {**release, "size_mb": round(release["size_bytes"] / (1024 * 1024), 1),
                       "download_url": url_for("companion_release_file", filename=COMPANION_ARCHIVE),
                       "checksum_url": url_for("companion_release_file", filename=COMPANION_CHECKSUM)}
            release.pop("path", None)
        return render_template(
            "companion-download.html", release=release, companion_version=COMPANION_VERSION,
            user=g.get("user"), authenticated=g.get("authenticated", False),
            config=app.extensions["config"],
        )

    @app.get("/downloads/<filename>")
    def companion_release_file(filename):
        if filename not in _PUBLIC_FILES:
            abort(404)
        release = _release_metadata()
        if release is None:
            abort(404)
        if filename == COMPANION_ARCHIVE:
            response = send_file(release["path"], mimetype="application/zip", as_attachment=True,
                                 download_name=COMPANION_ARCHIVE, conditional=True,
                                 etag=release["sha256"])
        elif filename in {COMPANION_CHECKSUM, "SHA256SUMS.txt"}:
            response = current_app.response_class(
                f"{release['sha256']}  {COMPANION_ARCHIVE}\n", mimetype="text/plain"
            )
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        else:
            response = jsonify({"version": release["version"], "artifacts": [{
                "filename": release["filename"], "sha256": release["sha256"],
                "size_bytes": release["size_bytes"],
            }]})
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
