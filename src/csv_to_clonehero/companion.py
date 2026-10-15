"""Isolated, loopback-only Riff companion preview.

This app reads a checksum-validated catalog, existing package caches, and .sng
libraries. It starts no production workers and never downloads provider media.
The only write operation is an explicitly confirmed, add-only copy into a
previously selected existing directory inside the preview sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import random
import re
import shutil
import stat
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_file

from csv_to_clonehero.catalog import load_enchor_catalog
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.exporter import FileArtifactCache, SNGPKG_MAGIC, validate_sng
from csv_to_clonehero.filesystem import sanitize_component
from csv_to_clonehero.library_inventory import InventoryLimits, scan_library
from csv_to_clonehero.normalization import normalize_artist, normalize_title
from csv_to_clonehero.server.catalog_browse import catalog_key, public_catalog_item

_HASH = re.compile(r"^[a-f0-9]{32}$")
_KEY = re.compile(r"^([a-z][a-z0-9_-]{0,31}):([1-9][0-9]{0,15})$")
_YEAR = re.compile(r"(?<!\d)((?:18|19|20|21)\d{2})(?!\d)")
_REPARSE_POINT = 0x0400
_MAX_RANGE = 1024 * 1024


def parsed_year(value: Any) -> int | None:
    """Use explicit four-digit year metadata; never infer it from popularity."""
    match = _YEAR.search(str(value or ""))
    return int(match.group(1)) if match else None


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _plain_path(path: Path) -> bool:
    """Reject links/junctions in every existing component, without following them."""
    try:
        return all(not _linked(part) for part in (path, *path.parents) if part.exists() or part.is_symlink())
    except OSError:
        return False


class _UninspectedEvidence:
    """No production database is opened or migrated by this preview."""

    @staticmethod
    def is_chart_artifact_eligible(_source: str, _chart_id: int) -> bool:
        return True

    @staticmethod
    def get_chart_artifact_validation(_source: str, _chart_id: int) -> None:
        return None


class CompanionState:
    def __init__(self, *, catalog_path: Path, sandbox_root: Path,
                 library_roots: Iterable[Path] = (), artifact_roots: Iterable[Path] = (),
                 config: AppConfig | None = None, background_inventory: bool = False):
        self.config = config or AppConfig()
        self.catalog_path = Path(catalog_path).absolute()
        self.sandbox_root = Path(sandbox_root).absolute()
        self.library_roots = tuple(dict.fromkeys(Path(root).absolute() for root in library_roots))
        self.artifact_roots = tuple(dict.fromkeys(Path(root).absolute() for root in artifact_roots))
        self.catalog = load_enchor_catalog(self.catalog_path)
        self.entries = self.catalog.charts() if self.catalog.available else ()
        self.total_songs = len({self.song_identity(chart.artist, chart.name) for chart in self.entries})
        self.search = tuple(" ".join(str(value or "") for value in (chart.name, chart.artist, chart.album)).casefold()
                            for chart in self.entries)
        self.years = tuple(parsed_year(chart.year) for chart in self.entries)
        self.orders: OrderedDict[tuple, tuple[int, ...]] = OrderedDict()
        self.lock = threading.RLock()
        self.folder: Path | None = None
        self.evidence = _UninspectedEvidence()
        self.validations: dict[tuple, Any] = {}
        self.inventory = []
        self.installed = set()
        self.sandbox_installed = set()
        self.inventory_status = "scanning" if self.library_roots else "ready"
        known_cache = {}
        self.cache_files = 0
        for root in self.artifact_roots:
            if root.is_dir() and _plain_path(root):
                for path in root.glob("*.sng"):
                    if not _linked(path) and path.is_file():
                        known_cache.setdefault(path.stem.casefold(), path)
                        self.cache_files += 1
        self.cached_by_key = {}
        self.charts_by_md5 = {}
        for chart in self.entries:
            md5 = str(chart.md5 or "").casefold()
            if _HASH.fullmatch(md5):
                self.charts_by_md5.setdefault(md5, chart)
            path = next((known_cache[key] for key in self.cache_keys(chart) if key in known_cache), None)
            if path:
                self.cached_by_key[catalog_key(chart)] = path
        if background_inventory and self.library_roots:
            # A single read-only inventory task, not the production job worker.
            threading.Thread(target=self._scan_inventory, name="riff-read-only-inventory", daemon=True).start()
        else:
            self._scan_inventory()

    def _scan_inventory(self):
        # Only read the named roots. scan_library excludes reparse points and
        # examines .sng archives without extracting, deleting, or rewriting.
        results = []
        installed = set()
        try:
            for root in self.library_roots:
                result = scan_library(root, limits=InventoryLimits(max_files=100_000), hash_files=False)
                results.append(result)
                installed.update(self.song_identity(entry.artist, entry.title)
                                 for entry in result.valid_entries if entry.artist and entry.title)
            with self.lock:
                self.inventory, self.installed = results, installed
                self.inventory_status = "partial" if any(result.diagnostics for result in results) else "ready"
        except Exception:
            with self.lock:
                self.inventory_status = "unavailable"

    @staticmethod
    def song_identity(artist: str, title: str) -> tuple[str, str]:
        return normalize_artist(artist), normalize_title(title)

    def chart(self, key: str):
        match = _KEY.fullmatch(key)
        return self.catalog.get_chart(match[1], int(match[2])) if match and self.catalog.available else None

    @staticmethod
    def cache_keys(chart):
        md5 = str(chart.md5 or "").casefold()
        # ExportService._artifact_key's source-id identity is independent of
        # target paths. Include the old chart-id key for pre-bulk-build caches.
        identity = f"{chart.source}\0source-id:{md5}" if md5 else f"{chart.source}\0chart:{chart.chart_id}"
        keys = [hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest(),
                hashlib.sha256(f"{chart.source}\0{chart.chart_id}\0".encode()).hexdigest()]
        if _HASH.fullmatch(md5):
            keys.append(md5)
        return keys

    def cached_artifact(self, chart):
        """Resolve only exporter cache identities; never accept a client path."""
        path = self.cached_by_key.get(catalog_key(chart))
        if path is None:
            return None
        # Recheck through the existing exporter cache boundary on every use.
        found = FileArtifactCache(path.parent).get(path.stem)
        return found if found is not None and _plain_path(found) else None

    def validation(self, path: Path):
        info = path.stat()
        key = (str(path), info.st_size, info.st_mtime_ns)
        with self.lock:
            result = self.validations.get(key)
            if result is None:
                result = validate_sng(path)
                self.validations[key] = result
            return result

    def artwork(self, chart):
        artwork_id = str(chart.source_artwork_id or chart.artwork_id or "").casefold()
        if not _HASH.fullmatch(artwork_id):
            return None
        for root in self.artifact_roots:
            path = root.parent / "artwork-cache" / f"{artwork_id}.jpg"
            if path.is_file() and _plain_path(path) and path.stat().st_size <= 10 * 1024 * 1024:
                return path
        return None

    def item(self, chart) -> dict:
        item = public_catalog_item(self.config, self.evidence, chart, {})
        path = self.cached_artifact(chart)
        valid = bool(path is not None and self.validation(path).valid)
        sngpkg = False
        if valid:
            with path.open("rb") as stream:
                sngpkg = stream.read(6) == SNGPKG_MAGIC
        md5 = item["md5"]
        local_url = f"/api/companion/audio/{md5}.sng" if valid and sngpkg and md5 else None
        reason = "" if local_url else (
            "This cached archive is not SNGPKG; the existing audio reader cannot preview it." if valid else
            "No validated cached package is available. This preview never downloads songs.")
        year = parsed_year(chart.year)
        identity = self.song_identity(chart.artist, chart.name)
        installed_known = self.inventory_status == "ready" or identity in self.installed
        installed = identity in self.installed if installed_known else None
        in_sandbox = identity in self.sandbox_installed
        item.update(installed=installed, installed_known=installed_known, installed_match="song-metadata" if installed else None,
                    sandbox_installed=in_sandbox, parsed_year=year, decade=(year // 10 * 10) if year else None,
                    install_available=valid and bool(item["selectable"]),
                    install_reason="" if valid and item["selectable"] else item["blocked_reason"] or reason,
                    artwork_url=f"/api/companion/artwork/{chart.source}/{chart.chart_id}.jpg" if self.artwork(chart) else None)
        item["preview"] = {"source": "local-cached-package", "url": local_url,
                           "local_url": local_url, "available": bool(local_url),
                           "availability": "cached-package-unverified-audio" if local_url else "unavailable",
                           "reason": reason, "has_video": bool(chart.has_video_background), "clip_limit_ms": 45000}
        return item

    def page(self, q: str, decade: int | None, seed: str, offset: int, limit: int, cached_only: bool = False):
        terms = tuple(q.casefold().split())
        cache_key = terms, decade, seed, cached_only
        with self.lock:
            indices = self.orders.get(cache_key)
            if indices is None:
                indices = [index for index, text in enumerate(self.search)
                           if all(term in text for term in terms) and (not cached_only or catalog_key(self.entries[index]) in self.cached_by_key) and
                           (decade is None or self.years[index] is not None and self.years[index] // 10 * 10 == decade)]
                if seed:
                    random.Random(hashlib.sha256((self.catalog.sha256 + seed).encode()).digest()).shuffle(indices)
                indices = tuple(indices)
                self.orders[cache_key] = indices
                while len(self.orders) > 8:
                    self.orders.popitem(last=False)
            return [self.item(self.entries[index]) for index in indices[offset:offset + limit]], len(indices)

    def checked_folder(self, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
            raise ValueError("Choose an existing sandbox folder.")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Use an absolute sandbox folder path without '..'.")
        path = path.absolute()
        if not path.is_dir() or not self.sandbox_root.is_dir() or not _plain_path(path):
            raise ValueError("The folder must exist and cannot contain links or junctions.")
        resolved = path.resolve()
        sandbox = self.sandbox_root.resolve()
        if not resolved.is_relative_to(sandbox):
            raise ValueError("This preview can write only inside its sandbox folder.")
        if any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) or root.resolve().is_relative_to(resolved)
               for root in (*self.library_roots, *self.artifact_roots)) or self.catalog_path.resolve().is_relative_to(resolved):
            raise ValueError("Catalog, cache, and song-library input folders are read-only in this preview.")
        return path

    def library_info(self) -> dict:
        roots = [{"path": str(result.root), "scanned_count": result.scanned,
                  "installed_count": len(result.valid_entries), "invalid_count": len(result.invalid_entries),
                  "diagnostics": list(result.diagnostics)} for result in self.inventory]
        ready = self.inventory_status == "ready"
        return {"path": str(self.library_roots[0]) if self.library_roots else None,
                "status": self.inventory_status,
                "roots": roots, "installed_count": sum(root["installed_count"] for root in roots) if ready else None,
                "unique_songs": len(self.installed) if ready else None, "scanned_count": sum(root["scanned_count"] for root in roots) if ready else None,
                "read_only": True, "scope": ".sng archives only; unpacked song folders are not indexed",
                "badge_scope": "Artist/title metadata match; chart variant is not verified",
                "sandbox_installed_count": len(self.sandbox_installed)}


def create_app(*, catalog_path: Path | str, sandbox_root: Path | str,
               library_roots: Iterable[Path | str] = (), artifact_roots: Iterable[Path | str] = (),
               config: AppConfig | None = None, background_inventory: bool = False) -> Flask:
    """Create the preview without opening a database, worker, or network client."""
    app = Flask(__name__, template_folder="server/templates", static_folder="server/static")
    app.config["MAX_CONTENT_LENGTH"] = 8192
    state = CompanionState(catalog_path=Path(catalog_path), sandbox_root=Path(sandbox_root),
                           library_roots=library_roots, artifact_roots=artifact_roots, config=config,
                           background_inventory=background_inventory)
    app.extensions["companion"] = state

    def error(message: str, code: int):
        return jsonify(error=message, message=message), code

    @app.before_request
    def loopback_only():
        try:
            remote = ipaddress.ip_address(request.remote_addr or "")
            host = urlsplit(request.host_url).hostname
            if not remote.is_loopback or host not in {"127.0.0.1", "localhost", "::1"}:
                return error("The companion preview accepts loopback requests only.", 403)
        except ValueError:
            return error("The companion preview accepts loopback requests only.", 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
                return error("Folder changes require a same-origin request.", 403)
            if not request.is_json:
                return error("Use a JSON request for explicit preview operations.", 415)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def home():
        return render_template("companion.html")

    @app.get("/api/companion/status")
    def status():
        catalog = {"status": "ready" if state.catalog.available else "unavailable",
                   "available": state.catalog.available, "total_charts": len(state.entries),
                   "total_songs": state.total_songs,
                   "snapshot_token": "sha256:" + state.catalog.sha256 if state.catalog.available else None}
        return jsonify(mode="sandbox-preview", catalog=catalog, catalog_count=len(state.entries),
                       total_songs=state.total_songs, library=state.library_info(),
                       sandbox_root=str(state.sandbox_root), folder=str(state.folder) if state.folder else None,
                       cached_artifacts=state.cache_files, cached_catalog_charts=len(state.cached_by_key), network_downloads=False, native_packaging=False,
                       install_policy="Explicit single-chart copy from existing cache; sandbox only; no overwrite",
                       preview_limit_ms=45000)

    @app.get("/api/companion/library")
    def library():
        return jsonify(**state.library_info(), sandbox_root=str(state.sandbox_root),
                       folder=str(state.folder) if state.folder else None)

    @app.get("/api/companion/catalog")
    def catalog():
        if not state.catalog.available:
            return error("The checksum-valid local catalog is unavailable.", 503)
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "40"))
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError
            decade_text = request.args.get("decade", "").strip().removesuffix("s")
            decade = int(decade_text) if decade_text else None
            if decade is not None and (not 1800 <= decade <= 2190 or decade % 10):
                raise ValueError
            q = request.args.get("q", "").strip()
            seed = request.args.get("shuffle_seed", "")
            cached_only_text = request.args.get("cached_only", "0")
            if cached_only_text not in {"0", "1"}:
                raise ValueError
            cached_only = cached_only_text == "1"
            if len(q) > 250 or len(seed) > 100:
                raise ValueError
        except ValueError:
            return error("Invalid catalog search, decade, or page parameters.", 400)
        items, count = state.page(q, decade, seed, offset, limit, cached_only)
        return jsonify(items=items, total_charts=len(state.entries), total_songs=state.total_songs,
                       total=len(state.entries), filtered=count, total_filtered_charts=count,
                       offset=offset, limit=limit, has_more=offset + limit < count,
                       snapshot_token="sha256:" + state.catalog.sha256)

    @app.get("/api/companion/catalog/<source>/<int:chart_id>")
    def detail(source: str, chart_id: int):
        chart = state.chart(f"{source}:{chart_id}")
        return jsonify(item=state.item(chart)) if chart else error("Chart not found in the local catalog.", 404)

    def package(chart):
        path = state.cached_artifact(chart) if chart else None
        if path is None or not state.validation(path).valid:
            return error("A validated cached chart package is unavailable.", 404)
        # The browser reader only needs bounded ranges; refuse unbounded full
        # media responses and multi-range reads, including accidental downloads.
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
        if match is None:
            return error("A single explicit byte range is required.", 416)
        start, end = int(match[1]), int(match[2])
        if end < start or end - start + 1 > _MAX_RANGE or end >= path.stat().st_size:
            return error("The requested package range exceeds the preview bounds.", 416)
        # Do not delegate this boundary to send_file's conditional handling:
        # If-Range can make it fall back to an unbounded HTTP 200 full file.
        try:
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if end >= size:
                    return error("The requested range is no longer available.", 416)
                stream.seek(start)
                payload = stream.read(end - start + 1)
        except OSError:
            return error("The cached chart could not be read.", 404)
        if len(payload) != end - start + 1:
            return error("The cached chart range was truncated.", 416)
        return app.response_class(payload, status=206, mimetype="application/octet-stream", headers={
            "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
            "Content-Length": str(len(payload)),
        })

    @app.get("/api/companion/audio/<md5>.sng")
    def audio(md5: str):
        if not _HASH.fullmatch(md5):
            return error("Invalid chart package identity.", 404)
        chart = state.charts_by_md5.get(md5)
        return package(chart)

    @app.get("/api/companion/package/<source>/<int:chart_id>.sng")
    def chart_package(source: str, chart_id: int):
        return package(state.chart(f"{source}:{chart_id}"))

    @app.get("/api/companion/artwork/<source>/<int:chart_id>.jpg")
    def artwork(source: str, chart_id: int):
        chart = state.chart(f"{source}:{chart_id}")
        path = state.artwork(chart) if chart else None
        return send_file(path, mimetype="image/jpeg", conditional=True, etag=False) if path else error("Cached artwork unavailable.", 404)

    @app.post("/api/companion/folder")
    def folder():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("Choose an existing sandbox folder.", 400)
        try:
            chosen = state.checked_folder(payload.get("path"))
        except ValueError as exc:
            return error(str(exc), 400)
        with state.lock:
            state.folder = chosen
        return jsonify(path=str(chosen), folder=str(chosen), sandbox=True, write_performed=False)

    @app.post("/api/companion/install")
    def install():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return error("Confirm this specific chart install explicitly.", 400)
        chart = state.chart(str(payload.get("catalog_key") or ""))
        if chart is None:
            return error("Choose a chart from the current local catalog.", 404)
        with state.lock:
            try:
                destination = state.checked_folder(payload.get("destination"))
            except ValueError as exc:
                return error(str(exc), 400)
            if state.folder is None or destination != state.folder:
                return error("Choose this sandbox folder before installing a chart.", 409)
            item = state.item(chart)
            if not item["install_available"]:
                return error(item["install_reason"], 409)
            source = state.cached_artifact(chart)
            filename = sanitize_component(f"{chart.artist} - {chart.name}", max_length=60)
            filename += f" [{chart.source}-{chart.chart_id}].sng"
            target = destination / filename
            if any(child.name.casefold() == filename.casefold() for child in destination.iterdir()):
                return error("A file already exists at this destination; nothing was overwritten.", 409)
            staging = None
            try:
                # Staging is confined to the explicitly selected sandbox. The
                # final publication is atomic and fails if a target appeared
                # meanwhile. Windows rename refuses an existing destination;
                # POSIX uses hard-link publication with the same guarantee.
                with tempfile.NamedTemporaryFile(prefix=".riff-add-", suffix=".part", dir=destination, delete=False) as stream:
                    staging = Path(stream.name)
                    with source.open("rb") as input_stream:
                        shutil.copyfileobj(input_stream, stream, length=1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not validate_sng(staging).valid:
                    return error("The staged chart failed validation; no song was added.", 409)
                state.checked_folder(str(destination))
                if os.name == "nt":
                    os.rename(staging, target)
                else:
                    os.link(staging, target, follow_symlinks=False)
            except FileExistsError:
                return error("A file already exists at this destination; nothing was overwritten.", 409)
            except (OSError, ValueError):
                return error("The sandbox add could not be published safely; existing songs were preserved.", 409)
            finally:
                if staging is not None:
                    staging.unlink(missing_ok=True)
            state.sandbox_installed.add(state.song_identity(chart.artist, chart.name))
            return jsonify(installed=True, sandbox=True, catalog_key=catalog_key(chart),
                           path=str(target), filename=filename, overwritten=False), 201

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".riff-companion",
                        help="Private local settings/sandbox location; no library is auto-discovered")
    parser.add_argument("--catalog", type=Path, help="Your existing checksum-valid catalog snapshot (optional)")
    parser.add_argument("--sandbox-root", type=Path, help="Add-only test area; defaults to DATA_DIR/sandbox")
    parser.add_argument("--library-root", type=Path, action="append", default=[])
    parser.add_argument("--artifact-root", type=Path, action="append", default=[])
    parser.add_argument("--port", type=int, default=8947)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    data_dir = args.data_dir.expanduser().absolute()
    catalog_path = (args.catalog or data_dir / "catalog" / "enchor.jsonl").expanduser().absolute()
    sandbox_root = (args.sandbox_root or data_dir / "sandbox").expanduser().absolute()
    if not _plain_path(sandbox_root):
        parser.error("The sandbox cannot contain links or junctions")
    resolved_sandbox = sandbox_root.resolve()
    protected_roots = (*args.library_root, *args.artifact_root)
    if any(resolved_sandbox == root.expanduser().resolve()
           or resolved_sandbox.is_relative_to(root.expanduser().resolve())
           or root.expanduser().resolve().is_relative_to(resolved_sandbox)
           for root in protected_roots) or catalog_path.resolve().is_relative_to(resolved_sandbox):
        parser.error("The sandbox and read-only catalog, cache, and library folders must not overlap")
    sandbox_root.mkdir(parents=True, exist_ok=True)
    app = create_app(catalog_path=catalog_path, sandbox_root=sandbox_root,
                     library_roots=args.library_root, artifact_roots=args.artifact_root,
                     background_inventory=True)
    print(f"Riff companion: http://127.0.0.1:{args.port}/", flush=True)
    print("Local access only. Linked libraries are read-only; additions stay in the sandbox.", flush=True)
    if not app.extensions["companion"].catalog.available:
        print("No valid catalog configured. Run CONFIGURE_COMPANION.cmd or pass --catalog.", flush=True)
    from waitress import serve
    serve(app, host="127.0.0.1", port=args.port, threads=4, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
