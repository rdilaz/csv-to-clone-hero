"""Loopback-only release candidate around the real public Riff application.

The production app factory, owner-workspace guards, durable jobs and signed ZIP
delivery are reused unchanged. This opt-in local wrapper has no OAuth identity,
provider downloads or production configuration. Catalog/cache/library inputs
are read-only; all generated files live beneath the explicit runtime root.
"""
from __future__ import annotations

import argparse
import ipaddress
import shutil
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from flask import jsonify, render_template, request, session

from csv_to_clonehero.companion import CompanionState, _plain_path, create_app as companion_app
from csv_to_clonehero.config import AppConfig, CryptoConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.models import DownloadInfo
from csv_to_clonehero.server.catalog_browse import catalog_key
from csv_to_clonehero.server.downloads import build_archive
from csv_to_clonehero.server.web import create_app as public_app
from csv_to_clonehero.sources.base import SourceError

LOCAL_OWNER = "riff-local-release-preview"
LOCAL_LABEL = "Local preview · sandbox only"
UNAVAILABLE = "Not cached locally. This preview does not download chart packages."


class _PreviewCrypto(CryptoConfig):
    def load_key(self):
        # Public, fixed development value only. No external credentials are
        # accepted or stored by this wrapper; do not use in production.
        return "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class PreviewConfig(AppConfig):
    """Ignore ambient production paths and credentials, without mutating env."""

    @property
    def database_path(self):
        return self.resolve(self.paths.database)

    @property
    def catalog_path(self):
        return self.resolve(self.paths.catalog)

    @property
    def public_base_url(self):
        return self.server.base_url

    @property
    def spotify_client_id(self):
        return "local-preview-no-oauth"


class CachedSource:
    """Exporter adapter that can only copy an already validated local cache."""

    name = "enchor"
    supports_direct_download = True
    download_hash_verified = False

    def __init__(self, state: CompanionState, runtime_root: Path):
        self.state, self.runtime_root = state, runtime_root.resolve()

    def _artifact(self, candidate):
        chart = self.state.chart(catalog_key(candidate))
        # Source-id equality prevents stale candidates from reading a newer
        # artifact even if the catalog's numeric identity has been reused.
        if chart is None or str(chart.md5 or "") != str(candidate.md5 or ""):
            raise SourceError("The selected chart changed. Reload the catalog.")
        path = self.state.cached_artifact(chart)
        if path is None or not self.state.validation(path).valid:
            raise SourceError(UNAVAILABLE)
        return path

    def search_song(self, artist, title):
        identity = self.state.song_identity(artist, title)
        return [chart for chart in self.state.entries
                if self.state.song_identity(chart.artist, chart.name) == identity]

    def search_artist(self, artist):
        from csv_to_clonehero.normalization import normalize_artist
        normalized = normalize_artist(artist)
        return [chart for chart in self.state.entries if normalize_artist(chart.artist) == normalized]

    def get_chart_metadata(self, chart_id):
        return self.state.chart(f"enchor:{int(chart_id)}")

    def catalog_contains_chart(self, chart_id, *, source_name="enchor"):
        return source_name == self.name and self.get_chart_metadata(chart_id) is not None

    def catalog_available(self, artist, title=""):
        return bool(self.search_song(artist, title) if title else self.search_artist(artist))

    def get_download_info(self, candidate):
        path = self._artifact(candidate)
        return DownloadInfo(url="", md5="", size_bytes=path.stat().st_size,
                            filename=path.name, notes="Validated local cache; no network")

    def download_chart(self, candidate, destination):
        source = self._artifact(candidate)
        destination = Path(destination).absolute()
        if not destination.resolve().is_relative_to(self.runtime_root) or not _plain_path(destination):
            raise SourceError("Preview exports must stay inside the isolated runtime.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def close(self):
        pass


def create_app(*, catalog_path: Path | str, runtime_root: Path | str,
               artifact_roots: Iterable[Path | str] = (), library_roots: Iterable[Path | str] = (),
               port: int = 8946, background_inventory: bool = True):
    runtime = Path(runtime_root).absolute()
    catalog = Path(catalog_path).absolute()
    artifacts = tuple(Path(path).absolute() for path in artifact_roots)
    libraries = tuple(Path(path).absolute() for path in library_roots)
    for read_root in (*artifacts, *libraries, catalog):
        if runtime.resolve().is_relative_to(read_root.resolve()) or read_root.resolve().is_relative_to(runtime.resolve()):
            raise ValueError("Preview runtime must not overlap read-only catalog, cache or song-library inputs.")
    if not _plain_path(runtime):
        raise ValueError("Preview runtime cannot contain links or junctions.")
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "Songs").mkdir(exist_ok=True)
    config = PreviewConfig()
    config.base_dir = runtime
    config.paths.database = "state/riff-preview.db"
    config.paths.catalog = str(catalog)
    config.paths.input_dir = "input"
    config.paths.output_dir = "Songs"
    config.paths.reports_dir = "reports"
    config.paths.artifact_cache_dir = "state/export-artifacts"
    config.paths.manifest_dir = "state/export-manifests"
    config.server.base_url = f"http://127.0.0.1:{port}"
    config.server.secret_key_env = "RIFF_LOCAL_PREVIEW_UNUSED_SESSION_KEY"
    config.crypto = _PreviewCrypto()
    config.sessions.cookie_name = "riff_local_release_preview"
    config.sessions.cookie_secure = False
    config.notifications.enabled = False
    config.monetization.ads_enabled = False
    config.export.artifact_retries = 0
    config.export.retry_backoff_seconds = 0

    # The loader caches the immutable snapshot, so the public catalog and the
    # companion share the model rather than building two 95k-chart indexes.
    companion = companion_app(catalog_path=catalog, sandbox_root=runtime,
                              library_roots=libraries, artifact_roots=artifacts,
                              config=config, background_inventory=background_inventory)
    state = companion.extensions["companion"]
    source = CachedSource(state, runtime)

    def cached_archive_builder(cfg, db, workspace, _sources, staging, **kwargs):
        return build_archive(cfg, db, workspace, [source], staging, **kwargs)

    app = public_app(config, source_factory=lambda _config: [source], archive_builder=cached_archive_builder)
    app.config["RIFF_LOCAL_PREVIEW"] = True
    app.extensions["companion"] = state
    app.extensions["preview_runtime"] = runtime

    jobs = app.extensions["jobs"]

    jobs.request_catalog_browse()
    jobs.catalog_browser.prepare()

    # Create one local DB owner only. No external account is created, linked,
    # impersonated, or authenticated. No token row is written.
    db = Database(config.database_path)
    try:
        owner = db.upsert_user(spotify_user_id=LOCAL_OWNER, display_name=LOCAL_LABEL)
        owner_id = int(owner["id"])
    finally:
        db.close()

    def fail(message, status):
        return jsonify(error=message, local_preview=True), status

    def local_boundary():
        try:
            if not ipaddress.ip_address(request.remote_addr or "").is_loopback or urlsplit(request.host_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                return fail("This local release preview accepts loopback requests only.", 403)
        except ValueError:
            return fail("This local release preview accepts loopback requests only.", 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("X-Riff-Request") != "1" or request.headers.get("Origin", request.host_url).rstrip("/") != request.host_url.rstrip("/"):
                return fail("Preview changes require a same-origin Riff request.", 403)
        if request.path == "/login" or request.path.startswith("/api/auth/"):
            return fail("External sign-in is unavailable in this local preview; use the local sandbox owner.", 409)
        if request.path in {"/api/spotify/scope", "/api/soundcloud/scope"}:
            return fail("Provider account access is unavailable in this local preview. Browse the catalog or import a local export.", 409)
        if request.path == "/api/preflight" and request.method == "POST":
            return fail("Live provider checks are disabled. The local catalog and cache are used here.", 409)
        if request.path == "/api/catalog/selection" and request.method == "POST":
            payload = request.get_json(silent=True) or {}
            if isinstance(payload, dict) and payload.get("action", "select") == "select":
                chart = state.chart(str(payload.get("catalog_key") or ""))
                if chart is not None and not state.item(chart)["install_available"]:
                    return fail(UNAVAILABLE, 409)
        if not request.path.startswith("/static/"):
            db = Database(config.database_path)
            try:
                current = db.get_user_by_session(str(session.get("sid") or ""))
                if current is None or int(current["id"]) != owner_id:
                    session.clear()
                    session["sid"] = db.create_session(owner_id, expires_at=time.time() + config.sessions.max_age_seconds)["session_id"]
                    session.permanent = True
            finally:
                db.close()

    # Run the restrictive wrapper before the production authorization hook.
    app.before_request_funcs.setdefault(None, []).insert(0, local_boundary)

    @app.context_processor
    def preview_context():
        return {"local_preview": True, "local_preview_label": LOCAL_LABEL,
                "local_preview_scope": "Cached packages only · 45-second stem previews · real library read-only"}

    @app.get("/api/local-preview")
    def preview_status():
        return jsonify(local_preview=True, label=LOCAL_LABEL, runtime_root=str(runtime),
                       total_charts=len(state.entries), total_songs=state.total_songs,
                       cached_catalog_candidates=len(state.cached_by_key), cached_packages=state.cache_files,
                       decoded_audio_coverage="Unverified until opened; cached packages can lack supported audio",
                       library=state.library_info(), network_downloads=False, native_packaging=False)

    @app.get("/local-companion")
    def companion_home():
        return render_template("companion.html", config=config)

    for rule in companion.url_map.iter_rules():
        if rule.rule.startswith("/api/companion/"):
            app.add_url_rule(rule.rule, "local_" + rule.endpoint,
                             companion.view_functions[rule.endpoint], methods=rule.methods)

    def enrich_item(item):
        if not isinstance(item, dict):
            return
        key = item.get("catalog_key")
        if not key and item.get("source") and item.get("chart_id"):
            key = f"{item['source']}:{item['chart_id']}"
        chart = state.chart(str(key or ""))
        if chart is None:
            return
        local = state.item(chart)
        item["preview"] = local["preview"]
        item["artwork"] = local["artwork_url"]
        item["artwork_url"] = local["artwork_url"]
        for name in ("installed", "installed_known", "installed_match", "parsed_year", "decade", "install_available", "install_reason"):
            item[name] = local[name]
        if "selectable" in item and not local["install_available"]:
            item["selectable"] = False
            item["blocked_reason"] = local["install_reason"] or UNAVAILABLE

    @app.after_request
    def local_response(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; connect-src 'self'; media-src 'self' blob:; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'self'; frame-src 'self'; "
            "img-src 'self' data: blob:; object-src 'none'; style-src 'self'; script-src 'self'"
        )
        response.headers["Cache-Control"] = "no-store" if not request.path.startswith("/static/") else response.headers.get("Cache-Control", "no-cache")
        if response.is_json and (request.path.startswith("/api/catalog") or request.path == "/api/listening-deck"):
            payload = response.get_json()
            if isinstance(payload, dict):
                for item in payload.get("items", []):
                    enrich_item(item)
                    for version in item.get("versions", []):
                        enrich_item(version)
                enrich_item(payload.get("item"))
                payload["local_preview"] = True
                response.set_data(app.json.dumps(payload))
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, action="append", default=[])
    parser.add_argument("--library-root", type=Path, action="append", default=[])
    parser.add_argument("--port", type=int, default=8946)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    app = create_app(catalog_path=args.catalog, runtime_root=args.runtime_root,
                     artifact_roots=args.artifact_root, library_roots=args.library_root, port=args.port)
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        app.extensions["jobs"].shutdown(timeout=2)
        app.extensions["downloads"].close()


if __name__ == "__main__":
    main()
