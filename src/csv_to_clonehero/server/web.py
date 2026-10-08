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
from csv_to_clonehero.matching import (
    ALTERNATE_RECORDING,
    BLOCKED,
    POSSIBLE_MATCH,
    RECOMMENDED_EXACT,
    SAME_RECORDING_CHART_VARIANT,
    SELECTABLE_RESULT_TIERS,
    classify_result_tier,
)
from csv_to_clonehero.normalization import normalize_artist, normalize_title
from csv_to_clonehero.server.auth import build_oauth, build_soundcloud_oauth, user_workspace_key
from csv_to_clonehero.server.downloads import BrowserDownloads, DownloadError, selection_snapshot
from csv_to_clonehero.server.jobs import JobManager
from csv_to_clonehero.server.safeguards import (
    get_discovery_availability,
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

    app.jinja_env.globals["now_year"] = str(datetime.now().year)

    downloads = BrowserDownloads(config, secret_key)
    jobs = JobManager(config, cipher, source_factory=source_factory, downloads=downloads)

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
        if (
            g.user is not None
            and config.server.enforce_secret_key
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.path.startswith("/api/")
        ):
            origin = str(request.headers.get("Origin") or "").rstrip("/")
            expected_origin = str(config.public_base_url or "").rstrip("/")
            if request.headers.get("X-Riff-Request") != "1" or (
                origin and expected_origin and origin != expected_origin
            ):
                return jsonify({"error": "request origin could not be verified"}), 403
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
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'self'; connect-src 'self'; "
                "font-src 'self'; form-action 'self'; frame-ancestors 'self'; "
                "img-src 'self' data: https:; object-src 'none'; "
                "style-src 'self'; script-src 'self'"
            )
        if secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
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
        return jsonify({
            "ok": True,
            "app": "csv-to-clonehero",
            "time": time.time(),
            "release": "riff-v2-20261008",
            "experience": "signal-check",
        })

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
        _require_user()
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
            if len(provider.import_library().tracks) > config.server.max_scope_tracks:
                return jsonify({"error": "SoundCloud export contains too many songs."}), 422
            db = _user_db()
            try:
                workspace = _request_workspace(db)
                from csv_to_clonehero.library_pipeline import run_library_import
                imported = run_library_import(config, db, workspace, {"soundcloud": provider})
                return jsonify({
                    "ok": True,
                    "workspace_id": workspace,
                    "source": imported.sources["soundcloud"].__dict__,
                    "sources": _public_source_rows(db, workspace),
                    "combined": imported.combined_summary,
                    "soundcloud_library": _provider_library_payload(
                        db, workspace, "soundcloud"
                    ),
                })
            finally:
                db.close()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 422

    @app.get("/")
    def index():
        next_path = request.args.get("next", "")
        if (
            next_path.startswith("/")
            and not next_path.startswith("//")
            and "\\" not in next_path
        ):
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
            return render_template("error.html", message=f"Spotify sign-in failed: {error}", config=config), 400
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
        except Exception:  # noqa: BLE001 - safe message to the browser
            log.warning("oauth callback failed")
            return render_template("error.html", message="Spotify sign-in was not completed; please try again.", config=config), 400
        try:
            profile = _fetch_profile(config, token)
            spotify_user_id = str(profile.get("id") or "").strip()
            if not spotify_user_id:
                raise ValueError("Spotify profile has no user ID")
        except Exception:  # noqa: BLE001 - safe OAuth/profile retry boundary
            log.warning("oauth callback could not load a valid Spotify profile")
            return render_template(
                "error.html",
                message="Spotify sign-in could not load your profile; please try again.",
                config=config,
            ), 502
        db = _user_db()
        try:
            user = db.upsert_user(
                spotify_user_id=spotify_user_id,
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
                    user_workspace_key(spotify_user_id),
                    name=profile.get("display_name", "") or spotify_user_id,
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
            return render_template("error.html", message="SoundCloud sign-in was cancelled.", config=config), 400
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
            return render_template("error.html", message="SoundCloud sign-in was not completed; please try again.", config=config), 400
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
            workspace_id = _ensure_workspace(db, user)
            oauth = build_oauth(config, int(user["id"]), cipher)
            client = _client_for(config, oauth)
            playlists = _list_playlists(client)
            liked = _count_liked(client) if hasattr(client, "get_liked_tracks") else 0
            refreshed_at = time.time()
            cache = {
                "playlists": playlists,
                "liked_count": liked,
                "refreshed_at": refreshed_at,
            }
            _set_spotify_library_cache(db, workspace_id, cache)
            return jsonify(_spotify_library_payload(
                db,
                workspace_id,
                live_cache=cache,
                cached=False,
            ))
        except Exception:  # noqa: BLE001 - preserve the last known cache
            log.warning("Spotify library refresh failed", exc_info=True)
            return jsonify({
                "error": "Spotify could not be refreshed. Your saved choices were not changed."
            }), 502
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
            return jsonify({"preflight": get_discovery_availability(config, db)})
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
            response = jsonify({"preflight": result})
            response.status_code = 200 if result.get("usable") else 503
            if result.get("retryable") and result.get("retry_after_seconds", 0) > 0:
                response.headers["Retry-After"] = str(result["retry_after_seconds"])
            return response
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
            active_expansion = db.get_active_job(
                int(user["id"]), workspace_key=key, kind="expansion"
            )
            if db.get_active_job(int(user["id"]), workspace_key=key, kind="download") is not None:
                return jsonify({"error": "Wait for ZIP preparation to finish before searching again."}), 409
            if active_expansion is not None:
                existing = jobs.get(int(active_expansion["id"]))
                return jsonify({
                    "error": "Wait for the active artist expansion to finish before searching again.",
                    "job": _job_public(existing) if existing is not None else None,
                }), 409
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

    # ---- optional catalog-only artist expansion -----------------------------
    @app.get("/api/expansion/artists")
    def expansion_artists():
        user = _require_user()
        if not config.server.enable_expansion:
            abort(404)
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            context = _public_expansion_context(config, db, workspace)
            prior_selected = {
                str(row["artist_norm"]): (
                    bool(row["selected"])
                    and int(_json_object(row["metadata_json"]).get("exact_generation_id") or 0)
                    == int(context.get("exact_generation_id") or 0)
                )
                for row in db.get_workspace_expansion_artists(workspace)
            }
            if context["available"]:
                db.replace_workspace_expansion_artists(workspace, [
                    {
                        "artist_name": item["artist"],
                        "artist_norm": item["artist_norm"],
                        "track_count": item["imported_songs"],
                        "cap": item["cap"],
                        "metadata": {
                            "catalog_songs": item["catalog_songs"],
                            "catalog_charts": item["catalog_charts"],
                            "eligible_songs": item["eligible_songs"],
                            "eligible_charts": item["eligible_charts"],
                            "exact_generation_id": context["exact_generation_id"],
                        },
                    }
                    for item in context["artists"]
                ])
                for row in db.get_workspace_expansion_artists(workspace):
                    db.set_expansion_artist_selected(
                        workspace,
                        str(row["artist_norm"]),
                        prior_selected.get(str(row["artist_norm"]), False),
                    )
            selected = {
                str(row["artist_norm"]): bool(row["selected"])
                for row in db.get_workspace_expansion_artists(workspace)
            }
            artists = [
                {**item, "selected": selected.get(item["artist_norm"], False)}
                for item in context["artists"]
            ]
            expansion_job = next(
                (
                    item for item in jobs.list_for_user(int(user["id"]), limit=20)
                    if item.get("kind") == "expansion"
                    and item.get("workspace_key") == user_workspace_key(user["spotify_user_id"])
                    and int((item.get("progress") or {}).get("exact_generation_id") or 0)
                    == int(context.get("exact_generation_id") or 0)
                ),
                None,
            )
            return jsonify({
                **context,
                "artists": artists,
                "max_artists": min(25, int(config.expansion.max_artists_per_batch)),
                "job": _job_mapping_public(expansion_job) if expansion_job else None,
            })
        finally:
            db.close()

    @app.post("/api/expansion")
    @app.post("/api/expansion/artists/select")
    def start_expansion():
        user = _require_user()
        if not config.server.enable_expansion:
            abort(404)
        payload = request.get_json(silent=True) or {}
        values = payload.get("artists")
        if not isinstance(values, list):
            return jsonify({"error": "artists must be a list"}), 400
        artists = []
        for value in values:
            norm = normalize_artist(str(value or ""))
            if norm and norm not in artists:
                artists.append(norm)
        maximum = min(25, int(config.expansion.max_artists_per_batch))
        if not artists:
            return jsonify({"error": "Choose at least one artist."}), 400
        if len(artists) > maximum:
            return jsonify({"error": f"Choose no more than {maximum} artists at a time."}), 400

        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            context = _public_expansion_context(config, db, workspace)
            if not context["available"]:
                return jsonify({"error": context["reason"]}), 409
            allowed = {item["artist_norm"] for item in context["artists"]}
            unknown = [artist for artist in artists if artist not in allowed]
            if unknown:
                return jsonify({"error": "Choose artists from the current eligible list."}), 400
            key = user_workspace_key(user["spotify_user_id"])
            active_discovery = db.get_active_job(
                int(user["id"]), workspace_key=key, kind="discovery"
            )
            if db.get_active_job(int(user["id"]), workspace_key=key, kind="download") is not None:
                return jsonify({"error": "Wait for ZIP preparation to finish before expanding artists."}), 409
            if active_discovery is not None:
                return jsonify({"error": "Wait for exact matching to finish first."}), 409
            job, created = jobs.enqueue_unique(
                int(user["id"]),
                "expansion",
                workspace_key=key,
                initial_progress={
                    "artists": artists,
                    "exact_generation_id": context["exact_generation_id"],
                    "total_artists": len(artists),
                    "completed_artists": 0,
                    "network_requests": 0,
                },
            )
            if not created:
                return jsonify({
                    "error": "An artist expansion job is already active for this workspace.",
                    "job": _job_public(job),
                }), 409
            for row in db.get_workspace_expansion_artists(workspace):
                db.set_expansion_artist_selected(
                    workspace, str(row["artist_norm"]), str(row["artist_norm"]) in artists
                )
            return jsonify({"job": _job_public(job)}), 202
        finally:
            db.close()

    @app.get("/api/expansion/results")
    def expansion_results():
        user = _require_user()
        if not config.server.enable_expansion:
            abort(404)
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            cards = _expansion_review_cards(config, db, workspace)
            limit = min(100, max(1, int(request.args.get("limit", 25))))
            offset = max(0, int(request.args.get("offset", 0)))
            selected = sum(int(card.get("selected_count") or 0) for card in cards)
            return jsonify({
                "songs": cards[offset:offset + limit],
                "total": len(cards),
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < len(cards),
                "summary": {"total": len(cards), "selected": selected},
            })
        except (TypeError, ValueError):
            return jsonify({"error": "invalid expansion paging parameters"}), 400
        finally:
            db.close()

    @app.get("/api/jobs/<int:job_id>")
    def job_status(job_id):
        user = _require_user()
        job = jobs.get(job_id)
        if job is None or job.user_id != int(user["id"]):
            abort(404)
        return jsonify({"job": _job_public(job)})

    @app.get("/api/jobs/<int:job_id>/songs")
    def job_songs(job_id):
        """Return a bounded, card-free page of matching activity rows."""
        user = _require_user()
        job = jobs.get(job_id)
        if job is None or job.user_id != int(user["id"]):
            abort(404)
        db = _user_db()
        try:
            workspace = db.get_workspace(job.workspace_key)
            if workspace is None:
                return jsonify({"songs": [], "total": 0, "offset": 0, "limit": 0})
            limit = min(100, max(1, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
            search = str(request.args.get("search", ""))[:200]
            rows = db.get_discovery_song_progress(
                workspace["id"], limit=limit, offset=offset, search=search
            )
            return jsonify({
                "songs": [dict(row) for row in rows],
                "total": db.count_discovery_song_progress(workspace["id"], search=search),
                "offset": offset, "limit": limit,
            })
        except (TypeError, ValueError):
            return jsonify({"error": "invalid song list parameters"}), 400
        finally:
            db.close()

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel_job(job_id):
        user = _require_user()
        job = jobs.cancel(job_id, int(user["id"]))
        if job is None:
            abort(404)
        return jsonify({"job": _job_public(job)})

    # ---- workspace + review ---------------------------------------------------
    @app.get("/api/journey")
    def journey():
        """Return the authenticated user's persisted public-workflow snapshot."""
        user = _require_user()
        db = _user_db()
        try:
            workspace_id = _ensure_workspace(db, user)
            row = db.get_workspace(workspace_id)
            minimum_score = db.get_workspace_minimum_score(workspace_id)
            cards = _review_cards(config, db, workspace_id, minimum_score)
            scope = db.get_workspace_scope_data(workspace_id)
            source_rows = _public_source_rows(db, workspace_id)
            key = user_workspace_key(user["spotify_user_id"])
            journey_jobs = [
                jobs.latest_for_user_workspace(int(user["id"]), workspace_key=key, kind=kind)
                for kind in ("discovery", "expansion")
            ]
            newest_job = max((job for job in journey_jobs if job is not None), key=lambda job: job.id, default=None)
            download_job = jobs.latest_for_user_workspace(
                int(user["id"]),
                workspace_key=key,
                kind="download",
            )
            download = downloads.latest_for_user_workspace(int(user["id"]), workspace_id)
            if download is not None:
                download["url"] = url_for("stream_download", token=download["token"])
            return jsonify({
                "workspace_id": workspace_id,
                "workspace_status": str(row["status"] or "") if row is not None else "",
                "workspace": {
                    "id": workspace_id,
                    "status": str(row["status"] or "") if row is not None else "",
                    "phase": str(row["phase"] or "") if row is not None else "",
                    "updated_at": row["updated_at"] if row is not None else None,
                },
                "spotify_scope": _public_scope(scope),
                "spotify_library": _spotify_library_payload(db, workspace_id),
                "soundcloud_library": _provider_library_payload(
                    db, workspace_id, "soundcloud"
                ),
                "sources": source_rows,
                "combined": db.get_combined_summary(workspace_id),
                "job": _job_public(newest_job) if newest_job is not None else None,
                "download_job": _job_public(download_job) if download_job is not None else None,
                "review": _review_summary(cards),
                "download": download,
            })
        finally:
            db.close()

    @app.get("/api/workspace")
    def workspace():
        user = _require_user()
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            minimum_score = db.get_workspace_minimum_score(workspace) if workspace else 50.0
            cards = _review_cards(config, db, workspace, minimum_score)
            summary = _review_summary(cards)
            summary["selection_count"] = summary["selected_charts"]
            states = {
                value.strip().casefold()
                for raw in [*request.args.getlist("state"), *request.args.getlist("review_state")]
                for value in raw.split(",")
                if value.strip()
            }
            tiers = {
                value.strip().upper()
                for raw in request.args.getlist("tier")
                for value in raw.split(",")
                if value.strip()
            }
            selected_arg = request.args.get("selected")
            selected: bool | None = None
            if selected_arg is not None:
                normalized = selected_arg.strip().casefold()
                if normalized in {"1", "true", "yes", "selected"}:
                    selected = True
                elif normalized in {"0", "false", "no", "unselected"}:
                    selected = False
                else:
                    raise ValueError("selected must be true or false")
            search = str(request.args.get("search") or request.args.get("query") or "").strip().casefold()[:200]
            filtered = [
                card for card in cards
                if (
                    not states
                    or str(card.get("review_state") or "").casefold() in states
                    or _card_review_bucket(card) in states
                )
                and (selected is None or bool(card.get("selected")) is selected)
                and (
                    not tiers
                    or any(
                        str(candidate.get("result_tier") or "").upper() in tiers
                        for candidate in card.get("candidates") or ()
                    )
                )
                and (not search or _card_matches_search(card, search))
            ]
            limit = min(100, max(1, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
            return jsonify({
                "workspace": workspace,
                "minimum_score": minimum_score,
                "tracks": filtered[offset:offset + limit],
                "total": len(filtered),
                "offset": offset,
                "limit": limit,
                "summary": summary,
            })
        except (TypeError, ValueError):
            return jsonify({"error": "invalid workspace paging or filter parameters"}), 400
        finally:
            db.close()

    @app.post("/api/selection")
    def selection():
        user = _require_user()
        payload = request.get_json(silent=True) or {}
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            if db.get_active_job(
                int(user["id"]),
                workspace_key=user_workspace_key(user["spotify_user_id"]),
                kind="download",
            ) is not None:
                return jsonify({"error": "Wait for ZIP preparation to finish before changing chart choices."}), 409
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
                generation_id = _public_generation_id(db, workspace)
                matching_rows = db.get_workspace_candidates(
                    workspace,
                    track_id=track_value,
                    kind=candidate_kind,
                    include_rejected=True,
                    generation_id=generation_id if candidate_kind == "exact" else None,
                )
                candidate_row = next(
                    (
                        row for row in matching_rows
                        if int(row["candidate_id"]) == candidate_id
                        and (
                            candidate_kind == "exact"
                            or not artist_norm
                            or str(row["artist_norm"]) == normalize_artist(artist_norm)
                        )
                    ),
                    None,
                )
                if candidate_row is None:
                    raise ValueError("candidate does not belong to this workspace track")
                candidate = db.get_candidate(candidate_id)
                if candidate_kind == "exact":
                    track = _track_by_id(db, candidate_row["track_id"])
                    current = _current_match(config, track, candidate)
                else:
                    row_metadata = _json_object(candidate_row["metadata_json"])
                    if int(row_metadata.get("exact_generation_id") or 0) != int(generation_id or 0):
                        raise ValueError("artist recommendation is stale; run expansion again")
                    artist_norm = str(candidate_row["artist_norm"] or "")
                    identity = (
                        normalize_artist(str(candidate.artist or "")),
                        normalize_title(str(candidate.name or "")),
                    )
                    if identity in _workspace_imported_identities(db, workspace):
                        raise ValueError("song is already represented by the imported library")
                    current = _current_expansion_match(config, artist_norm, candidate)
                current_eligibility, current_hard_gate, current_score, _current_quality, current_breakdown = current
                if current_hard_gate or current_eligibility == "INCOMPATIBLE":
                    raise ValueError("candidate is blocked by a hard compatibility gate")
                result_tier, tier_reasons = classify_result_tier(
                    current_breakdown, kind=candidate_kind
                )
                if result_tier == BLOCKED:
                    raise ValueError("candidate is blocked by a hard compatibility gate")
                if result_tier == POSSIBLE_MATCH and not bool(
                    payload.get("confirm_possible")
                ):
                    raise ValueError("possible matches require explicit confirmation")
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
                if current_score < threshold and result_tier not in SELECTABLE_RESULT_TIERS:
                    raise ValueError(f"candidate score is below the workspace threshold ({threshold:g})")
                db.select_workspace_candidate(
                    workspace,
                    track_value,
                    candidate_id,
                    kind=candidate_kind,
                    artist_norm=artist_norm,
                    force=True,
                    require_workspace_candidate=True,
                    metadata={
                        "result_tier": result_tier,
                        "result_tier_reasons": tier_reasons,
                        "possible_confirmed": bool(payload.get("confirm_possible")),
                        "selection_origin": "explicit",
                    },
                )
            return jsonify({"ok": True})
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
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
            if db.get_active_job(
                int(user["id"]),
                workspace_key=user_workspace_key(user["spotify_user_id"]),
                kind="download",
            ) is not None:
                return jsonify({"error": "Wait for ZIP preparation to finish before changing chart choices."}), 409
            threshold = db.get_workspace_minimum_score(workspace)
            changed = 0
            if action == "select":
                scope = str(
                    payload.get("preset")
                    or payload.get("scope")
                    or payload.get("selection_scope")
                    or payload.get("filter")
                    or "strong"
                ).casefold()
                if scope in {"recommended", "auto", "auto_accept", "strong", "best_matches"}:
                    allowed_tiers = {RECOMMENDED_EXACT}
                    scope = "best_matches"
                elif scope in {"all_safe_charts", "safe", "eligible"}:
                    allowed_tiers = {RECOMMENDED_EXACT, SAME_RECORDING_CHART_VARIANT}
                    scope = "all_safe_charts"
                elif scope in {"maximum_playable", "maximum", "max"}:
                    allowed_tiers = {
                        RECOMMENDED_EXACT,
                        SAME_RECORDING_CHART_VARIANT,
                        ALTERNATE_RECORDING,
                    }
                    scope = "maximum_playable"
                else:
                    return jsonify({"error": "unknown selection preset"}), 400
                db.clear_workspace_selection(workspace, kind="exact")
                requested = payload.get("track_ids")
                if isinstance(requested, (str, bytes)):
                    requested = [requested]
                requested_ids = {str(value) for value in requested} if requested is not None else None
                needle = str(payload.get("search") or payload.get("query") or "").strip().casefold()
                selected_rows: list[tuple[dict[str, Any], str]] = []
                generation_id = _public_generation_id(db, workspace)
                rows = db.get_workspace_candidates(
                    workspace,
                    kind="exact",
                    include_rejected=True,
                    generation_id=generation_id,
                )
                for row in rows:
                    data = dict(row)
                    if requested_ids is not None and not {
                        str(data.get("track_id")),
                        str(data.get("spotify_id")),
                        str(data.get("track_key")),
                    } & requested_ids:
                        continue
                    candidate = db.get_candidate(int(data.get("candidate_id") or 0))
                    track = _track_by_id(db, data.get("track_id"))
                    eligibility, hard_gate, score, quality, current_breakdown = _current_match(
                        config, track, candidate
                    )
                    if hard_gate:
                        continue
                    tier = classify_result_tier(current_breakdown)[0]
                    if tier not in allowed_tiers:
                        continue
                    if needle and needle not in " ".join(
                        str(data.get(key) or "")
                        for key in ("track_artist", "track_title", "source", "chart_id")
                    ).casefold():
                        candidate = _json_object(data.get("candidate_json"), {})
                        if needle not in " ".join(
                            str(candidate.get(key) or "")
                            for key in ("artist", "name", "charter", "url")
                        ).casefold():
                            continue
                    data["_rank"] = (score, quality, -int(data.get("candidate_id") or 0))
                    selected_rows.append((data, tier))
                if scope == "best_matches":
                    best_by_track: dict[int, tuple[dict[str, Any], str]] = {}
                    for data, tier in selected_rows:
                        track_id = int(data.get("track_id") or 0)
                        if track_id not in best_by_track or data["_rank"] > best_by_track[track_id][0]["_rank"]:
                            best_by_track[track_id] = (data, tier)
                    selected_rows = list(best_by_track.values())
                for row, tier in selected_rows:
                    try:
                        db.select_workspace_candidate(
                            workspace,
                            row["track_id"],
                            int(row["candidate_id"]),
                            kind="exact",
                            force=True,
                            require_workspace_candidate=True,
                            metadata={"selection_origin": "preset", "preset": scope, "result_tier": tier},
                        )
                        changed += 1
                    except ValueError:
                        continue
            else:
                changed = db.clear_workspace_selection(workspace, kind="exact")
            return jsonify({"ok": True, "changed": changed, "minimum_score": threshold, "scope": scope if action == "select" else "all"})
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
            key = user_workspace_key(user["spotify_user_id"])
            for kind in ("discovery", "expansion"):
                if db.get_active_job(int(user["id"]), workspace_key=key, kind=kind) is not None:
                    return jsonify({"error": "Wait for matching to finish before preparing a ZIP."}), 409
            snapshot = selection_snapshot(db, workspace)
            if not snapshot["selection_count"]:
                return jsonify({"error": "No charts selected yet; pick at least one chart to download."}), 400
            if snapshot["generation_id"] < 0:
                return jsonify({"error": "Run matching again before preparing a ZIP."}), 409
            job, created = jobs.enqueue_unique(
                int(user["id"]), "download", workspace_key=key,
                initial_progress=snapshot,
            )
            return jsonify({"job": _job_public(job), "created": created}), 202
        finally:
            db.close()

    @app.get("/api/download/<path:token>")
    def stream_download(token):
        user = _require_user()
        try:
            meta = downloads.resolve(token)
            if meta is None or int(meta.get("user_id", -1)) != int(user["id"]):
                abort(404)
            filename = "riff-library.zip"
            generator = downloads.stream(token, user_id=int(user["id"]))
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
    for playlist in method():
        items.append(
            {
                "id": str(getattr(playlist, "id", "") or ""),
                "name": str(getattr(playlist, "name", "") or ""),
                "track_count": getattr(playlist, "total_tracks", None),
                "owner_name": str(getattr(playlist, "owner_name", "") or ""),
            }
        )
    return items


def _count_liked(client: Any) -> int:
    count_method = getattr(client, "get_saved_tracks_count", None)
    if callable(count_method):
        return max(0, int(count_method()))
    method = getattr(client, "get_liked_tracks", None)
    if not callable(method):
        return 0
    return len(list(method()))


def _fetch_profile(config: AppConfig, token: Any) -> dict[str, Any]:
    import httpx

    headers = {"Authorization": f"Bearer {token.access_token}"}
    response = httpx.get("https://api.spotify.com/v1/me", headers=headers, timeout=20)
    response.raise_for_status()
    profile = response.json()
    if not isinstance(profile, dict):
        raise ValueError("Spotify profile response is invalid")
    return profile


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


def _spotify_library_payload(
    db: Database,
    workspace_id: int,
    *,
    live_cache: dict[str, Any] | None = None,
    cached: bool = True,
) -> dict[str, Any]:
    """Return saved Spotify choices and import truth without provider traffic."""
    workspace = db.get_workspace(workspace_id)
    metadata = _json_object(workspace["metadata_json"] if workspace is not None else "{}")
    stored_cache = metadata.get("spotify_library_cache")
    stored_cache = stored_cache if isinstance(stored_cache, dict) else {}
    cache = live_cache if live_cache is not None else stored_cache
    source_rows = [dict(row) for row in db.get_source_summary(workspace_id, "spotify")]
    provider_rows = db.get_provider_summaries(workspace_id)
    provider = next(
        (row for row in provider_rows if str(row.get("provider") or "") == "spotify"),
        None,
    )

    playlists: list[dict[str, Any]] = []
    raw_playlists = cache.get("playlists") if isinstance(cache, dict) else None
    if isinstance(raw_playlists, list):
        for item in raw_playlists:
            if not isinstance(item, dict):
                continue
            playlist_id = str(item.get("id") or "").strip()
            if not playlist_id:
                continue
            playlists.append({
                "id": playlist_id,
                "name": str(item.get("name") or "Untitled playlist"),
                "track_count": _optional_count(item.get("track_count")),
                "owner_name": str(item.get("owner_name") or ""),
            })

    cache_source = "spotify" if live_cache is not None or stored_cache else "latest_import"
    if not playlists and not stored_cache:
        for row in db.get_library_playlists(workspace_id, "spotify"):
            playlist_metadata = _json_object(row["metadata_json"])
            track_count = playlist_metadata.get(
                "total_tracks", playlist_metadata.get("item_count", row["track_count"])
            )
            playlists.append({
                "id": str(row["provider_id"]),
                "name": str(row["name"] or "Untitled playlist"),
                "track_count": _optional_count(track_count),
                "owner_name": str(playlist_metadata.get("owner_name") or ""),
            })

    scope = db.get_workspace_scope_data(workspace_id)
    selected_ids = {
        str(value) for value in (scope.get("playlist_ids") or ()) if str(value)
    } if scope else set()
    known_ids = {item["id"] for item in playlists}
    for playlist_id in sorted(str(value) for value in selected_ids - known_ids):
        playlists.append({
            "id": playlist_id,
            "name": "Saved playlist",
            "track_count": None,
            "owner_name": "",
        })

    imported_scope, last_imported_at = _imported_scope(source_rows)

    import_cache_available = any(row.get("import_id") is not None for row in source_rows)
    cache_available = bool(stored_cache) or import_cache_available
    refreshed_at = float(cache.get("refreshed_at") or 0) if isinstance(cache, dict) else 0.0
    if not refreshed_at and source_rows:
        refreshed_at = max(float(row.get("updated_at") or 0) for row in source_rows)
    liked_count = cache.get("liked_count") if stored_cache or live_cache is not None else None
    known_unique_songs = int(provider.get("unique_tracks") or 0) if provider else None

    return {
        "playlists": sorted(playlists, key=lambda item: (item["name"].casefold(), item["id"])),
        "liked_count": _optional_count(liked_count),
        "workspace": {
            "id": workspace_id,
            "exists": workspace is not None,
            "scope": _public_scope(scope),
            "soundcloud_scope": metadata.get("soundcloud_scope", {}),
        },
        "cache_available": cache_available,
        "cached": bool(cached),
        "cache_source": cache_source if cache_available else "none",
        "last_refreshed_at": refreshed_at or None,
        "last_imported_at": last_imported_at or None,
        "known_unique_songs": known_unique_songs,
        "import_status": str(provider.get("status") or "") if provider else "",
        "imported_scope": imported_scope,
    }


def _public_source_rows(db: Database, workspace_id: int) -> list[dict[str, Any]]:
    return [
        {
            "provider": source["provider"],
            "status": source["status"],
            "tracks": int(source["tracks"] or 0),
            "unique_tracks": int(source["unique_tracks"] or 0),
            "provenance": int(source["provenance"] or 0),
            "playlists": int(source["playlist_count"] or 0),
            "playlist_memberships": int(source["membership_count"] or 0),
            "playlist_unique_tracks": int(source["playlist_unique_tracks"] or 0),
            "liked": int(source["liked_count"] or 0),
            "liked_in_playlists": int(source["liked_in_playlists"] or 0),
            "playlist_only": int(source["playlist_only_tracks"] or 0),
            "liked_only": int(source["liked_only_tracks"] or 0),
            "raw_items": int(source["source_item_count"] or 0),
            "raw_items_lower_bound": bool(source["source_item_count_is_lower_bound"]),
            "accepted_items": int(source["accepted_rows"] or 0),
            "skipped_items": int(source["skipped_rows"] or 0),
            "duplicate_items": int(source["duplicate_rows"] or 0),
            "include_liked": bool(source["include_liked"]),
            "updated_at": source["updated_at"],
        }
        for source in db.get_provider_summaries(workspace_id)
    ]


def _provider_library_payload(
    db: Database, workspace_id: int, provider_name: str
) -> dict[str, Any]:
    source_rows = [
        dict(row) for row in db.get_source_summary(workspace_id, provider_name)
    ]
    provider = next(
        (
            row for row in db.get_provider_summaries(workspace_id)
            if str(row.get("provider") or "") == provider_name
        ),
        None,
    )
    imported_scope, last_imported_at = _imported_scope(source_rows)
    workspace = db.get_workspace(workspace_id)
    metadata = _json_object(workspace["metadata_json"] if workspace is not None else "{}")
    current_scope = metadata.get(f"{provider_name}_scope")
    current_scope = current_scope if isinstance(current_scope, dict) else {}
    return {
        "status": str(provider.get("status") or "") if provider else "",
        "known_unique_songs": int(provider.get("unique_tracks") or 0) if provider else None,
        "last_imported_at": last_imported_at or None,
        "current_scope": {
            "playlist_ids": sorted(
                str(value) for value in current_scope.get("playlist_ids", ()) if str(value)
            ),
            "include_liked": bool(current_scope.get("include_liked")),
        },
        "imported_scope": imported_scope,
    }


def _imported_scope(
    source_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], float]:
    imported_rows = [row for row in source_rows if row.get("import_id") is not None]
    playlist_ids: set[str] = set()
    include_liked = False
    last_imported_at = 0.0
    for row in imported_rows:
        source_scope = _json_object(row.get("scope_json"))
        playlist_ids.update(
            str(value) for value in source_scope.get("playlist_ids", ()) if str(value)
        )
        include_liked = include_liked or bool(source_scope.get("include_liked"))
        last_imported_at = max(
            last_imported_at,
            float(row.get("completed_at") or row.get("updated_at") or 0),
        )
    complete = bool(imported_rows) and all(
        str(row.get("import_status") or "").casefold()
        in {"complete", "completed", "success"}
        for row in imported_rows
    )
    return {
        "known": complete,
        "playlist_ids": sorted(playlist_ids),
        "include_liked": include_liked,
    }, last_imported_at


def _set_spotify_library_cache(
    db: Database, workspace_id: int, cache: dict[str, Any]
) -> None:
    workspace = db.get_workspace(workspace_id)
    metadata = _json_object(workspace["metadata_json"] if workspace is not None else "{}")
    metadata["spotify_library_cache"] = cache
    db.update_workspace(workspace_id, metadata=metadata)


def _optional_count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


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
    return int(db.create_workspace(
        workspace_key=key,
        name=str(user["display_name"] or user["spotify_user_id"]),
        kind="exact",
        status="created",
        phase="source_setup",
        metadata={"workflow": "public_exact"},
    ))


def _track_by_id(db: Database, track_id: Any) -> Any | None:
    try:
        expected = int(track_id)
    except (TypeError, ValueError):
        return None
    return next(
        (track for track in db.get_spotify_tracks(True) if int(track.db_id or 0) == expected),
        None,
    )


def _public_generation_id(db: Database, workspace_id: int) -> int | None:
    """Use -1 to fail closed when the newest public scan is not complete."""
    latest = db.get_latest_exact_generation(workspace_id)
    if latest is None:
        return None
    current = db.get_current_exact_generation(workspace_id)
    return int(current["id"]) if current is not None else -1


def _public_expansion_context(
    config: AppConfig,
    db: Database,
    workspace_id: int,
) -> dict[str, Any]:
    if not config.server.enable_expansion:
        return {"enabled": False, "available": False, "reason": "Artist expansion is disabled.", "artists": []}
    workspace = db.get_workspace(workspace_id)
    generation = db.get_current_exact_generation(workspace_id)
    metadata = _json_object(workspace["metadata_json"] if workspace is not None else "{}")
    if generation is None or metadata.get("workflow") != "public_catalog":
        return {
            "enabled": True,
            "available": False,
            "reason": "Complete catalog-backed exact matching before choosing artists.",
            "artists": [],
            "exact_generation_id": None,
        }
    from csv_to_clonehero.catalog import load_enchor_catalog
    from csv_to_clonehero.combined_pipeline import workspace_expansion_choices

    catalog = load_enchor_catalog(
        config.catalog_path,
        excluded_identities=db.get_blocked_chart_identities(),
        validation_fingerprint=db.chart_validation_fingerprint(),
    )
    if not catalog.available:
        return {
            "enabled": True,
            "available": False,
            "reason": catalog.diagnostic or "The verified chart catalog is unavailable.",
            "artists": [],
            "exact_generation_id": int(generation["id"]),
        }
    return {
        "enabled": True,
        "available": True,
        "reason": "",
        "artists": workspace_expansion_choices(
            config, db, workspace_id, catalog=catalog
        ),
        "exact_generation_id": int(generation["id"]),
        "catalog_snapshot_id": str(catalog.snapshot_id or ""),
    }


def _workspace_imported_identities(
    db: Database, workspace_id: int
) -> set[tuple[str, str]]:
    identities = {
        (str(row["normalized_artist"]), str(row["normalized_title"]))
        for row in db.get_neutral_library_tracks(workspace_id)
    }
    workspace_track_ids = db.get_workspace_library_track_ids(workspace_id)
    identities.update({
        (track.primary_artist_norm, track.title_norm)
        for track in db.get_spotify_tracks(True)
        if int(track.db_id or 0) in workspace_track_ids
    })
    return identities


def _current_match(
    config: AppConfig,
    track: Any,
    candidate: Any,
) -> tuple[str, bool, float, float, dict[str, Any]]:
    """Reapply the current matcher before any public recommendation or choice."""
    if track is None or candidate is None:
        return "INCOMPATIBLE", True, 0.0, 0.0, {
            "eligibility": "INCOMPATIBLE",
            "reasons": ["song or chart metadata is unavailable"],
            "factors": {"gates": {"hard_gate": True}},
        }
    try:
        from csv_to_clonehero.matching import score_candidate

        scored = score_candidate(track, candidate, config)
        breakdown = scored.as_dict() if hasattr(scored, "as_dict") else {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        eligibility = str(getattr(scored, "eligibility", "") or "EXCLUDED").upper()
        gates = getattr(scored, "gate_factors", {}) or {}
        return (
            eligibility,
            bool(gates.get("hard_gate")),
            float(getattr(scored, "final_score", 0.0) or 0.0),
            float(getattr(scored, "quality", 0.0) or 0.0),
            breakdown,
        )
    except Exception:  # noqa: BLE001 - fail closed at the public trust boundary
        log.warning("current public match evaluation failed", exc_info=True)
        return "INCOMPATIBLE", True, 0.0, 0.0, {
            "eligibility": "INCOMPATIBLE",
            "reasons": ["current compatibility check could not be completed"],
            "factors": {"gates": {"hard_gate": True}},
        }


def _current_expansion_match(
    config: AppConfig,
    artist_norm: str,
    candidate: Any,
) -> tuple[str, bool, float, float, dict[str, Any]]:
    """Reapply expansion-specific gates without treating it as an exact song."""
    if not artist_norm or candidate is None:
        return "INCOMPATIBLE", True, 0.0, 0.0, {
            "eligibility": "INCOMPATIBLE",
            "reasons": ["artist or chart metadata is unavailable"],
            "factors": {"gates": {"hard_gate": True}},
        }
    try:
        from csv_to_clonehero.matching import score_expansion_candidate

        scored = score_expansion_candidate(artist_norm, candidate, config)
        breakdown = scored.as_dict()
        eligibility = str(scored.eligibility or "EXCLUDED").upper()
        return (
            eligibility,
            bool(scored.gate_factors.get("hard_gate")),
            float(scored.final_score or 0),
            float(scored.quality or 0),
            breakdown,
        )
    except Exception:  # noqa: BLE001 - fail closed at the public trust boundary
        log.warning("current public expansion evaluation failed", exc_info=True)
        return "INCOMPATIBLE", True, 0.0, 0.0, {
            "eligibility": "INCOMPATIBLE",
            "reasons": ["current expansion compatibility check could not be completed"],
            "factors": {"gates": {"hard_gate": True}},
        }


def _public_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    return {
        "playlist_ids": list(scope.get("playlist_ids") or []),
        "include_liked": bool(scope.get("include_liked")),
        "track_ids": list(scope.get("track_ids") or []),
        "updated_at": scope.get("updated_at"),
    }


def _review_summary(cards: list[dict[str, Any]]) -> dict[str, int]:
    strong = sum(card.get("review_state") == "strong" for card in cards)
    review = sum(card.get("review_state") == "needs_review" for card in cards)
    selected_strong = sum(
        bool(card.get("selected")) and card.get("review_state") == "strong"
        for card in cards
    )
    selected_review = sum(
        bool(card.get("selected")) and card.get("review_state") == "needs_review"
        for card in cards
    )
    selected_charts = sum(int(card.get("selected_count") or 0) for card in cards)
    recommended_songs = sum(
        any(candidate.get("result_tier") == RECOMMENDED_EXACT for candidate in card.get("candidates") or ())
        for card in cards
    )
    selectable_charts = sum(
        candidate.get("result_tier") in SELECTABLE_RESULT_TIERS
        for card in cards
        for candidate in card.get("candidates") or ()
    )
    tier_counts = {
        tier: sum(
            candidate.get("result_tier") == tier
            for card in cards
            for candidate in card.get("candidates") or ()
        )
        for tier in (
            RECOMMENDED_EXACT,
            SAME_RECORDING_CHART_VARIANT,
            ALTERNATE_RECORDING,
            POSSIBLE_MATCH,
            BLOCKED,
        )
    }
    return {
        "strong": strong,
        "review": review,
        "unavailable": len(cards) - strong - review,
        "selected": selected_strong + selected_review,
        "selected_tracks": sum(bool(card.get("selected")) for card in cards),
        "selected_charts": selected_charts,
        "recommended_songs": recommended_songs,
        "additional_playable_charts": max(0, selectable_charts - recommended_songs),
        "selectable_charts": selectable_charts,
        "all_safe_charts": tier_counts[RECOMMENDED_EXACT] + tier_counts[SAME_RECORDING_CHART_VARIANT],
        "maximum_playable_charts": tier_counts[RECOMMENDED_EXACT] + tier_counts[SAME_RECORDING_CHART_VARIANT] + tier_counts[ALTERNATE_RECORDING],
        "possible_match_charts": tier_counts[POSSIBLE_MATCH],
        "tier_histogram": tier_counts,
        "selected_strong": selected_strong,
        "selected_review": selected_review,
        "total": len(cards),
    }


def _card_matches_search(card: dict[str, Any], needle: str) -> bool:
    values = [card.get("artist"), card.get("title")]
    for candidate in card.get("candidates") or []:
        values.extend(
            candidate.get(key)
            for key in ("artist", "title", "chart_id", "charter")
        )
    return needle in " ".join(str(value or "") for value in values).casefold()


def _card_review_bucket(card: dict[str, Any]) -> str:
    state = str(card.get("review_state") or "").casefold()
    if state == "strong":
        return "strong"
    if state == "needs_review":
        return "review"
    return "unavailable"


def _job_mapping_public(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job.get(key)
        for key in (
            "id", "kind", "status", "phase", "message", "error", "progress",
            "created_at", "started_at", "finished_at",
            "cancel_requested", "heartbeat_at",
        )
    }


def _review_cards(
    config: AppConfig,
    db: Database,
    workspace: int,
    minimum_score: float = 50.0,
) -> list[dict[str, Any]]:
    if not workspace:
        return []
    generation_id = _public_generation_id(db, workspace)
    rows = db.get_workspace_candidates(
        workspace,
        kind="exact",
        include_rejected=True,
        generation_id=generation_id,
    )
    tracks_by_id = {
        int(track.db_id): track
        for track in db.get_spotify_tracks(True)
        if track.db_id is not None
    }
    selection_metadata = {
        (int(row["track_id"] or 0), int(row["candidate_id"] or 0)):
            _json_object(row["metadata_json"])
        for row in db.list_workspace_selections(workspace, kind="exact")
    }
    by_track: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row) if hasattr(row, "keys") else {}
        tid = data.get("track_id")
        if not tid:
            continue
        candidate = db.get_candidate(int(data.get("candidate_id") or 0))
        if candidate is None or not db.is_chart_artifact_eligible(
            candidate.source, candidate.chart_id
        ):
            continue
        current_eligibility, _hard_gate, current_score, current_quality, breakdown = _current_match(
            config,
            tracks_by_id.get(int(tid)),
            candidate,
        )
        result_tier, tier_reasons = classify_result_tier(breakdown)
        selected = bool(data.get("selected"))
        selected_metadata = selection_metadata.get(
            (int(tid), int(data.get("candidate_id") or 0)), {}
        )
        if (
            result_tier == POSSIBLE_MATCH
            and selected_metadata.get("selection_origin")
            and not selected_metadata.get("possible_confirmed")
        ):
            selected = False
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
            "score": round(current_score, 1),
            "quality": round(current_quality, 1),
            # Raw fields remain available to existing API consumers. The
            # ``review`` object is the only shape the human-facing UI needs.
            "eligibility": current_eligibility,
            "explanation": breakdown.get("explanation") or "",
            "selected": selected,
            "result_tier": result_tier,
            "result_tier_reasons": tier_reasons,
        }
        item["review"] = build_review_presentation(
            {**item, "breakdown": breakdown},
            minimum_score=minimum_score,
        )
        item["review_state"] = item["review"]["state"]
        item["review_label"] = item["review"]["label"]
        item["review_summary"] = item["review"]["summary"]
        item["selectable"] = item["result_tier"] in SELECTABLE_RESULT_TIERS
        item["selected"] = bool(item["selected"] and item["selectable"])
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
        tier_order = {
            RECOMMENDED_EXACT: 0,
            SAME_RECORDING_CHART_VARIANT: 1,
            ALTERNATE_RECORDING: 2,
            POSSIBLE_MATCH: 3,
            BLOCKED: 4,
        }
        candidates.sort(key=lambda item: (
            tier_order.get(item["result_tier"], 5),
            -item["score"], item["source"], str(item["chart_id"]),
        ))
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
                if (
                    item is not best
                    and item["result_tier"] == RECOMMENDED_EXACT
                ):
                    item["result_tier"] = SAME_RECORDING_CHART_VARIANT
                    item["result_tier_reasons"] = [
                        "additional chart artifact for this recording"
                    ]
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
                "selected_candidate_ids": [
                    item["candidate_id"] for item in candidates if item["selected"]
                ],
                "selected_count": sum(item["selected"] for item in candidates),
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


def _expansion_review_cards(
    config: AppConfig,
    db: Database,
    workspace: int,
) -> list[dict[str, Any]]:
    """Build expansion-only cards and independently reapply current gates."""
    generation_id = _public_generation_id(db, workspace)
    if generation_id is None or generation_id < 0:
        return []
    minimum_score = db.get_workspace_minimum_score(workspace)
    imported_identities = _workspace_imported_identities(db, workspace)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in db.get_workspace_candidates(
        workspace, kind="expansion", include_rejected=True
    ):
        data = dict(row)
        metadata = _json_object(data.get("metadata_json"))
        if int(metadata.get("exact_generation_id") or 0) != generation_id:
            continue
        candidate = db.get_candidate(int(data.get("candidate_id") or 0))
        if candidate is None or not db.is_chart_artifact_eligible(
            candidate.source, candidate.chart_id
        ):
            continue
        artist_norm = str(data.get("artist_norm") or "")
        eligibility, hard_gate, score, quality, breakdown = _current_expansion_match(
            config, artist_norm, candidate
        )
        result_tier, tier_reasons = classify_result_tier(
            breakdown, kind="expansion"
        )
        selectable = not hard_gate and result_tier in SELECTABLE_RESULT_TIERS
        identity = (
            normalize_artist(str(candidate.artist or "")),
            normalize_title(str(candidate.name or "")),
        )
        if not all(identity) or identity in imported_identities:
            continue
        selected = bool(data.get("selected") and selectable)
        concerns = list(breakdown.get("reasons") or ())
        if eligibility == "ELIGIBLE_REVIEW" and not concerns:
            concerns = ["This playable chart has lower quality evidence and merits a quick review."]
        item = {
            "candidate_id": int(data["candidate_id"]),
            "kind": "expansion",
            "artist_norm": artist_norm,
            "source": candidate.source,
            "chart_id": candidate.chart_id,
            "artist": candidate.artist,
            "title": candidate.name,
            "charter": candidate.charter,
            "album": candidate.album,
            "pack_name": candidate.pack_name,
            "instruments": list(candidate.instruments or ()),
            "diff_guitar": candidate.diff_guitar,
            "score": round(score, 1),
            "quality": round(quality, 1),
            "eligibility": eligibility,
            "selected": selected,
            "selectable": selectable,
            "result_tier": result_tier,
            "result_tier_reasons": tier_reasons,
            "review": {
                "state": "strong" if eligibility == "RECOMMENDED" else "needs_review" if selectable else "unavailable",
                "label": "Recommended expansion" if eligibility == "RECOMMENDED" else "Playable expansion" if selectable else "Unavailable",
                "summary": "A playable catalog chart from an artist already in your library.",
                "concerns": concerns,
                "selectable": selectable,
                "action": "Select" if selectable else "Unavailable",
            },
        }
        grouped.setdefault(identity, []).append(item)

    cards = []
    for identity, candidates in grouped.items():
        candidates.sort(
            key=lambda item: (
                -item["score"], -item["quality"], item["source"], item["chart_id"]
            )
        )
        selected = next((item for item in candidates if item["selected"]), None)
        primary = candidates[0]
        primary["best"] = True
        for item in candidates[1:]:
            if item["result_tier"] == RECOMMENDED_EXACT:
                item["result_tier"] = SAME_RECORDING_CHART_VARIANT
                item["result_tier_reasons"] = ["additional expansion chart artifact"]
        cards.append({
            "song_key": "|".join(identity),
            "artist": primary["artist"],
            "title": primary["title"],
            "artist_norm": primary["artist_norm"],
            "selected": selected is not None,
            "selected_candidate_id": selected["candidate_id"] if selected else None,
            "selected_candidate_ids": [item["candidate_id"] for item in candidates if item["selected"]],
            "selected_count": sum(item["selected"] for item in candidates),
            "candidates": candidates,
        })
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
        "Cache-Control": "private, no-store",
    }
    if size:
        headers["Content-Length"] = str(size)
    return Response(generator, headers=headers)
