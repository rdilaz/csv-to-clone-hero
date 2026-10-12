# Riff V2 / Signal Check

V2 brings the accepted navy, porcelain, violet/cyan design to the public app,
with the original Riff wordmark and Signal Knot. It includes the complete
Add Music → Find → Confirm → Download → Done journey, cancellable background
ZIP preparation, consistent chart-service readiness and search recovery.

## What changed

- A busy chart service responds promptly rather than holding Find behind the
  global worker. Unusable checks expire within 60 seconds; verified checks keep
  normal caching. Matching and chart-quality gates remain unchanged.
- Failed, cancelled, interrupted and partial searches stay on Find. Saved
  results require an explicit action and are labeled as potentially earlier
  results. Busy/unavailable checks preserve source choices and offer retry.
- Preflight requests have browser deadlines. Failed checks do not count as
  missing charts. Incomplete completed searches show a warning.
- Assets have a release query parameter; the header shows V2. `/healthz` adds
  `release: riff-v2-20261008` and `experience: signal-check` to its liveness
  response so an old image is distinguishable from this release.
- The reviewed release also preserves catalog-cache freshness, zero-TTL
  behavior and archive identity independent of timestamp collisions.

## Update the existing Windows Docker host

Keep Docker Desktop running Linux containers. Preserve the existing `.env`,
Compose project and named `/data` volume. The helper defaults to read-only:

```powershell
.\scripts\update-riff-v2.ps1 -RepoPath C:\dev\csv-to-clone-hero -CheckOnly
```

Run it from a trusted copy of the reviewed V2 commit. Apply mode requires the
reviewed full commit hash from the release pull request:

```powershell
.\scripts\update-riff-v2.ps1 -RepoPath C:\dev\csv-to-clone-hero -Update -ExpectedCommit <reviewed-commit>
```

The helper fetches only the named V2 branch, rejects tracked changes or a
diverged local branch, verifies the commit, builds before recreating `clonehero`,
and checks that the original environment, project and data-volume identity
remain the same. It does not reset/stash work, regenerate keys, delete volumes
or change the tunnel. Retain its printed original commit/image for rollback.
If script execution is blocked, report the message; do not change security
settings to force it through.

## Confirm the actual running release

```powershell
curl.exe -sS --max-time 10 http://127.0.0.1:8000/healthz
```

Require both V2 marker fields, then open the public hostname and check that the
header shows V2 and the navy/porcelain design. Sign in, choose a small music
scope, find real charts, compare/select one and test a real ZIP before using a
large library. Liveness and a visible design do not prove provider or download
success. Public `/api/preflight` needs the normal authenticated session.

## Chart catalog

Generated chart metadata and user state are intentionally outside Git. V2
uses a complete checksum-valid catalog when available. Otherwise it uses the
existing bounded live fallback only after an authoritative service check.
An unavailable/busy service must never be presented as zero matching charts.

To rebuild the metadata catalog, follow the production runbook on the actual
host with its worker paused so a separate exporter does not compete for the
same per-IP budget. Use `catalog-export` with a staging output and its existing
checkpoint/resume behavior; validate completeness and checksum before atomic
replacement. `catalog-validate --status` is status only; validation without
`--status` downloads a bounded artifact batch. Keep the previous snapshot,
database and stable keys. A host-side file is not proof the container can see
`/data/catalog/enchor.jsonl`.

## Verification scope

Browser acceptance uses the actual templates/assets with synthetic providers,
separate from the production database. A bounded real Enchor check confirmed
current public metadata retrieval and playable-candidate parsing. Hosted
sign-in, current host/catalog state, real chart archives, physical devices and
production restart acceptance must be checked on the running deployment.

The local `WEB_INTERFACE.bat`/`python -m csv_to_clonehero web` interface remains
separate. The Docker public product runs `server.web:create_app` via `wsgi.py`.
