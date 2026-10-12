# Outcome Report (TL;DR)

Riff now has a persistent player and full-catalog browser on its public surface. Users can search the installed community chart catalog, preview available chart audio, queue and skip charts, and explicitly add a chart to their final ZIP. Importing a music library is optional for catalog choices. The listening queue, saved ZIP choices, and verified packaged files remain separate states.

This candidate continues the UX recovery source at commit 5dd015ec9067ec4e32afce4a0030b225d0032878. Public assets identify riff-catalog-20261011 and SQLite schema is 23. This is a review candidate; merging, deployment and production acceptance are separate owner actions. It does not establish that the original hosted NetworkError is fixed.

## Product structure

The public base template contains one native audio element and one persistent dock outside the five journey sections. Browse opens a side panel with search, counts, chart cards and pagination; Queue opens the ordered listening list. The vinyl button plays and pauses the current preview. Previous, Next, seek and volume operate on the listening queue. Each card and the current player expose Add to ZIP independently of playback. Catalog-cohort picks also expose Remove from ZIP in cards and the Queue panel; removing a choice preserves listening and can be restored by eligible global undo. Imported-library and expansion choices retain their own review controls.

Catalog pages return at most 100 records. Search matches artist, title, album and charter. Seeded shuffle operates across the installed catalog, rather than the current page or imported tracks. The listening queue retains at most 200 metadata records in owner-separated session storage, restores paused, and contains no audio blobs or ZIP credentials. Continue through catalog is enabled by default: after an explicit Play action, queued picks take priority and queue exhaustion lazily reads more metadata from the captured catalog search/order. Consumed records can be pruned to keep the resident queue bounded while playback advances through more pages. This resident bound is not a catalog listening limit. Users can turn continuation off. Automatic preview failure advancement stops after three failed entries; missing or exhausted catalog pools are explained. Audio starts after an explicit user gesture. A full page reload, sign-out or external OAuth navigation stops playback.

About, Terms, Privacy and DMCA open within the persistent shell. The journey remains mounted during these visits; returning Home resumes it. A direct visit to one of these pages can mount Home into the existing shell without replacing the audio element. Native external links, modified clicks and login redirects retain ordinary browser navigation.

The phone dock uses two rows; seek and volume move into the Queue panel. Its measured height reserves page space, including the safe-area inset. The prior ZIP selection rail remains in page flow so two fixed rails do not overlap. Essential controls have a 44 CSS pixel target height. Catalog animation obeys reduced motion and the existing Motion toggle; playback does not depend on animation.

## Catalog and selection contracts

- GET /api/catalog accepts limit, offset, q, order=source or shuffle, seed, and optional snapshot_token. It returns actual installed total_charts and total_songs, total_filtered_charts, items, has_more, readiness and guarded selection receipts. The product does not hard-code a 150,000 count.
- GET /api/catalog/{source}/{chart_id} resolves a chart's current metadata and preview descriptor.
- GET /api/catalog/selections returns the owner's separately paginated catalog choices.
- POST /api/catalog/selection accepts catalog_key, snapshot_token, selection_revision, exact_generation_id and action=select or clear. Production requires an authenticated same-origin request with X-Riff-Request: 1. Add requires the current snapshot token; Clear affects this catalog scope only and works without a current catalog. The generation guard is checked when supplied.

Anonymous catalog browsing is permitted; ZIP selection requires the existing signed-in owner workspace. Cold catalog reads queue local index preparation on the existing shared worker and return an observable 202 state. Missing or invalid catalog data returns unavailable. These reads do not crawl live search APIs or create per-user catalog copies. The immutable metadata index is shared by snapshot identity; user choices remain owner-scoped.

Schema 23 adds a partial uniqueness index for workspace catalog candidate projections without an imported track. It reuses workspace_candidates, workspace_selections, the selection revision clock and bounded global undo history. Catalog choices use stored kind catalog and ELIGIBLE_REVIEW; export validates them as CATALOG_CHOICE with no invented import-match score. Adding an artifact already selected from an imported song or artist expansion is a no-op that preserves that existing choice. Imported-library bulk changes retain independent catalog choices. Current artifact eligibility, revision, supplied discovery generation, snapshot row identity and active jobs are checked before saving or restoring choices. Global undo retains its matching revision/generation guard: a later or failed matching scan can disable an older undo even while catalog choices persist and remain removable.

Ordinary selection receipts add catalog_summary; full mutation/history state also includes selected.catalog and summary.catalog. Selected catalog records include artifact identity and items expose selected_catalog_key so aliases for the same package keep their badges and remove the canonical owned catalog choice, including when catalog metadata later becomes unavailable. Removing an alias preserves other owners and imported/expansion scopes. The journey's selection rail and Download ledger show imported-library, artist-expansion and catalog counts separately. Unique song counts group normalized artist/title identities. Catalog-only ZIP preparation works at discovery generation zero. A completed ZIP receipt still describes its original snapshot; adding later choices does not silently rewrite it. Review ZIP opens the current choices for another guarded preparation.

## Chart audio pipeline

Catalog metadata is not an audio library. Audio lives inside community SNG chart packages. The browser opens a verified Enchor package address only after Play, reads its header/index through byte ranges, and transfers recognized audio members. This follows the provider's package model; it does not extract Spotify audio or request an invented audio-preview API. The independent SNG reader follows the MIT-licensed SNG format specification; no Bridge GPL implementation was copied.

Only HTTPS files.enchor.us addresses derived from a 32 hexadecimal character package MD5 are accepted. Video packages use the provider's _novideo variant. The public CSP narrowly permits that origin for fetch and self/blob media. Package audio is not relayed through Riff's server, and listening does not consume a signed browser ZIP link.

An existing full song mix uses a native audio blob. Common song, guitar, bass, rhythm, keys, vocals and drums stems are read and decoded serially, combined at 22,050 Hz into a stereo clip of at most 45 seconds, gain-adjusted and peak-clamped. The source preview offset is used when valid. Unknown arrangements, unsupported browser codecs, failed ranges and excessive inputs produce an unavailable message; a lone instrument stem is not presented as the complete mix.

Application bounds include 256 KiB metadata/index sections, 512 metadata pairs, 1,024 package members, a 1 GiB address space, 24 MiB compressed audio transfer, 1 MiB range chunks, 15-second reads and at most twelve mixed stems. Mixed previews reject excessive reported duration and decoded frames/channels. Native decodeAudioData is browser-controlled and cannot be cancelled mid-decode: these bounds do not promise a hard peak native-decoder memory ceiling for a maliciously misreported file. Stale decoded outputs are discarded before further fetches or playback; cancelled reads and blob URLs are cleaned up.

Metadata presence means preview availability is unverified until opened. A successful synthetic preview test does not establish that every installed chart has reachable, supported audio. Real provider availability, range behavior through the production browser, physical-device playback and the installed production catalog count require acceptance on that host.

## Journey and surface boundaries

- Add Music: import the owner's music metadata or open Browse immediately; catalog choices do not fabricate imported songs.
- Find Charts: matching still uses the library workflow and existing identity gates. Playback and browsing remain available; active discovery blocks selection mutations with an explanation.
- Build Setlist: matched chart previews use the persistent player. Saved choices show three origins, and guarded global undo can restore catalog changes.
- Download: catalog choices enter the existing file validation, artifact deduplication and ZIP preparation path. Listening does not prepare downloads, and active preparation blocks selection edits.
- Done: the verified ZIP receipt and browser handoff remain separate from the current listening queue. Users can continue listening, add new choices and explicitly review another ZIP.

The local loopback app retains its own folder/import/export workflow and state. No public folder picker, local filesystem write capability or cloud/local selection synchronization is added.

## UX audit continuity

The full twenty-principle audit remains in UX_RECOVERY_RELEASE.md. This change addresses its catalog and player friction with these concrete structures:

- Cognitive Load & Hierarchy: **Jakob's Law** gives familiar player controls; **Fitts's Law** keeps 44 CSS pixel targets; **Hick's Law** separates Play, Queue and Add to ZIP; **Miller's Law** separates catalog charts, unique songs, listening records and ZIP counts; **Progressive Disclosure** keeps browse and queue details in a panel.
- Visual Design & Structure: **Gestalt Principles** groups current audio and its controls; **Contrast / WCAG 2.2** retains readable dark controls and state text; **Visual Hierarchy** puts current song and explicit actions ahead of catalog details; **Spatial Consistency / Grids** reserves measured dock space and reflows at narrow widths; **Affordance** turns the small vinyl into a labeled native play/pause control.
- Feedback & Interaction: **Status of System** reports index, range and audio readiness; **Error Prevention** requires guarded explicit ZIP changes; **Forgiveness / Reversibility** provides queue removal and global selection undo; **Acceleration / Flexibility** supports full-catalog search, shuffle, paging, skip and queue order; **Consistency & Standards** retains native audio controls and independent public/local delivery paths.
- Behavior & Psychology: **Peak-End Rule** identifies queue completion and preserves ZIP receipts; **Zeigarnik Effect** retains paused queue metadata and saved ZIP choices separately; **Aesthetic-Usability Effect** keeps the studio palette without hiding unavailable audio; **Von Restorff Effect** reserves lime for the current/selected state and strongest action; **Doherty Threshold** exposes immediate loading feedback and local indexing status without blocking the shell.

Accessibility acceptance goals are **4.5:1 normal text** and **3:1 large text** under [WCAG 2.2 SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), and **3:1 essential control/state graphics against adjacent colors** under [SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html). Large text means at least 18pt regular or 14pt bold. [SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html) specifies 24 CSS pixels or an applicable exception; Riff's 44-pixel target is a distinct product goal. Disabled controls have the applicable contrast exemption, while explanations remain readable. Browser checks and palette measurements are evidence, not WCAG certification.

Self-audit: exactly twenty principles accounted for above, every added structural solution maps to a named principle, exact contrast goals cited, and all five journey steps covered without blending public and local capabilities.
