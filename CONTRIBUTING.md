# Contributing

CSV to Clone Hero is a Python 3.11+ `src`-layout project. Changes should keep
the local single-user workflow and the public multi-user workflow distinct while
reusing the shared matching and source boundaries.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The optional `legacy-keyring` extra is needed only when running the old local
Spotify OAuth flow with persistent OS-keyring credentials:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,legacy-keyring]"
```

## Validation

Run these before opening a pull request:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

The default test command excludes live Enchor integration tests. Run
`pytest -m integration` only when network access and the service rate limit are
acceptable. Do not include credentials, playlist exports, SQLite state, reports,
or downloaded charts in a patch.

## Code Organization

- Keep library-provider parsing behind `providers/base.py:LibraryProvider`.
- Keep chart-source behavior behind `sources/base.py:ChartSource`.
- Keep SQLite reads/writes in `database.py`; update migrations and tests for
  schema changes.
- Keep matching deterministic and explainable. Hard playability gates must not
  be bypassed by a score threshold.
- Preserve the database's global query cache and per-user workspace isolation.
- Use atomic writes for downloaded files, manifests, catalog snapshots, and
  diagnostics.
- Add or update offline tests for every behavior change, especially cancellation,
  path safety, authorization, cache reuse, and partial failures.

## Pull Requests

Describe the user-visible behavior, affected execution surface, migration impact,
and validation commands. Keep generated files out of commits. If a dependency,
configuration key, API route, or operator procedure changes, update the relevant
README or document under `docs/` in the same change.
