# Documentation

More songs and difficulty labels: [Chart coverage](CHART_COVERAGE.md).

The musical setlist interface, whole-library selection, listening deck and
performance boundaries: [Riff Studio](RIFF_STUDIO.md).

- [`ARCHITECTURE.md`](ARCHITECTURE.md): current local/public surfaces, data
  flow, boundaries, matching methodology, and security model.
- [`CONFIGURATION.md`](CONFIGURATION.md): configuration precedence, paths,
  secrets, catalog snapshots, and retention rules.
- [`LIBRARY_COUNTS.md`](LIBRARY_COUNTS.md): full imported-song preview, separate
  song/chart totals, and how to identify the active chart index.
- [`runbook.md`](runbook.md): single-host Docker, Cloudflare, backups, restore,
  rollback, and incident checks.
- [`adr/0001-source-neutral-master-catalog.md`](adr/0001-source-neutral-master-catalog.md):
  provider-policy findings, neutral catalog identity/schema, and source-coverage
  gates for RhythmVerse and Custom Songs Central.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md): development setup, tests, and
  change boundaries.
- [`../SECURITY.md`](../SECURITY.md): secret handling and vulnerability reports.

The root [`README.md`](../README.md) is the user-facing quick start. `AGENTS.md`
contains repository-specific context for coding agents and is not a substitute
for the user or operator documentation above.
