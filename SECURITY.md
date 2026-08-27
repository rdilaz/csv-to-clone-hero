# Security Policy

## Reporting

Do not publish exploitable vulnerabilities, credentials, refresh tokens, or
private playlist data in an issue. Report security problems privately to the
repository owner and include the affected version, a minimal reproduction, and
the impact. Remove or redact all secrets from diagnostic output before sharing it.

## Secret Handling

- Keep `.env`, `config.yaml`, `overrides.yaml`, playlist exports, SQLite state,
  reports, and downloaded charts out of Git.
- Treat `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` as production secrets. Back them
  up separately from `/data` and rotate them through an intentional migration.
- Spotify uses PKCE and does not require a client secret in this application.
- Public refresh tokens are encrypted at rest with Fernet. Losing the Fernet key
  prevents token recovery.
- LAN access-token URLs and signed browser-download URLs are bearer credentials;
  do not share or log them.

## Data Boundaries

The public app stores user identity, encrypted refresh tokens, revocable sessions,
job state, query cache, and workspace metadata. Public chart ZIPs are assembled
in transient staging and removed after streaming or expiry. The local app stores
downloaded `.sng` files in the user's selected library by design.

SoundCloud imports and the browser exporter are limited to rendered/user-provided
metadata. The exporter does not read cookies, credentials, audio URLs, CAPTCHA
pages, or private endpoints.

## Deployment

Run one public application instance and one background worker behind the intended
Cloudflare edge. Keep the container non-root, expose only the loopback-mapped
port, use stable secret values across restarts, and follow `docs/runbook.md` for
backup and restore. Add edge/application rate limits before exposing a new public
endpoint that accepts uploads, OAuth requests, or job creation.
