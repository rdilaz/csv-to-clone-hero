# Agent notes

- Python 3.11+ package, src layout: `src/csv_to_clonehero/`.
- Install: `pip install -e ".[dev]"`. Tests: `pytest` (offline);
  `pytest -m integration` hits the live Enchor API.
- Enchor API (verified against the live service and the official Bridge
  client): `POST https://api.enchor.us/search` and `/search/advanced`;
  downloads at `https://files.enchor.us/{md5}.sng` (returns HTTP 201 on
  search POSTs; `md5` does NOT match served .sng bytes - do not checksum
  against it; some artist+title queries 500, retry title-only).
- Enchor rate-limits hard (HTTP 429) above ~1-2 req/s per IP; the adapter
  uses an adaptive AIMD throttle (`_note_rate_limited`/`_note_success`).
  Keep `network.concurrency` low (default 2). Query errors are never cached
  so transient failures retry on the next run; successes cache 7 days.
- Never commit `input_playlists/*.csv`, `state/`, `output_library/`,
  `reports/`, `overrides.yaml`, or `catalog/*.jsonl` (see .gitignore).
- All source-specific logic must stay behind `sources/base.py:ChartSource`.
  Multi-source works structurally (query keys are per-source, candidates are
  keyed by `(source, chart_id)`, and a quality tie-break makes the
  highest-quality chart win even when scores saturate at 100). Enchor is the
  only built-in adapter; the spike notes in `sources/__init__.py` explain why
  (no other legitimate public chart API verified).
- Importers (`csv_import.py`): Spotify-style `.csv`, iTunes/Music
  tab-delimited `.txt`, and iTunes/Music XML plist (`.xml`, stdlib
  `plistlib`, 50 MB guard). Decoding is BOM-aware UTF-8 with cp1252/latin-1
  fallbacks; comma/tab delimiter is sniffed. iTunes files have no playlist
  column, so the playlist label is the filename stem.
- Master list: computed on demand from `spotify_tracks` via
  `Database.get_master_list()` (artist -> album -> songs; empty album ->
  `Singles` bucket, `models.SINGLES_ALBUM_LABEL`); exported as
  `reports/master_list.csv` with per-song match status.
- Quality gates (`matching.py`): charts with no instruments AND no difficulty
  ratings are hard-rejected ("no playable chart content"). Bonuses favor
  official/full-band charts; single-instrument and unrated-guitar charts are
  penalized. `ScoreBreakdown.quality` (net bonuses) is persisted in
  breakdown_json and used as the planner tie-break.
- Library layout (`library.layout`, default `hybrid`): exact songs ->
  `<Artist>/<Album|Singles>/`, expansion -> `<Pack|Artist>/`; `legacy` keeps
  the old `hierarchy.*` section folders. Paths are built by
  `filesystem.destination_path` (track album is canonical for exact songs;
  ~180-char relative budget truncates the title first). Switching layouts
  auto-relocates completed downloads on the next build
  (`pipeline.run_relocation`: per-file atomic rename + DB path update,
  idempotent), then `cleanup_empty_folders` sweeps empty dirs.
- Only `.sng` files belong in the library root (Clone Hero/YARG scan it
  recursively); reports stay in `reports/`. `doctor` warns when reports are
  inside the library root or the root path is >60 chars.
- Path decoupling: `paths.database` defaults to base-dir-relative
  `state/csv_to_clonehero.db`; `config.migrate_state_db()` runs at CLI/web
  startup (moves legacy DBs, refuses ambiguous state). Per-run output
  overrides use `config.with_output_dir(...)` so the DB/reports stay pinned.
- Exact search (`pipeline.run_exact_search`): catalog-first by default.
  Artists with >= `search.artist_catalog_min_songs` unique primary-artist
  tracks share one `("artist", artist_search_name, "")` query per source;
  their tracks are scored against the catalog. After the main resolve, any
  catalog-scored track that ended REJECTED/NO_MATCH (including because the
  artist query errored) and has no manual decisions gets its match scores
  deleted and runs a per-song fallback query. Progress counts both waves.
  The artist search name matches expansion exactly
  (`overrides.artist_alias(norm) or first-seen primary_artist`).
- Enchor adapter (`sources/enchor.py`): `search_artist` paginates the
  `/search/advanced` response up to a 20-page/5,000-chart sanity cap,
  deduplicating by `chart_id`.
- File picker: disabled-file state is stored in the DB meta table under
  `disabled_files` (JSON list of basenames). `csv_import.import_playlist_dir`
  accepts a `skip` set; `pipeline.run_scan` and `webapp._estimate_work` both
  apply it. Web endpoints: `/api/files` returns `enabled`; `/api/files/toggle`
  persists. CLI and web share the same set.
- Web UI: `webapp.py` (Flask, 127.0.0.1-only), `python -m csv_to_clonehero
  web`, launcher `WEB_INTERFACE.bat`. The active home page is source-neutral
  (`_UNIFIED_PAGE`): SoundCloud export import plus optional Spotify and local
  catalog matching. `/advanced` redirects to the primary local journey;
  legacy CSV/iTunes review code is not a mounted alternate frontend.
  The local API exposes a capped live-log ring buffer in `/api/status`, and
  source factories are injectable for tests.
- Linked Clone Hero library (`webapp.py`): the Songs folder linked via
  `/api/spotify/library/link` (alias of `library-import`) persists in DB meta
  `spotify_inventory_root`; `scan_library` indexes it so discovery marks
  matching songs already owned. `/api/spotify/library/verify` re-checks
  access + rescans; `/api/spotify/library/issues` reports broken/suspicious/
  duplicate charts; `/api/spotify/library/clean` quarantines them to a
  `<root> - quarantine` sibling (never deletes). Remote (non-loopback)
  folder exports are allowed ONLY when the destination equals the linked
  root; every other remote path stays 403. Compatibility ZIP endpoints are
  mode-gated, not a universal local fallback. Native folder pickers require
  Windows and loopback; controls must name the Riff host they affect.
- Downloads are keyed by candidate in the `downloads` table so rebuilds are
  idempotent; selections are rebuilt each run.
- Deferred options: an interactive organizer / AI playlist builder were
  deliberately deferred.

# Multi-user cloud product (the public `server/` package)

- `src/csv_to_clonehero/server/` is the public product (`web.py:create_app`,
  a WSGI app factory; run via `wsgi.py`/waitress/gunicorn). Migrations are
  additive (`database.py:SCHEMA_VERSION` is the source of truth) and the
  offline suite covers both
  the legacy `webapp.py` engine and the new `server/` modules.
- Secrets are NEVER in `config.yaml` (now untracked/gitignored). They come from
  env/secret-manager: `SECRET_KEY` (session), `TOKEN_ENCRYPTION_KEY` (Fernet),
  `APP_BASE_URL`, `SPOTIFY_CLIENT_ID`, `NOTIFICATIONS_API_KEY`, `GOFUNDME_URL`.
  Ship `config.example.yaml` + `.env.example`; generate keys with
  `python -m csv_to_clonehero keys`.
- Multi-user identity = Spotify login. Per-user refresh tokens are stored in
  `user_tokens` encrypted with Fernet (`server/security.TokenCipher`) via
  `server/auth.EncryptedCredentialStore`; NOT the OS keyring (kept only as an
  optional `legacy-keyring` extra for the old single-user app). Sessions live in
  `user_sessions` (revocable) bound to a signed cookie.
- Workspace isolation reuses the engine's globally-unique `workspace_key`: each
  user is `spotify:u:<user_id>` (`Database.workspace_key_for_user`). The 7-day
  query cache + `candidate_charts` stay GLOBAL on purpose (shared cache is the
  main defense against Enchor's ~1-2 req/s per-IP hard cap on one public IP).
- Jobs live in `background_jobs` (per-user, durable). ONE global worker runs
  them FIFO (`server/jobs.JobManager`), additionally gated by the process-wide
  `server/throttle.GlobalGate`, so a single shared IP never exceeds Enchor's
  rate cap. Email-on-completion is optional/best-effort
  (`server/notifications`, resend|smtp|log).
- Public exact discovery is catalog-first: `CATALOG_PATH` should point at the
  complete checksum-valid snapshot on persistent `/data`. It reuses
  `combined_pipeline.py` and makes zero Enchor search POSTs; the bounded live
  pipeline is fail-safe only. The final cross-provider deduplicated library is
  hard-capped at 50,000 songs, above the observed 17k-class dogfood library.
- Public artist expansion is an explicit second-stage catalog job. It uses no
  Enchor search requests, preserves exact/expansion review separation, and
  applies the normal expansion gates and caps.
- `chart_artifact_validations` is the efficient current projection and
  `chart_artifact_validation_history` preserves replaced observations
  append-only. `REJECTED`/`UNAVAILABLE` identities remain auditable but are
  excluded from catalog eligibility; `catalog-validate` seeds once per catalog
  snapshot and processes bounded, restart-safe SQL batches.
- Public chart downloads are browser-only: `server/downloads.build_archive`
  assembles a ZIP from workspace selections into transient staging, and
  `server/downloads.BrowserDownloads` hands it over via a short-lived, signed,
  single-use URL (revoke + sweeper). The local folder exporter may persist
  verified artifacts under `state/export-artifacts` for idempotent rebuilds.
- The local `webapp.py` journey stays separate from the public WSGI
  product (`/advanced` redirects locally). It is protected by local loopback/LAN-token access, not the public
  multi-user session model.
- Ops: `Dockerfile` + `docker-compose.yml` (single host, SQLite `/data`
  volume), `.github/workflows/ci.yml` (offline on push, live Enchor on a
  schedule), `requirements.txt` runtime lock, `docs/ARCHITECTURE.md` and
  `docs/runbook.md` (Cloudflare DNS/Tunnel + single container host, backups,
  rollback). Consolidate ALL operator backends on that one host; never split
  the Enchor-facing worker across hosts.
- The local `catalog/enchor.jsonl` snapshot is generated metadata, not source
  code. It is intentionally ignored because a full snapshot is too large for
  ordinary GitHub storage; `catalog-export` is the reproducible acquisition
  utility and the loader refuses incomplete or checksum-invalid snapshots.

- UX recovery release: assets `riff-ux-20261010`, additive SQLite schema 22,
  owner-workspace selection revision/history/undo and asynchronous preflight.
  See `docs/UX_RECOVERY_RELEASE.md`. Keep status reconnection read-only after
  unknown outcomes and never probe signed single-use ZIP links with fetch.

- Catalog player release: public assets `riff-catalog-20261011`, additive
  schema 23; see `docs/CATALOG_BROWSER.md`. Direct catalog choices are an
  independent owner-workspace cohort, with generation-zero ZIP support and
  guarded Add/Remove/Undo. The browser listening queue and package-audio
  previews are separate from ZIP authority. Preserve same-artifact aliases,
  owner isolation, bounded media reads, cancellation and paused restoration.
  Production remains Python; Node.js 22 is used only by the offline JS harness.
