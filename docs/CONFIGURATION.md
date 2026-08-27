# Configuration Reference

`src/csv_to_clonehero/default_config.yaml` is the canonical local configuration
reference. `config.example.yaml` is the commit-safe example for deployments;
both contain settings only, never credentials. `config.yaml` is created beside
the selected config path on first run and is ignored by Git.

## Loading Rules

1. The CLI loads `config.yaml`, or the path supplied with `--config`.
2. Missing local configuration is copied from the bundled
   `default_config.yaml`.
3. Relative paths are resolved against the directory containing the loaded
   configuration, not against the installed package.
4. Environment variables override the configured values for secrets and the
   documented public identifiers: `SPOTIFY_CLIENT_ID`, `APP_BASE_URL`,
   `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `DATABASE_URL`, notification settings,
   and `GOFUNDME_URL`.
5. `config.migrate_state_db()` runs at CLI/local-web startup and moves one
   recognized legacy database only when the configured default target is absent.
   Ambiguous state is refused rather than silently merged.

## Important Sections

| Section | Used by | Notes |
| --- | --- | --- |
| `paths` | local app and shared persistence | Input, output, reports, state, artifact cache, and manifest locations |
| `spotify` | local and public OAuth | PKCE client ID, callback, scopes, and optional local keyring coordinates |
| `matching` / `instruments` | all discovery | Scores, thresholds, version, duration tolerance, and playability gates |
| `network` / `cache` / `search` | chart discovery | Request pacing, retries, cache lifetime, catalog-first query behavior, and result caps |
| `library` / `hierarchy` | local exports | `hybrid`, `artist_album`, `pack`, or legacy folder layout |
| `expansion` | local expansion and catalog workflows | Per-artist caps and minimum score |
| `server` / `sessions` / `crypto` | public app | Public URL, limits, session cookie, and Fernet key environment name |
| `notifications` / `monetization` | public presentation and optional mail | Mail is best-effort; user-facing mail capture is not enabled by default |

`server.session` is retained for compatibility with older configuration files;
the public app reads the top-level `sessions` section. The same applies to
legacy threshold aliases in `matching`: `auto_accept_threshold` and
`recommendation_threshold` are synchronized, as are `review_threshold` and
`export_floor`.

## Paths and State

The database defaults to `state/csv_to_clonehero.db` and remains pinned to the
config base directory. Choosing another output folder changes only the library
destination. Reports and state do not move into a user-selected Songs folder.

Local folder exports may retain verified chart artifacts in
`state/export-artifacts`; the public browser-download path deliberately does
not use that persistent cache. Public ZIP staging is transient and cleaned up
after delivery or expiry.

## Catalog Snapshot

The optional local `catalog/enchor.jsonl` file is a generated, metadata-only
snapshot. It must have a complete header and matching SHA-256 checksum before
it can provide negative filtering. Generate or replace it with:

```powershell
python -m csv_to_clonehero catalog-export
```

The snapshot and its `.partial`/checkpoint files are ignored by Git because a
full snapshot is too large for a normal GitHub repository. Keep its provenance,
refresh date, and distribution method in operator records.

## Production Secrets

Never place secrets in YAML or source. Generate values with
`python -m csv_to_clonehero keys`, inject them through a secret manager or
environment, and back up `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` separately from
the SQLite database. Rotating either key invalidates existing sessions; rotating
the Fernet key without re-encrypting stored tokens makes refresh tokens unusable.
