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

Optional values configure SoundCloud OAuth, transactional email, and the
GoFundMe link. For SoundCloud in production, set an explicit HTTPS
`soundcloud.redirect_uri` in `config.yaml` and register the same URI with
SoundCloud. The default loopback URI is for local use only.

Generate secrets with:

```bash
python -m csv_to_clonehero keys
```

Back up `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` separately from the database.
Losing the Fernet key makes stored refresh tokens permanently unreadable.

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
https://<public-host>/api/auth/spotify/callback
```

`APP_BASE_URL` and the registered callback must describe the same origin.

The Docker build includes the browser-exporter asset and Python package data.
The large local `catalog/` snapshot is intentionally excluded; the public
Spotify path uses bounded live Enchor searches and preflight. Any catalog-backed
source-neutral workflow requires an operator-managed snapshot outside the image
and a matching `paths.catalog` setting.

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
chosen history, such as 30 days.

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

- `/healthz` is unauthenticated and is the container probe target.
- Repeated Enchor HTTP 429 responses indicate too much concurrency, another
  running instance, or an overly aggressive request delay. Reduce concurrency;
  do not add workers.
- Watch job statuses for `partial`, `failed`, `cancelled`, and `interrupted`.
  Cached successful queries remain resumable.
- Public chart ZIPs are short-lived and single-use. They are assembled in
  transient staging and removed after streaming or expiry.
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
| Site is unreachable | Cloudflare Tunnel route, Compose health, port binding, `/healthz` |
