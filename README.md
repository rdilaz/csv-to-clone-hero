# CSV to Clone Hero

Turn your exported music library — Spotify playlist CSVs or iTunes/Apple Music
exports — into a personalized Clone Hero song library, automatically.

**Double-click `WEB_INTERFACE.bat` and sign in with Spotify.** The simple page
syncs playlist names first, lets you choose an explicit search scope, inventories
an existing Songs folder, estimates Enchor work, and then finds charts only for
the selected scope. (The full CSV interface with per-song review lives at
`http://127.0.0.1:8765/advanced`; or, if you prefer no browser: drop files
into `input_playlists` and double-click `BUILD_LIBRARY.bat`.)

The program reads your playlists, finds every song on the
[Chorus Encore (Enchor)](https://enchor.us) chart index, intelligently scores
the matches, and builds a tidy artist/album folder hierarchy that Clone Hero
and YARG can scan directly.

---

## Quick Start (Windows)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   During installation, tick **"Add python.exe to PATH"**.
2. **Double-click `WEB_INTERFACE.bat`.** A local web page opens
   (everything runs on your own computer; nothing is sent anywhere except the
   chart search server). The first run takes a minute to set up Python —
   that's normal.
3. On the simple page, click **Sync Spotify library**, select playlists and
   optionally Liked Songs, then save the scope. Nothing contacts Enchor before
   this selection. Optionally enter an existing Clone Hero/YARG Songs folder and
   click **Inventory existing library**; only exact artist/title metadata matches
   are marked already owned. Invalid archives are reported and never deleted.
4. Review the cache-aware estimate and click **Find charts in selected scope**.
   Searches are rate-limited, cancellable, and resumable from successful query
   cache rows if the local app is stopped. Enable browser notifications from the
   button after sign-in if you want a desktop completion notice.
5. Review candidates, select charts, choose an output folder, and export. Then
   open Clone Hero or YARG and scan the Songs folder.
6. Open Clone Hero (or YARG) → point it at the output folder
   (**Settings → Songs**) → **Scan Songs**. Both games read `.sng` files
   recursively, so the artist/album folders just work.
7. Play. 🎸

Want per-song review, dry runs and work estimates? Open
**`http://127.0.0.1:8765/advanced`** (linked at the top of the simple page).
Review decisions you make there are honored by simple-mode runs too.

Prefer the classic way? Put the export files in **`input_playlists`** and
double-click **`DRY_RUN.bat`** then **`BUILD_LIBRARY.bat`** — same engine, no
browser.

You can re-run the build any time. It remembers what it already downloaded
and won't download things twice — and if you switch the folder layout in
`config.yaml`, existing downloads are moved into the new layout automatically
on the next build. Adding a new playlist file and re-running just adds the
new songs. The app keeps its state (search cache, review decisions, disabled files) in
`state\`, outside the output folder, so changing the output folder never
loses it. Large collections take a while the first time —
Enchor rate-limits searches, so ~3000 exact songs means roughly an hour of
polite, rate-limited searching (leave the page open; partial review results
and progress are shown).
**Catalog-first search cuts that way down:** artists with 3 or more unique
playlist songs are searched once by artist and their tracks are scored
locally, with an automatic per-song fallback if the catalog misses. Artist
expansion reuses the same cached artist catalogs, so it adds almost no extra
queries for cataloged artists. Results are cached for 7 days, so later exact
passes and the expansion pass reuse fresh exact results. This application
uses the same Enchor search/download service as Chorus Encore/Bridge, so it
is not intended to make an individual lookup faster; its benefit is batch
orchestration, scoring, safe review decisions, folder placement, reports, and
resumable downloads. You can trade speed for politeness via `network.concurrency`
/ `request_delay_seconds` in `config.yaml`, or tune/disable catalog-first
search with the `search.*` keys.

If anything goes wrong, double-click **`CHECK_SETUP.bat`** — it checks your
Python, folders, disk space and internet connection and tells you what's wrong.

### Using it from another computer

The app itself always runs on this PC (it needs the local Spotify login, state
database, and downloads). A **named Cloudflare Tunnel** on `ryo-nd.com` gives it
a permanent HTTPS address: **https://charts.ryo-nd.com**. The tunnel, DNS
record, and tunnel config are already set up; the address never changes.

#### 1. Start the tunnel

Double-click **`REMOTE_TUNNEL.bat`** and leave the window open.

#### 2. Start the app

Double-click **`WEB_REMOTE.bat`** and leave the window open. It prints a
private link:

```
https://charts.ryo-nd.com/?access_token=...
```

The token is fixed, so this link stays the same between restarts. Anyone with
the link can use the app — keep it private.

#### 3. One-time Spotify callback registration

In the Spotify Developer Dashboard, open this project's application, choose
**Settings**, and add this exact URL under **Redirect URIs**:

```
https://charts.ryo-nd.com/api/auth/spotify/callback
```

This only needs to be done once because the address is permanent.

#### 4. Link the Clone Hero Songs folder (before scanning)

In step **1 · Clone Hero library**, paste the Songs folder path on the server
(for example `D:\Songs` or the Clone Hero install's `Songs` folder) and click
**Link & scan**. This does three things:

- verifies the app can reach and write to the folder (**Verify** re-checks
  anytime, including after a scan),
- indexes every existing chart so discovery marks those songs **already owned**
  and skips them,
- makes the folder the only place remote exports are allowed to write.

**Check folder health** scans the linked folder for broken archives,
suspicious files (missing metadata, unusually small), and duplicate songs.
**Quarantine problem files** moves them into a `… - quarantine` folder beside
the library — nothing is ever deleted.

#### 5. Sign in and run it from the other computer

1. Open the private link from step 2 on the other computer or phone.
2. Click **Sign in with Spotify** and finish authorization in that browser.
3. Choose playlists and click **Sync Spotify library**.
4. Save a scope, then start discovery and wait for it to finish.
5. Select chart matches (or use **Select all recommended charts**).
6. Click **Export selected charts to linked library** — charts are written
   straight into the linked Songs folder. Identical charts already present are
   skipped, so nothing is downloaded twice.
7. Fallback: **Download selected charts as ZIP** saves a ZIP on the device you
   are using instead.

Remotely, do not use **Choose folder**, **Open folder**, or the old
`/advanced` CSV interface — those act on the host computer by design.

To make the tunnel start automatically at boot instead of
`REMOTE_TUNNEL.bat`, run once in an elevated PowerShell:

```
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" --config "$env:USERPROFILE\.cloudflared\config.yml" service install
```

---

## What it does, in more detail

### 1. Exact playlist songs

Every unique song in your export files is matched. By default, **catalog-first
search** groups songs by primary artist: artists with 3 or more unique tracks
get a single paginated artist-catalog query (up to 5,000 charts), and all of
their songs are scored locally against that catalog. If a song isn't found in
the catalog, or if the artist query fails, it automatically falls back to a
per-song search, so recall stays identical while the request count drops
roughly 3–4× on typical libraries. Artists below the threshold, and any songs
whose catalog pass misses, still use per-song queries.

Candidate charts are **scored from 0 to 100** using artist similarity, title
similarity, remaster/edition-aware normalization (`Everlong - 2015 Remaster`
matches the chart `Everlong`), instrument compatibility, chart quality
signals, and penalties for wrong-version charts (covers, live versions,
remixes) or conflicting artists. A chart titled `Low` by *Nirvana* will never
be accepted for *Flo Rida's* `Low`, no matter how exact the title is.

Quality gates keep the library clean: charts with no playable content at all
(no instruments, no difficulty ratings) are hard-rejected, and official
full-band charts always outrank single-instrument or issue-flagged charts of
the same song.

| Score   | Outcome                                             |
|---------|-----------------------------------------------------|
| 90–100  | **Auto-accepted** — downloaded automatically        |
| 70–89   | **Review** — in the web UI (step 4) and `reports/review_needed.csv`; not downloaded unless you accept |
| below 70| **Rejected** — listed in `reports/no_matches.csv` / `reports/rejected_candidates.csv` |

Thresholds are configurable in `config.yaml`.

### 2. Artist expansion

After the exact songs, an optional expansion pass can look at the artists in
your playlists and add more of their songs:

| Unique playlist songs (as primary artist) | Extra charts downloaded |
|-------------------------------------------|-------------------------|
| 5 or more                                 | up to 50                |
| 2–4                                       | up to 25                |
| 1                                         | up to 10                |
| only appears as featured artist           | 0                       |

Expansion charts must have 5-fret guitar, pass a quality bar, and never
duplicate your exact matches. Official Guitar Hero / Rock Band conversions
(charted by Harmonix/Neversoft etc.) are preferred when available.

All of these numbers are configurable.

### 3. Guitar-first

Only charts with playable **5-fret lead guitar** are selected by default.
Drums-only, vocals-only, 6-fret-only and modcharts are excluded unless you
explicitly allow them in `config.yaml`.

### 4. Storage budget

The default budget is **48 GiB** — sized for a ~58.8 GB USB stick that also
holds Clone Hero itself. Real download sizes are checked before planning;
exact playlist songs always outrank artist-expansion songs when space runs
out, and anything that doesn't fit is listed in
`reports/skipped_for_space.csv` instead of breaking the build.

### 5. Folder layout (hybrid)

```
output_library\                      (or your Clone Hero songs folder)
  <Artist>\<Album>\<Artist> - <Title> [<Charter>].sng        exact playlist songs
  <Artist>\Singles\...                                       exact songs with no album info
  <Pack Name>\<Artist> - <Title> [<Charter>].sng             artist-expansion songs
```

The album grouping comes from *your* export metadata, not the chart's (chart
album tags are unreliable). Very long names are shortened — the title
truncates first, artist/album folders stay intact — so paths stay safely
under the Windows limit.

Downloads are `.sng` files, which Clone Hero and YARG scan directly — no
unzipping needed. Both games read song metadata from inside the `.sng`, so
folder names are purely for you. Only `.sng` files live in the library root;
reports stay in `reports\`. Windows-invalid filename characters and reserved
names are handled automatically.

Alternatives via `library.layout` in `config.yaml`: `artist_album` (everything
by artist/album), `pack` (everything by pack), or `legacy` (the pre-1.0
`Exact Playlist Songs` / `Artist Expansion` sections). Switching layouts
relocates existing downloads on the next build.

---

## Reports

Every run writes `reports\`:

| File | Contents |
|------|----------|
| `summary.txt` / `summary.json` | The headline numbers |
| `master_list.csv` | **Your whole library at a glance**: every imported song grouped artist → album → song, with match status and the selected chart |
| `exact_matches.csv` | Playlist songs that were matched & accepted |
| `artist_expansion.csv` | Extra songs added by artist expansion |
| `review_needed.csv` | One best/current row per review track, with alternatives, scores, explanations, and links |
| `no_matches.csv` | Playlist songs with no acceptable chart |
| `rejected_candidates.csv` | Candidates that were considered and rejected, with reasons |
| `downloaded.csv` | Everything that was downloaded |
| `download_errors.csv` | Anything that failed |
| `skipped_for_space.csv` | Selections dropped to respect the storage budget |

## Overrides (optional)

Copy `overrides.example.yaml` to `overrides.yaml` to:

- force-accept a specific chart for a specific song (use the `track_key` and
  `chart_id` from `review_needed.csv`);
- reject a bad chart everywhere;
- ignore a song;
- disable/limit expansion for an artist, or search an artist under an alias;
- alias a song title.

You never need this file for normal use.

## Command-line usage

The `.bat` files cover normal use, but everything is also available directly:

```
python -m csv_to_clonehero web           # local web interface (browser UI)
python -m csv_to_clonehero scan          # parse CSVs only
python -m csv_to_clonehero search        # search + score charts
python -m csv_to_clonehero search --exact-only
python -m csv_to_clonehero expand --dry-run # cached exact pass + artist expansion
python -m csv_to_clonehero plan          # compute the download plan
python -m csv_to_clonehero plan --exact-only
python -m csv_to_clonehero build         # everything, including downloads
python -m csv_to_clonehero build --dry-run
python -m csv_to_clonehero build --exact-only
python -m csv_to_clonehero report        # regenerate reports from saved state
python -m csv_to_clonehero doctor        # check setup and connectivity
python -m csv_to_clonehero reset-cache   # clear the search cache (keeps downloads)
```

Search results are cached for 7 days (configurable), so re-runs are fast and
polite to the Enchor server.

## Chart sources & ethics

The only built-in source is **Chorus Encore / Enchor** (`api.enchor.us` for
search, `files.enchor.us` for downloads) — the same endpoints used by
[Bridge](https://github.com/Geomitron/Bridge), the official Chorus Encore
desktop downloader. Charts there are community-made files intended for Clone
Hero/YARG, and downloads are the site's own supported mechanism.

The program deliberately does **not** scrape piracy sites, bypass anything, or
circumvent restrictions. The pipeline is multi-source-ready (search, scoring,
planning and downloads all operate on a list of sources, and the highest
quality chart wins regardless of origin), but Enchor is currently the only
built-in adapter — no other legitimate public chart index with a search +
direct-download API verified at last check. New sources can be added by
writing an adapter that implements the `ChartSource` interface
(`src/csv_to_clonehero/sources/base.py`); sources without a legitimate
direct-download mechanism can still be used for discovery, with downloads
left for manual review.

### Known source limitations

- Enchor's `md5` identifies a chart but does **not** match the served `.sng`
  bytes (the server re-packs files), so downloads are validated by size and
  file format instead of checksum.
- Enchor provides no popularity ranking, so artist expansion prefers official
  game conversions and chart-quality signals rather than fabricated
  "popularity".

## Troubleshooting

- **"Python was not found"** — install Python from python.org and tick
  "Add python.exe to PATH".
- **A song you know exists wasn't matched** — check `reports\review_needed.csv`
  and `reports\no_matches.csv`; force it with `overrides.yaml` if needed.
- **Weird matches** — lower `auto_accept_threshold` caution aside, raise it in
  `config.yaml` (e.g. 92) to be stricter.
- **Reset everything** — delete the `state` folder (keeps nothing), or run
  `python -m csv_to_clonehero reset-cache` to re-search but keep downloads.
- **Rate limiting / server errors** — increase `request_delay_seconds` in
  `config.yaml`. Transient failures are retried automatically.

## Development

```
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pytest                # unit tests (offline)
.venv\Scripts\pytest -m integration # live Enchor API tests
```

Project layout:

```
src/csv_to_clonehero/
  cli.py            command-line interface
  config.py         config.yaml loading (pydantic), state DB migration
  models.py         Track / ChartCandidate / ScoreBreakdown
  normalization.py  music-metadata string normalization
  csv_import.py     Spotify CSV + iTunes/Apple Music (.txt/.xml) parsing, dedupe
  matching.py       the scoring engine (quality gates)
  planner.py        selection + storage budget
  downloader.py     resumable, validated downloads
  database.py       SQLite state (cache, scores, downloads, master list)
  reporting.py      reports/ generation (incl. master_list.csv)
  filesystem.py     Windows-safe paths, library layouts, safe zip extraction
  overrides.py      overrides.yaml
  pipeline.py       scan/search/plan/relocate/build orchestration
  webapp.py         local web UI (simple mode at /, full interface at /advanced)
  sources/
    base.py         ChartSource adapter interface
    enchor.py       Chorus Encore / Enchor adapter
```
