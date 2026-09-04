# Architecture

This document describes the current implementation. The repository contains
two application surfaces that share the matching and chart-source engine:

| Surface | Entry point | Persistence | Intended use |
| --- | --- | --- | --- |
| Local app | `python -m csv_to_clonehero web` or `WEB_INTERFACE.bat` | Local SQLite state, reports, and a Songs-folder library | Single-user desktop workflows, including CSV/iTunes imports |
| Public app | `wsgi.py` or `python -m csv_to_clonehero serve` | SQLite on a persistent server volume | Multi-user Spotify discovery and browser ZIP delivery |

The local app is owner-controlled through loopback access, or through a signed
LAN token when explicitly enabled. The public app has its own Flask application
factory and does not mount the local `/advanced` interface.

## Shared Engine

The core data flow is:

```text
music export/API
    -> provider-normalized Track records
    -> SQLite workspace and provenance
    -> ChartSource queries or a complete local catalog
    -> deterministic scoring and quality gates
    -> review/selection
    -> storage planning
    -> folder export, browser ZIP, or no download in dry-run mode
```

The important boundaries are deliberately narrow:

- `providers/base.py:LibraryProvider` converts Spotify or SoundCloud input into
  neutral `ProviderTrack` and `LibrarySnapshot` values.
- `sources/base.py:ChartSource` is the only interface used by matching,
  planning, and chart downloads. Enchor is the only built-in adapter.
- `models.py` contains source-neutral `Track`, `ChartCandidate`,
  `ScoreBreakdown`, and selection data models.
- `database.py` owns SQLite connections, migrations, query-cache state,
  workspaces, candidates, selections, jobs, export metadata, and the global
  chart artifact validation ledger.

## Local Workflows

### Legacy CSV build

The CLI and local `/advanced` interface use this pipeline:

1. `csv_import.py` reads Spotify-style CSV, iTunes/Music tab-delimited TXT, or
   iTunes/Music XML plist files.
2. `pipeline.run_scan` deduplicates tracks, applies manual ignore rules, and
   stores the current import snapshot.
3. `pipeline.run_exact_search` resolves source queries from the seven-day cache
   or the chart adapter. Artists with enough tracks use one paginated artist
   query; catalog-scored rejects and errors receive a per-song fallback query.
4. `matching.py` computes title/artist similarity, version evidence, instrument
   compatibility, quality bonuses, and hard rejection gates.
5. `planner.py` keeps the best candidate per track, applies expansion caps,
   avoids duplicate chart/song/path identities, and enforces the storage budget.
6. `pipeline.run_relocation` moves previously completed files into the current
   layout before `downloader.py` resumes missing downloads.
7. `reporting.py` writes CSV, JSON, and text summaries outside the library root.

### Source-neutral local import

The local home page can combine an exported SoundCloud library and optional
Spotify data. `library_pipeline.py` persists provider provenance, and
`combined_pipeline.py` scores the merged normalized library against
`catalog/enchor.jsonl` without issuing per-song chart requests.

The catalog is only authoritative when its header marks it complete and its
SHA-256 checksum covers every row. If it is absent, incomplete, or corrupt, the
application reports that fact instead of treating every missing song as a
definitive no-match. The catalog snapshot is generated with the
`catalog-export` command and is intentionally kept outside Git because the
current snapshot is too large for a normal GitHub repository.

## Public Workflow

The public app in `server/web.py` follows this sequence:

1. Spotify Authorization Code + PKCE establishes identity and creates a
   revocable server-side session.
2. The user chooses playlists or Liked Songs. Optional SoundCloud OAuth or an
   uploaded export can add source data.
3. A complete checksum-valid Enchor snapshot on persistent server state is the
   preferred discovery authority. It satisfies availability without a live
   Enchor search probe. A globally cached bounded preflight gates only the live
   fail-safe path when that snapshot is absent or invalid.
4. `server/jobs.py:JobManager` stores a durable job and runs jobs FIFO on one
   process-wide worker. `server/throttle.py:GlobalGate` prevents concurrent
   Enchor-bound work from multiplying requests from the shared server IP.
5. `server/jobs.py` normalizes selected Spotify and optional SoundCloud records
   into one canonical provider-neutral library and enforces a 50,000-song hard
   bound after deduplication. With a valid snapshot it reuses
   `combined_pipeline.py` for resumable local matching and sends zero Enchor
   search requests. `spotify_pipeline.py` remains the bounded live fallback.
6. Review endpoints change workspace selections only after checking ownership,
   candidate membership, score thresholds, and hard-gate eligibility.
7. After exact matching, users may explicitly choose primary artists for a
   bounded catalog-backed expansion pass. Exact and expansion recommendations
   remain separate in review and share the final selection tray.
8. `server/downloads.py` asks `exporter.py:ExportService` to assemble a ZIP in
   a transient staging area. `BrowserDownloads` signs a short-lived, single-use
   token, streams the archive, and removes it after delivery or expiry.

Public chart bytes are not kept in the configured local artifact cache. Local
folder builds may use the persistent `state/export-artifacts` cache to make
repeated exports idempotent; that cache must never be mounted into the public
service if browser-only retention is required.

## Data Ownership

The database intentionally separates global data from workspace data:

- Query results and `candidate_charts` are global. Sharing them is the main
  defense against Enchor's approximate 1-2 requests/second per-IP limit.
- User identity, encrypted refresh tokens, sessions, jobs, library provenance,
  candidates, and selections are scoped to a user workspace.
- Local CSV state uses the same database schema but is not exposed by the public
  WSGI application.
- The `reports/`, `state/`, `output_library/`, and imported playlist files are
  runtime data and are ignored by Git.
- Public chart ZIPs and temporary verified artifacts are transient. Local
  downloaded `.sng` files are intentionally durable because they are the user's
  library output.

`database.py` is the sole persistence boundary. Schema changes are additive and
must update its migration logic and offline tests together.

## Matching Method

`matching.py` is deterministic and does not use popularity data or an ML model.
It combines:

- normalized title and artist similarity;
- duration and ISRC evidence when available;
- version-marker handling for live, remix, acoustic, cover, and related forms;
- required five-fret guitar and optional six-fret/no-guitar rules;
- official-game, full-band, rated-guitar, direct-download, and metadata-quality
  bonuses;
- penalties for single-instrument, unrated, modchart, issue, and wrong-version
  candidates.

Charts with no playable instrument content and no difficulty ratings are hard
rejected. A final score is capped at 100, but `ScoreBreakdown.quality` remains a
separate planner tie-break so two saturated scores do not become arbitrary.
Legacy `decision` values and public-workflow `eligibility` values are kept
separate for compatibility.

## Bounded Continuation Follow-up

The result-tier and candidate-level selection foundation deliberately does not
turn the current 10/25/50 artist-expansion presentation windows into permanent
discovery limits. The follow-up implementation must:

- materialize deterministic pages from the finite checksum-valid local catalog;
- bind every page to both the exact generation and catalog snapshot;
- preserve existing candidate-level selections while appending new rows;
- retain cross-artist song ownership and artifact/content deduplication;
- use bounded page sizes and an explicit hard safety ceiling;
- issue zero Enchor search requests.

Large delivery is a separate packaging concern. Before archive creation the
server must estimate selected artifact count and bytes, then split oversized
deliveries into bounded numbered ZIP packages instead of reducing discovery
yield or silently dropping selected charts.

## Source Policy

All source-specific behavior belongs behind `ChartSource`. Enchor uses:

- `POST https://api.enchor.us/search` for normal searches;
- `POST https://api.enchor.us/search/advanced` for paginated artist searches;
- `GET https://files.enchor.us/{md5}.sng` for direct chart downloads.

Enchor's `md5` identifies the catalog record but is not guaranteed to hash the
served `.sng` bytes, so the adapter does not perform a false checksum check.
Transient query errors are not cached. Successful responses are cached for seven
days, and the adapter uses adaptive throttling plus bounded retries.

## Security Model

- Spotify PKCE avoids storing a Spotify client secret.
- Public refresh tokens are encrypted with Fernet using
  `TOKEN_ENCRYPTION_KEY`; the key comes from the environment or a secret
  manager, never `config.yaml`.
- Public sessions are signed with `SECRET_KEY` and backed by revocable rows in
  `user_sessions`.
- User-supplied paths are resolved and checked before writes, folder exports,
  archive extraction, or browser delivery.
- Uploads have a configured size limit. SoundCloud browser export reads only
  rendered metadata and never reads cookies, credentials, audio URLs, or private
  endpoints.
- OAuth and token-like values are redacted from logs and diagnostics.

## Repository Map

```text
src/csv_to_clonehero/
  cli.py                  command-line entry points
  config.py               validated configuration and state migration
  models.py               shared dataclasses
  normalization.py        metadata normalization and query sanitization
  csv_import.py           Spotify CSV and iTunes/Music importers
  providers/              music-library provider adapters
  sources/                chart-source protocol and Enchor adapter
  matching.py             deterministic scoring and eligibility gates
  planner.py              candidate selection and storage budgeting
  pipeline.py             local scan/search/plan/build orchestration
  spotify_pipeline.py     resumable Spotify discovery orchestration
  combined_pipeline.py    local catalog-backed multi-provider discovery
  library_inventory.py    read-only Songs-folder inventory
  exporter.py             validated folder and ZIP export service
  downloader.py           resumable direct chart downloads
  database.py             SQLite persistence and migrations
  reporting.py            user-facing report generation
  webapp.py               local Flask application
  server/                 public Flask application, jobs, auth, and delivery
tests/                    offline unit and integration-boundary tests
browser_export/           optional rendered-metadata SoundCloud userscript
catalog/                  ignored generated Enchor metadata snapshot
```

Run `python -m pytest` for the offline suite. Live Enchor checks are marked
`integration` and are excluded by default because the service is rate-limited.
