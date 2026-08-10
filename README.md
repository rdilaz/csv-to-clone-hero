# CSV to Clone Hero

Turn exported Spotify playlist CSV files into a personalized Clone Hero song
library — automatically.

**Double-click `WEB_INTERFACE.bat` → upload your CSVs in the browser → click
"Build library".** (Or, if you prefer: drop CSVs into `input_playlists` and
double-click `BUILD_LIBRARY.bat`.)

The program reads your playlists, finds every song on the
[Chorus Encore (Enchor)](https://enchor.us) chart index, intelligently scores
the matches, downloads the good ones, and *also* adds more songs by the
artists you clearly like. Everything lands in a tidy folder hierarchy that
Clone Hero can scan directly.

---

## Quick Start (Windows)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   During installation, tick **"Add python.exe to PATH"**.
2. **Double-click `WEB_INTERFACE.bat`.** A local web page opens
   (everything runs on your own computer; nothing is sent anywhere except the
   chart search server). The first run takes a minute to set up Python —
   that's normal.
3. In the web page:
   - **Step 1** has direct links to the
     [Chosic exporter](https://www.chosic.com/spotify-playlist-exporter/) and
     [Exportify](https://exportify.net/) — log in with Spotify there and
     download your playlists as CSV.
   - **Step 2**: drag the downloaded CSV files onto the page (or use the
     file picker). You can remove files there too.
   - **Step 3**: click **"Dry run"** first (searches and plans, downloads
     nothing) and look at the results; then click **"Build library"**.
   - **Step 4 (optional)**: click **"Load review list"** to see the matches
     the program wasn't sure about (score 70–89). Click ✓ to accept one
     (it downloads on the next build) or ✗ to reject it permanently.
     Decisions apply the next time you run a dry run or build.
4. *(Optional)* Edit **`config.yaml`** in Notepad and set
   `clone_hero_songs_path` to your Clone Hero songs folder, e.g.
   `X:\Windows Standalone\PlayerData\Songs`.
   If you skip this, the library is built in the local `output_library` folder.
5. Open Clone Hero → **Settings → Scan Songs** (or just restart it).
6. Play. 🎸

Prefer the classic way? Put the CSVs in **`input_playlists`** and
double-click **`DRY_RUN.bat`** then **`BUILD_LIBRARY.bat`** — same engine, no
browser.

You can re-run the build any time. It remembers what it already downloaded
and won't download things twice. Adding a new playlist CSV and re-running
just adds the new songs. Large collections take a while the first time —
Enchor rate-limits searches, so ~3000 songs means roughly an hour of polite,
rate-limited searching (leave the page open; progress is shown). Results are
cached for 7 days, so later runs and retries are much faster. You can trade
speed for politeness via `network.concurrency` / `request_delay_seconds` in
`config.yaml`.

If anything goes wrong, double-click **`CHECK_SETUP.bat`** — it checks your
Python, folders, disk space and internet connection and tells you what's wrong.

### Using it from another device (phone, etc.)

The web interface binds to `127.0.0.1` (this computer only) on purpose, for
safety. To use it from your phone on the same Wi-Fi, start it manually with

```
python -m csv_to_clonehero web --port 8765
```

from a terminal *after* editing `webapp.py`'s host from `127.0.0.1` to
`0.0.0.0`, then browse to `http://<your-computer-ip>:8765`. Only do this on a
network you trust — there is no login.

---

## What it does, in more detail

### 1. Exact playlist songs

Every unique song in your CSVs is searched for individually. Duplicate rows
(the same song in several playlists) are merged — playlists are remembered.

Candidate charts are **scored from 0 to 100** using artist similarity, title
similarity, remaster/edition-aware normalization (`Everlong - 2015 Remaster`
matches the chart `Everlong`), instrument compatibility, chart quality
signals, and penalties for wrong-version charts (covers, live versions,
remixes) or conflicting artists. A chart titled `Low` by *Nirvana* will never
be accepted for *Flo Rida's* `Low`, no matter how exact the title is.

| Score   | Outcome                                             |
|---------|-----------------------------------------------------|
| 90–100  | **Auto-accepted** — downloaded automatically        |
| 70–89   | **Review** — in the web UI (step 4) and `reports/review_needed.csv`; not downloaded unless you accept |
| below 70| **Rejected** — listed in `reports/no_matches.csv` / `reports/rejected_candidates.csv` |

Thresholds are configurable in `config.yaml`.

### 2. Artist expansion

After the exact songs, the program looks at the artists in your playlists and
adds more of their songs:

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

### 5. Folder layout

```
output_library\                      (or your Clone Hero songs folder)
  00 - His Music\
    Exact Playlist Songs\
      <Artist>\<Artist> - <Title> [<Charter>].sng
    Artist Expansion\
      <Artist>\<Artist> - <Title> [<Charter>].sng
```

Downloads are `.sng` files, which Clone Hero scans directly — no unzipping
needed. Windows-invalid filename characters and reserved names are handled
automatically.

---

## Reports

Every run writes `reports\`:

| File | Contents |
|------|----------|
| `summary.txt` / `summary.json` | The headline numbers |
| `exact_matches.csv` | Playlist songs that were matched & accepted |
| `artist_expansion.csv` | Extra songs added by artist expansion |
| `review_needed.csv` | Ambiguous matches for a human to check (includes scores, explanations, and links) |
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
python -m csv_to_clonehero plan          # compute the download plan
python -m csv_to_clonehero build         # everything, including downloads
python -m csv_to_clonehero build --dry-run
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
circumvent restrictions. New sources can be added by writing an adapter that
implements the `ChartSource` interface (`src/csv_to_clonehero/sources/base.py`);
sources without a legitimate direct-download mechanism can still be used for
discovery, with downloads left for manual review.

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
  config.py         config.yaml loading (pydantic)
  models.py         Track / ChartCandidate / ScoreBreakdown
  normalization.py  music-metadata string normalization
  csv_import.py     Spotify CSV parsing + deduplication
  matching.py       the scoring engine
  planner.py        selection + storage budget
  downloader.py     resumable, validated downloads
  database.py       SQLite state (cache, scores, downloads)
  reporting.py      reports/ generation
  filesystem.py     Windows-safe paths, safe zip extraction
  overrides.py      overrides.yaml
  pipeline.py       scan/search/plan/build orchestration
  sources/
    base.py         ChartSource adapter interface
    enchor.py       Chorus Encore / Enchor adapter
```
