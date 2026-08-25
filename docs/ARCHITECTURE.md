# Architecture

CSV to Clone Hero is a multi-user, cloud-hosted web product that helps Clone
Hero / YARG players find every song in their real Spotify library that already
has a playable chart on Chorus Encore (Enchor), lets them pick the best match
for each song, and delivers a ZIP of those chart files to **their own**
computer. It does **not** store chart files server-side.

## Hosting decision: consolidate all backends onto ONE stateful host

The operator has several products (mcb.ryo-nd.com, charts.ryo-nd.com) and was
unsure whether they were on Google Cloud or Cloudflare. The current setup is:
Cloudflare DNS/edge + a Cloudflare Tunnel for `charts.ryo-nd.com` (currently
offline), no clear app host.

**Recommendation (implemented here):**

| Layer            | Choice                                                                 |
| ---------------- | ---------------------------------------------------------------------- |
| DNS + TLS + edge | **Cloudflare** (already their edge). A Cloudflare Tunnel terminates TLS. |
| Stateful app     | **One small container host** (e.g. a $4–8/mo VPS on Hetzner/DigitalOcean, or Fly.io with a single shared-cpu machine + volume). |
| Reverse proxy    | Cloudflare Tunnel to the container; no separate Caddy needed.          |
| Datastore        | **SQLite on a persistent volume** (single-instance; documented below). |
| WSGI             | **waitress** (cross-platform) or gunicorn inside the container.        |

### Why one host for all products

- One place to operate, monitor, back up, and bill. Boring and cheap.
- `mcb` and `clonehero` become sibling containers on the same host, each on
  its own subdomain (`mcb.ryo-nd.com`, `charts.ryo-nd.com`), each reached
  through one Cloudflare Tunnel / route. They share nothing but the host and a
  backup schedule.
- Enchor rate-limits **per IP**. A single public IP shared by all users is the
  simplest correct posture: one global worker + one shared query cache. Splitting
  across hosts would multiply the requests Enchor sees — never split this product.

### Why SQLite (and when to move to Postgres)

This places deliberately on **single-instance-with-volume**, which the product
constraints allow when chosen and documented:

- One web process + one background worker, both on one host, can share SQLite
  safely (WAL mode, busy timeout). No second service to operate.
- The 7-day query cache, per-user encrypted tokens, job state, and survey data
  fit comfortably in a few GB.
- Moving to Postgres later is a contained change: the `Database` layer is the
  only reader/writer of tables. Do it only if you scale past one host or need
  zero-downtime multi-host deploys. Until then, Postgres adds cost, a second
  moving part, and TLS/credentials to babysit for zero benefit at this scale.

### The single global worker

Enchor 429s hard above ~1–2 req/s per IP. Concurrency is the enemy. The app
therefore runs **one** background job worker, FIFO across all users, and gates
every Enchor-bound job with a process-global lock (`server/throttle.py`). Each
job still uses its own adapter instance whose built-in AIMD throttle and
`network.request_delay_seconds` pace individual queries. The shared 7-day
query cache means repeated users never re-hit Enchor. Being slow is fine;
correctness and completion messaging matter more than speed.

## Components

```
src/csv_to_clonehero/
  normalization.py      # artist/title normalization (shared, unchanged)
  matching.py           # quality gates + scoring (unchanged)
  sources/base.py       # ChartSource interface (unchanged)
  sources/enchor.py     # Enchor adapter, adaptive AIMD throttle (unchanged)
  csv_import.py         # Spotify/iTunes CSV importers (single-user legacy)
  library_inventory.py  # scan a local Songs folder (single-user legacy)
  reporting.py          # reports (reused where sensible)
  filesystem.py         # path building + safety (unchanged)
  models.py             # dataclasses (unchanged)
  database.py           # SQLite store; + multi-user tables/methods (Phase 2)
  planner.py            # selection planning (unchanged)
  exporter.py           # ZIP/folder export from selections (reused by downloads)
  webapp.py             # LEGACY single-user Flask app (admin-only, kept isolated)
  spotify.py            # Spotify OAuth + API primitives (reused)
  spotify_pipeline.py   # resumable discovery (reused; per-user workspace keys)
  server/               # NEW public product
    security.py         # Fernet token encryption + secret resolution (Phase 2)
    auth.py             # per-user encrypted credential store + OAuth (Phase 2)
    jobs.py             # durable job queue + global worker (Phase 3)
    throttle.py         # global egress gate (Phase 3)
    notifications.py    # completion email (Phase 3)
    downloads.py        # short-lived signed ZIP delivery (Phase 4)
    web.py              # multi-user WSGI app factory (Phase 5)
    templates/          # landing, legal, about, GoFundMe + ads (Phase 6)
```

## Multi-user data model

```
users(id, spotify_user_id UNIQUE, …)          # identity = Spotify login
user_sessions(session_id, user_id, expires…)  # server-side, revocable
user_tokens(user_id, encrypted_refresh_token) # Fernet-encrypted at rest
background_jobs(id, user_id, kind, status, workload, workspace_key, …)
```

Isolation without schema surgery: the discovery engine scopes every workspace
by a globally-unique `workspace_key`. Each user gets
`spotify:u:<user_id>` as their workspace key (`database.workspace_key_for_user`),
so all workspace data (candidates, selections, progress) is per-user
automatically. The **query cache and candidate table stay global on purpose** —
that is the shared cache that makes a public app survive Enchor's rate limit.

## Security model

- No passwords, no accounts: sign in with Spotify PKCE. There is no client
  secret (nothing to leak).
- Each user's refresh token is encrypted at rest with **Fernet**
  (`TOKEN_ENCRYPTION_KEY`), never the OS keyring, never in git.
- Session cookie is signed with `SECRET_KEY` and validated server-side against
  `user_sessions` (revocable/logout kills the DB row).
- Charts never touch persistent storage: a ZIP is assembled on demand into a
  per-user staging dir, handed to the browser via a **short-lived signed
  single-use URL**, then deleted (stream + revoke, plus a sweeper).
- OAuth pieces and any token-like values are redacted from logs (the existing
  OAuth log filter and `_safe_message` redaction remain).

## Legacy single-user CSV mode

The old `/advanced` CSV/upload flow writes charts to a server directory, which
does **not** fit the public download-to-browser model. It is kept intact but
isolated behind `admin.enable_legacy_csv` **and** an allowlisted `admin.admins`
Spotify user id — never exposed to the public product.

## Decisions honored from the engine (AGENTS.md)

- Enchor search at `https://api.enchor.us/search[/advanced]` (HTTP 201);
  downloads at `https://files.enchor.us/{md5}.sng`; `md5` does **not** match
  downloaded bytes (never checksum against it); some artist+title queries 500
  → retry title-only.
- Adaptive AIMD throttle in the Enchor adapter; keep `network.concurrency` low.
- Query errors are never cached; successes cache 7 days.
- Only `.sng` files belong in a library root; everything else stays in `reports/`.
- All source logic behind `sources/base.py:ChartSource`.
- Path decoupling: database pinned to base-dir state; per-run output overrides
  never move the DB.