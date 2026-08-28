"""Multi-user, cloud-hosted web application (the public product).

This is a single WSGI app factory (`create_app`) that serves anyone who signs in
with their real Spotify account. It deliberately does NOT run Flask's dev
server; production uses waitress/gunicorn behind Cloudflare (see Dockerfile,
docs/runbook.md, docs/ARCHITECTURE.md).

Design points (per the product decisions):
  * Per-user identity = Spotify login via PKCE. No passwords, no accounts.
  * Each user's refresh token is encrypted at rest in SQLite (Fernet), keyed to
    a server-side session bound to a signed cookie.
  * Background discovery jobs are queued per-user and run FIFO on ONE global
    worker, additionally gated by a global network throttle, so a public app
    sharing one server IP stays under Enchor's ~1-2 req/s hard cap.
  * Charts are delivered to the browser as a short-lived, single-use, signed
    ZIP download. NOTHING chart-shaped is persisted server-side.
  * Every render includes the monetization banner and the Clone Hero creator's
    GoFundMe (a core platform mission).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.server.auth import build_oauth, build_soundcloud_oauth, user_workspace_key
from csv_to_clonehero.server.downloads import BrowserDownloads, DownloadError, build_archive
from csv_to_clonehero.server.jobs import JobManager
from csv_to_clonehero.server.safeguards import (
    get_enchor_preflight,
    refresh_enchor_preflight,
    validate_discovery_start,
)
from csv_to_clonehero.server.security import resolve_session_key, resolve_token_cipher
from csv_to_clonehero.review_presentation import build_review_presentation
from csv_to_clonehero.sources.base import (
    source_catalog_available,
    source_catalog_contains_chart,
)

log = logging.getLogger(__name__)


def load_app_config() -> AppConfig:
    from csv_to_clonehero.config import load_config

    config = load_config()
    config.require_secrets_for_production()
    return config


def create_app(
    config: AppConfig | None = None,
    *,
    source_factory: Callable[[AppConfig], list] | None = None,
) -> Flask:
    """Create the public WSGI application and its durable worker services.

    The factory resolves production secrets, configures the signed session
    cookie, starts one global job manager, and registers authenticated workspace
    and browser-download routes. Tests may inject a chart-source factory.
    """
    config = config or load_app_config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.server.max_content_length
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = config.sessions.cookie_same_site
    # Secure only when serving over HTTPS (Cloudflare edge) or forced.
    secure = _cookie_secure(config)
    app.config["SESSION_COOKIE_SECURE"] = secure
    app.config["SESSION_COOKIE_NAME"] = config.sessions.cookie_name
    app.config["SESSION_COOKIE_DOMAIN"] = config.sessions.domain or None
    app.config["PERMANENT_SESSION_LIFETIME"] = config.sessions.max_age_seconds

    secret_key = resolve_session_key(config)
    app.secret_key = secret_key
    cipher = resolve_token_cipher(config)

    from datetime import datetime

    app.jinja_env.globals["now_year"] = lambda: str(datetime.now().year)

    jobs = JobManager(config, cipher, source_factory=source_factory)
    downloads = BrowserDownloads(config, secret_key)

    app.extensions["jobs"] = jobs
    app.extensions["downloads"] = downloads
    app.extensions["config"] = config

    # ---- request lifecycle --------------------------------------------------
    @app.before_request
    def _authorize() -> None:
        if request.path in {"/healthz"} or request.path.startswith("/static/"):
            return
        sid = session.get("sid")
        g.user = None
        if sid:
            db = Database(config.database_path)
            try:
                g.user = db.get_user_by_session(str(sid))
                if g.user is None:
                    session.clear()
            finally:
                db.close()
        # Public pages: landing + oauth + legal + about + download token route.
        g.authenticated = g.user is not None
        if _is_public_path(request.path):
            return
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required", "login": True}), 401
            return redirect(url_for("index", next=request.full_path))

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not response.headers.get("Content-Security-Policy"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        return response

    # ---- helpers ------------------------------------------------------------
    def _require_user() -> Any:
        user = g.get("user")
        if user is None:
            abort(401)
        return user

    def _user_db() -> Database:
        return Database(config.database_path)

    def _anonymous_workspace(db: Database) -> int:
        key = str(session.get("anonymous_workspace_key") or "")
        if not key.startswith("local:"):
            key = f"local:{uuid.uuid4()}"
            session["anonymous_workspace_key"] = key
        row = db.get_workspace(key)
        return int(row["id"]) if row is not None else int(db.create_workspace(
            workspace_key=key, name="Anonymous music library", kind="exact", status="created", phase="source_setup",
            metadata={"workflow": "source_neutral", "anonymous": True},
        ))

    def _request_workspace(db: Database) -> int:
        user = g.get("user")
        if user is not None:
            return int(_ensure_workspace(db, user))
        return _anonymous_workspace(db)

    # ---- public pages -------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "app": "csv-to-clonehero", "time": time.time()})

    @app.get("/browser-exporter.js")
    def browser_exporter_asset():
        asset = config.base_dir / "browser_export" / "soundcloud-exporter.user.js"
        if not asset.is_file():
            abort(404)
        return send_file(asset, mimetype="text/javascript", as_attachment=True, download_name=asset.name)

    @app.get("/api/sources")
    @app.get("/api/library/summary")
    def source_status():
        db = _user_db()
        try:
            workspace = _request_workspace(db)
            return jsonify({"workspace_id": workspace, "sources": [dict(row) for row in db.get_source_summary(workspace)], "combined": db.get_combined_summary(workspace)})
        finally:
            db.close()

    @app.post("/api/sources/soundcloud/import")
    def import_soundcloud_export():
        uploaded = request.files.get("file") or request.files.get("files")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "upload a SoundCloud CSV or JSON export"}), 400
        suffix = uploaded.filename.rsplit(".", 1)[-1].casefold() if "." in uploaded.filename else ""
        if suffix not in {"csv", "json"}:
            return jsonify({"error": "SoundCloud imports support only CSV or JSON"}), 400
        try:
            raw = uploaded.read()
            if len(raw) > int(config.server.max_content_length):
                return jsonify({"error": "SoundCloud export is too large"}), 413
            from csv_to_clonehero.providers.soundcloud import SoundCloudExportProvider
            provider = SoundCloudExportProvider.from_json(raw.decode("utf-8-sig")) if suffix == "json" else SoundCloudExportProvider.from_csv(raw.decode("utf-8-sig"))
            db = _user_db()
            try:
                workspace = _request_workspace(db)
                from csv_to_clonehero.library_pipeline import run_library_import
                imported = run_library_import(config, db, workspace, {"soundcloud": provider})
                return jsonify({"ok": True, "workspace_id": workspace, "source": imported.sources["soundcloud"].__dict__, "sources": [dict(row) for row in db.get_source_summary(workspace)], "combined": imported.combined_summary})
            finally:
                db.close()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 422

    @app.get("/browser-exporter.js")
    def browser_exporter():
        """Serve the optional, local-only SoundCloud userscript."""
        asset = config.base_dir / "browser_export" / "soundcloud-exporter.user.js"
        if not asset.is_file():
            abort(404)
        return send_file(asset, mimetype="text/javascript", as_attachment=True,
                         download_name="soundcloud-exporter.user.js")

    @app.get("/")
    def index():
        next_path = request.args.get("next", "")
        if next_path.startswith("/") and not next_path.startswith("//"):
            session["next"] = next_path
        return render_template(
            "index.html",
            authenticated=g.get("authenticated", False),
            user=_public_user(g.get("user")),
            config=config,
        )

    @app.get("/about")
    def about():
        return render_template(
            "about.html",
            user=_public_user(g.get("user")),
            config=config,
        )

    @app.get("/terms")
    def terms():
        return render_template("legal.html", page="Terms of Service", config=config)

    @app.get("/privacy")
    def privacy():
        return render_template("legal.html", page="Privacy Policy", config=config)

    @app.get("/dmca")
    def dmca():
        return render_template("legal.html", page="Copyright & DMCA", config=config)

    # ---- Spotify OAuth (multi-user) -----------------------------------------
    @app.get("/login")
    def login():
        oauth = _guest_oauth(config)
        request_obj = oauth.begin_authorization()
        session["oauth_state"] = request_obj.state
        session["oauth_verifier"] = request_obj.code_verifier
        session["oauth_redirect_uri"] = request_obj.redirect_uri
        return redirect(request_obj.url)

    @app.get("/api/auth/spotify/callback")
    def oauth_callback():
        from csv_to_clonehero.server.auth import EncryptedCredentialStore

        anonymous_workspace_key = str(session.get("anonymous_workspace_key") or "")
        error = request.args.get("error")
        if error:
            return render_template("error.html", message=f"Spotify sign-in failed: {error}"), 400
        state = request.args.get("state", "")
        code = request.args.get("code", "")
        expected_state = session.get("oauth_state")
        verifier = session.get("oauth_verifier")
        redirect_uri = session.get("oauth_redirect_uri")
        oauth = _guest_oauth(config)
        oauth._pending_request = None
        try:
            token = oauth.exchange_code(
                code,
                returned_state=state,
                state=expected_state,
                code_verifier=verifier,
                redirect_uri=redirect_uri or None,
            )
        except Exception as exc:  # noqa: BLE001 - safe message to the browser
            log.warning("oauth callback failed: %s", exc)
            return render_template("error.html", message="Spotify sign-in was not completed; please try again."), 400
        profile = _fetch_profile(config, token)
        db = _user_db()
        try:
            user = db.upsert_user(
                spotify_user_id=profile.get("id", ""),
                display_name=profile.get("display_name", ""),
                country=profile.get("country", ""),
                product=profile.get("product", ""),
                href=profile.get("href", ""),
                scopes=token.scope,
            )
            uid = int(user["id"])
            # Store the encrypted refresh token (encrypt in the process memory).
            store = EncryptedCredentialStore(config.database_path, cipher, uid)
            store.set_password("csv-to-clonehero.spotify", f"user:{uid}", token.refresh_token or "")
            if anonymous_workspace_key:
                db.adopt_workspace_key(
                    anonymous_workspace_key,
                    user_workspace_key(profile.get("id", "")),
                    name=profile.get("display_name", "") or profile.get("id", ""),
                )
        finally:
            db.close()
        db = _user_db()
        try:
            sess = db.create_session(
                uid,
                expires_at=time.time() + config.sessions.max_age_seconds,
                user_agent=request.headers.get("User-Agent", "")[:500],
            )
        finally:
            db.close()
        redirect_target = session.get("next") or url_for("index")
        session.clear()
        session["sid"] = sess["session_id"]
        session.permanent = True
        return redirect(redirect_target)

    @app.post("/api/auth/logout")
    def logout():
        sid = session.get("sid")
        if sid:
            db = _user_db()
            try:
                db.revoke_session(str(sid))
            finally:
                db.close()
        session.clear()
        return jsonify({"ok": True})

    # ---- optional SoundCloud OAuth ------------------------------------------
    @app.get("/api/auth/soundcloud/status")
    def soundcloud_auth_status():
        user = _require_user()
        db = _user_db()
        try:
            token = db.get_provider_token(int(user["id"]), "soundcloud")
            return jsonify({
                "configured": config.soundcloud.oauth_configured,
                "authenticated": bool(token and token["encrypted_refresh_token"]),
                "export_available": bool(config.soundcloud.allow_user_exports),
            })
        finally:
            db.close()

    @app.get("/api/auth/soundcloud/start")
    def soundcloud_auth_start():
        _require_user()
        if not config.soundcloud.oauth_configured:
            return jsonify({"error": "SoundCloud OAuth is not configured."}), 503
        try:
            oauth = build_soundcloud_oauth(config, int(g.user["id"]))
            request_obj = oauth.begin_authorization()
            session["soundcloud_oauth_state"] = request_obj.state
            session["soundcloud_oauth_verifier"] = request_obj.code_verifier
            session["soundcloud_oauth_redirect_uri"] = request_obj.redirect_uri
            return redirect(request_obj.url)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 503

    @app.get("/api/auth/soundcloud/callback")
    def soundcloud_auth_callback():
        if request.args.get("error"):
            return render_template("error.html", message="SoundCloud sign-in was cancelled."), 400
        if g.get("user") is None:
            return redirect(url_for("index"))
        oauth = None
        try:
            oauth = build_soundcloud_oauth(config, int(g.user["id"]))
            oauth.exchange_code(
                request.args.get("code", ""),
                returned_state=request.args.get("state", ""),
                expected_state=session.pop("soundcloud_oauth_state", None),
                code_verifier=session.pop("soundcloud_oauth_verifier", ""),
                redirect_uri=session.pop("soundcloud_oauth_redirect_uri", None),
            )
            return redirect(url_for("index"))
        except Exception:  # noqa: BLE001 - do not expose provider secrets
            return render_template("error.html", message="SoundCloud sign-in was not completed; please try again."), 400
        finally:
            if oauth is not None:
                oauth.close()

    @app.post("/api/auth/soundcloud/logout")
    def soundcloud_auth_logout():
        user = _require_user()
        db = _user_db()
        try:
            db.clear_provider_token(int(user["id"]), "soundcloud")
        finally:
            db.close()
        return jsonify({"ok": True})

    @app.get("/api/soundcloud/scope")
    def soundcloud_scope():
        _require_user()
        if not config.soundcloud.oauth_configured:
            return jsonify({"configured": False, "playlists": [], "liked_count": 0})
        oauth = build_soundcloud_oauth(config, int(g.user["id"]))
        client = None
        try:
            from csv_to_clonehero.providers.soundcloud_api import SoundCloudClient
            client = SoundCloudClient(oauth, timeout=float(config.network.timeout_seconds or 20))
            playlists = client.playlists()
            liked = len(client.liked_tracks())
            return jsonify({"configured": True, "playlists": playlists, "liked_count": liked})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "SoundCloud library could not be loaded."}), 502
        finally:
            if client is not None:
                client.close()
            oauth.close()

    @app.post("/api/soundcloud/scope")
    def save_soundcloud_scope():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        playlist_ids = sorted({str(value).strip() for value in payload.get("playlist_ids", []) if str(value).strip()})
        include_liked = bool(payload.get("include_liked"))
        if not playlist_ids and not include_liked:
            return jsonify({"error": "Choose at least one SoundCloud playlist or Liked Tracks."}), 400
        db = _user_db()
        try:
            key = user_workspace_key(user["spotify_user_id"])
            workspace = db.get_workspace(key)
            workspace_id = int(workspace["id"]) if workspace else db.create_workspace(
                workspace_key=key, name=user["spotify_user_id"], kind="exact", status="ready_for_scope", phase="ready_for_scope",
                metadata={"workflow": "public_combined"},
            )
            _set_provider_scope(db, workspace_id, "soundcloud", {"playlist_ids": playlist_ids, "include_liked": include_liked})
            return jsonify({"ok": True, "scope": {"playlist_ids": playlist_ids, "include_liked": include_liked}})
        finally:
            db.close()

    @app.get("/api/me")
    def me():
        user = _require_user()
        return jsonify(_public_user(user))

    # ---- Spotify scope (playlists + liked) ----------------------------------
    @app.get("/api/spotify/scope")
    def spotify_scope():
        user = _require_user()
        db = _user_db()
        client = None
        try:
            oauth = build_oauth(config, int(user["id"]), cipher)
            client = _client_for(config, oauth)
            playlists = _list_playlists(client)
            liked = _count_liked(client) if hasattr(client, "get_liked_tracks") else 0
            return jsonify({"playlists": playlists, "liked_count": liked, "workspace": _scope_workspace(db, user)})
        finally:
            if client is not None:
                client.close()
            db.close()

    @app.post("/api/spotify/scope")
    def save_spotify_scope():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        raw_playlist_ids = payload.get("playlist_ids") or []
        raw_track_ids = payload.get("track_ids") or []
        if not isinstance(raw_playlist_ids, list) or not isinstance(raw_track_ids, list):
            return jsonify({"error": "playlist_ids and track_ids must be lists"}), 400
        playlist_ids = sorted({str(value).strip() for value in raw_playlist_ids if str(value).strip()})
        track_ids = sorted({str(value).strip() for value in raw_track_ids if str(value).strip()})
        if track_ids:
            return jsonify({"error": "Track-only scope is not supported yet; choose playlists or Liked Songs."}), 400
        include_liked = bool(payload.get("include_liked"))
        if not (playlist_ids or track_ids or include_liked):
            return jsonify({"error": "Choose at least one playlist or Liked Songs."}), 400
        if len(playlist_ids) > config.server.max_scope_playlists:
            return jsonify({"error": "Too many playlists selected."}), 400
        if len(track_ids) > config.server.max_scope_tracks:
            return jsonify({"error": "Too many tracks selected."}), 400
        db = _user_db()
        try:
            key = user_workspace_key(user["spotify_user_id"])
            workspace = db.get_workspace(key)
            if workspace is None:
                workspace_id = db.create_workspace(
                    workspace_key=key,
                    name=user["spotify_user_id"],
                    kind="exact",
                    status="ready_for_scope",
                    phase="ready_for_scope",
                    metadata={"workflow": "public_exact"},
                )
            else:
                workspace_id = int(workspace["id"])
            db.set_workspace_scope(workspace_id, playlist_ids, include_liked, track_ids)
            _set_provider_scope(db, workspace_id, "spotify", {"playlist_ids": playlist_ids, "include_liked": include_liked})
            return jsonify({"ok": True, "workspace": _scope_workspace(db, user)})
        finally:
            db.close()

    # ---- Enchor preflight --------------------------------------------------
    @app.get("/api/preflight")
    def preflight_status():
        _require_user()
        db = _user_db()
        try:
            return jsonify({"preflight": get_enchor_preflight(db)})
        finally:
            db.close()

    @app.post("/api/preflight")
    def refresh_preflight():
        _require_user()
        db = _user_db()
        try:
            result = refresh_enchor_preflight(
                config,
                db,
                jobs.source_factory,
            )
            return jsonify({"preflight": result}), 200 if result.get("usable") else 503
        finally:
            db.close()

    # ---- jobs ---------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs():
        user = _require_user()
        return jsonify({"jobs": jobs.list_for_user(int(user["id"]))})

    @app.post("/api/jobs")
    def create_job():
        user = _require_user()
        kind = str((request.get_json(silent=True) or {}).get("kind") or "discovery")
        if kind != "discovery":
            return jsonify({"error": "unsupported job kind"}), 400
        db = _user_db()
        try:
            key = user_workspace_key(user["spotify_user_id"])
            validation = validate_discovery_start(
                db,
                config,
                user_id=int(user["id"]),
                workspace_key=key,
            )
            if not validation.ok:
                status = 409 if validation.active_job_id is not None else 412
                body: dict[str, Any] = {
                    "error": " ".join(validation.errors),
                    "errors": list(validation.errors),
                    "preflight": validation.preflight,
                }
                if validation.active_job_id is not None:
                    existing = jobs.get(validation.active_job_id)
                    if existing is not None:
                        body["job"] = _job_public(existing)
                return jsonify(body), status
            job, created = jobs.enqueue_unique(
                int(user["id"]), "discovery", workspace_key=key, notify_email=""
            )
        finally:
            db.close()
        if not created:
            return jsonify({
                "error": "A discovery job is already active for this workspace.",
                "job": _job_public(job),
            }), 409
        return jsonify({"job": _job_public(job)}), 202

    @app.get("/api/jobs/<int:job_id>")
    def job_status(job_id):
        user = _require_user()
        job = jobs.get(job_id)
        if job is None or job.user_id != int(user["id"]):
            abort(404)
        return jsonify({"job": _job_public(job)})

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel_job(job_id):
        user = _require_user()
        job = jobs.cancel(job_id, int(user["id"]))
        if job is None:
            abort(404)
        return jsonify({"job": _job_public(job)})

    # ---- workspace + review ---------------------------------------------------
    @app.get("/api/workspace")
    def workspace():
        user = _require_user()
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            minimum_score = db.get_workspace_minimum_score(workspace) if workspace else 50.0
            cards = _review_cards(db, workspace, minimum_score)
            return jsonify({"workspace": workspace, "minimum_score": minimum_score, "tracks": cards})
        finally:
            db.close()

    @app.post("/api/selection")
    def selection():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            candidate_id = int(payload.get("candidate_id") or 0)
            track_value = payload.get("track_id") or payload.get("track_key")
            candidate_kind = str(payload.get("kind") or "exact")
            if candidate_kind not in {"exact", "expansion"}:
                raise ValueError("candidate kind must be exact or expansion")
            artist_norm = str(payload.get("artist_norm") or "")
            action = str(payload.get("action") or "select")
            if action in {"clear", "deselect", "remove"}:
                db.clear_workspace_selection(workspace, track_value, candidate_id=candidate_id, kind=candidate_kind, artist_norm=artist_norm)
            else:
                threshold = db.get_workspace_minimum_score(workspace)
                matching_rows = db.get_workspace_candidates(
                    workspace,
                    track_id=track_value,
                    kind=candidate_kind,
                    include_rejected=True,
                )
                candidate_row = next(
                    (row for row in matching_rows if int(row["candidate_id"]) == candidate_id),
                    None,
                )
                if candidate_row is None:
                    raise ValueError("candidate does not belong to this workspace track")
                if candidate_kind == "exact":
                    catalog_sources = list(jobs.source_factory(config))
                    try:
                        relevant = [source for source in catalog_sources if str(getattr(source, "name", "")) == str(candidate_row["source"] or "")]
                        chart_memberships = [
                            source_catalog_contains_chart(
                                source,
                                int(candidate_row["chart_id"]),
                                source_name=str(candidate_row["source"] or ""),
                            )
                            for source in relevant
                            if candidate_row["chart_id"] not in (None, "")
                        ]
                        if chart_memberships and any(value is True for value in chart_memberships):
                            pass
                        elif chart_memberships and all(value is False for value in chart_memberships):
                            raise ValueError("song is unavailable in the local chart catalog")
                        else:
                            memberships = [source_catalog_available(source, str(candidate_row["track_artist"] or ""), str(candidate_row["track_title"] or "")) for source in relevant]
                            if memberships and all(value is False for value in memberships):
                                raise ValueError("song is unavailable in the local chart catalog")
                    finally:
                        for source in catalog_sources:
                            try:
                                source.close()
                            except Exception:
                                pass
                if float(candidate_row["score"] or 0) < threshold:
                    raise ValueError(f"candidate score is below the workspace threshold ({threshold:g})")
                db.select_workspace_candidate(
                    workspace,
                    track_value,
                    candidate_id,
                    kind=candidate_kind,
                    artist_norm=artist_norm,
                    force=True,
                    require_workspace_candidate=True,
                )
            return jsonify({"ok": True})
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            db.close()

    @app.get("/api/catalog/summary")
    def catalog_summary():
        from csv_to_clonehero.catalog import load_enchor_catalog

        catalog = load_enchor_catalog(config.catalog_path)
        return jsonify({
            "available": catalog.available,
            "diagnostic": catalog.diagnostic,
            "chart_count": catalog.chart_count if catalog.available else 0,
            "artist_count": len(catalog.artists()) if catalog.available else 0,
            "song_count": catalog.song_count if catalog.available else 0,
            "snapshot_id": catalog.snapshot_id,
        })

    @app.get("/api/expansion/artists")
    def expansion_artists():
        from csv_to_clonehero.catalog import load_enchor_catalog
        from csv_to_clonehero.combined_pipeline import rank_expansion_artists

        db = _user_db()
        try:
            tracks = db.get_spotify_tracks(True)
            catalog = load_enchor_catalog(config.catalog_path)
            return jsonify({"available": catalog.available, "artists": rank_expansion_artists(catalog, tracks) if catalog.available else []})
        finally:
            db.close()

    @app.post("/api/expansion/artists/select")
    def select_expansion_artists():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        values = payload.get("artists") or payload.get("artist_norms") or []
        if not isinstance(values, list):
            return jsonify({"error": "artists must be a list"}), 400
        selected = sorted({str(value.get("artist_norm") or value.get("artist") if isinstance(value, dict) else value).strip().casefold() for value in values})
        selected = [value for value in selected if value]
        if len(selected) > config.expansion.max_artists_per_batch:
            return jsonify({"error": "too many expansion artists selected"}), 400
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            if not workspace:
                return jsonify({"error": "choose a library scope first"}), 400
            row = db.get_workspace(workspace)
            metadata = _json_object(row["metadata_json"] if row is not None else "{}")
            metadata["combined_expansion_artists"] = selected
            db.update_workspace(workspace, metadata=metadata)
            return jsonify({"ok": True, "artists": selected})
        finally:
            db.close()

    @app.post("/api/expansion/score")
    def score_expansion():
        from csv_to_clonehero.combined_pipeline import expand_combined

        user = _require_user()
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            row = db.get_workspace(workspace) if workspace else None
            metadata = _json_object(row["metadata_json"] if row is not None else "{}")
            artists = metadata.get("combined_expansion_artists") or []
            result = expand_combined(config, db, workspace, artists)
            return jsonify({"ok": True, "result": vars(result)})
        finally:
            db.close()

    @app.get("/api/workspaces")
    def list_workspaces():
        user = _require_user()
        db = _user_db()
        try:
            prefix = user_workspace_key(user["spotify_user_id"])
            rows = [row for row in db.list_workspaces() if row["workspace_key"] == prefix]
            return jsonify({"workspaces": [_workspace_public(db, row) for row in rows]})
        finally:
            db.close()

    @app.get("/api/workspaces/<int:workspace_id>")
    def get_workspace(workspace_id):
        user = _require_user()
        db = _user_db()
        try:
            row = db.get_workspace(workspace_id)
            if row is None or row["workspace_key"] != user_workspace_key(user["spotify_user_id"]):
                abort(404)
            return jsonify({"workspace": _workspace_public(db, row)})
        finally:
            db.close()

    @app.post("/api/workspaces/<int:workspace_id>/reset")
    def reset_workspace(workspace_id):
        user = _require_user()
        db = _user_db()
        try:
            row = _owned_workspace(db, user, workspace_id)
            if row is None:
                abort(404)
            if not db.reset_workspace_if_idle(row["workspace_key"], int(user["id"])):
                return jsonify({"error": "Cancel the active job before resetting this workspace."}), 409
            downloads.revoke_workspace(workspace_id)
            return jsonify({"ok": True})
        finally:
            db.close()

    @app.delete("/api/workspaces/<int:workspace_id>")
    def delete_workspace(workspace_id):
        user = _require_user()
        db = _user_db()
        try:
            row = _owned_workspace(db, user, workspace_id)
            if row is None:
                if db.get_workspace(workspace_id) is not None:
                    abort(404)
                return jsonify({"ok": True, "deleted": False})
            idle, deleted = db.delete_workspace_if_idle(row["workspace_key"], int(user["id"]))
            if not idle:
                return jsonify({"error": "Cancel the active job before deleting this workspace."}), 409
            downloads.revoke_workspace(workspace_id)
            return jsonify({"ok": True, "deleted": deleted})
        finally:
            db.close()

    @app.post("/api/selection/bulk")
    def bulk_selection():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "select").casefold()
        if action not in {"select", "deselect", "clear"}:
            return jsonify({"error": "action must be select or deselect"}), 400
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            if not workspace:
                return jsonify({"error": "No workspace exists yet."}), 400
            threshold = db.get_workspace_minimum_score(workspace)
            changed = 0
            if action == "select":
                rows = db.get_workspace_candidates(workspace, kind="exact", include_rejected=True)
                for row in rows:
                    if float(row["score"] or 0) < threshold or bool(row["selected"]):
                        continue
                    try:
                        db.select_workspace_candidate(
                            workspace,
                            row["track_id"],
                            int(row["candidate_id"]),
                            kind="exact",
                            force=True,
                            require_workspace_candidate=True,
                        )
                        changed += 1
                    except ValueError:
                        continue
            else:
                changed = db.clear_workspace_selection(workspace, kind="exact")
            return jsonify({"ok": True, "changed": changed, "minimum_score": threshold})
        finally:
            db.close()

    @app.post("/api/workspace/settings")
    def workspace_settings():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            if not workspace:
                return jsonify({"error": "Choose a library scope first."}), 400
            value = db.set_workspace_minimum_score(workspace, payload.get("minimum_score"))
            return jsonify({"ok": True, "minimum_score": value})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            db.close()

    # ---- browser download -----------------------------------------------------
    @app.post("/api/download")
    def start_download():
        user = _require_user()
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            archive, summary = build_archive(
                config, db, workspace, None, downloads.root
            )
            download = downloads.create(int(user["id"]), workspace, archive)
            return jsonify({"download": download, "summary": summary})
        except DownloadError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            log.exception("download build failed")
            return jsonify({"error": "Could not create your download right now; try again in a moment."}), 500
        finally:
            db.close()

    @app.get("/api/download/<path:token>")
    def stream_download(token):
        user = _require_user()
        try:
            meta = downloads.resolve(token)
            if meta is None or int(meta.get("user_id", -1)) != int(user["id"]):
                abort(404)
            filename = f"clone-hero-charts-{meta['workspace_id']}.zip"
            generator = downloads.stream(token)
            return _zip_response(generator, filename, meta.get("size", 0))
        except DownloadError:
            return jsonify({"error": "Download expired; run it again."}), 410

    return app


# --------------------------------------------------------------------------- helpers


def _cookie_secure(config: AppConfig) -> bool:
    configured = config.sessions.cookie_secure
    if configured is not None:
        return bool(configured)
    return config.public_base_url.startswith("https://") or config.server.enforce_secret_key


def _is_public_path(path: str) -> bool:
    if path.startswith("/static/") or path in {
        "/",
        "/favicon.ico",
        "/healthz",
        "/login",
        "/api/auth/spotify/callback",
        "/api/auth/soundcloud/callback",
        "/browser-exporter.js",
        "/about",
    }:
        return True
    if path in {"/api/sources", "/api/library/summary", "/api/sources/soundcloud/import"}:
        return True
    if path.startswith(("/terms", "/privacy", "/dmca")):
        return True
    if path.startswith("/api/download/"):
        # The token itself is the authorization; still require a signed-in user.
        return True
    return False


def _guest_oauth(config: AppConfig):
    from csv_to_clonehero.spotify import SpotifyOAuth, SpotifyOAuthConfig

    model = SpotifyOAuthConfig(
        client_id=config.spotify_client_id,
        scopes=config.spotify_scopes,
        redirect_port=config.spotify.redirect_port,
        redirect_path=config.spotify.redirect_path,
        redirect_uri=config.spotify_redirect_uri,
        api_base_url="https://api.spotify.com/v1",
    )
    return SpotifyOAuth(config=model)


def _client_for(config: AppConfig, oauth):
    from csv_to_clonehero.server.auth import build_spotify_client

    return build_spotify_client(config, oauth)


def _list_playlists(client: Any) -> list[dict[str, Any]]:
    method = getattr(client, "list_playlists", None) or getattr(client, "get_playlists", None)
    if not callable(method):
        return []
    items = []
    try:
        for playlist in method():
            items.append(
                {
                    "id": str(getattr(playlist, "id", "") or ""),
                    "name": str(getattr(playlist, "name", "") or ""),
                    "track_count": getattr(playlist, "total_tracks", None),
                    "owner_name": str(getattr(playlist, "owner_name", "") or ""),
                }
            )
    except Exception:  # noqa: BLE001
        log.warning("could not list playlists", exc_info=True)
    return items


def _count_liked(client: Any) -> int:
    count_method = getattr(client, "get_saved_tracks_count", None)
    if callable(count_method):
        try:
            return max(0, int(count_method()))
        except Exception:  # noqa: BLE001
            pass
    method = getattr(client, "get_liked_tracks", None)
    if not callable(method):
        return 0
    try:
        return len(list(method()))
    except Exception:  # noqa: BLE001
        return 0


def _fetch_profile(config: AppConfig, token: Any) -> dict[str, Any]:
    import httpx

    headers = {"Authorization": f"Bearer {token.access_token}"}
    try:
        response = httpx.get("https://api.spotify.com/v1/me", headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001
        return {"id": "", "display_name": ""}


def _public_user(user: Any) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        "id": user["spotify_user_id"],
        "display_name": user["display_name"] or user["spotify_user_id"],
    }


def _scope_workspace(db: Database, user: Any) -> dict[str, Any]:
    row = db.get_workspace(user_workspace_key(user["spotify_user_id"]))
    workspace = int(row["id"]) if row is not None else None
    scope = db.get_workspace_scope_data(workspace) if workspace is not None else None
    metadata = _json_object(row["metadata_json"] if row is not None else "{}")
    return {"id": workspace, "exists": workspace is not None, "scope": scope, "soundcloud_scope": metadata.get("soundcloud_scope", {})}


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _set_provider_scope(db: Database, workspace_id: int, provider: str, scope: dict[str, Any]) -> None:
    row = db.get_workspace(workspace_id)
    metadata = _json_object(row["metadata_json"] if row is not None else "{}")
    metadata[f"{provider}_scope"] = scope
    db.update_workspace(workspace_id, metadata=metadata)


def _ensure_workspace(db: Database, user: Any) -> int:
    key = user_workspace_key(user["spotify_user_id"])
    row = db.get_workspace(key)
    if row is not None:
        return int(row["id"])
    return 0


def _review_cards(
    db: Database,
    workspace: int,
    minimum_score: float = 50.0,
) -> list[dict[str, Any]]:
    if not workspace:
        return []
    rows = db.get_workspace_candidates(workspace, kind="exact", include_rejected=True)
    by_track: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row) if hasattr(row, "keys") else {}
        tid = data.get("track_id")
        if not tid:
            continue
        candidate = db.get_candidate(int(data.get("candidate_id") or 0))
        if candidate is None:
            continue
        breakdown = {}
        try:
            breakdown = json.loads(data.get("breakdown_json") or "{}")
        except (TypeError, ValueError):
            breakdown = {}
        item = {
            "candidate_id": data.get("candidate_id"),
            "source": candidate.source,
            "chart_id": candidate.chart_id,
            "artist": candidate.artist,
            "title": candidate.name,
            "charter": candidate.charter,
            "album": candidate.album,
            "pack_name": candidate.pack_name,
            "instruments": list(candidate.instruments or ()),
            "diff_guitar": candidate.diff_guitar,
            "url": candidate.url,
            "reference": candidate.reference,
            "supports_download": bool(candidate.supports_download),
            "score": round(float(data.get("score") or 0), 1),
            "quality": round(float(data.get("quality") or 0), 1),
            # Raw fields remain available to existing API consumers. The
            # ``review`` object is the only shape the human-facing UI needs.
            "eligibility": data.get("eligibility") or breakdown.get("eligibility") or "",
            "explanation": breakdown.get("explanation") or "",
            "selected": bool(data.get("selected")),
        }
        item["review"] = build_review_presentation(
            {**item, "breakdown": breakdown},
            minimum_score=minimum_score,
        )
        item["review_state"] = item["review"]["state"]
        item["review_label"] = item["review"]["label"]
        item["review_summary"] = item["review"]["summary"]
        item["selectable"] = item["review"]["selectable"]
        item["selection_action"] = item["review"]["action"]
        by_track.setdefault(int(tid), []).append(item)

    workspace_track_ids = db.get_workspace_library_track_ids(workspace)
    if not workspace_track_ids:
        # Legacy workspaces may predate the workspace-library projection. The
        # candidate rows are still workspace-scoped, so this fallback preserves
        # their review cards without broadening visibility across users.
        workspace_track_ids = set(by_track)
    tracks = [
        track
        for track in db.get_spotify_tracks(True)
        if int(track.db_id or 0) in workspace_track_ids
    ]
    workspace_row = db.get_workspace(workspace)
    workspace_status = str(workspace_row["status"] or "") if workspace_row is not None else ""
    progress_row = db.get_workspace_progress(workspace)
    progress_status = str(progress_row["status"] or "") if progress_row is not None else ""
    metadata = _json_object(workspace_row["metadata_json"] if workspace_row is not None else "{}")
    stats = _json_object(metadata.get("stats"))
    if not isinstance(stats, dict):
        stats = {}
    has_catalog_gaps = bool(stats.get("catalog_unavailable_tracks"))
    active_statuses = {"pending", "running"}
    incomplete_statuses = {"failed", "partial", "interrupted", "cancelled"}
    matching_partial = bool(
        workspace_status in incomplete_statuses
        or progress_status in incomplete_statuses
        or (workspace_row is not None and int(workspace_row["error_count"] or 0) > 0)
        or metadata.get("partial")
        or stats.get("partial")
        or stats.get("query_errors")
    )

    def empty_review() -> dict[str, Any]:
        if workspace_status in active_statuses or progress_status in active_statuses:
            return {
                "state": "in_progress",
                "label": "Checking",
                "summary": "The chart search is still checking this song.",
                "why": ["The search has not finished for this song."],
                "concerns": [],
                "details": [],
                "score": None,
                "hard_gate": False,
                "selectable": False,
                "action": "Unavailable",
                "selected": False,
                "selected_label": "",
            }
        if workspace_status in {"created", "ready_for_scope"} or not workspace_status:
            return {
                "state": "not_checked",
                "label": "Not checked",
                "summary": "Run discovery to check this song for a playable chart.",
                "why": ["Matching has not started for this song."],
                "concerns": [],
                "details": [],
                "score": None,
                "hard_gate": False,
                "selectable": False,
                "action": "Unavailable",
                "selected": False,
                "selected_label": "",
            }
        if matching_partial:
            return {
                "state": "incomplete",
                "label": "Matching incomplete",
                "summary": "Matching did not finish; run the search again before deciding.",
                "why": ["The chart search did not finish for this song."],
                "concerns": [],
                "details": [],
                "score": None,
                "hard_gate": False,
                "selectable": False,
                "action": "Unavailable",
                "selected": False,
                "selected_label": "",
            }
        return {
            "state": "no_match",
            "label": "No suitable chart found",
            "summary": (
                "No suitable chart was found, or this song was not covered by the available catalog."
                if has_catalog_gaps
                else "No suitable chart was found in the completed search."
            ),
            "why": ["There is no chart result to review for this song."],
            "concerns": [],
            "details": [],
            "score": None,
            "hard_gate": False,
            "selectable": False,
            "action": "Unavailable",
            "selected": False,
            "selected_label": "",
        }

    cards: list[dict[str, Any]] = []
    for track in tracks:
        candidates = by_track.get(int(track.db_id or 0))
        candidates = candidates or []
        candidates.sort(key=lambda item: (-item["score"], item["source"], str(item["chart_id"])))
        if candidates:
            # A hard-gated high score is never presented as the best safe
            # recommendation when another usable version exists.
            best = next(
                (
                    item
                    for item in candidates
                    if item["review"]["state"] in {"strong", "needs_review"}
                ),
                candidates[0],
            )
            for item in candidates:
                item["best"] = item is best
            selected = next((item for item in candidates if item["selected"]), None)
            review = best["review"]
            review_notice = (
                "Some chart checks were incomplete; run discovery again before relying on missing alternatives."
                if matching_partial
                else ""
            )
        else:
            best = None
            selected = None
            review = empty_review()
            review_notice = ""
        cards.append(
            {
                "track_key": track.track_key,
                "spotify_id": track.spotify_id,
                "artist": track.artist_full,
                "title": track.title,
                "album": track.album,
                "candidates": candidates,
                "best_candidate_id": best["candidate_id"] if best else None,
                "selected_candidate_id": selected["candidate_id"] if selected else None,
                "selected": bool(selected),
                "review": review,
                "review_state": review["state"],
                "review_label": review["label"],
                "review_summary": review["summary"],
                "review_notice": review_notice,
            }
        )
    cards.sort(key=lambda item: (item["artist"].casefold(), item["title"].casefold()))
    return cards


def _job_public(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "phase": job.phase,
        "message": job.message,
        "error": job.error,
        "progress": job.progress or {},
        "result": job.result or {},
        "created_at": job.created_at if hasattr(job, "created_at") else None,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "cancel_requested": bool(getattr(job, "cancel_requested", False)),
        "heartbeat_at": getattr(job, "heartbeat_at", None),
    }


def _owned_workspace(db: Database, user: Any, workspace_id: int) -> Any:
    row = db.get_workspace(workspace_id)
    if row is None or row["workspace_key"] != user_workspace_key(user["spotify_user_id"]):
        return None
    return row


def _workspace_public(db: Database, row: Any) -> dict[str, Any]:
    workspace_id = int(row["id"])
    candidates = db.get_workspace_candidates(workspace_id, kind="exact", include_rejected=True)
    return {
        "id": workspace_id,
        "workspace_key": row["workspace_key"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "updated_at": row["updated_at"],
        "track_count": len(db.get_workspace_library_track_ids(workspace_id)),
        "candidate_count": len(candidates),
        "minimum_score": db.get_workspace_minimum_score(workspace_id),
    }


def _zip_response(generator, filename: str, size: int):
    from flask import Response

    headers = {
        "Content-Type": "application/zip",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    }
    if size:
        headers["Content-Length"] = str(size)
    return Response(generator, headers=headers)
