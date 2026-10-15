"""Owner-scoped Apple Music export routes; no MusicKit or audio access."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request

from csv_to_clonehero.combined_pipeline import enforce_library_limit, workspace_snapshots
from csv_to_clonehero.library_pipeline import run_library_import
from csv_to_clonehero.providers.apple_music import AppleMusicExportProvider, MAX_EXPORT_BYTES


def register_apple_music_import(
    app: Flask, config: Any, *, require_user: Callable, user_db: Callable,
    request_workspace: Callable, public_source_rows: Callable,
) -> None:
    """Reuse the public app's authenticated owner/workspace resolver callbacks."""

    @app.get("/api/sources/apple-music/status")
    def apple_music_import_status():
        require_user()
        db = user_db()
        try:
            workspace = request_workspace(db)
            summary = next((dict(row) for row in db.get_provider_summaries(workspace)
                            if row["provider"] == "apple_music"), None)
            return jsonify({
                "provider": "apple_music", "account_sync": "not_configured",
                "account_sync_available": False, "user_export_available": True,
                "supported_formats": ["txt", "xml"], "max_bytes": MAX_EXPORT_BYTES,
                "imported_unique_tracks": int((summary or {}).get("unique_tracks") or 0),
                "message": "Account sync needs MusicKit setup and your permission. Import a Music/iTunes export now.",
            })
        finally:
            db.close()

    @app.post("/api/sources/apple-music/import")
    def apple_music_import_export():
        user = require_user()
        # The explicit same-origin header also protects development/local
        # configurations where the application's production CSRF gate is off.
        origin = str(request.headers.get("Origin") or "")
        expected = urlsplit(str(config.public_base_url if config.server.enforce_secret_key else request.host_url))
        received = urlsplit(origin)
        if request.headers.get("X-Riff-Request") != "1" or (
            origin and (received.scheme, received.netloc) != (expected.scheme, expected.netloc)
        ):
            return jsonify({"error": "request origin could not be verified"}), 403
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Choose a Music/iTunes TXT or XML export."}), 400
        suffix = uploaded.filename.rsplit(".", 1)[-1].casefold() if "." in uploaded.filename else ""
        if suffix not in {"txt", "xml"}:
            return jsonify({"error": "Apple Music exports support only TXT or XML."}), 400
        limit = min(MAX_EXPORT_BYTES, int(config.server.max_content_length))
        raw = uploaded.stream.read(limit + 1)
        if len(raw) > limit:
            return jsonify({"error": "The Apple Music export is too large (maximum 10 MB)."}), 413
        try:
            provider = AppleMusicExportProvider.from_bytes(
                raw, extension=suffix,
                label=str(request.form.get("label") or "Apple Music export"),
                max_tracks=config.server.max_scope_tracks,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        finally:
            del raw
        db = user_db()
        try:
            workspace = request_workspace(db)
            workspace_row = db.get_workspace(workspace)
            if workspace_row is None:
                abort(409)
            if db.get_active_job(int(user["id"]), workspace_key=str(workspace_row["workspace_key"]), kind=None) is not None:
                return jsonify({"error": "Wait for your current job to finish before replacing this export."}), 409
            prospective = [snapshot for snapshot in workspace_snapshots(db, workspace)
                           if snapshot.provider != "apple_music"]
            prospective.append(provider.import_library())
            try:
                enforce_library_limit(prospective, config.server.max_library_tracks)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 422
            imported = run_library_import(config, db, workspace, {"apple_music": provider})
            result = imported.sources["apple_music"]
            if result.status != "complete":
                return jsonify({"error": "The export could not be saved. Try again."}), 500
            return jsonify({
                "ok": True, "workspace_id": workspace, "source": result.__dict__,
                "sources": public_source_rows(db, workspace), "combined": imported.combined_summary,
                "account_sync_available": False, "metadata_only": True,
                "message": "Export imported. Choose Find charts when you are ready.",
            })
        finally:
            db.close()
