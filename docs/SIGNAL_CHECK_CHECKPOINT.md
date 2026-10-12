# Signal Check: checkpoints and final human review

The first checkpoint implemented specification phases 0-3: the shared public shell,
Landing, Add Music, Find, and Confirm. Download, Done, artist expansion, and the
two post-confirm count corrections were reserved for phases 4-5 after approval.

## Owner approval

On 2026-09-10, the owner explicitly approved the human visual/interaction
checkpoint for phases 0-3 at commit
`25d3f4093bcbb0f3955e69b2de3d4e4bfba8f714` and authorized the phases 4-5
completion pass on this existing worktree and PR #10. This records the owner's
approval, not an inference from automated checks. RIFF / SIGNAL CHECK remains
locked to NULL FREQUENCY with LINE CHECK operational discipline: "Haunted
console outside; precise instrument panel inside."

Final product acceptance is separate and remains pending human review of the
complete journey. This approval does not authorize merging or deployment.

## Phases 4-5 implementation

The completion pass starts at the approved commit above. Production changes
are bounded to the public template, CSS, and JavaScript. Matching, catalog
recall, selection persistence, artifact validation/ownership, server routes,
and ZIP authority are unchanged. The existing Landing, Add Music, Find, and
Confirm composition remains the accepted baseline.

- Download reconciles the saved selection before preparation. Imported-song
  counts, selected exact charts, and selected expansion charts remain separate.
  A paginated, read-only disclosure identifies the selected candidates.
- Preparation uses the existing synchronous `POST /api/download`. The activity
  indicator is indeterminate: there is no fabricated percentage, ETA,
  per-chart progress, cancellation, or resumable download job.
- Done reports physical packaged artifacts from `summary.published` and
  represented selections from `summary.ready` separately. Duplicate-collapsed
  selections do not become additional ZIP files. Failed/remaining counts,
  eligibility differences, and server error details remain visible or
  explicitly disclosed; a partial archive is not labeled complete success.
- Prepare-again rereads authoritative chart counts, never the legacy
  song-count field as a chart total. Missing expansion counts stay unknown,
  not zero. Handoff and retry do not write or reconstruct selections.
- Reload uses `/api/journey` to reconcile archive availability. An optional,
  workspace-keyed session receipt can restore a previously received build
  report only for its matching token. Without a matching report, archive
  counts and failure breakdowns are explicitly unavailable. The receipt is
  UI context, not download or ownership authority.
- An interrupted preparation request is not automatically resubmitted. The UI
  offers status reconciliation and an explicit new-preparation retry. Existing
  earlier ZIPs are not presented as the result of the interrupted request.
- Single-use links are not probed. A native download request is guarded
  against repeated clicks, with expiry handling and explicit browser-save
  uncertainty. RIFF never claims to have saved, installed, or scanned a chart
  on the user's machine.
- Artist expansion stays optional and distinct within Confirm. Artist/song
  caps come from the API, multiple variants remain independently selectable,
  and rerunning expansion warns that the whole expansion set is replaced,
  including choices for unchecked artists.
  Polling retry, duplicate-action protection, selected siblings, paginated
  reconciliation, keyboard focus, and disclosure state are covered.

### Authority limitations

The existing API exposes no durable preparation status, failed-only retry,
per-item delivery manifest, or browser-save confirmation. A server restart can
lose transient download tokens. `/api/journey` returns archive metadata but no
build report; a different browser without the matching receipt cannot recover
that report. The UI explains these limits rather than changing server behavior.
Concurrent changes in another tab can affect the server's final revalidation;
the final report is authoritative, not a previously displayed selection tray.

The repository does not contain a separate finished Download/Done visual
specification. This pass carries forward the current public product copy,
browser-only architecture, palette, typography, and operational ledger, rather
than importing historical local-folder prototypes or opening new ideation.

### Final acceptance boundary

The complete journey requires a new human visual/interaction review. Automated
checks and generated screenshots are evidence only. PR #10 stays open and
draft; this pass does not merge, deploy, or modify another checkout.

## Completion verification (2026-09-10)

All Python commands use the existing worktree's `.venv/Scripts/python.exe`.
The approved starting baseline was rerun before implementation: **719 passed,
2 failed, 5 deselected**. The completed pass has **777 passed, 2 failed,
5 deselected**, adding 49 browser cases and 9 server cases without modifying
the original 20 checkpoint browser tests.

| Check | Exact command or coverage | Final result |
| --- | --- | --- |
| Public browser suites | `python -m pytest tests/test_public_browser.py tests/test_public_delivery_browser.py -q` | **69 passed**; also all pass in the final full suite |
| Server web/download suites | `python -m pytest tests/test_server_web.py tests/test_server_downloads.py -q` | **88 passed** |
| Full offline suite | `python -m pytest -q` | **777 passed, 2 failed, 5 deselected**, 394.67 s |
| Python compile | `python -m compileall -q src tests` | Exit 0 |
| Public JavaScript | `node --check src/csv_to_clonehero/server/static/public.js` | Exit 0 |
| Dependencies | `python -m pip check` | No broken requirements |
| Wheel packaging | `python -m pip wheel . --no-deps --wheel-dir .pytest_cache/signal-check-phase45-package` | Build passed; archive listing contains the public template, JS/CSS, both fonts/art assets, and SIL licenses |
| Real Chromium | All six surfaces at 1440x900, 390x844, and 320x800, plus expansion/partial-delivery captures | No material visual defects in technical inspection; headed desktop/mobile previews prepared |

The two unchanged failures are:

- `tests/test_local_journey_e2e.py::test_intentional_real_process_stop_recovers_durable_run`
- `tests/test_local_journey_e2e.py::test_real_cli_subprocess_owns_cleans_and_completes_journey`

Both require the absent, untracked root `config.yaml`. It was not copied from
another checkout or created to hide the baseline. No tests were deleted or
skipped to obtain the final result. The five integration tests remain
deselected by the existing offline default; no live provider/catalog data was
used for browser evidence.

An intermediate full run exposed lost keyboard focus on Download refresh and
old expansion cards remaining observable before replacement-job polling.
Both were fixed in the UI, not by weakening checkpoint assertions. Busy
expansion now withholds obsolete cards until authoritative reconciliation;
keyboard focus returns to the initiating control when appropriate. The final
full run above includes the strengthened keyboard regressions.

The first optional wheel attempt with `--no-build-isolation --no-index` could
not find `setuptools` in the runtime venv. The normal isolated PEP 517 build
passed without changing project dependencies. The embedded Browser panel
refused attachment with "Browser session does not belong to the requested
project or directory"; independent headed Chromium previews and captures
worked. No Agent Manager state was edited to work around that limitation.

The completion is committed locally on the existing PR branch. Publication
was not explicitly authorized, so the completion commit is not pushed; the PR
note identifies the local commit and this worktree as the final-review target.
Final human acceptance, merge, and deployment remain unapproved.

## First-checkpoint baseline

- Branch: `riff/signal-check-ui-v1`
- Baseline: `ab64a590c1bb6652cf0b6823e36cfe1c87b02207`
- Product data: never used by the preview or browser tests
- Backend/matcher changes: none
- Baseline full offline suite: 700 passed, 2 failed, 5 deselected
- Checkpoint full offline suite: 719 passed, 2 failed, 5 deselected

The same two `tests/test_local_journey_e2e.py` cases fail before and after this
change because an isolated worktree intentionally has no untracked root
`config.yaml`. Focused public tests are green.

## Headed preview

From this worktree:

```powershell
& ".\.venv\Scripts\python.exe" -m tests.public_preview --scenario all --viewport 1440x900
```

The command opens Landing, Add Music, Find, Confirm, Download, and Done in real
Chromium tabs.
It serves the production Flask templates, CSS, and JavaScript while intercepting
only provider/API requests with DTO-faithful fixtures. Its SQLite session
database and fixture state are temporary. Press `Ctrl+C` in the preview terminal
to stop the server and browser cleanly.

Use `--viewport 390x844` for the locked mobile baseline, or select one surface
with `--scenario landing`, `--scenario add`, `--scenario find`,
`--scenario confirm`, `--scenario download`, or `--scenario done`.
Use `--viewport 320x800` for the minimum-width regression layout. The preview
CLI also accepts 320x480; that is a short-viewport stress case, not the locked
mobile design baseline.

Additional deterministic scenarios are `confirm-expansion`,
`download-expansion`, and `done-partial`. In the partial fixture, six selected
chart entries produce three packaged artifacts representing four selections;
one duplicate collapses, and two selections fail. Those values are derived
from the fixture candidates and outcomes, not hard-coded into the UI.

The initial Confirm tab restores three previously saved exact chart choices.
A fresh search from Add Music selects nothing automatically; use a preset or
explicit candidate actions before continuing. The prepared fixture ZIPs are
clearly synthetic browser-test files, not playable production chart downloads.
Keep using the opened tabs: API interception is attached to those tabs, so
opening the bare preview-server URL is not a replacement for the fixture
launcher. All provider traffic is blocked; no personal production data is used.

Final screenshot directories, relative to this worktree:

| Viewport | Directory | Main images |
| --- | --- | --- |
| 1440x900 | `.pytest_cache/signal-check-phase45-desktop/` | `landing`, `add`, `find`, `confirm`, `download`, `done` + `-1440x900.png` |
| 390x844 | `.pytest_cache/signal-check-phase45-mobile/` | Same six names + `-390x844.png` |
| 320x800 | `.pytest_cache/signal-check-phase45-narrow/` | Same six names + `-320x800.png` |

Supplementary captures include desktop/mobile `confirm-expansion`, desktop
`download-expansion`, and mobile `done-partial`. These are ignored, isolated
local review artifacts. Screenshots are viewport-sized; installation steps,
reprepare controls, and longer disclosures can require scrolling. Both desktop
and mobile headed previews are prepared for owner inspection; passing the
preview's stage/DTO checks does not approve their visual or interaction design.

## Asset provenance

- IBM Plex Sans variable Latin-1 WOFF2, pinned to IBM/plex commit
  `bf260093582f04622aacc1e9f9ca604d7ccd0c42`.
- Space Grotesk variable WOFF2, pinned to floriankarsten/space-grotesk commit
  `03507d024a01282884232081fc6011c09ff4e849`.
- Exact SIL Open Font License text ships beside each font.
- `riff-wordmark.svg` and `signal-knot.svg` are original project assets.
- New initial-load art and font files total 121,570 bytes.

The package-data list was narrowly extended for `.svg`, `.woff2`, and `.txt`.
A built wheel was inspected and contained all ten required templates, scripts,
art assets, fonts, and license files.

## First-checkpoint verification

- `pytest tests/test_public_browser.py -q`: 20 passed in real Chromium.
- `pytest tests/test_server_web.py tests/test_server_downloads.py -q`: 79 passed.
- `pytest`: 719 passed, 2 baseline-only failures, 5 integration tests deselected.
- `python -m compileall -q src tests`: passed.
- `node --check src/csv_to_clonehero/server/static/public.js`: passed.
- `pip check`: passed.
- Both independent post-fix behavior and responsive reviews: passed.

## Proof comparison

- Landing preserves the approved dark-world composition, oversized interrupted
  wordmark, left porcelain action block, asymmetric violet/cyan knot, and lower
  three-part explainer.
- Find preserves the approved stepped rail, cropped knot field, single factual
  progress meter, large percentage, current phase, current song, calm leave
  note, detail disclosure, cancel action, and ETA.
- Confirm uses the corrected broad porcelain ledger with a narrow persistent
  selection rail. The fixture truthfully starts at three chart artifacts for
  one imported song and zero artist-expansion charts.
- Mobile keeps one DOM order, collapses selection presets, opens disclosures
  containing selected siblings, and places the first candidate action above
  the persistent bottom summary at 390x844. The first group starts within the
  380-pixel proof budget; an enabled support banner is separately verified at
  44 pixels total. Proof screenshots use the same disabled-banner treatment as
  the approved references.
