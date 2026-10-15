"""Bounded compression and caching for versioned public assets only."""
from __future__ import annotations

from functools import lru_cache
import gzip
from pathlib import Path

from flask import request


ASSET_VERSION = "riff-listening-room-20261015"


@lru_cache(maxsize=32)
def _gzip_asset(path: str, size: int, modified: int) -> bytes:
    # Size/mtime are cache keys, so a changed file cannot reuse old bytes.
    return gzip.compress(Path(path).read_bytes(), compresslevel=6, mtime=0)


def configure_asset_delivery(app):
    app.jinja_env.globals["asset_version"] = ASSET_VERSION
    @app.after_request
    def asset_delivery(response):
        if request.endpoint != "static":
            return response
        response.vary.add("Accept-Encoding")
        if request.args.get("v") == ASSET_VERSION and response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        if (request.method != "GET" or response.status_code != 200
                or request.headers.get("Range") or response.headers.get("Content-Encoding")
                or request.accept_encodings["gzip"] <= 0):
            return response
        filename = str((request.view_args or {}).get("filename") or "")
        if Path(filename).suffix.lower() not in {".js", ".css", ".svg", ".txt"}:
            return response
        root = Path(app.static_folder).resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            return response
        try:
            stat = path.stat()
            if not 1024 <= stat.st_size <= 2 * 1024 * 1024:
                return response
            compressed = _gzip_asset(str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return response
        if len(compressed) >= stat.st_size:
            return response
        original = response.response
        response.direct_passthrough = False
        response.set_data(compressed)
        if hasattr(original, "close"):
            original.close()
        response.headers["Content-Encoding"] = "gzip"
        value, _weak = response.get_etag()
        if value:
            response.set_etag(value, weak=True)
        return response
