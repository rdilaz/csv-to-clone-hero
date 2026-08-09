# Agent notes

- Python 3.11+ package, src layout: `src/csv_to_clonehero/`.
- Install: `pip install -e ".[dev]"`. Tests: `pytest` (offline);
  `pytest -m integration` hits the live Enchor API.
- Enchor API (verified against the live service and the official Bridge
  client): `POST https://api.enchor.us/search` and `/search/advanced`;
  downloads at `https://files.enchor.us/{md5}.sng` (returns HTTP 201 on
  search POSTs; `md5` does NOT match served .sng bytes - do not checksum
  against it; some artist+title queries 500, retry title-only).
- Enchor rate-limits hard (HTTP 429) above ~1-2 req/s per IP; the adapter
  uses an adaptive AIMD throttle (`_note_rate_limited`/`_note_success`).
  Keep `network.concurrency` low (default 2). Query errors are never cached
  so transient failures retry on the next run; successes cache 7 days.
- Never commit `input_playlists/*.csv`, `state/`, `output_library/`,
  `reports/` (see .gitignore).
- All source-specific logic must stay behind `sources/base.py:ChartSource`.
- Web UI: `webapp.py` (Flask, 127.0.0.1-only), `python -m csv_to_clonehero
  web`, launcher `WEB_INTERFACE.bat`. Source factory is injectable for tests.
- Downloads are keyed by candidate in the `downloads` table so rebuilds are
  idempotent; selections are rebuilt each run.
