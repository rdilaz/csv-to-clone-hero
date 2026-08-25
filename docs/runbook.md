# Production runbook

Operation of the consolidated single-host deployment of **csv-to-clonehero**
(branded as the charts product on `charts.ryo-nd.com`). Companion doc:
[ARCHITECTURE.md](ARCHITECTURE.md).

## Topology

```
Users ──> Cloudflare (DNS, WAF, TLS) ──> [Cloudflare Tunnel] ──> host:8000 (waitress)
                                                                     │  SQLite volume (/data)
                                                                     └─ 1 background job worker (same process)
```

Cloudflare owns DNS + TLS termination. The app itself is plain HTTP on
`127.0.0.1:8000` inside the host (or a private tunnel), so nobody reaches
waitress directly.

## Prerequisites / env vars

Copy `.env.example` to `.env` (gitignored) and fill in:

| Variable               | Required | Meaning                                                            |
| ---------------------- | :------: | ------------------------------------------------------------------ |
| `SPOTIFY_CLIENT_ID`    | yes      | Public Spotify app id (PKCE; no secret).                           |
| `APP_BASE_URL`         | yes      | `https://charts.ryo-nd.com` (also the OAuth redirect base).        |
| `SECRET_KEY`           | yes      | Session signing key. `python -m csv_to_clonehero keys`.            |
| `TOKEN_ENCRYPTION_KEY` | yes      | Fernet key for per-user refresh tokens (`keys` command).           |
| `NOTIFICATIONS_API_KEY`| no       | Resend (or SMTP via `notifications.*`) for job-completion mail.    |
| `GOFUNDME_URL`         | no       | Clone Hero creator GoFundMe campaign URL (mission banner).         |
| `DATABASE_URL`         | optional | `sqlite:////data/csv_to_clonehero.db` (set by the container).      |

Generate the two keys with:

```bash
python -m csv_to_clonehero keys
```

Put them in your secret manager / the host's environment instead of git.

## First deploy

```bash
# on the host
git clone <your-private-repo> && cd "CSV To CloneHero"
cp .env.example .env           # edit real values (never commit .env)
docker compose up -d --build
docker compose ps              # "healthy"
curl -s http://127.0.0.1:8000/healthz
```

### Cloudflare wiring (one-time)

1. DNS `charts.ryo-nd.com` -> proxied (orange cloud) Cloudflare/Tunnel.
2. A **Cloudflare Tunnel** `cloudflared` route maps `charts.ryo-nd.com` to
   `http://127.0.0.1:8000` on the host (or run `cloudflared` as a sidecar
   container that forwards to the `clonehero` service). TLS terminates at
   Cloudflare; the app sees `X-Forwarded-Proto: https`.
3. **Spotify Developer Dashboard**: add the callback
   `https://charts.ryo-nd.com/api/auth/spotify/callback` to the app.
4. `APP_BASE_URL` must be exactly `https://charts.ryo-nd.com` so OAuth builds
   the same redirect URI.

## Useful ops commands

```bash
docker compose up -d --build      # deploy/rebuild
docker compose logs -f clonehero  # tail logs
docker compose restart clonehero  # quick restart
docker compose down               # stop (data persists in the volume)
```

## Backup / restore

The only durable thing that matters is the SQLite database (tokens, 7-day
query cache, job state). Back it up with SQLite's online backup API so you
never copy a `WAL`-in-progress file inconsistently.

Quick (sidecar, included):

```bash
docker compose --profile backup run --rm backupper
# writes backups/csv_to_clonehero_<timestamp>.db (backups/ is gitignored)
```

Automated (recommended): a nightly `cron` on the host that runs the same
`backupper` container and then copies `backups/*.db` to an off-host bucket
(rclone/restic; keep ~30 days). Also back up the two keys (`SECRET_KEY`,
`TOKEN_ENCRYPTION_KEY`) **separately** — they are required to decrypt user
tokens, exactly like a disk-encryption key.

Restore:

```bash
docker compose down
# restore the .db over the volume, e.g. with a throwaway container:
#   docker run --rm -v clonehero_data:/data -v "$PWD/backups:/backups" \
#     alpine sh -c 'cp /backups/csv_to_clonehero_<ts>.db /data/csv_to_clonehero.db'
docker compose up -d
```

## Rollback

1. Tag images: `docker build -t csv-to-clonehero:<sha> .` before every deploy.
2. To roll back: `docker compose down`, then point `image:` (or `docker tag`)
   at the previous `<sha>` and `docker compose up -d`. The SQLite volume is
   untouched, so users/sessions/cache survive.
3. Because migrations are additive and versioned (`schema_version`), a rollback
   is safe: the older code simply ignores newer tables/columns.

## Health & monitoring

- `/healthz` returns `{"ok": true, ...}` (no auth) — use it for the container
  health check and upstream probes.
- Structured logging: the app logs to stdout (JSON-friendly if you prefer).
  OAuth/token values are redacted at the logging boundary.
- Watch for repeated `HTTP 429`/`rate-limited` lines from Enchor: that means a
  misconfiguration let concurrency rise (worker count, `network.concurrency`,
  or a second instance). The correct response is fewer concurrent requests, a
  longer `request_delay_seconds`, or relying more on the shared cache — not
  "add more workers".

## Incident cheat-sheet

| Symptom                             | First things to check                                        |
| ----------------------------------- | ------------------------------------------------------------ |
| Users can't sign in                 | `SPOTIFY_CLIENT_ID`, `APP_BASE_URL`/tunnel, Spotify callback |
| Tokens fail to decrypt              | `TOKEN_ENCRYPTION_KEY` changed/lost → restore from backup    |
| Sessions drop on restart            | `SECRET_KEY` not stable (must persist across deploys)        |
| Enchor 429s                         | One worker? `network.concurrency` low? cache TTL 7d?         |
| Job slow / emails not sent          | `NOTIFICATIONS_API_KEY` / SMTP config valid?                 |
| Site down                          | tunnel route, container healthy, `/healthz`                 |