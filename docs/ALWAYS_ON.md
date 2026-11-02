# Riff, always on, at riff.ryo.is

Riff runs on one Fly.io machine with a persistent volume, so it stays live
when the Aero is off. Nothing here changes riff.ryo-nd.com on the Aero.

## What it costs (Fly.io, Ashburn `iad`, checked Nov 2026)

| Piece | Size | Monthly |
|---|---|---|
| Machine | shared-cpu-2x, 2 GB RAM, always on | about $13.39 |
| Volume | 30 GB at $0.15/GB | $4.50 |
| Shared IPv4 + TLS certificate | first 10 certs free | $0 |
| **Total** | | **about $18/month** |

Fly bills by usage and has no free compute tier. 2 GB is the floor because the
chart catalog index alone takes roughly 0.7 to 1 GB in memory. If the machine
restarts for lack of memory, raise it with `fly scale memory 4096` (about
double the machine price).

## Files

- `fly.toml`: one always-on machine, `/data` volume, health check on `/healthz`.
- `Dockerfile.fly`: the normal image plus `scripts/fly-entrypoint.sh`, which
  gives the root-owned Fly volume to the app user and then drops root.
- The Aero keeps using `Dockerfile` and `docker-compose.yml` unchanged.

## Steps

Run these in PowerShell on any machine with this branch checked out.

1. Install flyctl and sign in (opens your browser):
   ```
   iwr https://fly.io/install.ps1 -useb | iex
   fly auth login
   ```
2. Create the app and its volume (pick another name if `riff-ryo` is taken,
   and change `app` in `fly.toml` to match):
   ```
   fly apps create riff-ryo
   fly volumes create riff_data --region iad --size 30 -a riff-ryo
   ```
3. Make fresh keys for the new host. This is also the key rotation: the new
   site never uses the Aero's keys. Run it, then paste the values only into
   the next command, nowhere else:
   ```
   .\.venv\Scripts\python.exe -m csv_to_clonehero keys
   fly secrets set SPOTIFY_CLIENT_ID=... SECRET_KEY=... TOKEN_ENCRYPTION_KEY=... -a riff-ryo
   ```
4. Deploy:
   ```
   fly deploy -a riff-ryo
   ```
5. Build the chart catalog on the volume (a few minutes, polite to Enchor):
   ```
   fly ssh console -a riff-ryo -C "python -m csv_to_clonehero catalog-export --output /data/catalog/enchor.jsonl"
   ```
6. Point riff.ryo.is at it:
   ```
   fly certs add riff.ryo.is -a riff-ryo
   ```
   In Cloudflare, zone ryo.is, add DNS record: type CNAME, name `riff`,
   target `riff-ryo.fly.dev`, proxy status **DNS only** (grey cloud), so Fly
   can issue the certificate. Check with `fly certs show riff.ryo.is -a riff-ryo`.
7. Spotify Developer Dashboard, your Riff app, Settings, Redirect URIs: add
   `https://riff.ryo.is/api/auth/spotify/callback` and save. Keep the old one
   until riff.ryo-nd.com is retired.
8. Open https://riff.ryo.is, sign in, run a small search, prepare a small ZIP.
9. When you are happy: stop the Aero container (`docker compose down`) and
   remove the old redirect URI. The Aero's old keys then go unused.

## Later updates

```
git pull
fly deploy -a riff-ryo
```

## Backups

```
fly ssh console -a riff-ryo -C "python -c \"import sqlite3; s=sqlite3.connect('/data/csv_to_clonehero.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)\""
fly ssh sftp get /data/backup.db -a riff-ryo
```
Fly also snapshots volumes daily and keeps them 5 days.
