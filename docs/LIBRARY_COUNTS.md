# Imported songs and chart coverage

The public review opens on **All imported songs**, including songs with no
chart recommendation. Search and page through the complete saved import;
the other filters narrow recommendations without changing the library total.
Read-only song cards explain whether matching is incomplete, no suitable chart
was found, or a candidate was blocked. None of these cards adds a download.

The imported total covers the selected Spotify playlists and Liked Songs plus
any imported SoundCloud music, after deduplication. It is not an account-wide
count of every playlist, listening-history entry, or song available on Spotify.
Spotify source totals remain separate from the combined library total. Source
memberships can overlap and must not be added to produce a unique-song count.

## Which chart pool is searched?

Riff currently has one built-in chart source: Chorus Encore (`enchor`).
The source-neutral master catalog described in ADR 0001 is a future design;
RhythmVerse and Custom Songs Central are not implemented chart adapters.
External sites' counters are not the deployed Riff pool and overlapping
compatibility views must not be added together.

The authenticated, read-only `GET /api/preflight` reports the running server:

- `mode: catalog` means a complete, checksum-valid local metadata index is
  available. `out_of` counts all known chart records, `eligible_charts` counts
  records remaining after safety exclusions, and `eligible_songs` counts their
  distinct normalized artist/title entries. Several chart records can describe
  the same song. `snapshot_id` identifies the loaded snapshot.
- `mode: live_fallback` means the local complete index is unavailable. Discovery
  uses bounded live queries. An authoritative provider `out_of` is a provider
  chart count observed at `checked_at`; it does not establish a local index or
  complete coverage for a particular user's search. A stale or failed observation
  cannot be presented as a current pool count or zero charts.
- A failed status request leaves the count unknown. It does not erase imported
  music or prove that charts are absent.

In catalog mode, matching compares imported song metadata against the indexed
metadata locally, including normalized exact lookup and bounded fuzzy retrieval.
It does not download the whole chart collection. Full catalog acquisition is a
separate operator job; the large generated JSONL snapshot remains outside Git
and must be installed on persistent storage visible to the running container.
See the production runbook before acquiring or replacing a snapshot.

Recommended songs, selectable chart files, selected files, and imported songs
are separate counts. A result of 60 recommended songs does not mean that only
60 songs were imported or that only 60 charts exist in the pool.
