# ADR 0001: Source-Neutral Master Chart Catalog

- Status: Accepted
- Date: 2026-09-03
- Scope: Source-coverage feasibility for GitHub Issue #2

## Decision

Do not add a RhythmVerse or Custom Songs Central (CSC) adapter yet, even behind
a disabled feature flag.

RhythmVerse has a technically usable internal browser endpoint, but its current
Terms of Use prohibit automated access and systematic catalog retrieval without
written permission. RhythmVerse also still describes its publicly documented,
token-authenticated API in the future tense. An MIT-licensed client using the
internal endpoint proves technical feasibility, not provider authorization or a
stable third-party contract.

Treat CSC as curation evidence on charts obtained from an authorized chart
catalog, currently Enchor. CSC is operationally a curated pack publisher, but
its public pages, RSS feed, and Clone Hero spreadsheet do not expose stable
per-song identities or manifests. A future CSC integration may be a curated
pack source if CSC publishes and supports a machine-readable manifest.

Define a separate source-neutral master-catalog format before adding another
source. Do not change the existing Enchor v1 snapshot in place. Its format,
integer identities, loader, and exporter are deliberately Enchor-specific.

## RhythmVerse Findings

These observations were made on 2026-09-03. Counts are volatile and are not
proof that records are unique, downloadable, or mutually exclusive.

### Catalog counts

RhythmVerse's public game page reported:

| View | Files available |
| --- | ---: |
| Clone Hero | 141,344 |
| Phase Shift | 2,758 |
| Rock Band 3, all platforms | 53,660 |
| Rock Band 3 Xbox 360 | 50,638 |
| Rock Band 3 Wii | 1,669 |
| Rock Band 3 PS3 | 1,353 |
| YARG | 191,982 |

These are compatibility views. They overlap and must not be summed. A single
bounded request to the internal all-formats listing reported
`total_filtered = 141,523`. Internal format-specific requests reported 87,408
`ch` rows and 2,790 `rv` rows, or 90,198 rows in the combined `chm` view. That
does not agree with the public 141,344 Clone Hero counter. There is no published
definition explaining the difference, so neither number can define a complete
production snapshot. No full crawl was performed.

### Current internal protocol

The current site and
[`clone-hero-chart-manager`](https://github.com/xlzipx/clone-hero-chart-manager)
use form-encoded requests:

```http
POST https://rhythmverse.co/api/{system}/songfiles/list
POST https://rhythmverse.co/api/{system}/songfiles/search/live
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

data_type=full&records=25&page=1&text=optional
```

Observed system slugs include `all`, `ch`, `chm`, `rv`, `ps`, and Rock Band
variants. Filters are repeated form fields such as `genre[]`, `instrument[]`,
`difficulties[]`, `year[]`, and `song_length[]`. Sorting uses fields such as
`sort[0][sort_by]=update_date` and `sort[0][sort_order]=DESC`. The dictionary
endpoint currently used by browser clients is:

```http
GET https://rhythmverse.co/api/{system}/dictionary/list
```

The listing response currently has this shape:

```json
{
  "status": "success",
  "data": {
    "records": {
      "total_available": 0,
      "total_filtered": 141523,
      "returned": 1
    },
    "pagination": {
      "start": 0,
      "records": "1",
      "page": "1"
    },
    "songs": [
      {
        "data": {
          "song_id": 110093,
          "record_id": "heartache-110093",
          "artist": "ONE OK ROCK",
          "title": "Heartache"
        },
        "file": {
          "file_id": "fa8117f2c6f1473a88c66db2d1de7807",
          "gameformat": "ch",
          "file_name": "ONE OK ROCK - Heartache.zip",
          "gameplay_file_hash": "5d349bbd1aed611f8fbf8bd2653f9693",
          "configuration_file_hash": "1ae6d292fb06b99323086c5422085636",
          "gamefile_hash": "98e46cee6ffbded7074d53946ad63510",
          "download_url": "/download_file/veralove82/fa8117f2c6f1473a88c66db2d1de7807/ONE OK ROCK - Heartache.zip",
          "download_page_url_full": "https://rhythmverse.co/download/fa8117f2c6f1473a88c66db2d1de7807"
        }
      }
    ]
  }
}
```

`file.file_id` is the best observed version/file identity and must be treated
as an opaque string. `data.song_id` and `data.record_id` group song metadata;
they do not identify a particular chart file. No provider guarantee was found
for the lifetime or reuse behavior of any identity or hash field. The three
file hash fields must not be treated as hashes of downloaded package bytes
until RhythmVerse documents their algorithms and coverage.

### Pagination and rate behavior

The public UI advertises 25 records by default, 100 records at most, and a
250-page ceiling. The generated class documentation says `records` is capped at
the configured API maximum. The internal endpoint nevertheless accepted 700
records in testing.

At commit
[`47357cf`](https://github.com/xlzipx/clone-hero-chart-manager/commit/47357cfed79036c923bdc3811da1e509a887e43f),
the MIT-licensed `clone-hero-chart-manager` works around deep pagination by
requesting 700 records per page, using concurrency 3, pausing 150 ms between
batches, and supporting resumable full and newest-first delta builds. Its code
reports an approximately four-minute RhythmVerse build. Relevant files are
[`rhythmverse.ts`](https://github.com/xlzipx/clone-hero-chart-manager/blob/47357cfed79036c923bdc3811da1e509a887e43f/app/src/main/core/rhythmverse.ts),
[`catalogsync.ts`](https://github.com/xlzipx/clone-hero-chart-manager/blob/47357cfed79036c923bdc3811da1e509a887e43f/app/src/main/core/catalogsync.ts),
[`gameformats.ts`](https://github.com/xlzipx/clone-hero-chart-manager/blob/47357cfed79036c923bdc3811da1e509a887e43f/app/src/main/core/gameformats.ts),
and
[`download.ts`](https://github.com/xlzipx/clone-hero-chart-manager/blob/47357cfed79036c923bdc3811da1e509a887e43f/app/src/main/core/download.ts).

Light requests returned HTTP 200 without standard rate-limit headers. No
official request quota, concurrency limit, retry contract, or cache policy was
found. The other client's observed tolerance is not a safe production rate.
Using a value above the UI and generated-documentation limit is also not a
supported complete-catalog boundary.

### Formats and downloads

RhythmVerse rows may provide `download_url`, `download_page_url`, and
`external_url`. They are not equivalent:

| Row type | First-version disposition |
| --- | --- |
| `ch` or `rv`, same-origin direct ZIP | Eligible only after provider approval and package validation |
| `ch` or `rv`, external direct URL | Defer until that host explicitly permits automation |
| Phase Shift `song.ini` plus `notes.mid` | Clone Hero-readable, but exclude from the first narrow source scope |
| RB3 Xbox CON | Exclude; requires conversion such as Onyx to Phase Shift |
| RB3 PS3 EDAT | Exclude; encrypted and may require user-specific keys |
| RB3 Wii | Exclude until a supported conversion and validation path is proven |
| Marketplace or `none` | Manual/unavailable, never claim direct download |
| MediaFire, MEGA, shortener, or browser page | Manual locator, not a machine-download contract |

A sampled same-origin `/download_file/...zip` returned a direct binary ZIP with
a content length. Other rows point to Google Drive, Dropbox, MEGA, MediaFire,
shorteners, source community pages, or official marketplaces. RhythmVerse's FAQ
says it normally indexes author-hosted content and links back to the original
source, except when creators opt into its external host. Therefore automation
must be decided per locator and host, not from the presence of a URL.

The current Riff library and validator accept `.sng` packages. Even an approved
RhythmVerse native ZIP needs a deliberate validate-and-package boundary rather
than being passed to the existing `.sng` downloader unchanged.

### Provider-policy blocker

[RhythmVerse's About page](https://rhythmverse.co/about) says it "will offer an
exhaustive publicly documented API" through secure tokens. No public onboarding,
token issuance, OpenAPI contract, version policy, or rate policy was found.
The generated PHP
[`RouteSongs` documentation](https://rhythmverse.co/docs/classes/RouteSongs.html)
describes implementation routes, but not a supported third-party contract, and
does not match all current browser paths.

[RhythmVerse's Terms of Use](https://rhythmverse.co/terms_of_use) prohibit:

- access through automated or non-human means;
- systematic retrieval that creates a collection, database, or directory;
- data mining, robots, data-gathering tools, scrapers, and offline readers;
- unauthorized scripts or other automated systems.

The terms require written permission for systematic retrieval. Consequently,
the internal endpoint cannot be promoted to a Riff production contract. The
MIT license on another client's code does not license RhythmVerse data or
service access.

The smallest proof that can unblock an adapter is written RhythmVerse approval
covering server-side search, full and delta catalog synchronization, metadata
retention, multi-user use, and same-origin downloads, together with the exact
supported token-authenticated endpoint, identity semantics, allowed rate,
pagination maximum, caching/refresh cadence, and withdrawal behavior. The
smallest subsequent technical proof is one approved search and one approved
same-origin `ch` or `rv` download.

## Custom Songs Central Findings

CSC is not currently equivalent to a general chart catalog.

The official site reported 73
[Quarterly and Monthly Pack](https://customsongscentral.com/tag/monthly-packs/)
posts and 34 [Setlist](https://customsongscentral.com/tag/setlists/) posts on
2026-09-03. It also separately publishes song packs/full albums and community
projects. Community projects are expressly work from outside CSC, so a post on
the CSC site is not by itself evidence that CSC reviewed each chart.

The official
[Clone Hero master spreadsheet](https://docs.google.com/spreadsheets/d/13B823ukxdVMocowo1s5XnT3tzciOfruhUVePENKc01o/edit)
is a directory of sets and drives, not a chart manifest. It tells users to use
Chorus to search for individual songs. On the observation date its CSC section
ended at Q1 2026, while CSC had published Q2 2026 on July 16. It does not provide
stable per-song CSC IDs, charters, revisions, checksums, instrument data, or
Enchor/RhythmVerse identity mappings.

CSC's standard [RSS feed](https://customsongscentral.com/rss/) and sitemap
provide stable publication URLs, Ghost GUIDs, dates, categories, and arbitrary
HTML content. They do not define a pack schema. Song lists vary among prose,
HTML lists, pasted tables, external spreadsheets, or no list. Download anchors
vary among CSC's Bunny CDN, Google Drive files/folders, and link shorteners.
Depending on HTML labels such as `Download` or `Mirror` would be DOM scraping,
not a provider contract.

The [Q2 2026 pack](https://customsongscentral.com/2nd-quarter-2026-video-games-4/)
has a directly retrievable 554,087,637-byte `.7z` on CSC's CDN and a Google
Drive backup with the same reported size. No checksum or archive manifest is
published, so equal size does not prove equal bytes. Other official releases
use `.rar`, multi-gigabyte `.7z`, Drive folders, or Bitly links. These are pack
artifacts rather than individual `.sng` files and are outside Riff's current
download/validation boundary.

CSC's
[quarterly submission guidelines](https://customsongscentral.com/monthly-pack-submission-guidelines/)
are useful quality evidence. They require full-difficulty guitar, lyrics,
accurate sync/tempo mapping, practice sections, solo markers, star power,
leading silence, defined metadata/assets, `icon = csc`, review by consensus,
and requested fixes before release. This supports a scoped curation annotation
for CSC monthly/quarterly charts. It must not be generalized to community
projects or every page hosted by CSC.

No CSC terms or automation policy, static chart/pack manifest, stable pack ID,
per-song ID, revision policy, checksums, or machine-readable withdrawal state
was found. Therefore:

- current model: curation/quality annotation on an Enchor chart;
- possible future model: curated pack source with an official manifest;
- rejected current model: independent per-song `ChartSource`;
- rejected dependency: page DOM, embedded RSS HTML, userscripts, or
  Tampermonkey-style inspection.

The smallest proof that can unblock CSC ingestion is a CSC-authored static
manifest for one release, plus permission to consume it. It must define a
stable pack ID and revision, artifact URL/size/SHA-256, per-song artist/title/
charter, source chart identities where available, polling expectations, and
withdrawal/replacement behavior.

## Measured Coverage Evidence

No RhythmVerse-to-Enchor or CSC-to-RhythmVerse overlap or unique-coverage
percentage is reported. Computing either requires automated RhythmVerse record
retrieval, which is prohibited without written permission, and headline
compatibility counters cannot substitute for record-level identity comparison.

A bounded CSC sample can be measured without acquiring CSC archives. The
official Q2 2026 page names 45 artist/title pairs and additionally mentions the
Super Mario Bros. NES soundtrack without enumerating its tracks. The 45 named
pairs were normalized with Riff's existing normalization and exact-matched
against the local complete Enchor snapshot:

| Measure | Result |
| --- | ---: |
| Enchor snapshot ID | `1787801228` (2026-08-27 03:27:08 UTC) |
| Enchor snapshot body SHA-256 | `868b09ab8b9f734cf9c066dcb0050e32796f677b9af45d91bba87a6c6a90fad4` |
| Enchor chart rows | 94,835 |
| Named Q2 2026 CSC artist/title pairs | 45 |
| Exact artist/title pairs present in Enchor | 38 (84.4%) |
| Exact pairs with `icon = csc` or CSC pack evidence | 37 (82.2%) |
| Enchor chart rows across the 38 matched pairs | 58 |
| Exact pairs not found | 7 (15.6%) |

This is a release-level sample, not a complete CSC coverage estimate. Exact
artist/title is suitable for measuring retrieval overlap but not for chart
dedupe. The 58 matching rows for 38 songs demonstrate why distinct chart
versions must remain separate. The unnamed soundtrack is excluded from both
the numerator and denominator. The seven misses may be true unique coverage,
metadata variation, or snapshot timing; they are not claimed as CSC-unique
charts without charter/version identities.

The seven exact misses were `Chris Christodoulou - Om Mani Padme Hum, Part
II`, `David Nowlin and Greg Weber - Johnny No More`, `Junichi Masuda - Champion
Rival Battle Music`, `Oli Wood - Ravenous Rapids`, `Sluggerfly - Meatgrinder`,
`Timothy J. Borquez & Mark Howlett - Sandwich Driving 101`, and `Toshihiko
Horiyama - Intro Stage (Zero)`. The input pairs are the 45 rows on the linked
official release page. The comparison key was exactly
`(normalize_artist(artist), normalize_title(title))` against every row in the
checksum-validated snapshot. Reproduction requires that ignored runtime
snapshot with the recorded body hash; no generated catalog is committed.

## Master Catalog Record

The smallest source-neutral current-record row is:

```json
{
  "type": "chart",
  "source": "source-key",
  "source_chart_id": "opaque-chart-version-id",
  "source_file_id": "opaque-file-id-or-empty",
  "source_revision_id": "opaque-provider-revision-or-empty",
  "observed_at": "2026-09-03T15:30:00Z",
  "artist": "Artist",
  "title": "Title",
  "version": "Live",
  "album": "Album",
  "charter": "Charter",
  "instruments": ["guitar", "bass", "drums"],
  "difficulties": {"guitar": 4, "bass": 3, "drums": 5},
  "format": "sng",
  "download": {
    "type": "direct_url",
    "locator": "https://provider.example/chart.sng"
  },
  "source_url": "https://provider.example/chart/123",
  "curation_evidence": [
    {"type": "pack", "value": "CSC Q2 2026", "url": "https://..."}
  ],
  "quality_evidence": [
    {"type": "source_review", "value": "approved", "url": "https://..."}
  ],
  "validation": {
    "state": "unverified",
    "reason": "artifact bytes have not been validated",
    "validator": ""
  },
  "content_hash": null
}
```

Field semantics:

| Field | Rule |
| --- | --- |
| `source` | Required canonical, case-normalized provider key |
| `source_chart_id` | Required opaque string for the source's chart/version record |
| `source_file_id` | Optional opaque string for one downloadable file/revision; never imply it is a hash |
| `source_revision_id` | Optional immutable provider revision; empty when the provider has no revision contract |
| `observed_at` | Required UTC observation time used by the validation/history ledger |
| `artist`, `title` | Required source text; normalized values are computed indexes, not interchange truth |
| `version` | Explicit source version label; empty means unknown, not original/default |
| `album`, `charter` | Source text; empty means unknown |
| `instruments` | Canonical instrument names known for this exact file |
| `difficulties` | Instrument-to-source-tier map; numeric tiers are not assumed comparable across sources |
| `format` | Exact known package/chart format, such as `sng`, `ch_zip`, or `phase_shift_zip` |
| `download.type` | At minimum `direct_url`, `source_api`, `manual`, or `none` |
| `download.locator` | Opaque source locator interpreted only by its adapter |
| `source_url` | Human-facing canonical chart or file page |
| `curation_evidence` | Provider-scoped factual provenance, never a derived match score |
| `quality_evidence` | Provider review/issues/instrument facts, never user-track-specific eligibility |
| `validation.state` | `verified`, `unverified`, `rejected`, or `unavailable`; non-verified states require a reason |
| `validation.reason` | Factual metadata/artifact validation result, not a matcher rejection |
| `content_hash` | Null or `{algorithm, value}` over verified complete artifact bytes |

The active source identity is `(source, source_chart_id, source_file_id)`. A
source without a separate chart/file split uses its version-level ID as
`source_chart_id` and leaves `source_file_id` empty. The append-only ledger
identity is `(source, source_chart_id, source_file_id, source_revision_id)` when
the provider supplies an immutable revision. Otherwise it additionally uses
the catalog snapshot ID and a canonical row hash. If bytes or metadata change
under unchanged provider IDs, preserve the previous observation and create a
new ledger revision. Never overwrite validation history merely because source
IDs stayed constant.

The snapshot header must declare completeness by source and scope:

```json
{
  "type": "master-chart-catalog",
  "format_version": 1,
  "complete": true,
  "sources": [
    {
      "source": "source-key",
      "scope": {
        "id": "provider-approved-clone-hero-native-v1",
        "formats": ["ch", "rv"],
        "locator_types": ["source_hosted_direct"]
      },
      "complete": true,
      "record_count": 123,
      "snapshot_id": "provider-snapshot-id",
      "body_sha256": "sha256-of-this-source's-canonical-rows"
    }
  ],
  "record_count": 123,
  "body_sha256": "sha256-of-all-newline-terminated-canonical-record-rows",
  "manifest_sha256": "sha256-of-canonical-format-sources-counts-and-body-hash"
}
```

An aggregate is complete only when every declared structured source scope is
complete, its count and source-row hash verify, the aggregate body hash
verifies, and `manifest_sha256` binds the format version, source scopes,
snapshot IDs, counts, and body hashes. A provider's absence from the header is
unknown coverage, not a negative match. Provider authorization and acquisition
method belong in the snapshot build manifest, not in each chart row.

This chart row intentionally does not model pack mirrors or pack membership as
chart identity. If CSC later publishes an approved pack manifest, retain that
as a separate pack entity with pack ID/revision, artifact size/hash, ordered
song entries, mirrors, and withdrawal/replacement state. Its song entries may
reference master chart identities or add curation evidence after an exact
identity/hash mapping; they must not turn artist/title similarity into identity.

## Cross-Source Dedupe

1. Never destructively dedupe on normalized artist and title. That pair is a
   retrieval/song-grouping key only.
2. Preserve every distinct source record identity. Different charters,
   explicit versions, instrument sets, and source file revisions remain
   separate records.
3. Missing charter or version is unknown. It does not equal every other missing
   or populated value.
4. Do not dedupe on URL, filename, album, duration, source-provided opaque hash,
   or CSC pack membership.
5. A source-provided hash is content evidence only when the provider contract
   states its algorithm and that it covers the exact served complete bytes.
   Enchor's `md5` does not satisfy this rule.
6. Cross-source artifact reuse requires successful package/playability
   validation, equal byte length, and equal SHA-256 over the complete bytes.
7. Identical verified bytes create artifact aliases; they do not erase either
   source record or its provenance.
8. Repacked artifacts with different byte hashes remain distinct. Optional
   semantic grouping may aid UI/retrieval but cannot authorize deletion or
   chart-identity collapse.
9. Apply CSC curation only from evidence attached to that exact candidate, such
   as its `icon = csc`, CSC pack/path metadata, an explicit source identity map,
   or a verified artifact-hash map. Artist/title overlap alone never grants a
   CSC annotation or quality bonus.

This follows the exporter's existing safe behavior: source identities are used
before materialization, and byte-level SHA-256 is used only after validation.

## Compatibility Consequences

`ChartCandidate.chart_id`, the `candidate_charts` unique constraint, catalog
identity sets, query caches, and manual decisions currently assume an integer
`(source, chart_id)` identity. `DownloadInfo` has no locator type or generic
verified content hash. The Enchor v1 catalog requires `type = enchor-catalog`
and serializes `ChartCandidate` directly. CSC packs use `.7z`/`.rar`, and
RhythmVerse native rows commonly use ZIP rather than Riff's `.sng` boundary.

A real implementation therefore requires a coordinated v2 catalog/import
change, opaque source identities, explicit locator/format semantics, and
artifact validation. Hiding the current internal RhythmVerse calls behind a
feature flag would not solve provider authorization, identity, or package
compatibility and would create false support.

## Implementation Gate

When a provider-authorized boundary exists, implement in this order:

1. Add and test a standalone v1 master-catalog row/header parser without
   changing the Enchor v1 loader.
2. Coordinate the core migration for opaque source identities across
   `ChartSource`, `ChartCandidate`, SQLite, caches, manual decisions, downloads,
   and exporter identity. The standalone parser must not feed the application
   before this migration.
3. Import only a provider-declared complete scope with structured scope and
   checksum validation.
4. Add a disabled-by-default source registration only for authorized search and
   metadata retrieval.
5. For RhythmVerse, first allow only provider-approved, same-origin direct
   `ch`/`rv` artifacts that are validated as Clone Hero-ready; exclude Phase
   Shift, every RB3 variant, external hosts, and manual locators.
6. For CSC, ingest only an official static manifest; otherwise annotate an
   Enchor row only from candidate-attached `icon`, pack, path, or verified-hash
   evidence without contacting CSC at runtime.
7. Measure overlap on source identities and verified hashes. Report normalized
   artist/title overlap separately as retrieval evidence, never as dedupe.

## Sources

- [RhythmVerse game/catalog page](https://rhythmverse.co/songfiles/game)
- [RhythmVerse About/API intent](https://rhythmverse.co/about)
- [RhythmVerse Terms of Use](https://rhythmverse.co/terms_of_use)
- [RhythmVerse FAQ and hosting policy](https://rhythmverse.co/faq)
- [RhythmVerse generated `RouteSongs` docs](https://rhythmverse.co/docs/classes/RouteSongs.html)
- [`clone-hero-chart-manager` at inspected commit](https://github.com/xlzipx/clone-hero-chart-manager/tree/47357cfed79036c923bdc3811da1e509a887e43f)
- [CSC quarterly/monthly packs](https://customsongscentral.com/tag/monthly-packs/)
- [CSC setlists](https://customsongscentral.com/tag/setlists/)
- [CSC community projects](https://customsongscentral.com/tag/community-projects/)
- [CSC Q2 2026 release](https://customsongscentral.com/2nd-quarter-2026-video-games-4/)
- [CSC quarterly submission guidelines](https://customsongscentral.com/monthly-pack-submission-guidelines/)
- [CSC RSS feed](https://customsongscentral.com/rss/)
- [Official Clone Hero master spreadsheet](https://docs.google.com/spreadsheets/d/13B823ukxdVMocowo1s5XnT3tzciOfruhUVePENKc01o/edit)
