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
from typing import Any, Callable

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.server.auth import build_oauth, user_workspace_key
from csv_to_clonehero.server.downloads import BrowserDownloads, DownloadError, build_archive
from csv_to_clonehero.server.jobs import JobManager
from csv_to_clonehero.server.security import resolve_session_key, resolve_token_cipher

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

    def _workspace_for_user(db: Database, user: Any) -> int | None:
        key = user_workspace_key(user["spotify_user_id"])
        row = db.get_workspace(key)
        return int(row["id"]) if row is not None else None

    # ---- public pages -------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "app": "csv-to-clonehero", "time": time.time()})

    @app.get("/")
    def index():
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
        from csv_to_clonehero.spotify import SpotifyOAuth

        oauth = _guest_oauth(config)
        request_obj = oauth.begin_authorization()
        session["oauth_state"] = request_obj.state
        session["oauth_verifier"] = request_obj.code_verifier
        session["oauth_redirect_uri"] = request_obj.redirect_uri
        return redirect(request_obj.url)

    @app.get("/api/auth/spotify/callback")
    def oauth_callback():
        from csv_to_clonehero.server.auth import EncryptedCredentialStore

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
        session.clear()
        session["sid"] = sess["session_id"]
        session.permanent = True
        redirect_target = session.get("next") or url_for("index")
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

    @app.get("/api/me")
    def me():
        user = _require_user()
        return jsonify(_public_user(user))

    # ---- Spotify scope (playlists + liked) ----------------------------------
    @app.get("/api/spotify/scope")
    def spotify_scope():
        user = _require_user()
        db = _user_db()
        try:
            oauth = build_oauth(config, int(user["id"]), cipher)
            client = _client_for(config, oauth)
            from csv_to_clonehero.spotify import SpotifyClient

            playlists = _list_playlists(client)
            liked = _count_liked(client) if hasattr(client, "get_liked_tracks") else 0
            return jsonify({"playlists": playlists, "liked_count": liked, "workspace": _scope_workspace(db, user)})
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
        db = _user_db()
        try:
            key = user_workspace_key(user["spotify_user_id"])
            job = jobs.enqueue(int(user["id"]), "discovery", workspace_key=key, notify_email="")
        finally:
            db.close()
        return jsonify({"job": _job_public(job)}), 202

    @app.get("/api/jobs/<int:job_id>")
    def job_status(job_id):
        user = _require_user()
        job = jobs.get(job_id)
        if job is None or job.user_id != int(user["id"]):
            abort(404)
        return jsonify({"job": _job_public(job)})

    # ---- workspace + review ---------------------------------------------------
    @app.get("/api/workspace")
    def workspace():
        user = _require_user()
        db = _user_db()
        try:
            workspace = _ensure_workspace(db, user)
            cards = _review_cards(db, workspace)
            return jsonify({"workspace": workspace, "tracks": cards})
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
            action = str(payload.get("action") or "select")
            if action in {"clear", "deselect", "remove"}:
                db.clear_workspace_selection(workspace, track_value, candidate_id=candidate_id)
            else:
                db.select_workspace_candidate(
                    workspace,
                    track_value,
                    candidate_id,
                    kind="exact",
                    score=payload.get("score"),
                    eligibility=payload.get("eligibility") or "RECOMMENDED",
                )
            return jsonify({"ok": True})
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
    workspace = _workspace_for_user(db, user)
    return {"id": workspace, "exists": workspace is not None}


def _ensure_workspace(db: Database, user: Any) -> int:
    key = user_workspace_key(user["spotify_user_id"])
    row = db.get_workspace(key)
    if row is not None:
        return int(row["id"])
    return 0


def _review_cards(db: Database, workspace: int) -> list[dict[str, Any]]:
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
        by_track.setdefault(int(tid), []).append(
            {
                "candidate_id": data.get("candidate_id"),
                "source": candidate.source,
                "chart_id": candidate.chart_id,
                "artist": candidate.artist,
                "title": candidate.name,
                "score": round(float(data.get("score") or 0), 1),
                "quality": round(float(data.get("quality") or 0), 1),
                "eligibility": data.get("eligibility") or breakdown.get("eligibility") or "",
                "explanation": breakdown.get("explanation") or "",
                "selected": bool(data.get("selected")),
            }
        )
    tracks = db.get_spotify_tracks(True)
    cards: list[dict[str, Any]] = []
    for track in tracks:
        candidates = by_track.get(int(track.db_id or 0))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-item["score"], item["source"], str(item["chart_id"])))
        cards.append(
            {
                "track_key": track.track_key,
                "spotify_id": track.spotify_id,
                "artist": track.artist_full,
                "title": track.title,
                "album": track.album,
                "candidates": candidates,
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