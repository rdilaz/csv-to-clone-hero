# Agent notes

- Python 3.11+ package, src layout: `src/csv_to_clonehero/`.
- Install: `pip install -e ".[dev]"`. Tests: `pytest` (offline);
  `pytest -m integration` hits the live Enchor API.
- Enchor API (verified against the live service and the official Bridge
  client): `POST https://api.enchor.us/search` and `/search/advanced`;
  downloads at `https://files.enchor.us/{md5}.sng` (returns HTTP 201 on
  search POSTs; `md5` does NOT match served .sng bytes - do not checksum
  against it; some artist+title queries 500, retry title-only).
- Never commit `input_playlists/*.csv`, `state/`, `output_library/`,
  `reports/` (see .gitignore).
- All source-specific logic must stay behind `sources/base.py:ChartSource`.
- Downloads are keyed by candidate in the `downloads` table so rebuilds are
  idempotent; selections are rebuilt each run.
