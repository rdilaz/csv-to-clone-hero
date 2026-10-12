# Production Runbook

This runbook covers the public `server/` application deployed as one stateful
container behind Cloudflare. It is intentionally separate from the local
single-user app documented in the root `README.md`.

## Topology

```text
Users -> Cloudflare DNS/WAF/TLS -> Cloudflare Tunnel -> host:8000 (waitress)
                                                        |
                                                        + SQLite volume: /data
                                                        + one in-process job worker
```

The container binds only to the host loopback through Compose. Cloudflare
Tunnel is the only intended public ingress. Do not add a second application
worker or host running Enchor jobs: the service uses one global queue and one
shared query cache to respect Enchor's per-IP rate limit.

## Required Configuration

Copy `.env.example` to `.env` and keep the real file outside Git. Required
values are:

| Variable | Purpose |
| --- | --- |
| `APP_BASE_URL` | Exact public HTTPS origin, including no trailing path |
| `SPOTIFY_CLIENT_ID` | Spotify public PKCE application ID |
| `SECRET_KEY` | Stable Flask/session signing key |
| `TOKEN_ENCRYPTION_KEY` | Stable Fernet key for encrypted refresh tokens |
| `CATALOG_PATH` | Complete catalog on persistent state; default `/data/catalog/enchor.jsonl` |

Optional values configure SoundCloud OAuth, transactional email, and the
GoFundMe link. Support links and the banner are disabled by default; no campaign
is assumed. Verify the destination before configuring `GOFUNDME_URL` and opting
in with `monetization.gofundme_enabled` / `gofundme_banner`.
For SoundCloud in production, register `<APP_BASE_URL>/api/auth/soundcloud/callback`
with SoundCloud. With an empty `soundcloud.redirect_uri`, the public app derives
that callback from `APP_BASE_URL`; an explicit configured URI overrides it.
The loopback fallback is for local use only.

Generate secrets with:

```bash
python -m csv_to_clonehero keys
```

Back up `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` separately from the database.
Losing the Fernet key makes stored refresh tokens permanently unreadable.

### Disk capacity

Set `EXPORT_MINIMUM_FREE_BYTES` in the existing deployment environment or `.env`
to a positive integer chosen for this host's capacity and other storage users.
Compose forwards it to the public application, where it overrides
`export.minimum_free_bytes` without changing the saved YAML. Blank or unset
preserves the YAML value; the bundled default is **0**, so leaving it blank does
not configure a reserve. Negative, boolean, and non-integer values stop startup.

The image does not copy the host's `config.yaml`, and Compose does not mount it.
Editing only that host file therefore does not configure the running container.
Verify the effective reserve inside the application before launching. Back up
the existing environment separately from `/data`.

`storage.max_library_gib` limits one export's chart contents (default 48 GiB),
not total retained ZIP bytes. One worker serializes builds, but completed ZIPs
can remain for 15 minutes, up to three per workspace, with no global byte cap.
Size the persistent volume for retained ZIPs, temporary artifacts and a new
ZIP, plus the chosen reserve. Watch free capacity and launch with a bounded
audience until aggregate retention has been measured or separately limited.

## First Deployment

```bash
git clone <repository-url> csv-to-clone-hero
cd csv-to-clone-hero
cp .env.example .env
# edit .env with real values
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
```

Register this exact Spotify callback after choosing the public origin:

```text
https://riff.ryo-nd.com/api/auth/spotify/callback
```

Set `APP_BASE_URL=https://riff.ryo-nd.com`. The registered callback and the
Cloudflare public hostname must use that exact origin.

The Docker build includes the browser-exporter asset and Python package data.
The large generated catalog is intentionally excluded from Git and the image.
Place it at `/data/catalog/enchor.jsonl` in the persistent volume (or set
`CATALOG_PATH`). Replace it atomically. A complete checksum-valid snapshot uses
local matching and sends zero Enchor search POSTs; the bounded live path and
preflight are used only as a fail-safe when the snapshot is unavailable.

Generate the snapshot in an operator workspace, verify it, then copy it into
the stopped container volume or another atomic staging path:

```bash
python -m csv_to_clonehero catalog-export --output /data/catalog/enchor.jsonl
python -m csv_to_clonehero catalog-validate --status
python -m csv_to_clonehero catalog-validate --limit 100
```

`catalog-validate` checkpoints every verdict in SQLite and validates at most
100 charts by default (hard maximum 1,000 per invocation). It never starts an
unbounded full-catalog download crawl. Back up the catalog and validation
ledger with `/data`.

## Routine Operations

```bash
docker compose up -d --build
docker compose logs -f clonehero
docker compose restart clonehero
docker compose down
```

The named `clonehero-data` volume contains the SQLite database and transient
download staging area. `docker compose down` does not remove that volume.

## Backup and Restore

Use the included SQLite sidecar so the database is copied through SQLite's
online backup API rather than by copying an active WAL file:

```bash
docker compose --profile backup run --rm backupper
```

The sidecar writes `backups/csv_to_clonehero_<timestamp>.db`, and `backups/`
is ignored by Git. Copy backups to an off-host location and retain an operator-
chosen history, such as 30 days. This command backs up SQLite only, including
the validation ledger; it does not copy `/data/catalog`. Separately preserve the
checksum-valid catalog snapshot (or its reproducible acquisition inputs), and
protect the stable encryption/session keys in the chosen secret manager.
Losing `TOKEN_ENCRYPTION_KEY` makes stored refresh tokens unreadable.

Restore only while the application is stopped:

```bash
docker compose down
docker compose run --rm --no-deps --entrypoint /bin/sh backupper \
  -c 'cp /backups/csv_to_clonehero_<timestamp>.db /data/csv_to_clonehero.db'
docker compose up -d
```

If the sidecar image has no database to restore, use a temporary container with
the `clonehero-data` volume and the host `backups` directory. Never delete the
original backup until `/healthz` and a sign-in check succeed.

## Rollback

Build and tag each release before deployment:

```bash
docker build -t csv-to-clonehero:<git-sha> .
```

Point the Compose image at a previously verified tag, restart the service, and
leave the SQLite volume in place. Migrations in `database.py` are additive, but
test the rollback against a copy of the production database before relying on
an older image after a schema change.

## Health and Monitoring

- `/healthz` is unauthenticated and is the container liveness probe target. It
  does not prove database, catalog, provider, or worker readiness.
- Repeated Enchor HTTP 429 responses indicate too much concurrency, another
  running instance, or an overly aggressive request delay. Reduce concurrency;
  do not add workers.
- `/api/preflight` reports `mode=catalog` when the persistent snapshot is
  checksum-valid, or `mode=live_fallback` with a catalog diagnostic otherwise.
- Watch job statuses for `partial`, `failed`, `cancelled`, and `interrupted`.
  Cached successful queries remain resumable.
- `POST /api/download` queues a ZIP preparation job and returns HTTP 202 with
  its ID. The authenticated browser polls `GET /api/jobs/<id>` and reads the
  single-use link from `/api/delivery/status` only after the job completes. This
  lightweight authenticated endpoint reads saved selection counts and delivery
  receipts without rebuilding the entire review pool. Chart details load only
  when expanded and do not block preparation if they are slow. Saved counts are
  not a promise of the final artifact count; the worker revalidates choices.
  Preparation
  uses the same one-worker queue as matching; do not add another service worker
  to make large ZIPs appear faster.
- Public chart ZIPs are short-lived and single-use. They are assembled in
  transient staging and removed after streaming or expiry. The job result
  records a report and archive timestamp, never the token or file path. A
  container restart can leave a completed job without a usable link; the user
  must prepare a new ZIP from saved choices in that case.
- User refresh tokens are encrypted at rest. Logs and diagnostics must not
  contain tokens, cookies, or OAuth authorization values.

## Incident Checklist

| Symptom | Check first |
| --- | --- |
| Spotify sign-in fails | `APP_BASE_URL`, Spotify callback registration, tunnel route, `SPOTIFY_CLIENT_ID` |
| Sessions disappear | `SECRET_KEY` changed or was not injected consistently |
| Tokens cannot refresh | `TOKEN_ENCRYPTION_KEY` changed/lost, or the database was restored without its key |
| Enchor returns 429 | Only one container instance, one job worker, low `network.concurrency`, seven-day cache |
| Jobs remain interrupted | Container restart, database health, and job heartbeat/status rows |
| ZIP creation fails | Selected eligible candidates, Enchor availability, writable `/data`, and container logs |
| ZIP job completes but link is unavailable | Container restart, token expiry or consumption; ask the user to prepare again from saved choices |
| Site is unreachable | Cloudflare Tunnel route, Compose health, port binding, `/healthz` |
