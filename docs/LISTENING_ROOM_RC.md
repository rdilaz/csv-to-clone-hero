# Riff Listening Room release

The Listening Room carries the compact companion's paper, olive, lime, vinyl, and typography into Studio, the catalog player, and the separate local folder journey. It uses the existing Python/Flask and browser JavaScript architecture.

## Public website

- Shared typography, color, focus, spacing, surfaces, controls, and status across Add Music, Find Charts, Studio, catalog/queue, and ZIP preparation/recovery.
- Actual playback drives vinyl rotation. Pause, buffering, completion, missing audio, reduced motion, and hidden pages stop movement without blocking playback controls.
- Search title, artist, album, and charter; filter by year-derived decade. Full, filtered, song, chart, and delivery counts retain separate meanings.
- Previous/Next, catalog shuffle, queue continuation/reorder/removal, mute/volume/seek, and paused restoration preserve cancellation guards.
- Listening never selects or downloads a chart. Explicit Add/Remove/Undo preserves snapshot, revision, generation, owner, same-origin, and alias-deduplication guards.
- The larger Listening deck shows the current track and actual idle, loading, playing, paused, and error states. Its action opens the catalog or controls the existing player; standby movement respects reduced-motion and the Motion setting.
- The public WSGI app serves a companion information page and a verified, static downloadable ZIP. It does not mount companion, local-preview, native picker, or local-library APIs.

## Music source imports

SoundCloud CSV/JSON and Music/iTunes TXT/XML exports import metadata into the existing private owner workspace for explicit chart matching. Uploaded Apple Music files are temporary, and only normalized song metadata and the chosen export label are retained. A new Apple Music export replaces that owner's previous Apple Music import; other sources are preserved.

One-click Apple Music account sync remains unconfigured and requires MusicKit setup and user consent. SoundCloud account sync is shown only when the existing approved application configuration is available. This release adds no credentials, account grants, provider-audio downloads, or combined provider streaming player.

## Local companion

The Windows download requires an existing CPython 3.12 x64 installation. It includes the limited, licensed runtime dependencies used by the companion; it does not include Python itself, an installer, a signed native executable, songs, a personal library inventory, catalog data, or credentials.

The companion binds to loopback only. Users supply an existing checksum-valid catalog snapshot and optional cache/library folders. Missing catalog or audio is shown honestly. Recognized multi-stem previews are capped at 45 seconds; cached presence does not promise supported, non-silent audio or full-track availability.

Library inspection remains read-only. Installed badges use song metadata rather than exact chart variants, and the current inventory recognizes `.sng` packages rather than unpacked song folders. Explicit additions are confined to a separate sandbox and never overwrite existing files. Real-library installation and native application packaging remain outside this release.

## Local review and validation

The original compact and unified previews remain separate from publication. The local review wrapper uses isolated owner/workspace state and cached media/export; it does not replace production authentication. To run it with existing dependencies, pass explicit local paths to `scripts/run_listening_room_preview.ps1`.

Before publication, browser validation covered the catalog/player, Studio, delivery recovery, separate local folder export, keyboard behavior, desktop and 320/390-pixel layouts, actual cached playback, artwork, mute, pause, reduced motion, and paused restoration. Offline suites covered catalog/year filtering, web/jobs, export/delivery, and wrapper boundaries. Synthetic fixtures and actual-media checks were recorded separately; overlapping suite totals must not be added.

The public asset version identifies the deployed UI independently of the legacy API compatibility release marker. The production image also reports its source revision through `/healthz`.
